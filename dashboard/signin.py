"""In-memory Codex device sign-in, via a fixed, credential-free management relay.

Protocol pinned to CLIProxyAPI v8.0.7 sdk/auth/codex_device.go and
internal/auth/codex/openai_auth.go. Never return provider bodies or credentials.
New tokens are written to disk under the gateway's credential lock
(credential_replace), then synced to memory and verified; see
docs/account-access.md.
"""
import base64
import datetime as dt
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import account_access as access
import credential_replace
import routing_policy
from request_deadline import LOCK_WAIT_SECONDS, MANAGEMENT_CALL_SECONDS, RequestDeadline

CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
USERCODE = 'https://auth.openai.com/api/accounts/deviceauth/usercode'
DEVICE_TOKEN = 'https://auth.openai.com/api/accounts/deviceauth/token'
TOKEN = 'https://auth.openai.com/oauth/token'
VERIFY = 'https://auth.openai.com/codex/device'
REDIRECT = 'https://auth.openai.com/deviceauth/callback'
AUTH = 'https://api.openai.com/auth'
# Gateway builds whose Codex persistence was validated with
# tests/gateway_replace_integration.py. Extend only after re-running it.
TESTED_GATEWAYS = ('8.0.30', '8.0.43', '8.0.47', '8.0.79')
# The gateway's own /api-call timeout (defaultAPICallTimeout, api_tools.go:24), plus margin.
ECHO_LINGER_SECONDS = 65
# Completion's sync, verify and enable after the save; expiry ends as saved_unconfirmed.
COMPLETE_SECONDS = 120
# Informational outcomes last this long.
OUTCOME_SECONDS = 600


class ManagementResponse(dict):
    """A management JSON object plus the gateway build that served it (X-CPA-VERSION)."""
    def __init__(self, value, version=None):
        super().__init__(value)
        self.version = version


def log(kind, **fields):
    """One journal line per outcome; never tokens, codes, or headers."""
    try:
        print(json.dumps(dict(fields, event='codex_signin', kind=kind), sort_keys=True), flush=True)
    except Exception:
        pass  # The outcome stands without its journal line.


