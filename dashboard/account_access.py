"""Account claim projection. Mutation helpers require lifecycle and controller locks."""
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode
import api_keys

PLAN_LABELS = {'free': 'Free', 'pro': 'Pro 200', 'plus': 'Plus', 'team': 'Team',
               'business': 'Business', 'enterprise': 'Enterprise', 'edu': 'Edu', 'go': 'Go',
               'prolite': 'Pro 100'}


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
