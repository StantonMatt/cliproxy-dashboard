"""Claude allowance warning projection from validated, secret-free observations.

The notice store supplies the durable history. Projection is read-only: overview
requests never send notices or write files.
"""
import copy
import datetime as dt
import hashlib
import json
import math

import routing_policy
import usage_adapters as usage

PRIMARY = ('five_hour', 'seven_day')
LOW_PREFIX = 'claude:low:'
OUT_KEY = 'claude:out'
RESET_TOLERANCE = 300


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def account_id(account):
    return hashlib.sha256(json.dumps([account.get('key'), account.get('auth_index')],
                                    separators=(',', ':')).encode()).hexdigest()[:24]


def label(window):
    return window.get('label') or {'five_hour': '5-hour limit', 'seven_day': 'Weekly · all models'}.get(
        window.get('kind'), 'Weekly · ' + str(window.get('model') or 'model'))


def left(window):
    value = window.get('remaining')
    if value is None and number(window.get('used')):
        value = 100 - window['used']
    return value if number(value) and 0 <= value <= 100 else None


def current_gateway(gateway, now):
    checked = usage.epoch(gateway.get('checked_at'))
    return gateway.get('online') is True and checked is not None and -30 <= now - checked <= 180


def validate_notice(key, notice):
    """Fail closed on damaged structured history rather than breaking overview GETs."""
    if not (key.startswith(LOW_PREFIX) or key == OUT_KEY):
        return
    warning = notice.get('warning')
    if not isinstance(warning, dict):
        raise ValueError('Invalid Claude warning')
    if key.startswith(LOW_PREFIX):
        if (any(not isinstance(warning.get(k), str) for k in ('id', 'account_id', 'email', 'limit', 'kind'))
                or key != LOW_PREFIX + warning['id']
                or not number(warning.get('resets_at')) or warning['resets_at'] < 0
                or left(warning) is None or type(notice.get('banner_dismissed', False)) is not bool
                or not isinstance(notice.get('aliases', []), list)
                or any(not isinstance(alias, str) or len(alias) != 24
                       or any(c not in '0123456789abcdef' for c in alias)
                       for alias in notice.get('aliases', []))):
            raise ValueError('Invalid Claude low row')
    else:
        if (not isinstance(warning.get('rows'), list) or not warning['rows']
                or type(warning.get('codex_ready')) is not bool
                or warning.get('resets_at') is not None and not number(warning['resets_at'])):
            raise ValueError('Invalid Claude outage')
        for row in warning['rows']:
            if (not isinstance(row, dict)
                    or any(not isinstance(row.get(k), str) for k in ('account_id', 'email', 'limit'))
                    or row.get('resets_at') is not None and not number(row['resets_at'])
                    or type(row.get('off', False)) is not bool
                    or row.get('note') is not None and not isinstance(row['note'], str)):
                raise ValueError('Invalid Claude outage row')


def model_key(row):
    # Old persisted model rows carried only the display label.
    model = row.get('model')
    if model is None and row.get('kind') == 'weekly_model':
        model = row.get('limit', '').removeprefix('Weekly · ')
    return str(model or '').casefold()


def same_period(a, b):
    return (all(a.get(k) == b.get(k) for k in ('account_id', 'kind'))
            and model_key(a) == model_key(b)
            and number(a.get('resets_at')) and number(b.get('resets_at'))
            and abs(a['resets_at'] - b['resets_at']) <= RESET_TOLERANCE)


def coalesce_history(history):
    """Keep the oldest identity and consume any already delivered/dismissed copy."""
    result = copy.deepcopy(history)
    canonical = []
    for key, notice in sorted(history.items(), key=lambda item: (item[1].get('created_at', 0), item[0])):
        if not key.startswith(LOW_PREFIX):
            continue
        row = notice['warning']
        target = next((n for n in canonical if same_period(n['warning'], row)), None)
        if target is None:
            canonical.append(result[key])
            continue
        target['aliases'] = sorted(set(target.get('aliases', []) + notice.get('aliases', []) + [row['id']]))
        for field in ('banner_dismissed', 'dismissed', 'hidden', 'active'):
            target[field] = bool(target.get(field) or notice.get(field))
        del result[key]
    return result