class AccessError(ValueError):
    """Only fixed, public error codes cross the HTTP boundary."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class IdentityError(ValueError):
    def __init__(self, kind, email=None, reason=None):
        self.kind, self.email, self.reason = kind, email, reason


def digest(value):
    return hashlib.sha256(value.encode()).digest()


def runtime_echo(call, auth_index, access_token, linger=ECHO_LINGER_SECONDS, deadline=None):
    """Have the gateway send its in-memory token to a one-shot loopback listener.

    /api-call substitutes $TOKEN$ from memory without refreshing Codex
    (api_tools.go:255-294,769-778). Only a SHA-256 comparison leaves the handler.
    The listener accepts until call() returns. If call() fails, the gateway may
    still deliver later, so the port stays bound until its /api-call timeout has
    passed rather than let another local process bind it and catch the token.
    A failed call, including its own management timeout, reads as no echo; only
    a used-up request deadline propagates, as TimeoutError, once that linger is
    arranged.
    """
    nonce, seen = secrets.token_urlsafe(32), []

    class Echo(BaseHTTPRequestHandler):
        timeout = 1  # A stray idle local connection cannot hold a handler.

        def do_GET(self):
            if self.path == '/' + nonce and not seen:
                seen.append(digest(self.headers.get('Authorization') or ''))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    class Listener(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, *_):
            pass

    listener = Listener(('127.0.0.1', 0), Echo)
    threading.Thread(target=listener.serve_forever, args=(0.05,), daemon=True).start()
    started, clean = time.monotonic(), False

    def stop(delay=0):
        time.sleep(delay)
        listener.shutdown()
        listener.server_close()

    try:
        # A request-level "direct" proxy outranks credential and global proxies.
        call('/api-call', {'auth_index': auth_index, 'method': 'GET', 'proxy_url': 'direct',
                           'url': 'http://127.0.0.1:%d/%s' % (listener.server_address[1], nonce),
                           'header': {'Authorization': 'Bearer $TOKEN$'}}, method='POST')
        clean = True
    except Exception:
        if deadline:
            lock_wait(deadline)  # Raises only once the request itself has run out.
    finally:
        if clean:
            stop()
        else:
            threading.Thread(target=stop, args=(max(0, started + linger - time.monotonic()),), daemon=True).start()
    return bool(seen) and hmac.compare_digest(seen[0], digest('Bearer ' + access_token))


def decode(token):
    if not isinstance(token, str) or len(token) > 65536 or len(token.split('.')) != 3:
        raise ValueError('Invalid ID token')
    value = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
    if not isinstance(value, dict):
        raise ValueError('Invalid ID token')
    return value


def safe_email(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9.!+_%=-]{1,100}@[A-Za-z0-9.-]{1,100}\.[A-Za-z]{2,24}', value) else None


def gateway_version(config):
    """The X-CPA-VERSION build for the journal, only if it looks like a version."""
    version = getattr(config, 'version', None)
    return version if isinstance(version, str) and re.fullmatch(r'[0-9A-Za-z.+_-]{1,40}', version) else None


def credential_identity(saved):
    try:
        claims = decode(saved['id_token'])
        auth = claims.get(AUTH) or {}
        email = claims.get('email')
        workspace = auth.get('chatgpt_account_id')
        if (not safe_email(email) or not isinstance(workspace, str) or not workspace
                or saved.get('email', email) != email
                or saved.get('account_id', workspace) != workspace
                or saved.get('chatgpt_account_id', workspace) != workspace):
            raise ValueError()
        return claims, auth, email, workspace
    except Exception:
        raise IdentityError('identity_unavailable') from None


def validate_tokens(tokens, saved, record, now):
    old, old_auth, email, workspace = credential_identity(saved)
    try:
        new = decode(tokens['id_token'])
        auth = new.get(AUTH) or {}
        aud, exp = new.get('aud'), new.get('exp')
        if (new.get('iss') != 'https://auth.openai.com'
                or not (aud == CLIENT_ID or isinstance(aud, list) and CLIENT_ID in aud)
                or type(exp) not in (int, float) or not math.isfinite(exp) or exp <= now):
            raise ValueError()
        approved = safe_email(new.get('email'))
        if not approved:
            raise ValueError()
        if approved != email or record.get('email') != email:
            raise IdentityError('wrong_account', approved)
        if auth.get('chatgpt_account_id') != workspace:
            raise IdentityError('wrong_workspace')
        listed_id = (record.get('id_token') or {}).get('chatgpt_account_id') or record.get('chatgpt_account_id')
        if listed_id and listed_id != workspace:
            raise IdentityError('wrong_workspace')
        for key in ('chatgpt_user_id', 'user_id'):
            if old_auth.get(key) and old_auth[key] != auth.get(key):
                raise IdentityError('invalid_identity')
        for key in ('chatgpt_user_id', 'user_id'):
            if saved.get(key) and saved[key] != auth.get(key):
                raise IdentityError('invalid_identity')
        if saved.get('sub') and saved['sub'] != new.get('sub'):
            raise IdentityError('invalid_identity')
        if old.get('sub') and old['sub'] != new.get('sub'):
            raise IdentityError('invalid_identity')
        plan = access.plan_value(auth.get('chatgpt_plan_type'))
        if plan is None:
            raise ValueError()
        if any(not isinstance(tokens.get(k), str) or not tokens[k] for k in ('access_token', 'refresh_token')):
            raise ValueError()
        seconds = tokens.get('expires_in')
        if type(seconds) is not int or not 0 < seconds <= 366 * 86400:
            raise ValueError()
        return plan
    except IdentityError:
        raise
    except Exception:
        raise IdentityError('invalid_identity') from None


def disk_identity(saved, record):
    """The downloaded credential is this listed account's: email, and the workspace if listed."""
    _, _, email, workspace = credential_identity(saved)
    listed = (record.get('id_token') or {}).get('chatgpt_account_id') or record.get('chatgpt_account_id')
    if email != record.get('email') or listed and listed != workspace:
        raise IdentityError('identity_unavailable')


