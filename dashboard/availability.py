"""Read-only, allowlisted account availability for the dashboard."""
import datetime as dt
import json
import math
import time


def epoch(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def classify(record, account, now=None):
    now = time.time() if now is None else now
    result = {'state': 'unknown', 'retry_at': None, 'scope': 'account'}
    def state(name, retry=None, scope='account'):
        return dict(result, state=name, retry_at=retry, scope=scope)
    if account.get('access_state') == 'disable_unconfirmed':
        return state('disable_unconfirmed')
    if record.get('disabled'):
        return state('disabled')
    if account.get('auth_state') == 'sign_in_required':
        return state('auth_required')
    # Parse only structured error codes. Generic 429/quota cooldown labels
    # cannot establish subscription exhaustion; never publish error text.
    try:
        message = json.loads(record.get('status_message') or '{}')
        error = message.get('error', message) if isinstance(message, dict) else {}
        error = error if isinstance(error, dict) else {}
    except (TypeError, ValueError):
        error = {}
    until = epoch(record.get('next_retry_after'))
    cooldowns = [c for c in (record.get('cooldowns') if isinstance(record.get('cooldowns'), list) else []) if isinstance(c, dict)
                 and (epoch(c.get('retry_at')) or 0) > now]
    quota = error.get('type') == 'usage_limit_reached' or error.get('code') == 'usage_limit_reached'
    checked = epoch(account.get('quota_checked_at'))
    fresh = (not account.get('quota_error') and account.get('auth_state') == 'authenticated'
             and checked is not None and -30 <= now - checked <= 180)
    windows = account.get('windows', [])
    positive = fresh and bool(windows) and all(
        isinstance(w.get('remaining'), (int, float)) and not isinstance(w.get('remaining'), bool)
        and math.isfinite(w['remaining']) and 0 < w['remaining'] <= 100 for w in windows)
    if quota and not positive and record.get('unavailable') and (until is None or until > now):
        return state('quota_exhausted', until)
    if fresh:
        empty = [w for w in windows if w.get('remaining') == 0]
        if empty:
            resets = [w['resets_at'] for w in empty if isinstance(w.get('resets_at'), (float, int))
                      and not isinstance(w['resets_at'], bool) and math.isfinite(w['resets_at'])
                      and w['resets_at'] > now]
            return state('quota_exhausted', max(resets) if resets else None)
    if cooldowns:
        code = 'rate_limited' if any(c.get('http_status') == 429 for c in cooldowns) else 'provider_unavailable'
        scope = 'account' if any(c.get('scope') == 'credential' for c in cooldowns) else 'model'
        return state(code, min(epoch(c['retry_at']) for c in cooldowns), scope)
    if record.get('unavailable'):
        if positive and quota:
            if until is not None and until > now:
                return state('rate_limited', until)
            return state('ready')
        return state('unknown' if until and until <= now else 'provider_unavailable', until)
    if not positive:
        return state('unknown')
    return state('ready')


def pool_status(gateway, now=None):
    now = time.time() if now is None else now
    checked = epoch(gateway.get('checked_at'))
    accounts = gateway.get('accounts', [])
    states = [a.get('availability', {}).get('state', 'unknown') for a in accounts]
    if gateway.get('online') is not True or checked is None or not -30 <= now-checked <= 180 or 'unknown' in states:
        return {'state': 'unknown', 'ready': None, 'total': len(accounts)}
    ready = states.count('ready')
    return {'state': 'unavailable' if ready == 0 else 'reduced' if ready < 2 else 'available',
            'ready': ready, 'total': len(accounts)}