def project(gateway, watched, history, now):
    authoritative = current_gateway(gateway, now)
    runtime = gateway.get('claude_accounts', [])
    accounts = []
    for account in runtime:
        observed = usage.claude_observation(account, runtime, watched, now)
        accounts.append((account, observed))
    ids = {account_id(a): a for a, _ in accounts}
    history = coalesce_history(history)
    low = {}
    for key, notice in history.items():
        row = notice.get('warning')
        if not key.startswith(LOW_PREFIX) or not isinstance(row, dict) or row['resets_at'] <= now:
            continue
        account = ids.get(row['account_id'])
        if authoritative and (account is None or account.get('routing_enabled') is False):
            continue
        low[row['id']] = dict(copy.deepcopy(row), dismissed=bool(notice.get('banner_dismissed')))
    blocked, ready, enabled = [], [], []
    for account, observed in accounts:
        identity = account_id(account)
        email = account.get('email') or account.get('key') or 'Claude'
        windows = observed.get('windows', [])
        valid = [w for w in windows if left(w) is not None and usage.window_fresh(
            w, observed.get('checked_at'), now, observed.get('fresh_until'), w.get('kind') in PRIMARY)]
        if account.get('routing_enabled') is True:
            enabled.append(identity)
            for window in valid:
                reset = usage.epoch(window.get('resets_at'))
                remaining = left(window)
                if remaining >= 10 or reset is None:
                    continue
                # Match persisted legacy IDs by fields before generating a new identity.
                row = dict(account_id=identity, email=email, limit=label(window),
                           kind=window.get('kind'), model=window.get('model'),
                           remaining=remaining, resets_at=reset)
                old = next((r for r in low.values() if same_period(r, row)), None)
                if old is None:
                    old = next((n['warning'] | {'dismissed': bool(n.get('banner_dismissed'))}
                                for k, n in history.items()
                                if k.startswith(LOW_PREFIX) and same_period(n['warning'], row)), {})
                row_id = old.get('id') or hashlib.sha256(json.dumps([identity, window.get('kind'),
                    model_key(row), float(reset)], separators=(',', ':')).encode()).hexdigest()[:24]
                # Keep the period's anchor reset; jitter must not walk into another period.
                low[row_id] = dict(row, id=row_id, resets_at=old.get('resets_at', reset),
                                   dismissed=old.get('dismissed', False))
        primary = [w for w in windows if w.get('kind') in PRIMARY]
        empty = [w for w in valid if w.get('kind') in PRIMARY and left(w) == 0]
        signed_out = (account.get('auth_state') == 'sign_in_required'
                      or observed.get('auth_state') == 'sign_in_required'
                      or observed.get('status') in ('expired_token', 'missing_token'))
        unavailable = (not routing_policy.claude_runtime_available(account)
                       or signed_out
                       or account.get('availability', {}).get('state') in ('auth_required', 'provider_unavailable'))
        row = dict(account_id=identity, email=email, resets_at=None, note=None)
        if account.get('routing_enabled') is False:
            remaining = next((left(w) for w in windows if w.get('kind') == 'seven_day'), None)
            blocked.append(dict(row, off=True, limit='Routing off', remaining=remaining))
        elif account.get('routing_enabled') is True:
            if signed_out:
                blocked.append(dict(row, limit='Signed out'))
            elif empty:
                reset = max(usage.epoch(w['resets_at']) for w in empty)
                limiting = max(empty, key=lambda w: usage.epoch(w['resets_at']))
                secondary = next((w for w in valid if w not in empty and left(w) < 10
                                  and (usage.epoch(w.get('resets_at')) or 0) > reset), None)
                note = (('Weekly' if secondary['kind'] == 'seven_day' else label(secondary))
                        + f' {left(secondary):g}% left') if secondary else None
                blocked.append(dict(row, limit=label(limiting), resets_at=reset, note=note))
            elif unavailable:
                blocked.append(dict(row, limit='Unavailable'))
            elif (observed.get('status') == 'ok' and not observed.get('error')
                  and any(w.get('kind') == 'seven_day' for w in primary)
                  and all(w in valid and left(w) > 0 for w in primary)):
                ready.append(email)
    previous = history.get(OUT_KEY, {})
    was_out = previous.get('active', False)
    out = None
    known_blocked = {r['account_id'] for r in blocked if not r.get('off')}
    if authoritative and enabled and set(enabled) <= known_blocked and not ready:
        out = dict(rows=blocked)
    elif was_out and not (authoritative and (ready or all(
            a.get('routing_enabled') is False for a, _ in accounts))):
        old_rows = (previous.get('warning') or {}).get('rows', [])
        rows = {r['account_id']: copy.deepcopy(r) for r in old_rows}
        rows.update({r['account_id']: r for r in blocked})
        if authoritative:
            rows = {key: row for key, row in rows.items() if key in ids}
        out = dict(rows=list(rows.values()))
    if out:
        out['rows'].sort(key=lambda r: (bool(r.get('off')), r.get('resets_at') or float('inf')))
        resets = [r['resets_at'] for r in out['rows'] if not r.get('off') and r.get('resets_at') is not None]
        out['resets_at'] = min(resets) if resets else None
        pool = gateway.get('pool') or {}
        out['codex_ready'] = (pool['ready'] > 0 if pool.get('ready') is not None else
                              bool((previous.get('warning') or {}).get('codex_ready')))
    order = {account_id(a): i for i, (a, _) in enumerate(accounts)}
    rows = sorted(low.values(), key=lambda r: (order.get(r['account_id'], len(order)),
                  0 if r['kind'] == 'five_hour' else 1 if r['kind'] == 'seven_day' else 2, r['limit']))
    return dict(low=rows, out=out, recovered=ready[0] if was_out and not out and authoritative and ready else None)


