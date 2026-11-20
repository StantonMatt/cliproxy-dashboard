"""Account claim projection. Mutation helpers require lifecycle and controller locks."""
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlencode
import api_keys

PLAN_LABELS = {'free': 'Free', 'pro': 'Pro 200', 'plus': 'Plus', 'team': 'Team',
               'business': 'Business', 'enterprise': 'Enterprise', 'edu': 'Edu', 'go': 'Go',
               'prolite': 'Pro 100', 'promax': 'Pro 500'}


def plan_value(value):
    """Plan labels are presentation, not an allowlist of provider entitlements."""
    return value if isinstance(value, str) and value.strip() else None


def plan_label(value):
    value = plan_value(value)
    if value is None:
        return 'Unknown'
    return PLAN_LABELS.get(value, value.replace('_', ' ').replace('-', ' ').title())


def json_object(value):
    try:
        value = json.loads(value) if isinstance(value, (str, bytes)) else value
    except ValueError:
        raise ValueError('Invalid JSON response') from None
    if not isinstance(value, dict):
        raise ValueError('Invalid JSON response')
    return value


def claim(record):
    # Only the management list's explicit runtime claim, never top-level defaults.
    token = record.get('id_token')
    value = token.get('plan_type') if isinstance(token, dict) else None
    return plan_value(value)


def identity(record):
    if api_keys.is_api_key_row(record):
        return None
    fields = [record.get(k) for k in ('name', 'auth_index', 'email')]
    if not all(isinstance(v, str) and v for v in fields):
        return None
    token = record.get('id_token')
    account_id = (token.get('chatgpt_account_id') if isinstance(token, dict) else None)
    account_id = account_id or record.get('chatgpt_account_id')
    fields.append(account_id if isinstance(account_id, str) else None)
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def claude_routing_id(record):
    """Opaque binding to a listed Claude identity, without credential material."""
    fields = [record.get(k) for k in ('type', 'name', 'auth_index', 'email')]
    if (fields[0] != 'claude' or api_keys.is_api_key_row(record)
            or not all(isinstance(v, str) and v for v in fields)):
        return None
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def runtime_matches(record, expected):
    """Compare disabled, effective priority, and selected runtime token claims."""
    def priority(value):
        if value is None:
            return 0
        if isinstance(value, str) and re.fullmatch(r'[+-]?[0-9]+', value.strip()):
            return int(value.strip())
        if type(value) in (int, float) and math.isfinite(value):
            return int(value)
        raise ValueError('Invalid priority')

    try:
        for field, value in expected.items():
            if field == 'disabled':
                if type(record.get(field)) is not bool or record[field] != (value is True):
                    return False
            elif field == 'priority':
                if priority(record.get(field)) != priority(value):
                    return False
            elif field == 'id_token':
                actual = record.get(field)
                if not isinstance(actual, dict) or not isinstance(value, dict):
                    return False
                for key, wanted in value.items():
                    observed = actual.get(key)
                    if key in ('chatgpt_account_id', 'plan_type'):
                        if not isinstance(wanted, str) or not isinstance(observed, str):
                            return False
                        wanted, observed = wanted.strip(), observed.strip()
                    if observed != wanted:
                        return False
            else:
                return False
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def set_disabled_verified(record, disabled, call):
    """PATCH a listed identity and require runtime readback of that exact identity."""
    if record.get('type') != 'codex':
        raise ValueError('Codex account required')
    key = identity(record)
    if key is None:
        raise ValueError('Account identity unavailable')
    call('/auth-files/status', {'name': record['name'], 'auth_index': record['auth_index'],
                               'disabled': disabled}, method='PATCH')
    matches = [r for r in call('/auth-files').get('files', [])
               if r.get('type') == 'codex' and identity(r) == key]
    if len(matches) != 1 or not runtime_matches(matches[0], {'disabled': disabled}):
        raise ValueError('Account status readback did not match')
    return matches[0]


