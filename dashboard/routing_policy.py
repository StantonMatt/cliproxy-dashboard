#!/usr/bin/env python3
"""Apply the saved account routing policy; never move healthy live sessions.

Uses the gateway's supported priority PATCH API. Does not read OAuth tokens,
change affinity, restart the gateway, or expose a management endpoint.
"""
import argparse
import copy
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

from account_access import runtime_matches
from request_deadline import RequestDeadline
import usage_adapters
import api_keys

STATE = Path('/var/lib/personal-codex-router-routing')
KEY = Path('/etc/cliproxyapi/dashboard-management-key')
MODES = ('round-robin', 'earliest-weekly-reset', 'highest-remaining-quota', 'preferred-account')
PROVIDERS = {
    'codex': {'modes': MODES, 'default': 'earliest-weekly-reset'},
    'claude': {'modes': MODES, 'default': 'earliest-weekly-reset'},
}


def inventory_snapshot(call=None):
    """Read inventory once; an absent/invalid response is never an empty roster."""
    value = (call or request)('/auth-files')
    if not isinstance(value, dict) or not isinstance(value.get('files'), list):
        raise Hold('Gateway inventory is unavailable')
    return copy.deepcopy([r for r in value['files'] if not api_keys.is_api_key_row(r)])


def inventory_for(overview):
    gateway = overview.get('gateway', {})
    if 'inventory' in gateway:
        return [r for r in gateway['inventory'] if not api_keys.is_api_key_row(r)]
    # Public projections also carry filename keys; useful for read-only callers.
    return [dict(a, type=provider, name=a.get('key', a.get('name')))
            for provider, field in (('codex', 'accounts'), ('claude', 'claude_accounts'))
            for a in gateway.get(field, [])]


def identities(provider, inventory):
    return tuple(a.get('email') for a in sorted(inventory, key=lambda a: a.get('name') or '')
                 if a.get('type', a.get('provider')) == provider and a.get('email') and not api_keys.is_api_key_row(a))


def membership(inventory, provider):
    return [{k: a.get(k) for k in ('name', 'email', 'auth_index')}
            for a in sorted(inventory, key=lambda a: a.get('name') or '')
            if a.get('type') == provider and not api_keys.is_api_key_row(a)]


def state_path(provider='codex'):
    return STATE if provider == 'codex' else STATE / provider



class Hold(Exception):
    """A safe, non-secret reason not to change routing."""


class DataHold(Hold):
    """Quota data cannot safely select an account."""


class ResetHold(DataHold):
    """The only missing data is the first reading after a reset that just passed."""


