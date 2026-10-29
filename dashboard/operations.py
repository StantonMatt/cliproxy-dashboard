"""Allowlisted health and durable account notices, without notification providers."""
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

OPS = Path('/var/lib/personal-codex-router-ops')
NOTICES = Path('/var/lib/personal-codex-router/notifications.json')
EMAILS = ('account-a@example.com', 'account-b@example.com')


def age(value, now):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return now - parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def fresh(value, now, limit=180):
    elapsed = age(value, now)
    return elapsed is not None and -30 <= elapsed <= limit


def signin_superseded(account):
    """A confirmed sign-in newer than the account's last check makes its sign_in_required stale."""
    outcome = (account.get('signin') or {}).get('last_outcome') or {}
    at = outcome.get('at')
    if outcome.get('kind') != 'signed_in' or type(at) not in (int, float) or not math.isfinite(at):
        return False
    elapsed = age(account.get('quota_attempted_at'), at)
    return elapsed is not None and elapsed > 0


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def account_health(account, gateway, now):
    if account is None:
        return 'unknown'
    if not fresh(gateway.get('checked_at'), now) or gateway.get('online') is not True:
        return 'stale'
    if not fresh(account.get('quota_attempted_at'), now):
        return 'stale'
    if account.get('access_state') == 'disable_unconfirmed':
        return 'disable_unconfirmed'
    if account.get('disabled'):
        outcome = (account.get('signin') or {}).get('last_outcome') or {}
        # A confirmed sign-in whose automatic enable failed was not turned off on purpose.
        if outcome.get('kind') == 'signed_in' and outcome.get('enable_failed') is True:
            return 'enable_failed'
        return 'intentionally_disabled'
    if account.get('auth_state') == 'sign_in_required' and not signin_superseded(account):
        return 'sign_in_required'
    if not account.get('active'):
        return 'unavailable'
    if account.get('quota_error') or account.get('auth_state') != 'authenticated':
        return 'unknown'
    windows = account.get('windows') or []
    if any(w.get('remaining') == 0 for w in windows):
        return 'limited'
    return 'healthy' if windows and all(w.get('remaining') is not None for w in windows) else 'unknown'


def authenticated(account, gateway, now):
    return (account is not None and gateway.get('online') is True
            and fresh(gateway.get('checked_at'), now)
            and fresh(account.get('quota_attempted_at'), now)
            and account.get('auth_state') == 'authenticated')


def weekly_allowance(account, gateway, now):
    if not can_resume(account) or not authenticated(account, gateway, now) or account.get('quota_error'):
        return None
    return next((w for w in account.get('windows') or []
                 if (w.get('seconds') == 604800 or w.get('label') == 'Weekly')
                 and type(w.get('remaining')) in (int, float)
                 and math.isfinite(w['remaining'])), None)


def can_resume(account):
    """Quota resets cannot enable credentials or repair revoked authentication."""
    return bool(account and not account.get('disabled')
                and account.get('access_state') != 'disable_unconfirmed'
                and account.get('auth_state') != 'sign_in_required'
                and (account.get('availability') or {}).get('state') not in
                ('disabled', 'disable_unconfirmed', 'auth_required'))


def can_serve(account, gateway, now):
    if (not can_resume(account) or not authenticated(account, gateway, now)
            or account.get('quota_error') or not fresh(account.get('quota_checked_at'), now)):
        return False
    availability = account.get('availability') or {}
    if availability.get('scope', 'account') != 'account' or availability.get('state') != 'ready':
        return False
    windows = account.get('windows') or []
    return bool(windows) and all(type(w.get('remaining')) in (int, float)
                                and math.isfinite(w['remaining']) and 0 < w['remaining'] <= 100
                                for w in windows)


def exhaustion_message(details, both, fallback, now):
    times = [a.get('weekly_resets_at') for a in details if type(a.get('weekly_resets_at')) in (int, float)
             and math.isfinite(a['weekly_resets_at']) and a['weekly_resets_at'] > now]
    when = ''
    if times:
        # Match the dashboard countdown, including rounding up partial minutes.
        seconds = math.ceil(min(times) - now)
        hours = seconds // 3600
        days = hours // 24
        countdown = (f'{days}d {hours % 24}h' if days else
                     f'{hours}h {math.ceil((seconds % 3600) / 60)}m' if hours else
                     f'{math.ceil(seconds / 60)}m')
        when = ' · resets in ' + countdown
    if both:
        return 'Weekly limits reached · Codex requests fail' + when
    if fallback:
        return 'Weekly limit reached · using the other account'
    return 'Weekly limit reached · no account can serve' + when


