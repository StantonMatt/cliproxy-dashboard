"""Gateway-owned OAuth add sessions; no credentials or callback URLs are logged."""
import copy
import datetime as dt
import hashlib
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from request_deadline import RequestDeadline

TESTED_GATEWAYS = ('8.0.47',)
# Exact origins from CLIProxyAPI v8.0.47 internal/auth provider implementations.
FLOWS = {
    'claude': ('browser', 'claude.ai', 54545, '/callback'),
    'codex': ('browser', 'auth.openai.com', 1455, '/auth/callback'),
    'antigravity': ('browser', 'accounts.google.com', 51121, '/oauth-callback'),
    # xai/xai_auth_test.go: verification_uri(_complete), distinct from its issuer.
    'xai': ('device', 'accounts.x.ai', None, None),
    'kimi': ('device', 'auth.kimi.com', None, None),
    'kimi-ai': ('device', 'auth.kimi.ai', None, None),
    'meta': ('device', 'auth.meta.com', None, None),
}
NAMES = {'claude': 'Claude', 'codex': 'Codex', 'antigravity': 'Antigravity',
         'xai': 'Grok', 'kimi': 'Kimi (kimi.com)', 'kimi-ai': 'Kimi (kimi.ai)', 'meta': 'Meta'}
PREFIX = '/v8/management/oauth/'
ACTIVE = frozenset(('waiting', 'finishing'))


class AccountError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def catalog(version):
    if version not in TESTED_GATEWAYS:
        return []
    result = []
    for provider, (flow, _, port, path) in FLOWS.items():
        row = dict(id=provider, name=NAMES[provider], login=flow)
        if flow == 'browser':
            row['callback_example'] = f'http://localhost:{port}{path}?code=…&state=…'
        result.append(row)
    return result


