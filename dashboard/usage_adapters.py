"""Gateway usage adapters, credential budgets and watch-compatible projections.

xAI contract: Cli-Proxy-API-Management-Center v1.26.7, quota/constants.ts,
providers/xai/data.ts and quota/builders.ts. Only billing GETs are used; never
the upstream management center's paid-account chat fallback.
"""
import copy
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import math
import random
import re
import threading
import time

INTERVAL = 300
MAX_AGE = 900
CLAUDE_URL = 'https://api.anthropic.com/api/oauth/usage'
XAI_URL = 'https://cli-chat-proxy.grok.com/v1/billing?format=credits'
XAI_HEADERS = {
    'Authorization': 'Bearer $TOKEN$', 'x-xai-token-auth': 'xai-grok-cli',
    'x-grok-client-version': '1.0.46', 'accept': '*/*',
    'user-agent': 'grok-pager/1.0.46 grok-shell/1.0.46 (macos; aarch64)',
}


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def numeric(value):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return value if number(value) else None


def epoch(value):
    if number(value):
        return value if value >= 0 else None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def stamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def fresh(value, now):
    checked = epoch(value)
    return checked is not None and 0 <= now - checked <= MAX_AGE


def record(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('parse')
    return value


def window(label, used, reset, **fields):
    used = min(100, max(0, used))
    return dict(label=label, used=used, used_pct=used, remaining=100 - used,
                resets_at=epoch(reset), **fields)


def normalize_claude(payload):
    payload = record(payload)
    if 'error' in payload or payload.get('type') == 'error':
        raise ValueError('parse')
    windows, models = [], set()

    def add(kind, label, raw, model=None, field='utilization'):
        if not isinstance(raw, dict) or not number(raw.get(field)) or raw[field] < 0:
            return
        row = window(label, raw[field], raw.get('resets_at'), kind=kind,
                     seconds=18000 if kind == 'five_hour' else 604800)
        if raw.get('resets_at') is not None and row['resets_at'] is None:
            row['invalid_reset'] = True
        if model:
            if model.casefold() in models:
                return
            models.add(model.casefold())
            row['model'] = model
        windows.append(row)

    for kind, label in (('five_hour', '5-hour limit'), ('seven_day', 'Weekly · all models')):
        add(kind, label, payload.get(kind))
    limits = payload.get('limits')
    for raw in limits if isinstance(limits, list) else []:
        if not isinstance(raw, dict) or raw.get('kind') != 'weekly_scoped':
            continue
        scope = raw.get('scope')
        model = scope.get('model') if isinstance(scope, dict) else None
        name = model.get('display_name') if isinstance(model, dict) else None
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .()+-]{0,63}', name):
            add('weekly_model', 'Weekly · ' + name, raw, name, 'percent')
    for kind, raw in payload.items():
        if re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind):
            name = 'Fable' if kind == 'seven_day_overage_included' else kind[10:].replace('_', ' ').title()
            add('weekly_model', 'Weekly · ' + name, raw, name)
    if not all(any(w['kind'] == kind for w in windows) for kind in ('five_hour', 'seven_day')):
        raise ValueError('no_usage_data')
    extra = payload.get('extra_usage')
    extra = extra if isinstance(extra, dict) else {}
    currency = extra.get('currency')
    currency = currency if isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency) else None
    divisor = 1 if currency in ('JPY', 'KRW', 'VND') else 100
    return dict(windows=windows, independent_windows=False, extra_usage={
        'enabled': extra.get('is_enabled') is True,
        'used': extra['used_credits'] / divisor if number(extra.get('used_credits')) else None,
        'limit': extra['monthly_limit'] / divisor if number(extra.get('monthly_limit')) else None,
        'currency': currency,
        'utilization': extra.get('utilization') if number(extra.get('utilization')) else None})


