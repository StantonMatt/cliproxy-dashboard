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

EMAILS = ('account-a@example.com', 'account-b@example.com')
CLAUDE_EMAILS = ('account-a@example.com', 'account-b@example.com')
STATE = Path('/var/lib/personal-codex-router-routing')
KEY = Path('/etc/cliproxyapi/dashboard-management-key')
MODES = ('round-robin', 'earliest-weekly-reset', 'highest-remaining-quota', 'preferred-account')
PROVIDERS = {
    'codex': {'emails': EMAILS, 'modes': MODES, 'default': 'earliest-weekly-reset'},
    'claude': {'emails': CLAUDE_EMAILS, 'modes': MODES, 'default': 'preferred-account'},
}


def identities(provider):
    return PROVIDERS[provider]['emails']


def state_path(provider='codex'):
    return STATE if provider == 'codex' else STATE / provider



class Hold(Exception):
    """A safe, non-secret reason not to change routing."""


class DataHold(Hold):
    """Quota data cannot safely select an account."""


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
    result = {k: copy.deepcopy(gateway[k]) for k in ('online', 'checked_at') if k in gateway}
    fields = ('id', 'label', 'email', 'active', 'disabled', 'access_state', 'auth_state', 'priority',
              'quota_checked_at', 'quota_error', 'windows', 'availability')
    result['accounts'] = [{k: copy.deepcopy(account[k]) for k in fields if k in account}
                          for account in gateway.get('accounts', [])]
    return {'gateway': result}


def ineligible_reason(account):
    availability = account.get('availability') or {}
    state = availability.get('state')
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
    accounts = list(overview.get('watch_accounts', []))
    expected = {'claude': identities('claude')[0], 'claude-b': identities('claude')[1]}
    for account_id, email in expected.items():
        runtime = [r for r in gateway.get('claude_accounts', []) if r.get('email') == email]
        if (not any(a.get('id') == account_id for a in accounts) and len(runtime) == 1
                and (runtime[0].get('disabled') is True or runtime[0].get('status') == 'disabled')):
            accounts.append({'id': account_id, 'email': email})
    if (len(accounts) != 2 or {a.get('id') for a in accounts} != set(expected)
            or any(a.get('email') != expected.get(a.get('id')) for a in accounts)):
        raise DataHold('Claude usage account pair is incomplete or ambiguous')
    result = []
    for account in accounts:
        label = account['email']
        runtime = next((r for r in gateway.get('claude_accounts', [])
                        if r.get('email') == account['email']), {})
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
        if account.get('status') != 'ok' or account.get('error'):
            raise DataHold(f'quota unavailable for {label}')
        # Collectors run every five minutes; preserve their measured timestamps.
        checked = account.get('checked_at')
        if not fresh(checked, now, max_age=900):
            raise DataHold(f'stale quota data for {label}')
        windows = copy.deepcopy(account.get('windows', []))
        for kind, seconds in (('five_hour', 18000), ('seven_day', 604800)):
            primary = [w for w in windows if w.get('kind') == kind]
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
            if reset is None and finite(remaining) and remaining == 100:
                # An unused allowance cannot expire until one full period after first use.
                reset = now + (18000 if window.get('kind') == 'five_hour' else 604800)
                window.update(remaining=100, resets_at=reset)
            if (not finite(reset) or not now < reset <= now + 8 * 86400
                    or not finite(window.get('remaining')) or not 0 <= window['remaining'] <= 100):
                raise DataHold(f'account allowance is not reported for {label}')
            # Only the all-model weekly window determines the ranking.
            if window.get('kind') not in ('five_hour', 'seven_day'):
                window.pop('seconds', None)
        result.append(dict(email=account['email'], active=not disabled,
                           disabled=disabled, windows=windows, quota_checked_at=checked))
    # Quota freshness was checked against the collector's five-minute cadence.
    return {'gateway': {**gateway, 'accounts': result}}