def needs_signin(record, observed_required=False):
    """The gateway reports that this account's own tokens were rejected."""
    message = str(record.get('status_message') or '').lower()
    return observed_required or any(k in message for k in ('refresh_token_reused', 'refresh_token_expired',
                                                           'refresh_token_invalidated', 'invalid_grant'))


def lock_wait(deadline):
    return deadline.timeout(LOCK_WAIT_SECONDS) if deadline else LOCK_WAIT_SECONDS


def overdue(deadline, error):
    """On a request deadline, a timeout or a used-up deadline propagates as TimeoutError."""
    if deadline:
        if isinstance(error, TimeoutError):
            raise error
        lock_wait(deadline)  # A controller lock wait may have used up the request.


def verification_url(value, user_code=None):
    # Only accept the official page, optionally prefilled; never arbitrary URLs.
    if isinstance(value, str) and len(value) <= 2048:
        try:
            url = urlsplit(value)
            if (url.scheme == 'https' and url.netloc == 'auth.openai.com'
                    and url.path == '/codex/device' and not url.fragment
                    and all(k in ('user_code', 'usercode', 'code') and (user_code is None or v == user_code)
                            for k, v in parse_qsl(url.query, keep_blank_values=True))):
                return value
        except ValueError:
            pass
    return VERIFY


