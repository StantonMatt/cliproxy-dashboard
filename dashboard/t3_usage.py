"""Cache-only CLIProxyAPI usage facade. No gateway/provider client belongs here."""
import datetime as dt
import hmac
import json
import math
from pathlib import Path
import re
import time

from request_deadline import RequestDeadline
import usage_adapters

KEYS = Path('/etc/personal-codex-router/t3-usage-keys.json')
CODEX_URL = 'https://chatgpt.com/backend-api/wham/usage'
CLAUDE_URL = 'https://api.anthropic.com/api/oauth/usage'
MAX_BODY = 16 * 1024
READ_SECONDS = 3


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def fresh(value, now, maximum):
    try:
        stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.tzinfo is not None and 0 <= now - stamp.timestamp() <= maximum
    except (AttributeError, ValueError, TypeError, OverflowError):
        return False


def text(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.@:+-]{1,254}', value) is not None


def codex_projection(payload, identity, account_id=None):
    """Retain provider slots and a binding, never raw payloads or token claims."""
    result = {'identity': identity, 'body': {'rate_limit': {}}}
    if text(account_id):
        result['account_id'] = account_id
    plan = payload.get('plan_type')
    if isinstance(plan, str) and re.fullmatch(r'[a-zA-Z0-9_()+ -]{1,80}', plan):
        result['body']['plan_type'] = plan
    limits = payload.get('rate_limit')
    for slot in ('primary_window', 'secondary_window'):
        row = limits.get(slot) if isinstance(limits, dict) else None
        if not isinstance(row, dict) or not number(row.get('used_percent')) or not 0 <= row['used_percent'] <= 100:
            continue
        safe = {'used_percent': row['used_percent']}
        if number(row.get('reset_at')) and row['reset_at'] >= 0:
            safe['reset_at'] = row['reset_at']
        if number(row.get('limit_window_seconds')) and row['limit_window_seconds'] > 0:
            safe['limit_window_seconds'] = row['limit_window_seconds']
        result['body']['rate_limit'][slot] = safe
    return result


def claude_body(observation, now):
    independent = observation.get('independent_windows') is True
    if observation.get('status') not in (('ok', 'stale') if independent else ('ok',)):
        return None
    if not independent and not fresh(observation.get('checked_at'), now, 900):
        return None
    body = {}
    seen = set()
    for row in observation.get('windows', []):
        used, reset, kind = row.get('used'), row.get('resets_at'), row.get('kind')
        # B retains windows independently; a fresh main window cannot freshen a model window.
        if (independent or 'measured_at' in row) and not fresh(row.get('measured_at'), now, 900):
            continue
        if (not number(used) or not 0 <= used <= 100 or row.get('invalid_reset') or
                (reset is not None and (not number(reset) or reset <= now))):
            continue
        try:
            resets_at = dt.datetime.fromtimestamp(reset, dt.timezone.utc).isoformat() if reset is not None else None
        except (ValueError, OverflowError, OSError):
            continue
        if kind in ('five_hour', 'seven_day'):
            if kind in seen:
                return None
            seen.add(kind)
            body[kind] = {'utilization': used, 'resets_at': resets_at}
        elif kind == 'weekly_model' or (isinstance(kind, str) and re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind)):
            model = row.get('model') if kind == 'weekly_model' else kind[10:].replace('_', ' ').title()
            if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .()+-]{0,63}', model):
                continue
            if model.casefold() in seen:
                return None
            seen.add(model.casefold())
            body.setdefault('limits', []).append({'kind': 'weekly_scoped', 'percent': used,
                'resets_at': resets_at, 'scope': {'model': {'display_name': model}}})
    return body or None


def accounts(cache, now):
    """Bind every observation to a unique current cached runtime identity."""
    result = []
    current = fresh(cache.get('checked_at'), now, 180) and cache.get('online') is True
    codex = cache.get('accounts', [])
    claude = cache.get('claude_accounts', [])
    watched = cache.get('watch_accounts', [])
    for provider, rows in (('codex', codex), ('claude', claude)):
        for row in rows:
            native = row.get('id') if provider == 'codex' else row.get('auth_index')
            email = row.get('email')
            identity = row.get('access_identity') if provider == 'codex' else row.get('routing_id')
            if not all(text(v) for v in (native, identity)) or not isinstance(email, str) or '@' not in email:
                continue
            if (sum((r.get('id') if provider == 'codex' else r.get('auth_index')) == native for r in rows) != 1
                    or sum(str(r.get('email', '')).casefold() == email.casefold() for r in rows) != 1
                    or sum(r.get('access_identity' if provider == 'codex' else 'routing_id') == identity for r in rows) != 1):
                continue
            shim_id = f'{provider}:{native}:{identity}'
            public = dict(id=shim_id, auth_index=shim_id, provider=provider, email=email,
                          disabled=row.get('disabled') is True)
            body = None
            if provider == 'codex':
                observed = row.get('_t3_usage') or {}
                account_id = observed.get('account_id')
                if account_id and sum((r.get('_t3_usage') or {}).get('account_id') == account_id for r in rows) != 1:
                    continue
                if observed.get('identity') != identity:
                    continue
                if text(account_id):
                    public['id_token'] = {'chatgpt_account_id': account_id}
                    # Bind JWT-derived workspaces too, even if the access controller's ID stayed stable.
                    public['id'] = public['auth_index'] = f'{shim_id}:{account_id}'
                candidate = observed.get('body')
                if (current and not row.get('quota_error') and fresh(row.get('quota_checked_at'), now, 180)
                        and isinstance(candidate, dict) and candidate.get('rate_limit')):
                    body = candidate
            else:
                if current:
                    body = claude_body(usage_adapters.claude_observation(row, rows, watched, now), now)
            result.append((public, body))
    return result


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid(_):
        raise ValueError()
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