@contextmanager
def controller_flock(timeout=10, state_dir=None):
    state_dir = STATE if state_dir is None else state_dir
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / 'controller.lock').open('a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Hold('Routing update already in progress; try again') from None
                time.sleep(0.05)
        yield


def stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def fresh(value, now, max_age=180):
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed.tzinfo is not None and -30 <= now - parsed.timestamp() <= max_age
    except (TypeError, ValueError):
        return False


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def account_label(account):
    return account['email']


def observations(gateway):
    """Build the controller input from a local or HTTP dashboard snapshot.

    Keep eligibility, allowance, and their original observation timestamps
    together. Never make cached quota appear fresh when constructing input.
    """
    result = {k: copy.deepcopy(gateway[k]) for k in ('online', 'checked_at', 'inventory') if k in gateway}
    fields = ('key', 'provider', 'id', 'label', 'email', 'active', 'disabled', 'access_state', 'auth_state', 'priority',
              'quota_checked_at', 'fresh_until', 'quota_error', 'windows', 'availability')
    result['accounts'] = [{k: copy.deepcopy(account[k]) for k in fields if k in account}
                          for account in gateway.get('accounts', [])]
    return {'gateway': result}


def ineligible_reason(account):
    availability = account.get('availability') or {}
    state = availability.get('state')
    if state == 'no_data':
        return 'no_data'
    if account.get('access_state') == 'disable_unconfirmed' or state == 'disable_unconfirmed':
        return 'disable_unconfirmed'
    if account.get('disabled') or state == 'disabled':
        return 'disabled'
    if account.get('auth_state') == 'sign_in_required' or state == 'auth_required':
        return 'auth_required'
    if availability.get('scope', 'account') != 'model' and state in (
            'quota_exhausted', 'rate_limited', 'provider_unavailable'):
        return state
    if not account.get('active') and state != 'ready' and availability.get('scope') != 'model':
        return 'inactive'
    return None


def claude_runtime_available(runtime):
    """Use the same availability check for raw records and public projections."""
    return (runtime.get('runtime_available') is not False
            and runtime.get('unavailable') is not True and runtime.get('status') != 'error')


def claude_observations(overview, now):
    gateway = overview.get('gateway', {})
    watched = overview.get('watch_accounts', [])
    accounts = []
    runtime_rows = gateway.get('claude_accounts', [])
    for runtime in sorted(runtime_rows, key=lambda a: a.get('name', a.get('key', ''))):
        email = runtime.get('email')
        observed = usage_adapters.claude_observation(runtime, runtime_rows, watched, now)
        accounts.append(dict(observed, email=email, _runtime=runtime))
    result = []
    resetting = None
    for account in accounts:
        label = account['email']
        runtime = account['_runtime']
        disabled = runtime.get('disabled') is True or runtime.get('status') == 'disabled'
        unavailable = not claude_runtime_available(runtime)
        if disabled:
            # Match Codex: disabled credentials need no allowance observation.
            result.append(dict(email=account['email'], active=False, disabled=True))
            continue
        if unavailable:
            result.append(dict(email=account['email'], active=False,
                               availability={'state': 'provider_unavailable'}))
            continue
        if not account.get('id'):
            result.append(dict(email=account['email'], active=False, availability={'state': 'no_data'}))
            continue
        if account.get('just_reset'):
            # Any other account's fault is still reported first.
            resetting = resetting or label
            continue
        if account.get('status') != 'ok' or account.get('error'):
            raise DataHold(f'quota unavailable for {label}')
        # Preserve event/header measurement times; established windows expire at reset.
        checked = account.get('checked_at')
        if not usage_adapters.observation_fresh(account, now):
            raise DataHold(f'stale quota data for {label}')
        windows = copy.deepcopy(account.get('windows', []))
        for kind, seconds in (('five_hour', 18000), ('seven_day', 604800)):
            primary = [w for w in windows if w.get('kind') == kind]
            if kind == 'five_hour' and not primary:
                continue
            if len(primary) != 1:
                raise DataHold(f'account allowance is not reported for {label}')
            primary[0]['seconds'] = seconds
        for window in windows:
            if window.get('invalid_reset'):
                raise DataHold(f'account allowance is not reported for {label}')
            reset = window.get('resets_at')
            remaining = window.get('remaining')
            if remaining is None and finite(window.get('used')) and window['used'] == 0:
                remaining = 100
            measured = window.get('measured_at', checked)
            if not usage_adapters.window_fresh(window, checked, now, account.get('fresh_until'),
                    primary=window.get('kind') in ('five_hour', 'seven_day')):
                raise DataHold(f'stale quota window for {label}')
            if reset is None and finite(remaining) and remaining == 100:
                # Fresh explicit zero is unused; no reset window is established yet.
                window['remaining'] = 100
                if window.get('kind') not in ('five_hour', 'seven_day'):
                    window.pop('seconds', None)
                continue
            if (not finite(reset) or not now < reset <= now + 8 * 86400
                    or not finite(window.get('remaining')) or not 0 <= window['remaining'] <= 100):
                raise DataHold(f'account allowance is not reported for {label}')
            # Only the all-model weekly window determines the ranking.
            if window.get('kind') not in ('five_hour', 'seven_day'):
                window.pop('seconds', None)
        result.append(dict(email=account['email'], active=not disabled,
                           disabled=disabled, windows=windows, quota_checked_at=checked,
                           usage_source=account.get('usage_source'), fresh_until=account.get('fresh_until')))
    if resetting:
        # An offline or stale gateway snapshot is reported as before.
        quiet = gateway.get('online') and fresh(gateway.get('checked_at'), now)
        raise (ResetHold(f'waiting for the new reading after a reset for {resetting}') if quiet
               else DataHold(f'quota unavailable for {resetting}'))
    # Each allowance was checked independently against its measured reset.
    return {'gateway': {**gateway, 'accounts': result}}


def eligible_accounts(overview, now, provider='codex', inventory=None, model_spent_ok=False):
    """Accounts with allowance. Routing passes model_spent_ok: a Claude account with only a model's own
    allowance spent still serves its other models, since the gateway blocks just that model there
    (model-level cooling) and sends it to the next account."""
    inventory = inventory_for(overview) if inventory is None else inventory
    if provider == 'claude':
        overview = claude_observations(overview, now)
    emails = identities(provider, inventory)
    gateway = overview.get('gateway', {})
    if not gateway.get('online'):
        raise Hold('Gateway observations are unavailable')
    if not fresh(gateway.get('checked_at'), now):
        raise DataHold('Gateway observations are stale')
    accounts = [a for a in gateway.get('accounts', []) if a.get('email') in emails]
    if len(accounts) != len(emails) or {a['email'] for a in accounts} != set(emails):
        raise Hold('Expected accounts are incomplete or ambiguous')
    candidates, excluded = {}, {}
    for account in accounts:
        email, label = account['email'], account_label(account)
        row = next((r for r in inventory if r.get('type') == provider and r.get('email') == email), {})
        if account.get('key') and account['key'] != row.get('name'):
            raise DataHold('Account observations no longer match gateway membership')
        if provider == 'codex' and account.get('id') != row.get('auth_index'):
            excluded[email] = 'no_data'
            continue
        reason = ineligible_reason(account)
        # Disabled/revoked credentials cannot serve regardless of quota. Other
        # active accounts still need valid observations before changing priority.
        if reason in ('disabled', 'disable_unconfirmed', 'auth_required') or (not account.get('active') and reason):
            excluded[email] = reason
            continue
        if account.get('quota_error'):
            error_class = 'auth_required' if account.get('auth_state') == 'sign_in_required' else 'quota_unavailable'
            raise DataHold(f'quota error for {label}: {error_class}')
        if not usage_adapters.observation_fresh(account, now, 'quota_checked_at'):
            raise DataHold(f'stale quota data for {label}')
        windows = account.get('windows', [])
        if any(finite(w.get('resets_at')) and w['resets_at'] <= now for w in windows):
            raise DataHold(f'waiting for the next usage window reset observation for {label}')
        weekly = [w for w in windows if w.get('seconds') == 604800]
        # Plans without a weekly window (Free reports one 30-day window) are
        # secondary: ranked only when no weekly account has allowance.
        longer = [w for w in windows if finite(w.get('seconds')) and w['seconds'] > 604800]
        secondary = not weekly and bool(longer)
        if secondary:
            ranked = max(longer, key=lambda w: w['seconds'])
            if sum(w['seconds'] == ranked['seconds'] for w in longer) != 1:
                raise DataHold(f'usage window reset time is not reported for {label}')
            remaining = ranked.get('remaining')
            if ranked.get('resets_at') is None and (remaining == 100 or (
                    remaining is None and finite(ranked.get('used')) and ranked['used'] == 0)):
                # An unused allowance cannot expire until one full period after first use.
                unused = {**ranked, 'remaining': 100, 'resets_at': now + ranked['seconds']}
                windows = [unused if w is ranked else w for w in windows]
                ranked = unused
            if not finite(ranked.get('resets_at')):
                raise DataHold(f'usage window reset time is not reported for {label}')
            reset = ranked.get('resets_at')
            if not now < reset <= now + ranked['seconds'] + 86400:
                raise DataHold(f'waiting for the next usage window reset observation for {label}')
        else:
            if len(weekly) != 1:
                raise DataHold(f'weekly reset time is not reported for {label}')
            ranked = weekly[0]
            reset = ranked.get('resets_at')
            unused = provider == 'claude' and reset is None and ranked.get('remaining') == 100
            if not unused and (not finite(reset) or not now < reset <= now + 8 * 86400):
                raise DataHold(f'waiting for the next weekly reset observation for {label}')
        if any(not finite(w.get('remaining')) or not 0 <= w['remaining'] <= 100 for w in windows):
            raise DataHold(f'account allowance is not reported for {label}')
        spent = [w for w in windows if w['remaining'] <= 0]
        if model_spent_ok and provider == 'claude':
            spent = [w for w in spent if w.get('kind') != 'weekly_model']
        if reason or spent:
            excluded[email] = reason or 'no remaining allowance'
            continue
        candidates[email] = {'remaining': ranked['remaining'], 'reset': reset, 'secondary': secondary}
        if provider == 'claude':
            candidates[email]['near_limit'] = bool(usage_adapters.claude_near_limit_windows(account, now))
    return candidates, excluded


def priority_plan(email, reset=None, reason=None, provider='codex', inventory=None):
    emails = identities(provider, inventory)
    return {'preferred_email': email, 'weekly_resets_at': reset, 'reason': reason,
            'priorities': {e: 100 if e == email else 0 for e in emails}}


def choose(overview, now, mode='earliest-weekly-reset', current=None, provider='codex', inventory=None):
    inventory = inventory_for(overview) if inventory is None else inventory
    if mode not in ('earliest-weekly-reset', 'highest-remaining-quota'):
        raise ValueError('Choose an automatic routing mode')
    if len(identities(provider, inventory)) <= 1:
        return priority_plan(None, reason='Fewer than two accounts are configured', provider=provider, inventory=inventory)
    candidates, _ = eligible_accounts(overview, now, provider, inventory, model_spent_ok=True)
    if not candidates:
        raise Hold('No account currently reports available allowance')
    # Never compare a weekly percentage with a longer window's percentage.
    candidates = {e: c for e, c in candidates.items() if not c['secondary']} or candidates
    warned = {e for e, c in candidates.items() if c.get('near_limit')}
    unwarned = {e: c for e, c in candidates.items() if e not in warned}
    reason = None
    if warned and unwarned:
        label = current if current in warned else next(e for e in candidates if e in warned)
        reason = f'Moving new sessions off {label}: near its limit'
        # Apply ranking and stability only within the unwarned group. If all
        # accounts are warned, preserve the existing ranking and margins.
        candidates = unwarned
    quota = mode == 'highest-remaining-quota'
    # An unused weekly allowance has no measured reset. Sort it after known
    # resets without manufacturing a date in the observation or published plan.
    rank = lambda email: (-candidates[email]['remaining'] if quota else
                          candidates[email]['reset'] if candidates[email]['reset'] is not None else float('inf'))
    winner = min(candidates, key=rank)
    if current in candidates and winner != current:
        lead = rank(current) - rank(winner)
        if rank(current) == rank(winner) or (quota and lead < 5) or (not quota and lead <= 900):
            winner = current
            reason = 'Keeping the current account within the stability margin'
    elif current not in candidates:
        leaders = {e for e in candidates if (
            rank(e) == rank(winner) or (not quota and rank(e) - rank(winner) <= 900))}
        if len(leaders) > 1:
            plan = priority_plan(None, None if quota else candidates[winner]['reset'], reason, provider=provider, inventory=inventory)
            # Preserve neutral priorities when everyone ties; otherwise demote non-leaders.
            if len(leaders) < len(plan['priorities']):
                plan['priorities'].update({e: 100 for e in leaders})
            return plan
    return priority_plan(winner, candidates[winner]['reset'], reason, provider, inventory)


def request(path, body=None, method=None, base='http://127.0.0.1:8317', timeout=15):
    req = Request(base + '/v0/management' + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  method=method,
                  headers={'Authorization': 'Bearer ' + KEY.read_text().strip(),
                           'Content-Type': 'application/json'})
    with urlopen(req, timeout=timeout) as response:
        result = json.load(response)
        if path == '/auth-files' and isinstance(result.get('files'), list):
            result['files'] = [r for r in result['files'] if not api_keys.is_api_key_row(r)]
        return result


def records(call=None, provider='codex', inventory=None):
    inventory = inventory_snapshot(call) if inventory is None else inventory
    rows = sorted((a for a in inventory if a.get('type') == provider and not api_keys.is_api_key_row(a)), key=lambda a: a.get('name') or '')
    emails = [a.get('email') for a in rows]
    if any(not isinstance(e, str) or not e for e in emails) or len({e.casefold() for e in emails if isinstance(e, str)}) != len(emails):
        raise Hold('Gateway accounts are incomplete or ambiguous')
    if any(not isinstance(a.get('name'), str) or not a['name']
           or sum(f.get('name') == a['name'] for f in inventory) != 1 for a in rows):
        raise Hold('Gateway account filenames are incomplete or ambiguous')
    return {a['email']: a for a in rows}


def write_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, indent=2)
        output.write('\n')
    os.replace(temp, path)