def exhaustion_candidates(accounts, gateway, now):
    windows = {email: weekly_allowance(accounts.get(email), gateway, now) for email in EMAILS}
    # Auth failures can explain an unavailable fallback without a quota response.
    # Unknown/stale observations alone do not invent an exhaustion episode.
    if any(w is None and not (accounts.get(email) and gateway.get('online') is True
                             and fresh(gateway.get('checked_at'), now)
                             and fresh(accounts[email].get('quota_attempted_at'), now)
                             and not can_resume(accounts[email]))
           for email, w in windows.items()):
        return {}
    exhausted = [accounts[email] for email in EMAILS
                 if windows[email] is not None and windows[email]['remaining'] == 0]
    if not exhausted:
        return {}
    both = len(exhausted) == len(EMAILS)
    other = next((accounts[email] for email in EMAILS if accounts[email] not in exhausted), None)
    fallback = not both and can_serve(other, gateway, now)
    details, choices = [], []
    for account in exhausted:
        resets = account.get('resets') or {}
        details.append({'account_id': account.get('id'), 'label': account['email'],
                        'weekly_resets_at': windows[account['email']].get('resets_at'),
                        'reset_credits_available': resets.get('available')})
        if (not account.get('disabled') and (account.get('availability') or {}).get('state') != 'disabled'
                and account.get('auth_state') == 'authenticated'
                and account.get('id') and resets.get('eligible') is True
                and type(resets.get('available')) is int and resets['available'] > 0
                and not resets.get('error')):
            for credit in resets.get('credits', []):
                expiry = credit.get('expires_at')
                if expiry is None or (type(expiry) in (int, float) and math.isfinite(expiry) and expiry > now):
                    choices.append((float('inf') if expiry is None else expiry, account['id'], account))
    values = {'email': 'Both Codex accounts' if both else exhausted[0]['email'],
              'kind': 'pool_exhausted' if both else 'weekly_exhausted',
              'urgency': 'normal' if fallback else 'critical',
              'message': exhaustion_message(details, both, fallback, now),
              'accounts': details, 'hidden': False}
    if choices:
        account = min(choices, key=lambda c: (c[0], c[1]))[2]
        values['action'] = {'type': 'redeem_reset', 'account_id': account['id'],
                            'label': account['email']}
    return {'exhausted:' + ('pool' if both else exhausted[0]['email']): values}


def usage_health(probe, now):
    usage = probe.get('usage_collection')
    if not isinstance(usage, dict):
        return {'status': 'unknown', 'issues': [], 'last_success_at': None}
    issues = []
    if usage.get('last_run_failed') is True:
        issues.append('Last usage collection run failed.')
    if usage.get('timer_active') is False:
        issues.append('Usage collection timer is inactive.')
    successful = usage.get('last_success_at')
    if not fresh(successful, now, 300):
        issues.append('No successful usage collection run in the last five minutes.')
    if isinstance(usage.get('failed_spools'), int) and usage['failed_spools'] > 0:
        issues.append('Failed usage spools need attention.')
    if usage.get('quarantine_increased') is True:
        issues.append('Usage records were quarantined since the previous check.')
    if usage.get('collector_status_error'):
        issues.append('Usage collector status is unavailable or invalid; aggregate counts are unknown.')
    status = ('failed' if usage.get('last_run_failed') is True or usage.get('timer_active') is False else
              'unknown' if usage.get('collector_status_error') else
              'stale' if not fresh(successful, now, 300) else
              'warning' if issues else 'healthy')
    if not fresh(probe.get('checked_at'), now, 120):
        status = 'stale'
        issues.append('Usage collection health observations are stale.')
    elif usage.get('unit_available') is not True or usage.get('timer_active') is None:
        status = 'unknown'
        issues.append('Usage collection unit status is unavailable.')
    return {'status': status, 'issues': issues, 'last_success_at': successful,
            'checked_at': probe.get('checked_at'), 'failed_spools': usage.get('failed_spools'),
            'quarantined_records': usage.get('quarantined_records'),
            'last_run_at': usage.get('last_run_at'),
            'collector_status_error': usage.get('collector_status_error')}