def eligible_accounts(overview, now, provider='codex'):
    if provider == 'claude':
        overview = claude_observations(overview, now)
    emails = identities(provider)
    gateway = overview.get('gateway', {})
    if not gateway.get('online'):
        raise Hold('Gateway observations are unavailable')
    if not fresh(gateway.get('checked_at'), now):
        raise DataHold('Gateway observations are stale')
    accounts = [a for a in gateway.get('accounts', []) if a.get('email') in emails]
    if len(accounts) != len(emails) or {a['email'] for a in accounts} != set(emails):
        raise Hold('Expected account pair is incomplete or ambiguous')
    candidates, excluded = {}, {}
    for account in accounts:
        email, label = account['email'], account_label(account)
        reason = ineligible_reason(account)
        # Disabled/revoked credentials cannot serve regardless of quota. Other
        # active accounts still need valid observations before changing priority.
        if reason in ('disabled', 'disable_unconfirmed', 'auth_required') or (not account.get('active') and reason):
            excluded[email] = reason
            continue
        if account.get('quota_error'):
            error_class = 'auth_required' if account.get('auth_state') == 'sign_in_required' else 'quota_unavailable'
            raise DataHold(f'quota error for {label}: {error_class}')
        if not fresh(account.get('quota_checked_at'), now, 900 if provider == 'claude' else 180):
            raise DataHold(f'stale quota data for {label}')
        windows = account.get('windows', [])
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
            reset = ranked['resets_at']
            if not now < reset <= now + ranked['seconds'] + 86400:
                raise DataHold(f'waiting for the next usage window reset observation for {label}')
        else:
            if len(weekly) != 1 or not finite(weekly[0].get('resets_at')):
                raise DataHold(f'weekly reset time is not reported for {label}')
            ranked = weekly[0]
            reset = ranked['resets_at']
            if not now < reset <= now + 8 * 86400:
                raise DataHold(f'waiting for the next weekly reset observation for {label}')
        if any(not finite(w.get('remaining')) or not 0 <= w['remaining'] <= 100 for w in windows):
            raise DataHold(f'account allowance is not reported for {label}')
        if reason or any(w['remaining'] <= 0 for w in windows):
            excluded[email] = reason or 'no remaining allowance'
            continue
        candidates[email] = {'remaining': ranked['remaining'], 'reset': reset, 'secondary': secondary}
    return candidates, excluded


def priority_plan(email, reset=None, reason=None, provider='codex'):
    emails = identities(provider)
    return {'preferred_email': email, 'weekly_resets_at': reset, 'reason': reason,
            'priorities': {e: 100 if e == email else 0 for e in emails}}


def choose(overview, now, mode='earliest-weekly-reset', current=None, provider='codex'):
    if mode not in ('earliest-weekly-reset', 'highest-remaining-quota'):
        raise ValueError('Choose an automatic routing mode')
    candidates, _ = eligible_accounts(overview, now, provider)
    if not candidates:
        raise Hold('No account currently reports available allowance')
    # Never compare a weekly percentage with a longer window's percentage.
    candidates = {e: c for e, c in candidates.items() if not c['secondary']} or candidates
    quota = mode == 'highest-remaining-quota'
    rank = lambda email: -candidates[email]['remaining'] if quota else candidates[email]['reset']
    winner = min(candidates, key=rank)
    reason = None
    if current in candidates and winner != current:
        lead = rank(current) - rank(winner)
        if (quota and lead < 5) or (not quota and lead <= 900):
            winner = current
            reason = 'Keeping the current account within the stability margin'
    elif current not in candidates and sum(
            rank(e) == rank(winner) if quota else rank(e) - rank(winner) <= 900
            for e in candidates) > 1:
        return priority_plan(None, None if quota else candidates[winner]['reset'], provider=provider)
    return priority_plan(winner, candidates[winner]['reset'], reason, provider)