def routing_linked():
    path = STATE / 'linked.json'
    try:
        value = json.loads(path.read_text())
        return isinstance(value, dict) and value.get('linked') is True
    except FileNotFoundError:
        return True
    except (OSError, ValueError, UnicodeError):
        return False  # A damaged flag must not silently link independent controls.


def set_routing_linked(linked, deadline=None):
    if type(linked) is not bool:
        raise ValueError('Linked must be true or false')
    deadline = deadline or RequestDeadline()
    with controller_flock(timeout=deadline.timeout(10)):
        deadline.timeout(10)
        write_json(STATE / 'linked.json', {'linked': linked})
    return {'linked': linked}


def apply_plan(plan, call, state_dir, provider='codex', inventory=None):
    inventory = inventory_snapshot(call) if inventory is None else inventory
    emails = identities(provider, inventory)
    rows = records(provider=provider, inventory=inventory)
    if provider == 'claude' and any(plan['priorities'][email] > 0 and
                                  (row.get('disabled') is True or row.get('status') == 'disabled')
                                  for email, row in rows.items()):
        raise Hold('Claude account was disabled before priority promotion')
    for row in rows.values():
        if row.get('priority') not in (None, 0, 100):
            raise Hold('Unexpected manual priority; policy did not overwrite it')
    baseline_path = state_dir / 'baseline.json'
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
        if any(e in baseline and baseline[e]['name'] != rows[e]['name'] for e in emails):
            raise Hold('Account files changed; check policy ownership before continuing')
        # An identity configured after the baseline was captured starts from its current priority.
        added = {e: {'name': rows[e]['name'], 'priority': rows[e].get('priority')}
                 for e in emails if e not in baseline}
        if added:
            write_json(baseline_path, {**baseline, **added})
    else:
        write_json(baseline_path, {e: {'name': rows[e]['name'], 'priority': rows[e].get('priority')}
                                  for e in emails})
    changed = []

    def verified_rows():
        actual = records(call, provider)
        if set(actual) != set(rows):
            raise Hold('Gateway membership changed; priorities left on hold')
        if any(any(actual[email].get(k) != rows[email].get(k)
                   for k in ('type', 'email', 'name', 'auth_index')) for email in emails):
            raise Hold('Gateway account identity changed; priorities left on hold')
        return actual

    # Promote first, then demote: a partial failure never leaves both demoted.
    for email in sorted(emails, key=lambda e: -plan['priorities'][e]):
        wanted = plan['priorities'][email]
        if (rows[email].get('priority') or 0) != wanted:
            current = verified_rows()[email]
            if provider == 'claude' and wanted > 0 and (
                    current.get('disabled') is True or current.get('status') == 'disabled'):
                raise Hold('Claude account was disabled before priority promotion')
            call('/auth-files/fields', {'name': rows[email]['name'], 'priority': wanted}, 'PATCH')
            changed.append(email)
            if not runtime_matches(verified_rows()[email], {'priority': wanted}):
                raise Hold('Gateway has not confirmed the requested priority')
    actual = verified_rows()
    if any(not runtime_matches(actual[email], {'priority': plan['priorities'][email]}) for email in emails):
        raise Hold('Gateway has not confirmed the requested priorities')
    return changed