def fingerprint(row):
    # Ignore request counters, cooldowns and quota reads, which change without sign-in.
    fields = ('name', 'auth_index', 'email', 'label', 'type', 'updated_at', 'modified_at',
              'modtime', 'size', 'last_refresh', 'last_refresh_at', 'id_token')
    return hashlib.sha256(json.dumps({k: row.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def clean_url(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 16384
            or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value):
        raise ValueError()
    url = urlsplit(value)
    if url.username is not None or url.password is not None or url.fragment:
        raise ValueError()
    return url


class AddManager:
    def __init__(self, call, lifecycle_lock, changed, clock=time.time):
        self.call, self.lock, self.changed, self.clock = call, lifecycle_lock, changed, clock
        self.sessions = {}

    def _expire(self, call, reconcile_busy=False):
        now = self.clock()
        for handle, session in list(self.sessions.items()):
            if session['result']['state'] in ACTIVE and now >= session['expires'] and not session.get('cancel_attempted'):
                try:
                    self._cancel(session, call, 'expired')
                except Exception:
                    pass  # Keep the slot busy until cleanup or terminal status is confirmed.
            if session['result']['state'] in ACTIVE and (now >= session['expires'] or reconcile_busy):
                self._reconcile(session, call)
            if session['result']['state'] not in ACTIVE and now >= session.get('finished_at', session['expires']) + 600:
                del self.sessions[handle]

    def _cancel(self, session, call, reason='cancelled'):
        session.setdefault('cancel_reason', reason)
        session['result'] = {'state': 'finishing', 'message': 'Waiting for the gateway to confirm sign-in cleanup.'}
        response = call(PREFIX + 'session?' + urlencode({'state': session['gateway_state']}), method='DELETE')
        if response.get('status') != 'ok' or type(response.get('cancelled')) is not bool:
            raise AccountError('gateway_error', 'The gateway did not confirm cancellation. Try again.', 503)
        session['cancel_attempted'] = True
        if response['cancelled']:
            session['result'] = {'state': session['cancel_reason']}
            session['finished_at'] = self.clock()
        else:
            session['result'] = {'state': 'finishing', 'message': 'Finishing sign-in. Waiting for the gateway.'}

    def _session(self, handle, call):
        self._expire(call)
        if not isinstance(handle, str) or handle not in self.sessions:
            raise AccountError('session_changed', 'This sign-in session is no longer available.', 409)
        return self.sessions[handle]

    def start(self, provider, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(self.call)
        with deadline.lock(self.lock):
            self._expire(call, reconcile_busy=True)
            if any(s['result']['state'] in ACTIVE for s in self.sessions.values()):
                raise AccountError('busy', 'Finish or cancel the current sign-in first.', 409)
            if not isinstance(provider, str) or provider not in FLOWS:
                raise AccountError('invalid_provider', 'Choose a supported provider.')
            config = call('/config')
            if getattr(config, 'version', None) not in TESTED_GATEWAYS:
                raise AccountError('unsupported_gateway', 'Account sign-in is unavailable on this gateway version.', 409)
            before = call('/auth-files').get('files', [])
            response = call(PREFIX + 'auth-url?' + urlencode({'provider': provider, 'is_webui': 'false'}))
            state = response.get('state')
            # Retain only the minimum server-owned state; never echo gateway errors.
            try:
                if not isinstance(state, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', state):
                    raise ValueError()
                url = clean_url(response.get('url'))
                flow, host, _, _ = FLOWS[provider]
                if url.scheme != 'https' or url.hostname != host or url.port not in (None, 443):
                    raise ValueError()
                if flow == 'browser' and parse_qs(url.query).get('state') != [state]:
                    raise ValueError()
                if response.get('status') != 'ok' or response.get('flow', flow) != flow:
                    raise ValueError()
            except (ValueError, TypeError):
                if isinstance(state, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,512}', state):
                    try:
                        call(PREFIX + 'session?' + urlencode({'state': state}), method='DELETE')
                    except Exception:
                        pass
                raise AccountError('gateway_error', 'The gateway returned an invalid sign-in response.', 503) from None
            lifetime = response.get('expires_in', 300)
            if type(lifetime) is not int or lifetime <= 0:
                lifetime = 300
            expires = self.clock() + min(lifetime, 900)
            handle = secrets.token_urlsafe(32)
            self.sessions[handle] = dict(provider=provider, gateway_state=state, expires=expires,
                before={r['name']: fingerprint(r) for r in before if r.get('type') == provider and r.get('name')},
                result={'state': 'waiting'})
            result = dict(session=handle, provider=provider, flow=flow, url=response['url'],
                          expires_at=dt.datetime.fromtimestamp(expires, dt.timezone.utc).isoformat())
            code = response.get('user_code')
            if flow == 'device' and isinstance(code, str) and 0 < len(code) <= 128 and code.isprintable():
                result['user_code'] = code
            return result

    def status(self, handle, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(self.call)
        with deadline.lock(self.lock):
            session = self._session(handle, call)
            if session['result']['state'] not in ACTIVE:
                return copy.deepcopy(session['result'])
            return self._reconcile(session, call)

    def _reconcile(self, session, call):
        try:
            response = call(PREFIX + 'status?' + urlencode({'state': session['gateway_state']}))
            status = response.get('status')
            if status == 'wait':
                return copy.deepcopy(session['result'])
            if status == 'ok':
                result = {'state': 'ok', 'message': 'Sign-in completed. Refresh the account inventory.'}
                try:
                    rows = call('/auth-files').get('files', [])
                    candidates = [r for r in rows if r.get('type') == session['provider'] and r.get('name')
                                  and session['before'].get(r['name']) != fingerprint(r)]
                    if len(candidates) == 1:
                        row = candidates[0]
                        result = {'state': 'ok', 'account': dict(key=row['name'], provider=session['provider'],
                            label=row.get('email') or row.get('label') or row['name'],
                            replaced=row['name'] in session['before'])}
                except Exception:
                    pass  # Inventory attribution cannot undo a confirmed gateway sign-in.
                session['result'] = result
                try:
                    self.changed()
                except Exception:
                    pass  # A refresh failure cannot undo a confirmed gateway sign-in.
            elif status == 'error':
                session['result'] = {'state': 'error', 'message': 'Sign-in failed. Start again.'}
            else:
                return {**session['result'], 'message': 'Gateway temporarily unavailable. Try again.'}
            session['finished_at'] = self.clock()
        except Exception:
            # A failed read may be transient; preserve the pending session for retry/cancel.
            return {**session['result'], 'message': 'Gateway temporarily unavailable. Try again.'}
        return copy.deepcopy(session['result'])

    def callback(self, handle, redirect_url, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(self.call)
        with deadline.lock(self.lock):
            session = self._session(handle, call)
            try:
                flow, _, port, path = FLOWS[session['provider']]
                url = clean_url(redirect_url)
                query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
                if (flow != 'browser' or session['result']['state'] != 'waiting'
                        or url.scheme not in ('http', 'https') or url.hostname not in ('localhost', '127.0.0.1')
                        or url.port != port or url.path != path or query.get('state') != [session['gateway_state']]
                        or len(query.get('code', [])) != 1 or not query['code'][0].strip()
                        or 'error' in query or 'error_description' in query):
                    raise ValueError()
            except (ValueError, TypeError):
                raise AccountError('invalid_callback', 'Paste the complete callback URL for this sign-in.') from None
            # This URL is data sent to management, never a network destination.
            response = call(PREFIX + 'callback', {'provider': session['provider'], 'redirect_url': redirect_url}, method='POST')
            if response.get('status') != 'ok':
                raise AccountError('gateway_error', 'The gateway did not accept the callback.', 503)
            return {'accepted': True}

    def cancel(self, handle, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(self.call)
        with deadline.lock(self.lock):
            session = self._session(handle, call)
            if session['result']['state'] in ACTIVE:
                self._cancel(session, call)
            state = session['result']['state']
            if state in ('cancelled', 'expired'):
                return {'cancelled': True}
            return {'cancelled': False, 'state': state}