def enable_after_signin(record, call, can_enable=lambda: True):
    """Sign-in hook: caller holds both locks through the final enable check."""
    key = identity(record)
    matches = [r for r in call('/auth-files').get('files', [])
               if r.get('type') == 'codex' and key is not None and identity(r) == key]
    if len(matches) != 1:
        raise ValueError('Verified account claim required')
    current = matches[0]
    if claim(current) is None:
        raise ValueError('Verified account claim required')
    if not can_enable():
        raise ValueError('Sign-in no longer enables account')
    enabled = set_disabled_verified(current, False, call)
    if claim(enabled) is None:
        raise ValueError('Account claim changed during enable')
    return enabled


class AccountAccess:
    def __init__(self):
        self.refreshes = {}

    def reconcile(self, records, call, prune=True):
        if prune:
            now = time.monotonic()
            self.refreshes = {k: v for k, v in self.refreshes.items() if now - v < 600}
        return [record for record in records if record.get('type') == 'codex' and not api_keys.is_api_key_row(record)]

    def project(self, record, account):
        key = identity(record)
        account['access_identity'] = key
        account['plan_type'] = claim(record)
        account['plan'] = plan_label(account['plan_type'])
        account['access_state'] = 'disabled' if record.get('disabled') is True else 'enabled'
        account['access_reason'] = None
        account['access_error'] = None

    def maybe_refresh(self, record, usage_plan, call, now=None):
        if record.get('type') != 'codex':
            return
        key = identity(record)
        saved = claim(record)
        usage_plan = plan_value(usage_plan)
        if key is None or saved is None or usage_plan is None or saved == usage_plan:
            return
        now = time.monotonic() if now is None else now
        if key in self.refreshes and now - self.refreshes[key] < 600:
            return
        self.refreshes[key] = now  # Failed attempts also consume the bounded refresh.
        try:
            from routing_policy import controller_flock
            with controller_flock():
                call('/auth-files/refresh?' + urlencode({'name': record['name']}), {}, method='POST')
        except Exception:
            pass  # The next poll still derives access only from the list claim.

# Kept with the account-access path so partial dashboard installations ship it too.
CREDIT_SPENDING_STATE = Path('/var/lib/personal-codex-router/credit-spending-controls.json')


def credit_spending_decision(spend, marker, record, account, now):
    """Pure guard decision. A passed reset never proves renewed allowance."""
    import usage_adapters
    if marker and spend:
        return 'resume', None
    fresh = (not account.get('quota_error') and account.get('auth_state') == 'authenticated'
             and usage_adapters.observation_fresh(account, now, 'quota_checked_at'))
    if not fresh:
        return 'keep', None
    windows = account.get('windows') or []
    def window_name(window):
        return ('weekly' if window.get('seconds') == 604800 else
                'five_hour' if window.get('seconds') == 18000 else window.get('slot', 'allowance'))

    if marker and set(marker['windows']) <= {window_name(w) for w in windows} and all(
            type(w.get('remaining')) in (int, float) and math.isfinite(w['remaining'])
            and w['remaining'] > 0 and w.get('status') != 'rejected' for w in windows):
        return 'resume', None
    if spend or record.get('disabled') is not False:
        return 'keep', None
    spent = [w for w in windows if (w.get('status') == 'rejected' or
             type(w.get('remaining')) in (int, float) and math.isfinite(w['remaining']) and w['remaining'] <= 0)
             and type(w.get('resets_at')) in (int, float) and math.isfinite(w['resets_at'])
             and w['resets_at'] > now]
    if spent:
        names = list(dict.fromkeys(window_name(w) for w in spent))
        return 'pause', dict(since=(marker or {}).get('since', now), windows=names,
                             resets_at=max(w['resets_at'] for w in spent),
                             access_identity=identity(record))
    # Missing windows cannot clear even a pending pause whose PATCH has not landed.
    return 'keep', None


def credit_paused(account):
    return bool((account.get('credit_spending') or {}).get('paused'))