class SigninManager:
    def __init__(self, call, lifecycle_lock, changed, clock=time.time):
        self.call, self.lifecycle, self.changed = call, lifecycle_lock, changed
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions, self.outcomes = {}, {}
        self.routing_generations, self.cancel_generations = {}, {}
        self.closed = False
        self.instance = secrets.token_urlsafe(16)

    def resolve(self, account_id, identity, call=None):
        rows = [r for r in (call or self.call)('/auth-files').get('files', [])
                if r.get('type') == 'codex' and r.get('auth_index') == account_id
                and access.identity(r) == identity]
        if len(rows) != 1 or not identity:
            raise AccessError('account_changed')
        return rows[0]

    def download(self, record, call=None):
        saved = (call or self.call)('/auth-files/download?' + urlencode({'name': record['name']}))
        if not isinstance(saved, dict):
            raise ValueError('Invalid credential')
        return saved

    def relay(self, url, data, form=False, call=None):
        if url not in (USERCODE, DEVICE_TOKEN, TOKEN):
            raise ValueError('Invalid relay URL')
        # The relay must never select an account or substitute its token.
        if '$TOKEN$' in json.dumps(data):
            raise ValueError('Token substitution is forbidden')
        result = (call or self.call)('/api-call', {'method': 'POST', 'url': url,
            'header': {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
                       'Accept': 'application/json'},
            'data': urlencode(data) if form else json.dumps(data)}, method='POST')
        status = result.get('status_code')
        if url == DEVICE_TOKEN and status in (403, 404):
            return status, {}
        return status, access.json_object(result.get('body'))

    def active(self, session):
        with self.lock:
            return not self.closed and self.sessions.get(session['account_id']) is session and not session['stop'].is_set()

    def prune(self, records=None):
        with self.lock:
            identities = {record.get('auth_index'): access.identity(record) for record in records} if records is not None else None
            self.outcomes = {key: outcome for key, outcome in self.outcomes.items()
                             if self.clock() - outcome['at'] < OUTCOME_SECONDS
                             and (identities is None or identities.get(key) == outcome['identity'])}
            while len(self.outcomes) > 128:
                self.outcomes.pop(next(iter(self.outcomes)))
            if identities is not None:
                for key, session in list(self.sessions.items()):
                    if identities.get(key) != session['identity']:
                        session['stop'].set()
                        self.sessions.pop(key)

    def finish(self, session, kind, email=None, checks=None, enabled=None):
        fields = {'email': session.get('expected_email'), 'checks': checks or {},
                  'seconds': round(self.clock() - session.get('started_at', self.clock()), 1)}
        if kind == 'signed_in':
            fields['enabled'] = session['enable'] if enabled is None else enabled
        with self.lock:
            if not self.active(session):
                log(kind, recorded=False, **fields)
                return
            self.sessions.pop(session['account_id'], None)
            session['stop'].set()
            outcome = {'kind': kind, 'at': self.clock(), 'identity': session['identity']}
            if kind == 'wrong_account' and safe_email(email):
                outcome['approved_email'] = email
            if kind == 'signed_in':
                outcome['enabled'] = fields['enabled']
                if (checks or {}).get('enable_failed'):
                    outcome['enable_failed'] = True
            self.outcomes[session['account_id']] = outcome
            self.prune()
        log(kind, **fields)
        # Drop all remaining secrets as soon as the worker exits.

    def view(self, account_id, identity):
        """The public sign-in state."""
        with self.lock:
            self.prune()
            session = self.sessions.get(account_id)
            if session and session['identity'] == identity and 'user_code' in session:
                if session['state'] == 'pending' and self.clock() >= session['expires_at']:
                    self.finish(session, 'expired')
                else:
                    return {'instance': self.instance, 'session': {k: session[k] for k in (
                        'handle', 'state', 'user_code', 'verification_url', 'expected_email', 'started_at', 'expires_at')},
                        'last_outcome': None}
            outcome = self.outcomes.get(account_id)
            if outcome and outcome['identity'] == identity:
                return {'instance': self.instance, 'session': None,
                        'retry_allowed': outcome['kind'] == 'save_failed',
                        'last_outcome': {k: v for k, v in outcome.items() if k != 'identity'}}
            return {'instance': self.instance, 'session': None, 'last_outcome': None}

    def open_session(self, account_id, identity, record, existing, observed_required, routing_generation):
        if not (needs_signin(record, observed_required) or existing.get('retry_allowed')):
            raise AccessError('signin_not_required')
        now = self.clock()
        session = {'account_id': account_id, 'identity': identity,
            'handle': secrets.token_urlsafe(32), 'state': 'pending', 'expected_email': record['email'],
            'started_at': now, 'expires_at': now + 900,
            'routing_generation': routing_generation,
            'enable': routing_generation == self.routing_generations.get(account_id, 0),
            'stop': threading.Event()}
        self.outcomes.pop(account_id, None)
        self.sessions[account_id] = session
        return session

    def start(self, account_id, identity, observed_required=False, deadline=None):
        with deadline.lock(self.lock) if deadline else self.lock:
            routing_generation = self.routing_generations.get(account_id, 0)
            cancel_generation = self.cancel_generations.get(account_id, 0)
        call = deadline.wrap(self.call) if deadline else self.call
        record = self.resolve(account_id, identity, call)
        with deadline.lock(self.lock) if deadline else self.lock:
            existing = self.view(account_id, identity)
            if account_id in self.sessions or cancel_generation != self.cancel_generations.get(account_id, 0):
                return existing
            if self.closed:
                raise AccessError('unavailable')
            session = self.open_session(account_id, identity, record, existing, observed_required,
                                        routing_generation)
        step = 'preflight_failed'
        try:
            # Fail closed before the user approves a sign-in that could not be saved.
            if not os.access(credential_replace.AUTH_DIR, os.W_OK):
                self.finish(session, 'save_unavailable', checks={'reason': 'auth_dir_unwritable'})
                return self.view(account_id, identity)
            try:
                # Every reason the save would refuse this credential now; writes nothing to it.
                credential_replace.preflight(record)
            except credential_replace.ReplaceError as error:
                # Only a lasting refusal hides the button; a transient one can pass on the next click.
                self.finish(session, 'save_unavailable' if error.code in credential_replace.LASTING
                            else 'save_failed', checks={'reason': error.code})
                return self.view(account_id, identity)
            step = 'download_failed'
            # The same disk-against-listing identity check the save makes.
            disk_identity(self.download(record, call), record)
            step = 'config_failed'
            config = call('/config')
            if not isinstance(config, dict) or config.get('proxy-url') not in (None, ''):
                raise AccessError('proxy_configured')
            # Fail closed before the user approves a sign-in this build cannot save.
            if not self.tested(config):
                self.finish(session, 'gateway_untested',
                            checks={'reason': 'gateway_untested', 'version': gateway_version(config)})
                return self.view(account_id, identity)
            step = 'usercode_failed'
            status, body = self.relay(USERCODE, {'client_id': CLIENT_ID}, call=call)
            code = body.get('user_code') or body.get('usercode')
            device = body.get('device_auth_id')
            if (not isinstance(status, int) or not 200 <= status < 300
                    or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9 -]{3,32}', code)
                    or not isinstance(device, str) or not 1 <= len(device) <= 4096):
                raise ValueError()
            try:
                interval = int(body.get('interval', 5))
                if interval <= 0:
                    interval = 5
            except (TypeError, ValueError):
                interval = 5
            with deadline.lock(self.lock) if deadline else self.lock:
                if not self.active(session):
                    return self.view(account_id, identity)
                session.update(user_code=code, device_auth_id=device, interval=interval,
                    verification_url=verification_url(body.get('verification_uri_complete') or body.get('verification_url_complete') or body.get('verification_uri') or body.get('verification_url'), code))
            if deadline:
                deadline.timeout(MANAGEMENT_CALL_SECONDS)
            threading.Thread(target=self.run, args=(session,), daemon=True).start()
        except IdentityError as error:
            self.finish(session, error.kind, checks={'reason': error.reason or error.kind})
        except TimeoutError:
            self.finish(session, 'start_failed', checks={'reason': step, 'timed_out': True})
            raise
        except Exception as error:
            self.finish(session, 'start_failed',
                        checks={'reason': error.code if isinstance(error, AccessError) else step})
        return self.view(account_id, identity)

    def cancel(self, account_id, identity, handle, deadline=None):
        self.resolve(account_id, identity, deadline.wrap(self.call) if deadline else None)
        with deadline.lock(self.lock) if deadline else self.lock:
            session = self.sessions.get(account_id)
            if not session or session['identity'] != identity or session['handle'] != handle:
                raise AccessError('session_changed')
            if session['state'] == 'finishing':
                raise AccessError('already_finishing')
            session['stop'].set()
            self.sessions.pop(account_id, None)
            self.outcomes.pop(account_id, None)
            self.cancel_generations[account_id] = self.cancel_generations.get(account_id, 0) + 1
        return {'instance': self.instance, 'session': None, 'last_outcome': None}

    def suppress_enable(self, account_id):
        with self.lock:
            self.routing_generations[account_id] = self.routing_generations.get(account_id, 0) + 1
            if account_id in self.sessions:
                self.sessions[account_id]['enable'] = False

    def routing_set(self, account_id):
        """A deliberate routing change succeeded: a failed automatic enable no longer matters."""
        with self.lock:
            outcome = self.outcomes.get(account_id)
            if outcome:
                outcome.pop('enable_failed', None)

    def can_enable(self, session):
        with self.lock:
            return (self.active(session) and session['enable']
                    and session['routing_generation'] == self.routing_generations.get(session['account_id'], 0))

    def close(self):
        with self.lock:
            self.closed = True
            for session in self.sessions.values():
                session['stop'].set()
            self.sessions.clear()
            self.outcomes.clear()

    @staticmethod
    def tested(config):
        return getattr(config, 'version', None) in TESTED_GATEWAYS

    def save(self, session, tokens):
        """Write the new tokens to disk under the gateway's credential lock. Raises before writing."""
        record = self.resolve(session['account_id'], session['identity'])
        credential_replace.listed_file(record)
        # Fail fast on identity before waiting for the lock; re-checked on the locked bytes.
        now = self.clock()
        plan = validate_tokens(tokens, self.download(record), record, now)
        if not self.tested(self.call('/config')):
            raise IdentityError('gateway_untested')
        overlay = {key: tokens[key] for key in ('access_token', 'refresh_token', 'id_token')}
        overlay.update(expired=credential_replace.stamp(now + tokens['expires_in']),
                       last_refresh=credential_replace.stamp(now), plan_type=plan)

        def check(current):
            validate_tokens(tokens, current, record, now)
            if not self.active(session):
                raise ValueError('Sign-in cancelled')

        credential_replace.replace(record['name'], record['path'], overlay, check)
        return record, overlay

    def converge(self, record, expected, call=None, deadline=None):
        """Sync memory to the saved disk tokens and verify; never writes disk, never rolls back.

        Disk already holds the tokens, so the gateway's merge-from-disk makes this
        PATCH a synchronous memory update (conductor_persistence.go:225-232).
        With a request deadline, running out of time raises TimeoutError instead
        of returning failed checks.
        """
        call = call or self.call
        checks = {'disk': False, 'listing': False, 'runtime': False, 'repatched': False}
        try:
            saved = self.download(record, call)
            checks['disk'] = all(saved.get(key) == value for key, value in expected.items())
            if not checks['disk']:
                return checks
            auth = decode(expected['id_token'])[AUTH]
            claims = {'id_token': {'chatgpt_account_id': auth['chatgpt_account_id'], 'plan_type': expected['plan_type']}}
            with routing_policy.controller_flock(timeout=lock_wait(deadline)):
                lock_wait(deadline)
                for attempt in range(2):
                    checks['repatched'] = attempt > 0
                    call('/auth-files/fields', dict(expected, name=record['name']), method='PATCH')
                    current = self.resolve(record['auth_index'], access.identity(record), call)
                    checks['listing'] = access.runtime_matches(current, claims)
                    # The echo itself uses a "direct" proxy, so a configured proxy cannot affect it.
                    checks['runtime'] = runtime_echo(call, record['auth_index'], expected['access_token'],
                                                     deadline=deadline)
                    lock_wait(deadline)  # Raises once a request deadline has run out.
                    if checks['listing'] and checks['runtime']:
                        break
            lock_wait(deadline)
        except Exception as error:
            overdue(deadline, error)
            checks['error'] = True
        return checks

    @staticmethod
    def confirmed(checks):
        return bool(checks.get('disk') and checks.get('listing')) and checks.get('runtime') is True

    def still_disabled(self, record, call=None, deadline=None):
        """After a failed enable: the listing or the disk still shows the account off, or cannot say."""
        try:
            current = self.resolve(record['auth_index'], access.identity(record), call)
            return current.get('disabled') is not False or self.download(current, call).get('disabled') is not False
        except Exception as error:
            overdue(deadline, error)
            return True

    def enable(self, record, can_enable, call=None, deadline=None):
        call = call or self.call
        with routing_policy.controller_flock(timeout=lock_wait(deadline)):
            lock_wait(deadline)
            record = access.enable_after_signin(record, call, can_enable=can_enable)
            if self.download(record, call).get('disabled') is not False:
                raise ValueError('Enable persistence could not be confirmed')

    def enable_failure(self, record, can_enable, checks, error, call=None, deadline=None):
        """Keep a failed automatic enable visible unless routing is confirmed on or superseded."""
        if isinstance(error, TimeoutError):
            checks['timed_out'] = True
        if not can_enable():
            return
        try:
            disabled = self.still_disabled(record, call, deadline)
        except TimeoutError:
            # No time to read back: routing may still be off, never intentionally off.
            disabled, checks['timed_out'] = True, True
        if disabled and can_enable():
            checks['enable_failed'] = True

    def complete(self, session, tokens):
        with self.lock:
            if not self.active(session):
                # The token exchange happened; record that nothing was saved.
                self.finish(session, 'save_failed', checks={'reason': 'session_ended'})
                return
            session['state'] = 'finishing'
        # The credential lock can take up to 90 s; wait for it without the lifecycle
        # lock so routing changes and fetches continue. check() re-checks the session.
        record, expected = self.save(session, tokens)
        # Saved. Never roll back: the new refresh token exists only on disk now.
        # One deadline bounds the rest, so a hung gateway cannot hold the lifecycle
        # lock or controller.lock for long; running out ends as saved_unconfirmed.
        deadline = RequestDeadline(COMPLETE_SECONDS)
        call = deadline.wrap(self.call)

        def unconfirmed(checks):
            self.finish(session, 'saved_unconfirmed', checks=checks)

        try:
            acquired = self.lifecycle.acquire(timeout=deadline.timeout(COMPLETE_SECONDS))
        except TimeoutError:
            acquired = False
        if not acquired:
            unconfirmed({'timed_out': True})
            self.changed(session['account_id'])
            return
        try:
            try:
                checks = self.converge(record, expected, call, deadline)
            except TimeoutError:
                checks = {'timed_out': True}
            except Exception:
                checks = {'error': True}  # Saved regardless: never reported as a failed save.
            if not self.confirmed(checks):
                unconfirmed(checks)
            else:
                enabled = session['enable']
                if self.can_enable(session):
                    try:
                        self.enable(record, lambda: self.can_enable(session), call, deadline)
                    except Exception as error:
                        enabled = False
                        self.enable_failure(record, lambda: self.can_enable(session), checks, error, call, deadline)
                self.finish(session, 'signed_in', checks=checks, enabled=enabled)
        finally:
            self.lifecycle.release()
        self.changed(session['account_id'])

    def run(self, session):
        step = 'device_poll_failed'  # The journal reason for an OpenAI relay error.
        try:
            while self.active(session):
                delay = min(session['interval'], max(0, session['expires_at'] - self.clock()))
                if session['stop'].wait(delay):
                    return
                if self.clock() >= session['expires_at']:
                    self.finish(session, 'expired')
                    return
                status, body = self.relay(DEVICE_TOKEN, {
                    'device_auth_id': session['device_auth_id'], 'user_code': session['user_code']})
                if not self.active(session):
                    return
                if self.clock() >= session['expires_at']:
                    self.finish(session, 'expired')
                    return
                if status in (403, 404):
                    continue
                error = body.get('error')
                error = error.get('code') if isinstance(error, dict) else error
                if error in ('access_denied', 'authorization_declined', 'user_denied'):
                    self.finish(session, 'declined')
                    return
                if error in ('expired_token', 'expired_device_code'):
                    self.finish(session, 'expired')
                    return
                if error in ('authorization_pending', 'slow_down'):
                    if error == 'slow_down':
                        session['interval'] += 5
                    continue
                step = 'device_response_invalid'
                if type(status) is not int or not 200 <= status < 300:
                    raise ValueError()
                if not all(isinstance(body.get(k), str) and body[k] for k in ('authorization_code', 'code_verifier')):
                    raise ValueError()
                with self.lock:
                    if not self.active(session):
                        return
                    session['state'] = 'finishing'
                step = 'token_exchange_failed'
                status, tokens = self.relay(TOKEN, {'grant_type': 'authorization_code',
                    'client_id': CLIENT_ID, 'code': body['authorization_code'],
                    'redirect_uri': REDIRECT, 'code_verifier': body['code_verifier']}, form=True)
                if status != 200:
                    raise ValueError()
                try:
                    self.complete(session, tokens)
                except IdentityError as failure:
                    self.finish(session, failure.kind, failure.email)
                except credential_replace.ReplaceError as failure:
                    # Nothing was saved. A lasting refusal would refuse a new sign-in too, so it offers no retry.
                    self.finish(session, 'save_unavailable' if failure.code in credential_replace.LASTING
                                else 'save_failed', checks={'reason': failure.code})
                except Exception as failure:
                    # Raised before the disk write; nothing was saved.
                    self.finish(session, 'save_failed', checks={'reason': getattr(failure, 'code', type(failure).__name__)})
                return
        except Exception:
            self.finish(session, 'provider_error', checks={'reason': step})