def rollback(call, state_dir):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / 'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Hold('Routing update already in progress; try again') from None
        baseline_path = state_dir / 'baseline.json'
        recorded = baseline_path.exists()
        baseline = json.loads(baseline_path.read_text()) if recorded else None
        inventory = inventory_snapshot(call)
        emails = identities('codex', inventory)
        rows = records(inventory=inventory)
        if any(rows[e].get('priority') not in (None, 0, 100) or
               (recorded and e in baseline and rows[e]['name'] != baseline[e]['name']) for e in emails):
            raise Hold('Account files or priorities changed; rollback left them untouched')
        if recorded:
            baseline = {**{e: {'name': rows[e]['name'], 'priority': rows[e].get('priority')}
                           for e in emails if e not in baseline}, **baseline}
        if not recorded:
            if call('/config').get('routing', {}).get('strategy') != 'round-robin':
                raise Hold('Gateway strategy changed; inspect before applying priorities')
            baseline = {e: {'name': rows[e]['name'], 'priority': 0} for e in emails}
            write_json(state_dir / 'selection.json', validate_selection({'mode': 'round-robin'}, inventory=inventory))
        write_json(state_dir / 'suspension.json', {'suspended': True})
        for email in emails:
            call('/auth-files/fields', baseline[email], 'PATCH')
        actual = records(call)
        if set(actual) != set(rows):
            raise Hold('Gateway membership changed during rollback')
        if any(not runtime_matches(actual[email], {'priority': baseline[email]['priority']}) for email in emails):
            raise Hold('Gateway has not confirmed restored priorities')
        write_json(state_dir / 'status.json', {
            'policy': 'baseline' if recorded else 'round-robin',
            'status': 'disabled', 'checked_at': stamp(time.time()),
            'selected_email': None, 'preferred_email': None, 'weekly_resets_at': None,
            'suspended': True, 'intended_priorities': {e: baseline[e]['priority'] for e in emails},
            'hold_reason': None, 'held_since': None, 'pending_plan': False,
        })
        return {'status': 'restored', 'policy': 'baseline' if recorded else 'round-robin'}