def health(gateway, now=None, directory=OPS):
    now = time.time() if now is None else now
    probe, backup = read_json(directory / 'health.json'), read_json(directory / 'backup.json')
    codex = probe.get('codex', {})
    daemon = codex.get('status') if isinstance(codex, dict) else None
    if not probe:
        daemon = 'unknown'
    elif not fresh(probe.get('checked_at'), now, 120):
        daemon = 'stale'
    if daemon not in ('healthy', 'down', 'unknown', 'stale'):
        daemon = 'unknown'
    gateway_state = 'unknown'
    if gateway.get('online') is False:
        gateway_state = 'down'
    elif gateway.get('online') is True:
        gateway_state = 'healthy' if fresh(gateway.get('checked_at'), now) else 'stale'
    accounts = {a.get('email'): a for a in gateway.get('accounts', [])}
    successful = backup.get('last_success_at')
    backup_state = ('failed' if backup.get('ok') is False else
                    'healthy' if fresh(successful, now, 36 * 3600) else
                    'stale' if successful else 'unknown')
    return {'codex': {'status': daemon, 'checked_at': probe.get('checked_at')},
            'usage_collection': usage_health(probe, now),
            'gateway': {'status': gateway_state, 'checked_at': gateway.get('checked_at')},
            'accounts': [{'email': email, 'status': account_health(accounts.get(email), gateway, now),
                          'reason': (accounts.get(email) or {}).get('access_reason') or
                                    (accounts.get(email) or {}).get('access_error')} for email in EMAILS],
            'backup': {'status': backup_state, 'last_success_at': successful, 'storage': 'same_disk'}}


def candidates(gateway, now, usage=None):
    result = {}
    if usage and usage['status'] != 'healthy':
        result['usage:collection'] = {'email': 'Usage collection', 'kind': 'usage_collection',
                                      'message': ' '.join(usage['issues']) or 'Usage collection health is unknown.'}
    accounts = {a.get('email'): a for a in gateway.get('accounts', [])}
    result.update(exhaustion_candidates(accounts, gateway, now))
    today = dt.datetime.fromtimestamp(now, ZoneInfo('America/Santiago')).date()
    for email in EMAILS:
        a = accounts.get(email)
        if not a:
            continue
        state = account_health(a, gateway, now)
        # A disabled account, including one whose automatic enable failed, can still need a new
        # sign-in; a confirmed sign-in newer than the last check cannot.
        if (state in ('sign_in_required', 'intentionally_disabled', 'enable_failed')
                and a.get('auth_state') == 'sign_in_required' and not signin_superseded(a)):
            result['signin:' + email] = {'email': email, 'kind': 'sign_in', 'message': 'Needs a new sign-in'}
        if (gateway.get('online') is True and fresh(gateway.get('checked_at'), now)
                and a.get('plan_type') == 'free' and a.get('access_identity')):
            result['free:' + a['access_identity']] = {
                'email': email, 'kind': 'free_plan', 'urgency': 'info', 'message': 'ChatGPT reports a Free plan'}
        outcome = (a.get('signin') or {}).get('last_outcome') or {}
        if outcome.get('kind') == 'signed_in' and outcome.get('enabled') is True:
            result['enabled:' + a['access_identity'] + ':' + str(outcome['at'])] = {
                'email': email, 'kind': 'signin_enabled',
                'message': 'Signed in · account enabled'}
        alert = a.get('resets', {}).get('expiry_alert') or {}
        if alert.get('status') == 'failed' and isinstance(alert.get('at'), (int, float)) and 0 <= now - alert['at'] <= 7 * 86400:
            expired = isinstance(alert.get('expires_at'), (int, float)) and alert['expires_at'] <= now
            result['reset:' + email + ':' + str(alert.get('credit_id'))] = {
                'email': email, 'kind': 'reset_failed', 'message':
                ('Reset expired unconfirmed · check allowance' if expired else
                 'Reset not confirmed · retrying until expiry' if a['resets'].get('automatic') is True else
                 'Reset not confirmed · check allowance')}
        bill = a.get('billing') or {}
        try:
            renewal = dt.date.fromisoformat(bill.get('renewal_date'))
        except (ValueError, TypeError):
            continue
        if not fresh(gateway.get('checked_at'), now) or gateway.get('online') is not True:
            continue
        days = (renewal - today).days
        if not 0 <= days <= 7:
            continue
        stage = 'review' if days <= 2 else 'upcoming'
        confirmed = bill.get('confirmed') is True
        # Compare calendar dates in Santiago, not elapsed 24-hour periods across DST.
        # Explicit en-GB month names match Intl.DateTimeFormat regardless of server locale.
        months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sept', 'Oct', 'Nov', 'Dec')
        date = {0: 'today', 1: 'tomorrow', 2: 'in 2 days'}.get(days,
                f'{renewal.day} {months[renewal.month - 1]}')
        message = 'Renews ' + date + ('' if confirmed else ' · date unconfirmed')
        result[f'renewal:{email}:{renewal}:{confirmed}:{stage}'] = {
            'email': email, 'kind': 'renewal', 'message': message}
    return result