class CreditSpending:
    """Private settings keyed by email, like resets/fast mode. Caller owns lifecycle lock.

    Mark before disabling; clear only after verified enable. Ambiguous writes keep
    the marker, including across restarts. Never record automatic pauses as user
    routing changes in SigninManager.
    """
    def __init__(self, path=None):
        import threading
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        self.state = {'accounts': {}}
        self.last_warning = None
        if path is not None:
            try:
                value = json.loads(self.path.read_text())
                assert isinstance(value, dict) and isinstance(value['accounts'], dict)
                for key, row in value['accounts'].items():
                    assert isinstance(key, str) and isinstance(row, dict) and type(row['spend_credits']) is bool
                    marker = row.get('paused')
                    if marker is not None:
                        assert isinstance(marker, dict) and type(marker['since']) in (int, float)
                        assert math.isfinite(marker['since']) and type(marker['resets_at']) in (int, float)
                        assert math.isfinite(marker['resets_at']) and isinstance(marker['windows'], list)
                        assert marker['windows'] and all(isinstance(w, str) for w in marker['windows'])
                self.state = value
            except FileNotFoundError:
                pass
            except Exception:
                self.warn()

    def warn(self):
        import logging
        now = time.monotonic()
        if self.last_warning is None or now - self.last_warning >= 300:
            logging.getLogger(__name__).warning('Credit spending controls unavailable; automatic change deferred')
            self.last_warning = now

    def save(self, state):
        from fast_mode import write_json
        if self.path is not None:
            write_json(self.path, state, 0o600)
        self.state = state

    def update(self, email, **fields):
        import copy
        with self.lock:
            state = copy.deepcopy(self.state)
            state['accounts'].setdefault(email, {'spend_credits': True}).update(fields)
            self.save(state)

    def view(self, record):
        import copy
        with self.lock:
            row = self.state['accounts'].get(record.get('email'), {})
            marker = row.get('paused')
            if marker and marker.get('access_identity') != identity(record):
                marker = None
            return {'enabled': row.get('spend_credits', True),
                    'paused': {k: copy.deepcopy(marker[k]) for k in ('windows', 'resets_at')}
                    if marker else None}

    def marked(self, record):
        return self.view(record)['paused'] is not None

    def project(self, record, account):
        view = self.view(record)
        # A marker can precede a failed PATCH. Publish Paused only with a disabled row.
        if record.get('disabled') is not True:
            view['paused'] = None
        account['credit_spending'] = view
        account['routing_enabled'] = self.marked(record) or record.get('disabled') is not True
        if view['paused']:
            account['access_state'] = 'paused'
            account['active'] = False
            account['availability'] = dict(state='paused', retry_at=view['paused']['resets_at'], scope='account')

    def prune(self, records):
        import copy
        emails = {r.get('email') for r in records}
        with self.lock:
            state = copy.deepcopy(self.state)
            for email, row in state['accounts'].items():
                if email not in emails:
                    row.pop('paused', None)  # Keep the setting for a later sign-in.
            if state != self.state:
                self.save(state)

    def reconcile(self, record, account, call, deadline):
        import routing_policy
        import signin
        with self.lock:
            marker = self.state['accounts'].get(record.get('email'), {}).get('paused')
            if marker and marker.get('access_identity') != identity(record):
                # Preferences survive replacement; automatic pause ownership does not.
                self.update(record['email'], paused=None)
                self.project(record, account)
                return record
            # No work and no extra management read for the default setting.
            view = self.view(record)
            if view['enabled'] and not view['paused']:
                self.project(record, account)
                return record
            with routing_policy.controller_flock(timeout=deadline.timeout(10)):
                key = identity(record)
                rows = [r for r in call('/auth-files').get('files', [])
                        if r.get('type') == 'codex' and key is not None and identity(r) == key]
                if len(rows) != 1:
                    raise signin.AccessError('account_changed')
                current = rows[0]
                marker = self.state['accounts'].get(current.get('email'), {}).get('paused')
                action, paused = credit_spending_decision(view['enabled'], marker, current, account, time.time())
                if action == 'pause':
                    self.update(current['email'], paused=paused)
                    current = set_disabled_verified(current, True, call)
                elif action == 'resume':
                    current = set_disabled_verified(current, False, call)
                    self.update(current['email'], paused=None)
                account['access_state'] = 'disabled' if current.get('disabled') is True else 'enabled'
                account.update(disabled=current.get('disabled') is True,
                               active=not current.get('disabled') and not current.get('unavailable'))
                from availability import classify
                account['credit_spending'] = self.view(current)
                account['availability'] = classify(current, account)
                self.project(current, account)
                return current