def validate_selection(value, provider='codex', inventory=None):
    if not isinstance(value, dict) or value.get('mode') not in PROVIDERS[provider]['modes']:
        raise ValueError('Choose a supported routing mode')
    mode, email = value['mode'], value.get('preferred_email')
    if mode == 'preferred-account' and 'preferred_key' in value:
        matches = [a for a in (inventory or []) if a.get('type') == provider
                   and a.get('name') == value['preferred_key'] and not api_keys.is_api_key_row(a)]
        if not isinstance(value['preferred_key'], str) or len(matches) != 1:
            raise ValueError('Choose a connected account')
        email = matches[0].get('email')
    if mode == 'preferred-account' and email not in identities(provider, inventory):
        raise ValueError('Choose a connected account')
    return {'mode': mode, 'preferred_email': email if mode == 'preferred-account' else None}


def selection(state_dir=None, provider='codex', inventory=None):
    state_dir = state_path(provider) if state_dir is None else state_dir
    path = state_dir / 'selection.json'
    default = {'mode': PROVIDERS[provider]['default'], 'preferred_email': None}
    if not path.exists():
        return default
    value = json.loads(path.read_text())
    if (inventory is None and isinstance(value, dict) and value.get('mode') == 'preferred-account'
            and isinstance(value.get('preferred_email'), str) and value['preferred_email']):
        return {'mode': value['mode'], 'preferred_email': value['preferred_email']}
    if (isinstance(value, dict) and value.get('mode') == 'preferred-account'
            and value.get('preferred_email') not in identities(provider, inventory)):
        return default
    return validate_selection(value, provider, inventory)