def duration(reset, now):
    if reset is None:
        return 'return time unknown'
    if reset <= now:
        return 'Reset due · checking'
    minutes = max(1, math.ceil((reset - now) / 60))
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    return (f'{days}d {hours}h' if days else f'{hours}h {minutes}m' if hours else f'{minutes}m')


def clock(reset, now=None):
    # The operations service runs on the desktop, whose local timezone owns delivery.
    if reset is None:
        return None
    value = dt.datetime.fromtimestamp(reset).astimezone()
    today = dt.datetime.fromtimestamp(now).astimezone().date() if now is not None else None
    return value.strftime('%-I:%M %p' if value.date() == today else '%b %-d, %-I:%M %p')


def low_notice(row, now):
    name = {'five_hour': '5-hour', 'seven_day': 'weekly'}.get(row['kind'], row['limit'].replace('Weekly · ', '') + ' weekly')
    when = duration(row['resets_at'], now)
    message = (f'Claude {name} limit reached · back in {when}' if row['remaining'] == 0 else
               f'Claude {name} limit · {row["remaining"]:g}% left · resets in {when}')
    return dict(email=row['email'], kind='claude_low', urgency='normal',
                message=message + f' [{clock(row["resets_at"], now)}]')


def out_notice(out, now):
    action = 'switch to OpenAI' if out['codex_ready'] else 'Codex has no ready account either'
    reset = out['resets_at']
    when = (f'first back in {duration(reset, now)} [{clock(reset, now)}]' if reset and reset > now else
            f'Reset due · checking [{clock(reset, now)}]' if reset else f'return time unknown [{clock(now, now)}]')
    reason = 'Limits reached' if any(r.get('resets_at') for r in out['rows'] if not r.get('off')) else 'Claude unavailable'
    return dict(email='All Claude accounts', kind='claude_all_out', urgency='critical',
                message=f'{reason} · {action} · {when}')