class NoticeStore:
    def __init__(self, path=NOTICES, ops_directory=OPS):
        self.path = Path(path)
        self.ops_directory = Path(ops_directory)
        self.lock = threading.RLock()
        self.rows = {}
        self.error = False
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text())
                if value.get('version') != 1 or not isinstance(value.get('rows'), dict):
                    raise ValueError('Invalid notice state')
                for key, row in value['rows'].items():
                    if (not isinstance(key, str) or not isinstance(row, dict)
                            or any(not isinstance(row.get(k), str) for k in ('id', 'email', 'kind', 'message'))
                            or type(row.get('active')) is not bool or type(row.get('dismissed')) is not bool
                            or type(row.get('created_at')) not in (int, float)
                            or not math.isfinite(row['created_at'])):
                        raise ValueError('Invalid notice entry')
                self.rows = value['rows']
        except (OSError, ValueError, AttributeError):
            # Do not recreate deduplication history from corrupt state.
            self.error = True

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'version': 1, 'rows': self.rows}, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def update(self, gateway, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if self.error:
                return
            before = copy.deepcopy(self.rows)
            usage = usage_health(read_json(self.ops_directory / 'health.json'), now)
            # Missing initial observations do not invent an outage. Once an episode
            # exists, unknown/stale observations retain it until confirmed recovery.
            observed_usage = usage if usage.get('checked_at') or 'usage:collection' in self.rows else None
            wanted = candidates(gateway, now, observed_usage)
            accounts = {a.get('email'): a for a in gateway.get('accounts', [])}
            for key, row in self.rows.items():
                if key.startswith('free:'):
                    row['message'] = 'ChatGPT reports a Free plan'
                    if key not in wanted and gateway.get('online') is True and fresh(gateway.get('checked_at'), now):
                        row['active'] = False
                elif key.startswith('signin:') and key not in wanted:
                    row['message'] = 'Needs a new sign-in'
                    email = row['email']
                    account = accounts.get(email)
                    # Outages/unknown data must not reset the sign-in episode.
                    if authenticated(account, gateway, now):
                        row['active'] = False
                elif key.startswith('exhausted:'):
                    emails = EMAILS if key == 'exhausted:pool' else (row['email'],)
                    if any((w := weekly_allowance(accounts.get(email), gateway, now)) is not None
                           and w['remaining'] > 0 for email in emails) or (
                            gateway.get('online') is True and fresh(gateway.get('checked_at'), now)
                            and any(accounts.get(email) and not can_resume(accounts[email]) for email in emails)):
                        row['active'] = False
                    if key not in wanted:
                        row.pop('action', None)
                        # Retained episodes may predate the compact copy. Use their
                        # structured data, and keep countdowns current during outages.
                        row['message'] = exhaustion_message(row.get('accounts') or [], key == 'exhausted:pool',
                                                            row.get('urgency') != 'critical', now)
                elif key.startswith('usage:'):
                    if usage['status'] == 'healthy':
                        row['active'] = False
                elif not key.startswith('signin:'):
                    row['active'] = key in wanted
            for key, values in wanted.items():
                row = self.rows.get(key)
                if row is None or (key.startswith(('signin:', 'free:', 'usage:', 'exhausted:')) and not row.get('active')):
                    identity = hashlib.sha256((key + ':' + str(now)).encode()).hexdigest()[:24]
                    self.rows[key] = {'id': identity, 'created_at': now, 'dismissed': False, **values, 'active': True}
                else:
                    if key.startswith('exhausted:'):
                        row.pop('action', None)
                    row.update(values, active=True)
            pool_active = self.rows.get('exhausted:pool', {}).get('active', False)
            for key, row in self.rows.items():
                if key.startswith('exhausted:') and key != 'exhausted:pool':
                    # Supersession lasts only for the pool episode. Unknown quota
                    # retains the individual episode, its identity and dismissal.
                    row['hidden'] = pool_active
            self.rows = {k: r for k, r in self.rows.items() if r.get('active') or now - r.get('created_at', now) < 90 * 86400}
            if before != self.rows:
                try:
                    self.save()
                except OSError:
                    self.rows = before
                    self.error = True

    def view(self):
        # Honor saved dismissals from older dashboards for desktop delivery.
        with self.lock:
            return {'available': not self.error, 'items': [
                {k: r[k] for k in ('id', 'email', 'kind', 'message', 'created_at', 'urgency', 'accounts', 'action') if k in r}
                for r in self.rows.values() if r.get('active') and not r.get('dismissed') and not r.get('hidden')]}
