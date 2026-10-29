"""Account-scoped earned resets. Never purchases credits or changes login state."""
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

STATE = Path('/var/lib/personal-codex-router/reset-controls.json')
SUCCESS = {'reset', 'already_redeemed'}
AUTO_WINDOW_SECONDS = 5 * 60
RECOVERY_BACKOFF_SECONDS = 10
RECOVERY_MAX_BACKOFF_SECONDS = 5 * 60


def epoch(value):
    try:
        if isinstance(value, str):
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return None
            value = parsed.timestamp()
        return float(value) if not isinstance(value, bool) and math.isfinite(float(value)) else None
    except (ValueError, TypeError, OverflowError):
        return None


def recovery_pending(attempt, now):
    steps = attempt.get('steps') or {}
    deadline = epoch(attempt.get('weekly_resets_at'))
    if deadline is None:
        deadline = attempt.get('at', now) + 7 * 86400
    return (attempt.get('outcome') in SUCCESS and now < deadline
            and (steps.get('gateway_reset') != 'ok'
                 or any(steps.get(k) not in ('ok', 'skipped')
                        for k in ('quota_refresh', 'routing_reconcile'))))


class ResetManager:
    def __init__(self, api, metadata, path=STATE, after_reset=None):
        self.api, self.metadata, self.path = api, metadata, Path(path)
        self.after_reset = after_reset
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.cache = {}
        self.state = {'accounts': {}, 'attempts': {}}
        try:
            self.state = json.loads(self.path.read_text())
            assert isinstance(self.state['accounts'], dict) and isinstance(self.state['attempts'], dict)
        except FileNotFoundError:
            pass
        except Exception:
            # Invalid state must not silently recreate redemption identities.
            raise RuntimeError('Reset control state needs repair') from None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w') as f:
            os.chmod(tmp, 0o600)
            json.dump(self.state, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def upstream(self, file, path, body=None):
        assert path in ('usage', 'rate-limit-reset-credits', 'rate-limit-reset-credits/consume')
        meta = self.metadata(file)
        auth = meta.get('https://api.openai.com/auth') or {}
        aid = meta.get('chatgpt_account_id') or auth.get('chatgpt_account_id') or file.get('chatgpt_account_id')
        if not aid:
            raise ValueError('Account identity unavailable')
        request = {'auth_index': file['auth_index'], 'method': 'POST' if body is not None else 'GET',
                   'url': 'https://chatgpt.com/backend-api/wham/' + path,
                   'header': {'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json',
                              'Chatgpt-Account-Id': aid}}
        if body is not None:
            request['data'] = json.dumps(body)
        response = self.api('/api-call', request)
        if response.get('status_code') != 200:
            raise RuntimeError('Provider request unavailable')
        data = response.get('body')
        data = json.loads(data) if isinstance(data, str) else data
        if not isinstance(data, dict):
            raise ValueError('Provider response unavailable')
        return data

    def fetch(self, file, usage=None):
        details = self.upstream(file, 'rate-limit-reset-credits')
        now = time.time()
        credits = []
        for row in details.get('credits') or []:
            expiry = epoch(row.get('expires_at'))
            if (row.get('status') != 'available' or row.get('reset_type') != 'codex_rate_limits'
                    or row.get('is_supported_by_plan') is not True or not isinstance(row.get('id'), str)
                    or (expiry is not None and expiry <= now)):
                continue
            credits.append({'id': row['id'], 'expires_at': expiry})
        credits.sort(key=lambda r: (r['expires_at'] or float('inf'), r['id']))
        count = details.get('available_count')
        if type(count) is not int or count < 0:
            count = None
        result = {'available': count, 'credits': credits, 'eligible': bool(credits),
                  'checked_at': now, 'error': None}
        with self.lock:
            self.cache[file['email']] = result
        return result

    def view(self, email):
        with self.lock:
            result = copy.deepcopy(self.cache.get(email, {'available': None, 'credits': [], 'eligible': False,
                                                         'checked_at': None, 'error': 'Reset balance unavailable'}))
            if result['checked_at'] is not None and time.time() - result['checked_at'] > 120:
                result.update(eligible=False, error='Reset balance needs refresh')
            policy = self.state['accounts'].get(email, {})
            result['automatic'] = policy.get('automatic', False)
            result['last_event'] = policy.get('last_event')
            result['expiry_alert'] = copy.deepcopy(policy.get('expiry_alert'))
            result['pending_credit_id'] = next((a['credit_id'] for a in self.state['attempts'].values()
                                                if a.get('email') == email
                                                and a.get('outcome') in ('pending', 'uncertain')), None)
            result['recovery_pending'] = any(a.get('email') == email and recovery_pending(a, time.time())
                                             for a in self.state['attempts'].values())
            return result

    def expiry_result(self, email, credit_id, expires_at, success):
        with self.lock:
            policy = self.state['accounts'].setdefault(email, {})
            previous = policy.get('expiry_alert') or {}
            if success and previous.get('credit_id') != credit_id:
                return
            alert = {'credit_id': credit_id, 'expires_at': expires_at,
                     'status': 'resolved' if success else 'failed',
                     'at': previous.get('at', time.time()) if previous.get('credit_id') == credit_id else time.time()}
            if previous != alert:
                policy['expiry_alert'] = alert
                self.save()

    def account(self, account_id):
        matches = [f for f in self.api('/auth-files').get('files', [])
                   if f.get('type') == 'codex' and f.get('auth_index') == account_id]
        if len(matches) != 1 or not matches[0].get('email') or matches[0].get('disabled'):
            raise ValueError('Connected account unavailable')
        message = str(matches[0].get('status_message') or '').lower()
        if any(code in message for code in ('refresh_token_reused', 'refresh_token_expired',
                                            'refresh_token_invalidated', 'invalid_grant',
                                            'invalid_api_key', 'authentication_error')):
            raise ValueError('Connected account unavailable')
        return matches[0]

    def policy(self, account_id, enabled):
        if type(enabled) is not bool:
            raise ValueError('Automatic must be true or false')
        # Serialize opt-out with automatic redemption; a completed opt-out prevents new attempts.
        with self.operation:
            f = self.account(account_id)
            with self.lock:
                self.state['accounts'].setdefault(f['email'], {})['automatic'] = enabled
                self.save()
            return self.view(f['email'])

    def event(self, email, message, steps=None):
        with self.lock:
            self.state['accounts'].setdefault(email, {})['last_event'] = {'at': time.time(), 'message': message}
            if steps is not None:
                self.state['accounts'][email]['last_event']['steps'] = steps
            self.save()

    def recovered(self, file, identity, outcome, automatic):
        with self.lock:
            previous = self.state['attempts'][identity].get('steps') or {}
        steps = {'provider': outcome, 'gateway_reset': 'failed',
                 'quota_refresh': 'unavailable', 'routing_reconcile': 'unavailable'}
        if previous.get('gateway_reset') == 'ok':
            steps['gateway_reset'] = 'ok'
        else:
            try:
                result = self.api('/reset-quota', {'auth_index': file['auth_index']})
                if result.get('status') == 'ok' and result.get('auth_index') == file['auth_index']:
                    steps['gateway_reset'] = 'ok'
            except Exception:
                pass
        if steps['gateway_reset'] == 'ok' and self.after_reset is not None:
            try:
                steps.update(self.after_reset(file['auth_index']))
            except Exception:
                steps.update(quota_refresh='failed', routing_reconcile='failed')
        elif steps['gateway_reset'] == 'ok':
            steps.update(quota_refresh='skipped', routing_reconcile='skipped')
        with self.lock:
            self.state['attempts'][identity]['steps'] = steps
            self.schedule_recovery(self.state['attempts'][identity])
            self.save()
        self.event(file['email'], 'Automatic reset applied.' if automatic else 'Reset applied.', steps)
        return steps

    def schedule_recovery(self, attempt):
        retries = attempt.get('recovery_attempts', 0)
        delay = min(RECOVERY_MAX_BACKOFF_SECONDS, RECOVERY_BACKOFF_SECONDS * 2 ** min(retries, 6))
        attempt.update(recovery_attempts=retries + 1, recovery_retry_at=time.time() + delay)

    def weekly_reset(self, file):
        try:
            limits = self.upstream(file, 'usage').get('rate_limit') or {}
            for key in ('primary_window', 'secondary_window'):
                window = limits.get(key) or {}
                reset = epoch(window.get('reset_at'))
                if window.get('limit_window_seconds') == 604800 and reset is not None:
                    return reset
        except Exception:
            pass
        return None

    def consume(self, account_id, credit_id, automatic=False):
        with self.operation:
            f = self.account(account_id)
            email = f['email']
            if automatic and not self.view(email)['automatic']:
                return {'outcome': 'disabled'}
            fresh = self.fetch(f)
            credit = next((c for c in fresh['credits'] if c['id'] == credit_id), None)
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'personal-codex-router:' + email + ':' + credit_id))
            with self.lock:
                attempt = self.state['attempts'].get(identity)
                pending = [a for a in self.state['attempts'].values() if a.get('email') == email
                           and a.get('outcome') in ('pending', 'uncertain')]
            if pending:
                # Balance alone cannot prove a lost response succeeded (credits can expire).
                # Observe allowance too, but only provider confirmation releases the guard.
                try:
                    self.upstream(f, 'usage')
                except Exception:
                    raise RuntimeError('Reset result uncertain; allowance recheck unavailable') from None
                if any(a.get('credit_id') != credit_id for a in pending):
                    self.event(email, 'Reset result uncertain. Retry the original credit before selecting another.')
                    return {'outcome': 'uncertain', 'credit_id': pending[0]['credit_id']}
            if attempt and attempt.get('outcome') in SUCCESS:
                steps = attempt.get('steps') or {}
                if recovery_pending(attempt, time.time()):
                    steps = self.recovered(f, identity, 'already_redeemed', automatic)
                self.expiry_result(email, credit_id, credit['expires_at'] if credit else None, True)
                return {'outcome': 'already_redeemed', 'steps': steps}
            if credit is None and not pending:
                return {'outcome': 'no_credit'}
            if not pending and credit_id != fresh['credits'][0]['id']:
                return {'outcome': 'earliest_required'}
            expiry = credit['expires_at'] if credit else (attempt or {}).get('expires_at')
            remaining = None if expiry is None else expiry - time.time()
            if automatic and (remaining is None or not 0 < remaining <= AUTO_WINDOW_SECONDS):
                return {'outcome': 'not_due'}
            # Capture the original weekly deadline before redemption can move it.
            # If unavailable, retries are limited to one week from the attempt.
            weekly_reset = (attempt or {}).get('weekly_resets_at') or self.weekly_reset(f)
            # Both manual and expiry requests delegate allowance eligibility to the provider.
            # Save identity before the network write. Manual/automatic/restart retries reuse it.
            with self.lock:
                self.state['attempts'][identity] = {'outcome': 'pending', 'email': email,
                                                   'credit_id': credit_id, 'expires_at': expiry,
                                                   'weekly_resets_at': weekly_reset, 'automatic': automatic,
                                                   'at': (attempt or {}).get('at', time.time())}
                self.save()
            try:
                result = self.upstream(f, 'rate-limit-reset-credits/consume',
                                       {'redeem_request_id': identity, 'credit_id': credit_id})
                outcome = result.get('code')
                if outcome not in SUCCESS | {'nothing_to_reset', 'no_credit'}:
                    raise ValueError('Unknown reset result')
                if pending and outcome == 'no_credit':
                    outcome = 'uncertain'
            except Exception:
                self.event(email, 'Reset result uncertain. Retrying this credit uses the same request; no second credit is selected.')
                raise RuntimeError('Reset result uncertain; refresh before retrying the same credit') from None
            with self.lock:
                self.state['attempts'][identity]['outcome'] = outcome
                self.save()
            steps = None
            if outcome in SUCCESS:
                steps = self.recovered(f, identity, outcome, automatic)
            else:
                self.event(email, 'Reset result uncertain. Retry the original credit before selecting another.'
                           if outcome == 'uncertain' else 'No reset applied: ' + outcome.replace('_', ' ') + '.')
            if automatic or outcome in SUCCESS:
                self.expiry_result(email, credit_id, expiry, outcome in SUCCESS)
            try:
                self.fetch(f)
            except Exception:
                with self.lock:
                    self.cache.pop(email, None)
            if outcome == 'uncertain':
                return {'outcome': outcome, 'credit_id': credit_id}
            return {'outcome': outcome, 'steps': steps} if steps is not None else {'outcome': outcome}

    def tick(self):
        # Recovery never calls consume: the provider has already confirmed it.
        with self.operation:
            with self.lock:
                recoveries = [identity for identity, a in self.state['attempts'].items()
                              if recovery_pending(a, time.time())
                              and a.get('recovery_retry_at', 0) <= time.time()]
            for identity in recoveries:
                with self.lock:
                    attempt = copy.deepcopy(self.state['attempts'][identity])
                try:
                    matches = [f for f in self.api('/auth-files').get('files', [])
                               if f.get('type') == 'codex' and f.get('email') == attempt['email']]
                    if len(matches) != 1:
                        raise ValueError('Connected account unavailable')
                    file = self.account(matches[0]['auth_index'])
                    if not recovery_pending(attempt, time.time()):
                        continue
                    self.recovered(file, identity, attempt['outcome'], attempt.get('automatic', False))
                except Exception:
                    with self.lock:
                        self.schedule_recovery(self.state['attempts'][identity])
                        self.save()
        # This ten-second check is local. Failed attempts retry on the next tick, only during the final five minutes.
        with self.lock:
            due = [(email, c['id'], c['expires_at']) for email, data in self.cache.items()
                   if self.state['accounts'].get(email, {}).get('automatic')
                   for c in data['credits'][:1] if c['expires_at'] is not None
                   and 0 < c['expires_at'] - time.time() <= AUTO_WINDOW_SECONDS
                   and not any(a.get('email') == email and a.get('credit_id') == c['id']
                               and a.get('outcome') in SUCCESS for a in self.state['attempts'].values())]
        if not due:
            return
        try:
            files = self.api('/auth-files').get('files', [])
        except Exception:
            files = []
        for email, cid, expiry in due:
            matches = [f for f in files if f.get('type') == 'codex' and f.get('email') == email]
            if len(matches) == 1:
                try:
                    result = self.consume(matches[0]['auth_index'], cid, automatic=True)
                    if result.get('outcome') == 'no_credit':
                        self.expiry_result(email, cid, expiry, False)
                except Exception:
                    self.expiry_result(email, cid, expiry, False)
                    self.event(email, 'Automatic reset could not be confirmed. Check account allowance.')
            else:
                self.expiry_result(email, cid, expiry, False)