def selected_plan(selected, overview, now, current=None, provider='codex', inventory=None):
    inventory = inventory_for(overview) if inventory is None else inventory
    selected = validate_selection(selected, provider, inventory)
    if len(identities(provider, inventory)) <= 1:
        return priority_plan(None, reason='Fewer than two accounts are configured', provider=provider, inventory=inventory)
    if selected['mode'] in ('earliest-weekly-reset', 'highest-remaining-quota'):
        return choose(overview, now, selected['mode'], current, provider, inventory)
    if selected['mode'] == 'round-robin':
        return priority_plan(None, provider=provider, inventory=inventory)
    candidates, excluded = eligible_accounts(overview, now, provider, inventory, model_spent_ok=True)
    email = selected['preferred_email']
    if email in candidates:
        return priority_plan(email, provider=provider, inventory=inventory)
    other = next((e for e in identities(provider, inventory) if e in candidates), None)
    reason = f"Preferred account {email}: {excluded.get(email, 'unavailable')}"
    return priority_plan(other, reason=reason, provider=provider, inventory=inventory)


def read_status(state_dir):
    path = state_dir / 'status.json'
    return json.loads(path.read_text()) if path.exists() else {}


def suspended(state_dir):
    path = state_dir / 'suspension.json'
    return path.exists() and json.loads(path.read_text()).get('suspended') is True