def request(path, body=None, method=None, base='http://127.0.0.1:8317', timeout=15):
    req = Request(base + '/v0/management' + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  method=method,
                  headers={'Authorization': 'Bearer ' + KEY.read_text().strip(),
                           'Content-Type': 'application/json'})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def records(call, provider='codex'):
    emails = identities(provider)
    files = call('/auth-files').get('files', [])
    rows = [a for a in files if a.get('type') == provider
            and (provider == 'claude' or a.get('email') in emails)]
    if len(rows) != len(emails) or {a.get('email') for a in rows} != set(emails):
        raise Hold('Gateway account pair is incomplete or ambiguous')
    if any(not isinstance(a.get('name'), str) or not a['name']
           or sum(f.get('name') == a['name'] for f in files) != 1 for a in rows):
        raise Hold('Gateway account filenames are incomplete or ambiguous')
    if provider == 'claude':
        for i, email in enumerate(emails):
            row = next(a for a in rows if a['email'] == email)
            if row.get('note') in ('Claude A', 'Claude B') and row['note'] != 'Claude ' + chr(65+i):
                raise Hold('Claude account identity conflicts with its label')
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


def apply_plan(plan, call, state_dir, provider='codex'):
    emails = identities(provider)
    rows = records(call, provider)
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
        if any(baseline[e]['name'] != rows[e]['name'] for e in emails):
            raise Hold('Account files changed; check policy ownership before continuing')
    else:
        write_json(baseline_path, {e: {'name': rows[e]['name'], 'priority': rows[e].get('priority')}
                                  for e in emails})
    changed = []

    def verified_rows():
        actual = records(call, provider)
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
        rows = records(call)
        if any(rows[e].get('priority') not in (None, 0, 100) or
               (recorded and rows[e]['name'] != baseline[e]['name']) for e in EMAILS):
            raise Hold('Account files or priorities changed; rollback left them untouched')
        if not recorded:
            if call('/config').get('routing', {}).get('strategy') != 'round-robin':
                raise Hold('Gateway strategy changed; inspect before applying priorities')
            baseline = {e: {'name': rows[e]['name'], 'priority': 0} for e in EMAILS}
            write_json(state_dir / 'selection.json', validate_selection({'mode': 'round-robin'}))
        write_json(state_dir / 'suspension.json', {'suspended': True})
        for email in EMAILS:
            call('/auth-files/fields', baseline[email], 'PATCH')
        actual = records(call)
        if any(not runtime_matches(actual[email], {'priority': baseline[email]['priority']}) for email in EMAILS):
            raise Hold('Gateway has not confirmed restored priorities')
        write_json(state_dir / 'status.json', {
            'policy': 'baseline' if recorded else 'round-robin',
            'status': 'disabled', 'checked_at': stamp(time.time()),
            'selected_email': None, 'preferred_email': None, 'weekly_resets_at': None,
            'suspended': True, 'intended_priorities': {e: baseline[e]['priority'] for e in EMAILS},
            'hold_reason': None, 'held_since': None, 'pending_plan': False,
        })
        return {'status': 'restored', 'policy': 'baseline' if recorded else 'round-robin'}


def validate_selection(value, provider='codex'):
    if not isinstance(value, dict) or value.get('mode') not in PROVIDERS[provider]['modes']:
        raise ValueError('Choose a supported routing mode')
    mode, email = value['mode'], value.get('preferred_email')
    if mode == 'preferred-account' and email not in identities(provider):
        raise ValueError('Choose a connected account')
    return {'mode': mode, 'preferred_email': email if mode == 'preferred-account' else None}


def selection(state_dir=None, provider='codex'):
    state_dir = state_path(provider) if state_dir is None else state_dir
    path = state_dir / 'selection.json'
    if not path.exists():
        return {'mode': PROVIDERS[provider]['default'],
                'preferred_email': identities(provider)[0] if provider == 'claude' else None}
    return validate_selection(json.loads(path.read_text()), provider)


def selected_plan(selected, overview, now, current=None, provider='codex'):
    selected = validate_selection(selected, provider)
    if selected['mode'] in ('earliest-weekly-reset', 'highest-remaining-quota'):
        return choose(overview, now, selected['mode'], current, provider)
    if selected['mode'] == 'round-robin':
        return priority_plan(None, provider=provider)
    candidates, excluded = eligible_accounts(overview, now, provider)
    email = selected['preferred_email']
    if email in candidates:
        return priority_plan(email, provider=provider)
    other = next((e for e in identities(provider) if e in candidates), None)
    reason = f"Preferred account {email}: {excluded.get(email, 'unavailable')}"
    return priority_plan(other, reason=reason, provider=provider)


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