def normalize_xai(payload):
    config = record(payload).get('config')
    if not isinstance(config, dict):
        return {'windows': []}

    def get(camel, snake):
        return config.get(camel) if config.get(camel) is not None else config.get(snake)

    def cents(value):
        return numeric(value.get('val') if isinstance(value, dict) else value)

    period = get('currentPeriod', 'current_period')
    period = period if isinstance(period, dict) else {}
    period_type = str(period.get('type', '')).lower()
    credit = numeric(get('creditUsagePercent', 'credit_usage_percent'))
    products = get('productUsage', 'product_usage')
    weekly = credit is not None or 'weekly' in period_type or bool(products if isinstance(products, list) else [])
    windows = []
    if weekly:
        if credit is not None and credit >= 0:
            windows.append(window('Monthly' if 'monthly' in period_type else 'Weekly', credit,
                                  period.get('end') or get('billingPeriodEnd', 'billing_period_end')))
    else:
        limit, used = cents(get('monthlyLimit', 'monthly_limit')), cents(config.get('used'))
        if limit is not None and limit > 0 and used is not None and used >= 0:
            windows.append(window('Monthly', min(used, limit) / limit * 100,
                                  get('billingPeriodEnd', 'billing_period_end')))
    return {'windows': windows}


def request(file):
    provider = file['type']
    headers = dict(XAI_HEADERS) if provider == 'xai' else {
        'Authorization': 'Bearer $TOKEN$', 'anthropic-beta': 'oauth-2025-04-20',
        'Accept': 'application/json'}
    if provider == 'xai':
        # MC accepts these inventory metadata locations for the optional user ID.
        sources = [file] + [file.get(k) for k in ('metadata', 'attributes')]
        sources = [s for s in sources if isinstance(s, dict)]
        candidates = [s.get(k) for s in sources for k in ('sub', 'subject', 'user_id', 'userId')]
        oauth = next((s['oauth'] for s in sources if isinstance(s.get('oauth'), dict)), {})
        profile = next((s['user'] for s in sources if isinstance(s.get('user'), dict)), {})
        candidates += [oauth.get('sub'), oauth.get('subject'), profile.get('sub'), profile.get('id')]
        user = next((v for v in candidates if isinstance(v, str)
                     and re.fullmatch(r'[A-Za-z0-9_.:@+-]{1,254}', v)), None)
        if user:
            headers['x-userid'] = user
    return dict(auth_index=file['auth_index'], method='GET',
                url=CLAUDE_URL if provider == 'claude' else XAI_URL, header=headers)


def retry_after(headers, now):
    if not hasattr(headers, 'items'):
        return 0
    raw = next((v for k, v in headers.items() if k.casefold() == 'retry-after'), None)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    seconds = numeric(raw)
    if seconds is not None:
        return max(0, seconds)
    try:
        parsed = parsedate_to_datetime(raw)
        return max(0, parsed.timestamp() - now) if parsed.tzinfo else 0
    except (TypeError, ValueError, OverflowError):
        return 0


class UsageCache:
    """One in-flight request and a non-bypassable budget per credential.

    Scheduling uses a monotonic clock; published observations use wall time.
    Jitter is additive (0..30 s), so no caller can poll sooner than 300 s.
    Failed attempts retain original successful timestamps, never response bodies.
    """
    def __init__(self, clock=time.monotonic, wall_clock=time.time, jitter=None):
        self.clock, self.wall_clock = clock, wall_clock
        self.jitter = jitter if jitter is not None else lambda: random.uniform(0, 30)
        self.lock = threading.Lock()
        self.entries = {}

    def fetch(self, file, call):
        # Aliases for one auth_index share the budget but never inherit another
        # filename/email binding's observation.
        key = (file.get('type'), file.get('auth_index'))
        binding = (file.get('name'), file.get('email'))
        empty = dict(key=file.get('name'), auth_index=file.get('auth_index'),
                     provider=file.get('type'), email=file.get('email'), id=file.get('name'),
                     windows=[], status='unavailable', error=None, checked_at=None,
                     usage_checked_at=None, usage_source='gateway', attempted_at=None)
        if file.get('type') not in ('claude', 'xai') or not file.get('name') or not file.get('auth_index'):
            return empty
        with self.lock:
            entry = self.entries.setdefault(key, dict(view=empty, binding=binding,
                                                       due=0, inflight=False, failures=0))
            if entry['inflight'] or self.clock() < entry['due']:
                return copy.deepcopy(entry['view']) if entry['binding'] == binding else empty
            view = copy.deepcopy(entry['view']) if entry['binding'] == binding else empty
            entry.update(inflight=True, binding=binding, view=copy.deepcopy(view))
        view['attempted_at'] = stamp(self.wall_clock())
        wait = 0
        success = False
        try:
            response = call('/api-call', request(file))
            status = response.get('status_code')
            if status == 429 or (type(status) is int and 500 <= status <= 599):
                wait = retry_after(response.get('header'), self.wall_clock())
            if status != 200:
                raise ValueError('http')
            normalized = (normalize_claude if file['type'] == 'claude' else normalize_xai)(response.get('body'))
            if not normalized['windows']:
                raise ValueError('no_usage_data')
            checked = stamp(self.wall_clock())
            for row in normalized['windows']:
                row.update(measured_at=checked, usage_source='gateway')
            view.update(normalized, status='ok', error=None, checked_at=checked, usage_checked_at=checked)
            success = True
        except Exception as error:
            # Outer management HTTP errors may carry their own Retry-After.
            if getattr(error, 'code', None) == 429 or (isinstance(getattr(error, 'code', None), int)
                                                      and 500 <= error.code <= 599):
                wait = max(wait, retry_after(getattr(error, 'headers', None), self.wall_clock()))
            view.update(status='error', error='usage_unavailable')
        finally:
            with self.lock:
                entry['failures'] = 0 if success else min(5, entry['failures'] + 1)
                backoff = 0 if success else min(3600, INTERVAL * 2 ** (entry['failures'] - 1))
                entry.update(view=view, inflight=False,
                             due=self.clock() + max(INTERVAL, backoff, wait) + self.jitter())
        return copy.deepcopy(view)