def authorized(headers, path):
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            return False
        keys = strict_json(data)
        if (not isinstance(keys, dict) or not 1 <= len(keys) <= 16 or
                not all(isinstance(k, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', k) and
                        isinstance(v, str) and re.fullmatch(r't3u_[0-9a-f]{64}', v) for k, v in keys.items()) or
                len(set(keys.values())) != len(keys)):
            return False
        if hasattr(headers, 'get_all') and len(headers.get_all('Authorization', [])) != 1:
            return False
        supplied = headers.get('Authorization', '')
        # Compare every machine key; never load the gateway management key.
        matches = [hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + key).encode()) for key in keys.values()]
        return any(matches)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False


def valid_call(value):
    if (not isinstance(value, dict) or set(value) - {'auth_index', 'method', 'url', 'header'} or
            not {'auth_index', 'method', 'url'} <= set(value) or value['method'] != 'GET' or
            not isinstance(value['auth_index'], str) or not isinstance(value['url'], str)):
        return False
    header = value.get('header', {})
    return (isinstance(header, dict) and len(header) <= 32 and all(
        isinstance(k, str) and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", k) and
        isinstance(v, str) and len(v) <= 1024 and all(32 <= ord(c) <= 126 for c in v)
        for k, v in header.items()))


def handle(handler, method):
    """Return whether this request belongs to the facade, including denied routes."""
    if not handler.path.startswith('/v0/management'):
        return False

    def send(status, value):
        handler.send(status, json.dumps(value, allow_nan=False).encode())
        return True

    # Refuse unread bodies and never reuse an ambiguous request framing.
    handler.close_connection = True
    # BaseHTTPRequestHandler collapses a leading // before dispatch. Keep the raw target strict.
    requestline = getattr(handler, 'requestline', '')
    if requestline and requestline.split()[1] != handler.path:
        return send(404, {'error': 'Not found'})
    if not handler.allowed():
        return True
    if any(name in handler.headers for name in ('Origin', 'Sec-Fetch-Site')) or handler.headers.get('X-Router-Transport') == 'tailscale':
        return send(403, {'error': 'Native loopback client required'})
    if (method, handler.path) not in (('GET', '/v0/management/auth-files'), ('POST', '/v0/management/api-call')):
        return send(404, {'error': 'Not found'})
    if not authorized(handler.headers, getattr(handler.server, 't3_usage_keys', KEYS)):
        return send(401, {'error': 'Usage hub unavailable or unauthorized'})
    value = None
    if method == 'POST':
        try:
            length = handler.headers.get('Content-Length', '')
            if (not re.fullmatch(r'[0-9]{1,5}', length) or not 0 < int(length) <= MAX_BODY or
                    handler.headers.get('Transfer-Encoding') is not None or
                    handler.headers.get('Content-Type') != 'application/json'):
                raise ValueError()
            if hasattr(handler.headers, 'get_all') and len(handler.headers.get_all('Content-Length', [])) != 1:
                raise ValueError()
            value = strict_json(handler.read_body(int(length), RequestDeadline(seconds=READ_SECONDS)))
            if not valid_call(value):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, OSError, RecursionError):
            return send(400, {'error': 'Invalid usage request'})
    now = time.time()
    rows = accounts(handler.server.state.t3_usage_cache(now), now)
    if value is None:
        return send(200, {'files': [public for public, _ in rows]})
    matches = [(public, body) for public, body in rows if public['auth_index'] == value['auth_index']]
    if len(matches) != 1:
        return send(403, {'error': 'Usage account unavailable'})
    public, body = matches[0]
    expected = CODEX_URL if public['provider'] == 'codex' else CLAUDE_URL
    if value['url'] != expected or public['disabled']:
        return send(403, {'error': 'Usage request denied'})
    return send(200, {'status_code': 200 if body else 503,
                      'body': json.dumps(body if body else {'error': 'Cached usage unavailable'}, allow_nan=False)})