def public_status(status):
    return {k: status.get(k) for k in (
        'policy', 'selected_email', 'preferred_email', 'weekly_resets_at', 'status',
        'checked_at', 'intended_priorities', 'hold_reason', 'held_since', 'reason', 'suspended')}


def reconcile(overview, selected=None, call=request, state_dir=None, provider='codex', inventory=None):
    state_dir = state_path(provider) if state_dir is None else state_dir
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / 'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Hold('Routing update already in progress; try again') from None
        now = time.time()
        previous = read_status(state_dir)
        if selected is None and suspended(state_dir):
            previous.update(status='disabled', checked_at=stamp(now), suspended=True,
                            hold_reason=None, held_since=None, pending_plan=False)
            write_json(state_dir / 'status.json', previous)
            return public_status(previous)
        inventory = inventory_snapshot(call) if inventory is None else inventory
        emails = identities(provider, inventory)
        wanted = validate_selection(selected, provider, inventory) if selected is not None else selection(state_dir, provider, inventory)
        if not any(a.get('type') == provider and not api_keys.is_api_key_row(a) for a in inventory):
            status = dict(policy=wanted['mode'], status='idle', checked_at=stamp(now),
                          selected_email=None, preferred_email=None, intended_priorities={},
                          pending_plan=False, membership=membership(inventory, provider))
            write_json(state_dir / 'status.json', status)
            return public_status(status)
        config = call('/config')
        if config.get('routing', {}).get('strategy') != 'round-robin':
            raise Hold('Gateway strategy changed; inspect before applying priorities')
        rows = records(provider=provider, inventory=inventory)
        if provider == 'claude' and any(row.get('priority') not in (None, 0, 100) for row in rows.values()):
            raise Hold('Unexpected manual priority; policy did not overwrite it')
        if selected is not None:
            write_json(state_dir / 'selection.json', wanted)
            write_json(state_dir / 'suspension.json', {'suspended': False})
        status = dict(previous, policy=wanted['mode'], selected_email=wanted['preferred_email'],
                      checked_at=stamp(now), suspended=False)
        if previous.get('membership') != membership(inventory, provider):
            previous['pending_plan'] = False
            status['pending_plan'] = False
        status['membership'] = membership(inventory, provider)
        if selected is not None:
            status['pending_plan'] = False
            if (wanted['mode'] != previous.get('policy')
                    or wanted['preferred_email'] != previous.get('selected_email')):
                status.update(hold_reason=None, held_since=None)

        def apply(plan, target):
            status.update(plan, intended_priorities=plan['priorities'], pending_plan=True,
                          plan_status=target, status='applying')
            write_json(state_dir / 'status.json', status)
            try:
                changed = apply_plan(plan, call, state_dir, provider, inventory)
            except Exception:
                status.update(status='held', hold_reason='Routing update incomplete; automatic retry pending',
                              held_since=status.get('held_since') or stamp(now))
                write_json(state_dir / 'status.json', status)
                raise
            status.update(status=target, changed_accounts=changed, pending_plan=False)
            write_json(state_dir / 'status.json', status)

        # Codex completes persisted plans without fresh quota. Claude recomputes
        # from fresh usage before retrying, so stale snapshots never trigger writes.
        if (provider == 'codex' and previous.get('pending_plan') and selected is None
                and set(previous.get('intended_priorities', {})) == set(emails)):
            plan = {k: previous.get(k) for k in ('preferred_email', 'weekly_resets_at', 'reason')}
            plan['priorities'] = previous['intended_priorities']
            apply(plan, previous.get('plan_status', 'active'))
            rows = {e: dict(row, priority=plan['priorities'][e]) for e, row in rows.items()}
        if provider == 'claude':
            gateway = overview.get('gateway', {})
            # Fresh inventory owns runtime identity and state; cached usage must
            # be rebound before gateway/watch precedence selects an observation.
            claude_accounts = usage_adapters.bind_claude_usage(
                [dict(row) for row in rows.values()], gateway.get('usage_accounts', []))
            overview = {**overview, 'gateway': {**gateway, 'claude_accounts': claude_accounts}}
        winners = [e for e in emails if rows[e].get('priority') == 100]
        current = winners[0] if len(winners) == 1 else None
        try:
            plan = selected_plan(wanted, overview, now, current, provider, inventory)
        except Hold as error:
            if (isinstance(error, ResetHold) and selected is None and previous.get('status') == 'active'
                    and previous.get('policy') == wanted['mode']
                    and previous.get('selected_email') == wanted['preferred_email']):
                # The applied plan stands through the few minutes a reading takes to follow a reset.
                write_json(state_dir / 'status.json', status)
                return public_status(status)
            held_since = status.get('held_since') or stamp(now)
            status.update(status='held', hold_reason=str(error), held_since=held_since,
                          reason=str(error))
            status.setdefault('intended_priorities', {e: rows[e].get('priority') or 0 for e in emails})
            if provider == 'codex' and isinstance(error, DataHold) and now - dt.datetime.fromisoformat(held_since).timestamp() >= 600:
                plan = priority_plan(None, reason='Quota data held for 10 minutes; using neutral priorities', inventory=inventory)
                apply(plan, 'fallback')
            write_json(state_dir / 'status.json', status)
            return public_status(status)
        status.update(hold_reason=None, held_since=None)
        apply(plan, 'active')
        return public_status(status)