def bind_claude_usage(runtime, observations):
    for row in runtime:
        matches = [o for o in observations if o.get('provider') == 'claude'
                   and o.get('key') == row.get('key', row.get('name'))
                   and o.get('auth_index') == row.get('auth_index')]
        if len(matches) == 1:
            row['_gateway_usage'] = copy.deepcopy(matches[0])
    return runtime


def claude_observation(runtime, runtime_rows, watched, now=None):
    """Select usage; email joins only attach unambiguous watch observations."""
    now = time.time() if now is None else now
    email = str(runtime.get('email') or '').casefold()
    matches = [a for a in watched if a.get('provider', 'claude') == 'claude' and email
               and str(a.get('email') or '').casefold() == email]
    unique = sum(str(r.get('email') or '').casefold() == email for r in runtime_rows) == 1
    watch = matches[0] if len(matches) == 1 and unique else None
    gateway = runtime.get('_gateway_usage') or {}
    if (gateway.get('provider') != 'claude' or gateway.get('key') != runtime.get('key', runtime.get('name'))
            or gateway.get('auth_index') != runtime.get('auth_index')
            or str(gateway.get('email') or '').casefold() != email):
        gateway = {}
    primary = gateway.get('status') == 'ok' and not gateway.get('error') and fresh(gateway.get('checked_at'), now)
    selected = gateway if primary else watch if watch is not None else gateway
    if not selected:
        return {}
    view = copy.deepcopy(selected)
    source = 'gateway' if selected is gateway else 'watch'
    view.update(usage_source=source, usage_checked_at=view.get('checked_at'))
    if view.get('status') == 'ok' and not fresh(view.get('checked_at'), now):
        view['status'] = 'stale'
    if watch is not None:
        view.update(watch_checked_at=watch.get('checked_at'), watch_attempted_at=watch.get('attempted_at'),
                    watch_id=watch.get('id'))
    if gateway:
        view['gateway_checked_at'] = gateway.get('checked_at')
    if primary and watch is not None:
        # Do not overwrite gateway usage/status/time with watch metadata.
        view['id'] = watch.get('id', view.get('id'))
        for field in ('plan', 'resets'):
            if view.get(field) is None and watch.get(field) is not None:
                view[field] = copy.deepcopy(watch[field])
        if watch.get('resets') is not None:
            view['resets_checked_at'] = watch.get('checked_at')
        has_fable = any(w.get('model', '').casefold() == 'fable' for w in view.get('windows', []))
        if not has_fable:
            for row in watch.get('windows', []):
                measured = row.get('measured_at', watch.get('checked_at'))
                if (row.get('kind') == 'weekly_model' and row.get('model', '').casefold() == 'fable'
                        and fresh(measured, now) and number(row.get('used'))
                        and (row.get('resets_at') is None or
                             (number(row['resets_at']) and row['resets_at'] > now))):
                    view['windows'].append(dict(copy.deepcopy(row), measured_at=measured, usage_source='watch'))
                    break
    for row in view.get('windows', []):
        row.setdefault('measured_at', view.get('checked_at'))
        row.setdefault('usage_source', source)
        row.setdefault('used_pct', row.get('used'))
    return view