def reconcile(overview, selected=None, call=request, state_dir=None, provider='codex'):
    state_dir = state_path(provider) if state_dir is None else state_dir
    emails = identities(provider)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / 'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Hold('Routing update already in progress; try again') from None
        now = time.time()
        previous = read_status(state_dir)
        wanted = validate_selection(selected, provider) if selected is not None else selection(state_dir, provider)
        if selected is None and suspended(state_dir):
            previous.update(status='disabled', checked_at=stamp(now), suspended=True,
                            hold_reason=None, held_since=None, pending_plan=False)
            write_json(state_dir / 'status.json', previous)
            return public_status(previous)
        config = call('/config')
        if config.get('routing', {}).get('strategy') != 'round-robin':
            raise Hold('Gateway strategy changed; inspect before applying priorities')
        rows = records(call, provider)
        if provider == 'claude' and any(row.get('priority') not in (None, 0, 100) for row in rows.values()):
            raise Hold('Unexpected manual priority; policy did not overwrite it')
        if selected is not None:
            write_json(state_dir / 'selection.json', wanted)
            write_json(state_dir / 'suspension.json', {'suspended': False})
        status = dict(previous, policy=wanted['mode'], selected_email=wanted['preferred_email'],
                      checked_at=stamp(now), suspended=False)
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
                changed = apply_plan(plan, call, state_dir, provider)
            except Exception:
                status.update(status='held', hold_reason='Routing update incomplete; automatic retry pending',
                              held_since=status.get('held_since') or stamp(now))
                write_json(state_dir / 'status.json', status)
                raise
            status.update(status=target, changed_accounts=changed, pending_plan=False)
            write_json(state_dir / 'status.json', status)

        # Codex completes persisted plans without fresh quota. Claude recomputes
        # from fresh usage before retrying, so stale snapshots never trigger writes.
        if provider == 'codex' and previous.get('pending_plan') and selected is None:
            plan = {k: previous.get(k) for k in ('preferred_email', 'weekly_resets_at', 'reason')}
            plan['priorities'] = previous['intended_priorities']
            apply(plan, previous.get('plan_status', 'active'))
            rows = records(call, provider)
        if provider == 'claude':
            overview = {**overview, 'gateway': {**overview.get('gateway', {}),
                                              'claude_accounts': list(rows.values())}}
        winners = [e for e in emails if rows[e].get('priority') == 100]
        current = winners[0] if len(winners) == 1 else None
        try:
            plan = selected_plan(wanted, overview, now, current, provider)
        except Hold as error:
            held_since = status.get('held_since') or stamp(now)
            status.update(status='held', hold_reason=str(error), held_since=held_since,
                          reason=str(error))
            status.setdefault('intended_priorities', {e: rows[e].get('priority') or 0 for e in emails})
            if provider == 'codex' and isinstance(error, DataHold) and now - dt.datetime.fromisoformat(held_since).timestamp() >= 600:
                plan = priority_plan(None, reason='Quota data held for 10 minutes; using neutral priorities')
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
    results, failed = {}, False
    for provider in PROVIDERS:
        if provider == 'claude' and not (state_path(provider) / 'selection.json').exists():
            results[provider] = {'status': 'unchanged', 'reason': 'No Claude selection saved'}
            continue
        try:
            observed = observations(overview.get('gateway', {})) if provider == 'codex' else overview
            deadline = RequestDeadline()
            results[provider] = reconcile(observed, call=deadline.wrap(request, maximum=15), provider=provider)
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
            wanted = selection()
            overview = {}
            if wanted['mode'] != 'round-robin' and not suspended(STATE):
                req = Request('http://127.0.0.1:8320/api/overview', headers={'X-Router-Request': '1'})
                try:
                    with urlopen(req, timeout=15) as response:
                        overview = observations(json.load(response).get('gateway', {}))
                except Exception:
                    if not args.apply:
                        raise
            result = reconcile(overview) if args.apply else selected_plan(wanted, overview, time.time())
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'status': 'held', 'reason': 'Routing check could not be completed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