def reconcile_providers():
    """One timer reconciles independently; an unsaved Claude default is passive."""
    overview = {}
    try:
        req = Request('http://127.0.0.1:8320/api/overview', headers={'X-Router-Request': '1'})
        with urlopen(req, timeout=15) as response:
            overview = json.load(response)
    except Exception:
        pass  # Each policy reports a hold and retains its saved selection.
    inventory = inventory_snapshot()
    results, failed = {}, False
    for provider in PROVIDERS:
        if provider == 'claude' and not (state_path(provider) / 'selection.json').exists():
            results[provider] = {'status': 'unchanged', 'reason': 'No Claude selection saved'}
            continue
        try:
            observed = observations(overview.get('gateway', {})) if provider == 'codex' else overview
            deadline = RequestDeadline()
            results[provider] = reconcile(observed, call=deadline.wrap(request, maximum=15), provider=provider, inventory=inventory)
        except Exception:
            results[provider] = {'status': 'held', 'reason': 'Routing check could not be completed'}
            failed = True
    return results, int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true')
    parser.add_argument('--all-providers', action='store_true', help='Reconcile saved Codex and Claude choices')
    group.add_argument('--rollback', action='store_true', help='Restore original priorities; stop the preference timer/service first')
    args = parser.parse_args()
    try:
        if args.all_providers:
            if not args.apply:
                raise ValueError('--all-providers requires --apply')
            result, code = reconcile_providers()
            print(json.dumps(result))
            return code
        if args.rollback:
            result = rollback(request, STATE)
        else:
            if args.apply and suspended(STATE):
                print(json.dumps(reconcile({})))
                return 0
            inventory = inventory_snapshot()
            emails = identities('codex', inventory)
            wanted = selection(inventory=inventory)
            overview = {}
            if wanted['mode'] != 'round-robin' and len(emails) > 1 and not suspended(STATE):
                req = Request('http://127.0.0.1:8320/api/overview', headers={'X-Router-Request': '1'})
                try:
                    with urlopen(req, timeout=15) as response:
                        overview = observations(json.load(response).get('gateway', {}))
                except Exception:
                    if not args.apply:
                        raise
            result = reconcile(overview, inventory=inventory) if args.apply else selected_plan(wanted, overview, time.time(), inventory=inventory)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'status': 'held', 'reason': 'Routing check could not be completed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
