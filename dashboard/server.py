#!/usr/bin/env python3
"""Loopback-only dashboard. Management credentials never reach the browser."""
import argparse
import base64
import concurrent.futures
import copy
import datetime as dt
import importlib.util
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import time
from request_deadline import RequestDeadline, LOCK_WAIT_SECONDS
from reset_credits import ResetManager
from operations import NoticeStore, health
from availability import classify, pool_status
import routing_policy
import account_access
import signin
import t3_usage
from fast_mode import FastModeManager, forwarded_tier
from usage_time import timestamp_key
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
# Always acquire this request lock before a provider's controller lock.
ROUTING_SELECTION_LOCK = threading.Lock()
# Scripts are beside dashboard/ when installed, and under scripts/ in the checkout.
_redeemer_path = ROOT.parent / 'claude-redeemer.py'
if not _redeemer_path.is_file():
    _redeemer_path = ROOT.parent / 'scripts/claude-redeemer.py'
_redeemer_spec = importlib.util.spec_from_file_location('claude_redeemer', _redeemer_path)
_redeemer = importlib.util.module_from_spec(_redeemer_spec)
_redeemer_spec.loader.exec_module(_redeemer)
CLAUDE_RESULT_CODES = _redeemer.CODES
CLAUDE_MUTATION_TIMEOUT = 55
KEY = Path('/etc/cliproxyapi/dashboard-management-key')
DB = Path('/var/lib/personal-codex-router/usage.sqlite3')
PREFERENCE = Path('/var/lib/personal-codex-router-routing/status.json')
# Builds where a TTL change was verified to replace the selector and release every pin; separate from
# sign-in's TESTED_GATEWAYS, whose check never touches affinity. See docs/move-conversations-2026-10-17.md.
MOVE_TESTED_GATEWAYS = ('8.0.30', '8.0.43')
MOVE_REQUEST_DIR = Path('/run/personal-codex-router-move-request')
MOVE_RESULT = Path('/run/personal-codex-router-move/result.json')
MOVE_HELPER = Path('/opt/personal-codex-router/move-conversations.py')
# The path unit starts the helper within milliseconds, and a healthy helper answers in under 2 s.
MOVE_START_WAIT = 2.0
MOVE_RESULT_WAIT = 8.0
MOVE_RELOAD_WAIT = 5.0
MOVE_TTL_READ = 2.0
# At most one helper start per interval: with the first, 5 per 10 s, far below the unit's StartLimitBurst.
MOVE_START_INTERVAL = 2.0
# When the dashboard last created a request, on the move's monotonic clock; callers hold ROUTING_SELECTION_LOCK.
MOVE_THROTTLE = {'created_at': None}
# One check before the request covers the TTL read, the whole wait and the last reload poll.
MOVE_MIN_SECONDS = MOVE_TTL_READ + MOVE_RESULT_WAIT + MOVE_RELOAD_WAIT + MOVE_TTL_READ
BILLING = Path('/var/lib/personal-codex-router/billing.json')
CLAUDE_USAGE = Path('/var/lib/personal-codex-router-watch/claude.json')
CLAUDE_RESETS = CLAUDE_USAGE.with_name('claude-resets.json')
CLAUDE_SOCKET = CLAUDE_USAGE.with_name('claude-redeem.sock')
ALLOWED_HOSTS = {'router.localhost', 'localhost', '127.0.0.1'}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def account_metadata(file):
    meta = file.get('id_token') or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(base64.urlsafe_b64decode(meta.split('.')[1] + '==='))
        except (ValueError, IndexError):
            return {}
    return meta if isinstance(meta, dict) else {}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
        return parsed.astimezone(dt.timezone.utc).isoformat() if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def claude_request(data):
    """One bounded socket request; an ambiguous redemption is never retried here."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(CLAUDE_SOCKET))
        client.sendall(json.dumps(data).encode())
        client.shutdown(socket.SHUT_WR)
        client.settimeout(2 if data['action'] == 'status' else CLAUDE_MUTATION_TIMEOUT)
        content = bytearray()
        while True:
            part = client.recv(4097 - len(content))
            if not part:
                break
            content.extend(part)
            if len(content) > 4096:
                raise ValueError('unavailable')
    result = json.loads(content)
    if not isinstance(result, dict) or result.get('error') == 'unavailable':
        raise ValueError('unavailable')
    return result


def claude_reset_view(raw, now):
    raw = raw if isinstance(raw, dict) else {}
    valid_id = lambda v: isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', v)
    grants = []
    for g in raw.get('grants', []) if isinstance(raw.get('grants'), list) else []:
        if not isinstance(g, dict) or not valid_id(g.get('id')):
            continue
        kind = g.get('kind') if g.get('kind') in ('full', 'five_hour') else 'other'
        row = {'id': g['id'], 'kind': kind,
               'label': {'full': 'Full reset', 'five_hour': '5-hour reset', 'other': 'Reset'}[kind],
               'ends_at': g.get('ends_at') if finite(g.get('ends_at')) else None}
        for key in ('resets_left', 'resets_total'):
            row[key] = g.get(key) if type(g.get(key)) is int and 0 <= g[key] <= 1000000 else None
        for key in ('usable', 'usable_now', 'paused', 'use_requires_limit'):
            row[key] = g.get(key) if type(g.get(key)) is bool else None
        grants.append(row)
    result = {'service': 'unavailable', 'eligible': raw.get('eligible') if type(raw.get('eligible')) is bool else None,
              'ineligible_reason': _redeemer.usage.ineligible_reason(raw),
              'at_limit': raw.get('at_limit') if type(raw.get('at_limit')) is bool else None,
              'cooldown_until': raw.get('cooldown_until') if finite(raw.get('cooldown_until')) else None,
              'next_grant_id': raw.get('next_grant_id') if valid_id(raw.get('next_grant_id')) else None,
              'available': _redeemer.usage.grants_available(grants, now),
              'grants': grants, 'automatic': False, 'automatic_locked': False,
              'pending': None, 'last_event': None}
    status = {}
    fresh = False
    try:
        status = json.loads(CLAUDE_RESETS.read_text())
        if not isinstance(status, dict):
            status = {}
        checked = timestamp(status.get('checked_at'))
        fresh = checked is not None and 0 <= now - dt.datetime.fromisoformat(checked).timestamp() <= 120
        live = claude_request({'action': 'status'})
        if fresh and type(live.get('automatic')) is bool and type(live.get('automatic_locked')) is bool:
            result['service'] = 'ok'
            status = live
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    cooldown = status.get('cooldown_until')
    if finite(cooldown):
        result['cooldown_until'] = max(result['cooldown_until'], cooldown) if result['cooldown_until'] is not None else cooldown
    result['automatic'] = status.get('automatic') is True
    result['automatic_locked'] = status.get('automatic_locked') is True
    pending = status.get('pending')
    if (isinstance(pending, dict) and valid_id(pending.get('grant_id')) and
            pending.get('state') in ('pending', 'uncertain') and timestamp(pending.get('since'))):
        result['pending'] = {'grant_id': pending['grant_id'], 'state': pending['state'],
                             'since': timestamp(pending['since'])}
    event = status.get('last_event')
    if (isinstance(event, dict) and event.get('outcome') in ('reset', 'rejected', 'uncertain', 'error') and
            timestamp(event.get('at')) and (valid_id(event.get('grant_id')) or event.get('grant_id') is None) and
            isinstance(event.get('code'), str) and event['code'] in CLAUDE_RESULT_CODES):
        result['last_event'] = {key: event[key] for key in ('at', 'outcome', 'code', 'grant_id')}
    return result


def email_address(value):
    value = value.strip() if isinstance(value, str) else ''
    return value if len(value) <= 254 and re.fullmatch(
        r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
        r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
        r'[A-Za-z0-9]+(?:[A-Za-z0-9-]*[A-Za-z0-9])?'
        r'(?:\.[A-Za-z0-9]+(?:[A-Za-z0-9-]*[A-Za-z0-9])?)+', value) else None


def watch_accounts(now=None):
    """Local watch-only metadata; never part of gateway state or health."""
    result = []
    for path, account_id, label, resets in (
            (CLAUDE_USAGE, 'claude', 'Claude A', True),
            (CLAUDE_USAGE.with_name('claude-b.json'), 'claude-b', 'Claude B', False)):
        account = watch_account(path, account_id, label, resets, now)
        if account is not None:
            result.append(account)
    return result


def watch_account(path, account_id, account_label, resets_enabled, now=None, *, usage_only=False):
    now = time.time() if now is None else now
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    windows = []
    for row in raw.get('windows', []) if isinstance(raw.get('windows'), list) else []:
        if not isinstance(row, dict) or (not finite(row.get('used')) and
                (resets_enabled or row.get('used') is not None)):
            continue
        kind = row.get('kind')
        model = row.get('model')
        if kind == 'five_hour':
            label = '5-hour limit'
        elif kind == 'seven_day':
            label = 'Weekly · all models'
        elif kind == 'weekly_model' and isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .()+-]{0,63}', model):
            label = 'Weekly · ' + model
        elif isinstance(kind, str) and re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind):
            label = 'Weekly · ' + kind[len('seven_day_'):].replace('_', ' ').title()
        else:
            continue
        used = min(100, max(0, row['used'])) if finite(row.get('used')) else None
        public = {'kind': kind, 'label': label, 'used': used, 'remaining': 100 - used if used is not None else None,
                  'resets_at': row.get('resets_at') if finite(row.get('resets_at')) else None}
        if row.get('invalid_reset') is True or (row.get('resets_at') is not None and not finite(row['resets_at'])):
            public['invalid_reset'] = True
        if not resets_enabled:
            public['measured_at'] = timestamp(row.get('measured_at', raw.get('checked_at')))
            if public['resets_at'] is not None and public['resets_at'] <= now:
                public.update(used=None, remaining=None)
        if finite(row.get('seconds')) and row['seconds'] > 0:
            public['seconds'] = row['seconds']
        if kind == 'weekly_model':
            public['model'] = model
        windows.append(public)
    extra = raw.get('extra_usage')
    extra = extra if isinstance(extra, dict) else {}
    currency = extra.get('currency')
    extra = {'enabled': extra.get('enabled') is True,
             'used': extra.get('used') if finite(extra.get('used')) else None,
             'limit': extra.get('limit') if finite(extra.get('limit')) else None,
             'currency': currency if isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency) else None,
             'utilization': extra.get('utilization') if finite(extra.get('utilization')) else None}
    checked = timestamp(raw.get('checked_at'))
    if not resets_enabled:
        measured = [row['measured_at'] for row in windows if row['kind'] in ('five_hour', 'seven_day')]
        checked = min(measured, key=lambda at: dt.datetime.fromisoformat(at).timestamp()) if measured and all(measured) else None
    status = raw.get('status')
    status = status if status in ('ok', 'expired_token', 'missing_token', 'unavailable', 'error') else 'unavailable'
    if status == 'ok' and (checked is None or now - dt.datetime.fromisoformat(checked).timestamp() > 900):
        status = 'stale'
    error = raw.get('error')
    # Match only codes emitted by collect-claude-usage.py, never response text.
    error = error if isinstance(error, str) and re.fullmatch(
        r'http_[1-5][0-9]{2}|network|parse|credentials|expired_token|missing_token|cli_timeout|cli_unavailable|collection_deadline|no_usage_data|api_(?:rate_limit_)?error|unexpected_response',
        error) else None
    return {'id': account_id, 'provider': 'claude', 'label': account_label,
             'email': email_address(raw.get('email')),
             'plan': raw.get('plan') if raw.get('plan') in ('Pro', 'Max', 'Max (5x)', 'Max (20x)') else None,
             'status': status, 'error': error, 'checked_at': checked,
             'attempted_at': timestamp(raw.get('attempted_at')), 'windows': windows,
             'extra_usage': extra if resets_enabled else None,
             'resets': claude_reset_view(raw.get('resets'), now) if resets_enabled and not usage_only else None}


def billing_records():
    try:
        data = json.loads(BILLING.read_text())
        records = data.get('accounts', {}) if isinstance(data, dict) else {}
        return records if isinstance(records, dict) else {}
    except (OSError, ValueError):
        return {}


def billing_status(email, period_end, records):
    """An entitlement end is only an estimate until checked against billing."""
    period_end = timestamp(period_end)
    estimated = period_end[:10] if period_end else None
    record = records.get(email, {})
    if not isinstance(record, dict):
        record = {}
    try:
        date = dt.date.fromisoformat(record.get('renewal_date'))
        # Accept only canonical dates, not timestamps or compact date strings.
        confirmed = date.isoformat() == record['renewal_date']
    except (TypeError, ValueError, KeyError):
        confirmed = False
    verified_at = timestamp(record.get('verified_at'))
    source = record.get('source')
    confirmed = confirmed and bool(verified_at) and source in ('chatgpt_billing', 'user_confirmed')
    # A new entitlement period requires a new billing confirmation. Never roll a
    # saved date forward just because a month passed, or imply auto-renewal.
    if confirmed and (not estimated or estimated == record['renewal_date']):
        return {'renewal_date': record['renewal_date'], 'confirmed': True,
                'source': source, 'verified_at': verified_at}
    return {'renewal_date': estimated, 'confirmed': False,
            'source': 'account_metadata' if estimated else None, 'verified_at': None}


def preference_status(accounts=None, provider='codex'):
    path = PREFERENCE if provider == 'codex' else PREFERENCE.parent / provider / 'status.json'
    emails = routing_policy.identities(provider)
    try:
        if provider == 'claude' and not path.exists():
            saved = routing_policy.selection(path.parent, provider)
            return {'policy': saved['mode'], 'selected_email': saved['preferred_email'],
                    'saved_mode': saved['mode'], 'saved_preferred_email': saved['preferred_email'],
                    'status': 'checking', 'drift': None}
        value = json.loads(path.read_text())
        public = {k: value.get(k) for k in ('policy', 'status', 'checked_at', 'preferred_email', 'weekly_resets_at', 'selected_email')}
        try:
            saved = (routing_policy.selection(path.parent, provider) if (path.parent / 'selection.json').exists()
                     else {'mode': None, 'preferred_email': None})
            public['saved_mode'] = saved['mode']
            public['saved_preferred_email'] = saved['preferred_email']
        except (OSError, ValueError, TypeError):
            public.update(saved_mode=None, saved_preferred_email=None)
        public['hold_reason'] = value.get('hold_reason') if isinstance(value.get('hold_reason'), str) else None
        public['held_since'] = value.get('held_since') if timestamp(value.get('held_since')) else None
        public['reason'] = value.get('reason') if isinstance(value.get('reason'), str) else None
        public['suspended'] = value.get('suspended') is True
        actual = {a['email']: a.get('priority') or 0 for a in (accounts or [])
                  if a.get('email') in emails}
        intended = value.get('intended_priorities', value.get('priorities'))
        public['actual_priorities'] = actual
        public['drift'] = None
        checked_at = timestamp(value.get('checked_at'))
        observed_at = [timestamp(a.get('priority_checked_at')) for a in (accounts or [])
                       if a.get('email') in emails]
        fresh = checked_at and all(observed and dt.datetime.fromisoformat(observed) >
                                  dt.datetime.fromisoformat(checked_at) for observed in observed_at)
        if len(actual) == len(emails) and fresh:
            if isinstance(intended, dict) and all(e in intended for e in emails):
                public['drift'] = any(actual[e] != intended[e] for e in emails)
            elif value.get('status') == 'active':
                public['drift'] = sorted(actual.values()) != [0, 100]
        if public['status'] not in ('held', 'fallback', 'disabled'):
            age = time.time() - dt.datetime.fromisoformat(public['checked_at']).timestamp()
            if not -30 <= age <= 180:
                public['status'] = 'stale'
        return public
    except (OSError, ValueError, TypeError, KeyError):
        return None


def move_payload(outcome, code):
    """The move object returned to the frontend, inside the routing result."""
    return {'outcome': outcome, 'code': code}


def move_available(request_dir=None, helper=None):
    """Whether the request directory and the installed helper exist; paths only, no unit query."""
    request_dir = MOVE_REQUEST_DIR if request_dir is None else request_dir
    helper = MOVE_HELPER if helper is None else helper
    return request_dir.is_dir() and helper.is_file()


def move_tested(config):
    """Whether the gateway build that served this config is one where a TTL change was verified to drop pins."""
    return getattr(config, 'version', None) in MOVE_TESTED_GATEWAYS


def _duration(value):
    match = isinstance(value, str) and re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', value)
    if not match or not value:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _read_config(call, deadline):
    return call('/config', timeout=deadline.timeout(MOVE_TTL_READ))


def _affinity_ttl(config):
    routing = config.get('routing') if isinstance(config, dict) else None
    return routing.get('session-affinity-ttl') if isinstance(routing, dict) else None


def _withdraw_move_request(directory, inode):
    """Remove the request only while it is still ours.

    Returns 'removed' when this call removed it, 'gone' when ours no longer
    exists, and 'kept' when it may still be there.
    """
    target = directory / 'request'
    try:
        if os.lstat(target).st_ino != inode:
            return 'gone'
        os.unlink(target)
    except FileNotFoundError:
        return 'gone'
    except OSError:
        return 'kept'
    return 'removed'


def _open_move_request(directory):
    """Create the empty request and return its open descriptor; raises only when nothing was created."""
    return os.open(directory / 'request', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                   0o640)


def _read_move_result(inode, created_ns, result_path=None):
    """The helper's result for our request, or None.

    tmpfs can reuse a consumed request's inode, so the result must also be newer
    than the request, by both its file time and its own timestamp.
    """
    result_path = MOVE_RESULT if result_path is None else result_path
    try:
        with open(result_path, 'rb') as stream:
            modified = os.fstat(stream.fileno()).st_mtime_ns
            result = json.loads(stream.read())
        at = dt.datetime.fromisoformat(result['at'])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None
    if (not isinstance(result, dict) or result.get('request_inode') != inode or modified <= created_ns
            or at.tzinfo is None or int(at.timestamp() * 1_000_000) < created_ns // 1000):
        return None
    return result


def _log_move(outcome, code):
    def safe(value):
        return value if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,64}', value) else None
    try:
        print(json.dumps({'event': 'move_conversations', 'helper_outcome': safe(outcome), 'helper_code': safe(code)},
                         sort_keys=True), file=sys.stderr, flush=True)
    except Exception:
        pass  # The outcome stands without its journal line.


def move_conversations(deadline, call=None, request_dir=None, result_path=None,
                       clock=time.monotonic, sleep=time.sleep):
    """Ask the root helper to flip the affinity TTL and confirm the hot reload.

    Callers hold ROUTING_SELECTION_LOCK and have already checked that every
    requested provider applied its routing change.
    """
    call = api if call is None else call
    request_dir = MOVE_REQUEST_DIR if request_dir is None else request_dir
    result_path = MOVE_RESULT if result_path is None else result_path

    def remaining():
        return deadline.ends_at - deadline.clock()

    if not move_available(request_dir):
        return move_payload('not_moved', 'unavailable')
    if remaining() < MOVE_MIN_SECONDS:
        return move_payload('not_moved', 'no_time')
    try:
        config = _read_config(call, deadline)
    except Exception:
        return move_payload('not_moved', 'error')
    # Only a tested build is known to drop its pins on a TTL change; elsewhere the readback proves nothing.
    if not move_tested(config):
        return move_payload('not_moved', 'unavailable')
    before = _duration(_affinity_ttl(config))
    if before is None:
        return move_payload('not_moved', 'error')
    # Each request starts the helper, even one it refuses, so a scripted burst must not exhaust its start limit.
    last = MOVE_THROTTLE['created_at']
    if last is not None and clock() - last < MOVE_START_INTERVAL:
        return move_payload('not_moved', 'too_soon')
    try:
        fd = _open_move_request(request_dir)
    except FileExistsError:
        return move_payload('not_moved', 'in_progress')
    except OSError:
        return move_payload('not_moved', 'error')
    MOVE_THROTTLE['created_at'] = clock()
    inode, result, withdrawal, failed = None, None, None, False
    try:
        try:
            info = os.fstat(fd)
            inode, created_ns = info.st_ino, info.st_mtime_ns
        finally:
            os.close(fd)
        started = clock()
        ends_at = started + min(MOVE_RESULT_WAIT, max(0, remaining()))
        while True:
            result = _read_move_result(inode, created_ns, result_path)
            if result is not None or clock() >= ends_at:
                break
            # The path unit starts the helper within milliseconds; an untaken request means it isn't watching.
            if clock() - started >= MOVE_START_WAIT:
                withdrawal = _withdraw_move_request(request_dir, inode)
                if withdrawal == 'removed':
                    break
            sleep(min(0.05, max(0, ends_at - clock())))
    except Exception:
        failed = True
    finally:
        # Every exit withdraws a request the helper never took, so it can't start a later, unrequested move.
        if withdrawal != 'removed' and inode is not None:
            withdrawal = _withdraw_move_request(request_dir, inode)
    withdrawn = withdrawal == 'removed'
    if failed:
        # The request existed, so only our own confirmed withdrawal proves the helper never took it.
        return move_payload('not_moved', 'error') if withdrawn else move_payload('uncertain', 'error')
    if result is None:
        return move_payload('not_moved', 'helper_not_started') if withdrawn else move_payload('uncertain', 'helper_timeout')
    outcome, code = result.get('outcome'), result.get('code')
    _log_move(outcome, code)
    # A helper that couldn't remove the request leaves it for the path unit's next run, so only our own
    # withdrawal proves nothing moves; any other refusal or failure removed it first, unless ours is still there.
    if outcome in ('refused', 'failed') and (withdrawal == 'kept' or (code == 'consume_failed' and not withdrawn)):
        return move_payload('uncertain', 'error')
    if outcome == 'refused':
        return move_payload('not_moved', 'too_soon' if code == 'too_soon' else 'helper_refused')
    if outcome == 'failed':
        # The helper stopped before writing anything.
        return move_payload('not_moved', 'helper_failed')
    if outcome != 'written':
        return move_payload('uncertain', 'error')
    written = _duration(result.get('value'))
    if written is None or written == before:
        # An earlier missed reload can make the old value look like the new one.
        return move_payload('uncertain', 'reload_not_observed')
    ends_at = clock() + min(MOVE_RELOAD_WAIT, max(0, remaining()))
    while clock() < ends_at:
        try:
            if _duration(_affinity_ttl(_read_config(call, deadline))) == written:
                return move_payload('moved', 'moved')
        except Exception:
            pass
        sleep(min(0.1, max(0, ends_at - clock())))
    return move_payload('uncertain', 'reload_not_observed')


def claude_routing_records(call):
    """Verify the provider pair and unambiguous management mutation targets."""
    files = call('/auth-files').get('files', [])
    rows = routing_policy.records(lambda _: {'files': files}, 'claude')
    for row in rows.values():
        if (account_access.claude_routing_id(row) is None
                or sum(f.get('name') == row['name'] for f in files) != 1
                or sum(f.get('auth_index') == row['auth_index'] for f in files) != 1):
            raise routing_policy.Hold('Claude account identity is incomplete or ambiguous')
    return rows


def claude_routing_projection(files, checked_at):
    try:
        verified = claude_routing_records(lambda _: {'files': files})
    except routing_policy.Hold:
        verified = {}
    return [dict({k: f.get(k) for k in ('auth_index', 'email', 'priority', 'disabled', 'status')},
                 priority_checked_at=checked_at,
                 routing_id=account_access.claude_routing_id(f) if f.get('email') in verified else None,
                 routing_enabled=(f.get('disabled') is not True and f.get('status') != 'disabled')
                                 if f.get('email') in verified else None,
                 runtime_available=routing_policy.claude_runtime_available(f))
            for f in files if f.get('type') == 'claude']


def quota_windows(payload):
    result = []
    limits = payload.get('rate_limit') or {}
    for key in ('primary_window', 'secondary_window'):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get('used_percent')
        seconds = window.get('limit_window_seconds')
        reset = window.get('reset_at')
        result.append({
            'label': 'Weekly' if seconds == 604800 else '5-hour' if seconds == 18000 else 'Usage window',
            'seconds': seconds if finite(seconds) else None,
            'used': min(100, max(0, used)) if finite(used) else None,
            'remaining': min(100, max(0, 100 - used)) if finite(used) else None,
            'resets_at': reset if finite(reset) else None,
        })
    return result


def api(path, body=None, method=None, timeout=18):
    request = Request('http://127.0.0.1:8317/v0/management' + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      method=method,
                      headers={'Authorization': 'Bearer ' + KEY.read_text().strip(),
                               'Content-Type': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        return signin.ManagementResponse(account_access.json_object(response.read()),
                                         response.headers.get('X-CPA-VERSION'))


def codex_account_order(account):
    emails = routing_policy.identities('codex')
    email = account['email']
    return (emails.index(email) if email in emails else len(emails), email)


def fetch_account(file, resets=None, call=None):
    email = str(file.get('email', 'Connected account'))[:160]
    public = {'id': str(file.get('auth_index', ''))[:80], 'label': email,
              'email': email, 'active': not file.get('disabled') and not file.get('unavailable'),
              'disabled': bool(file.get('disabled')),
              '_fast_file': file.get('name'),
              'plan': account_access.plan_label(account_access.claim(file)),
              'windows': [], 'quota_error': None, 'quota_checked_at': None,
              'quota_attempted_at': utcnow(), 'auth_state': 'unknown'}
    public['priority'] = file.get('priority') if finite(file.get('priority')) else None
    meta = account_metadata(file)
    # These refresh failures require a new sign-in; generic routing/403 errors
    # do not establish an authentication problem. Never forward error text.
    message = str(file.get('status_message') or '').lower()
    if any(code in message for code in ('refresh_token_reused', 'refresh_token_expired',
                                        'refresh_token_invalidated', 'invalid_grant')):
        public['auth_state'] = 'sign_in_required'
    auth = meta.get('https://api.openai.com/auth')
    auth = auth if isinstance(auth, dict) else {}
    public['subscription_period_ends_at'] = timestamp(meta.get('chatgpt_subscription_active_until') or auth.get('chatgpt_subscription_active_until'))
    account_id = meta.get('chatgpt_account_id') or auth.get('chatgpt_account_id') or file.get('chatgpt_account_id')
    public['_t3_usage'] = t3_usage.codex_projection({}, account_access.identity(file), account_id)
    try:
        headers = {'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json',
                   'User-Agent': 'codex-tui/0.149.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.149.1)'}
        if account_id:
            headers['Chatgpt-Account-Id'] = account_id
        response = (call or api)('/api-call', {'auth_index': file['auth_index'], 'method': 'GET',
                       'url': 'https://chatgpt.com/backend-api/wham/usage', 'header': headers})
        if response.get('status_code') == 401:
            public['auth_state'] = 'sign_in_required'
        if response.get('status_code') != 200:
            raise ValueError('Quota unavailable')
        body = account_access.json_object(response.get('body'))
        public['_t3_usage'] = t3_usage.codex_projection(body, account_access.identity(file), account_id)
        public['windows'] = quota_windows(body)
        public['auth_state'] = 'authenticated'
        public['_usage_plan'] = account_access.plan_value(body.get('plan_type'))
        public['quota_checked_at'] = utcnow()
        if not public['windows']:
            public['quota_error'] = 'Allowance not reported by provider'
    except Exception:
        public['quota_error'] = 'Quota refresh unavailable'
    if resets is not None:
        try:
            resets.fetch(file)
        except Exception:
            pass  # Balance and quota polling succeed independently.
    public['availability'] = classify(file, public)
    return public


class GatewayState:
    def __init__(self, resets=None, notices=None, fast_modes=None):
        self.resets = resets if resets is not None else ResetManager(api, account_metadata)
        self.notices = notices if notices is not None else NoticeStore()
        self.fast_modes = fast_modes if fast_modes is not None else FastModeManager()
        self.lock = threading.Lock()
        self.lifecycle_lock = threading.RLock()
        self.access = account_access.AccountAccess()
        self.signins = signin.SigninManager(lambda *a, **kw: api(*a, **kw), self.lifecycle_lock, self.signin_changed)
        self.running = False
        self.last_attempt = 0
        self.account_updates = {}
        self.data = {'online': None, 'checked_at': None, 'accounts': [], 'routing': {}, 'error': None,
                     'move_available': False}
        self.resets.after_reset = self.after_reset

    def after_reset(self, account_id):
        with self.lifecycle_lock:
            return self._after_reset(account_id)

    def fetch_access_account(self, file):
        account = fetch_account(file, self.resets)
        self.access.project(file, account)
        self.access.maybe_refresh(file, account.pop('_usage_plan', None), api)
        return account

    def _after_reset(self, account_id):
        steps = {'quota_refresh': 'failed', 'routing_reconcile': 'failed'}
        try:
            priority_checked_at = utcnow()
            files = [f for f in api('/auth-files').get('files', []) if f.get('type') == 'codex'
                     and f.get('auth_index') == account_id]
            if len(files) != 1:
                raise ValueError('Account identity unavailable')
            files = self.access.reconcile(files, api, prune=False)
            account = self.fetch_access_account(files[0])
            account['priority_checked_at'] = priority_checked_at
            with self.lock:
                self.account_updates[account_id] = self.account_updates.get(account_id, 0) + 1
                self.data['accounts'] = [a for a in self.data['accounts'] if a['id'] != account_id] + [account]
                self.data['accounts'].sort(key=codex_account_order)
            steps['quota_refresh'] = 'failed' if account['quota_error'] else 'ok'
        except Exception:
            pass
        try:
            status = routing_policy.reconcile(routing_policy.observations(self.snapshot())).get('status')
            steps['routing_reconcile'] = {'active':'ok', 'fallback':'ok', 'held':'held', 'disabled':'skipped'}.get(status, 'failed')
        except routing_policy.Hold as error:
            steps['routing_reconcile'] = 'skipped' if 'already in progress' in str(error) else 'held'
        except Exception:
            pass
        return steps

    def signin_changed(self, account_id):
        self.after_reset(account_id)
        try:
            self.notices.update(self.snapshot())
        except Exception:
            pass

    def start_signin(self, account_id, identity, deadline=None):
        deadline = deadline or RequestDeadline()
        with deadline.lock(self.lock):
            required = any(a['id'] == account_id and a.get('access_identity') == identity
                           and a.get('auth_state') == 'sign_in_required' for a in self.data['accounts'])
        return self.signins.start(account_id, identity, observed_required=required, deadline=deadline)

    def set_routing_enabled(self, account_id, identity, enabled, confirmed=False, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(api)
        with deadline.lock(self.lifecycle_lock):
            record = self.signins.resolve(account_id, identity, call)
            if not enabled and record.get('disabled') is not True and not confirmed:
                files = [f for f in call('/auth-files').get('files', []) if f.get('type') == 'codex']
                accounts = [fetch_account(f, call=call) for f in files]
                deadline.timeout(LOCK_WAIT_SECONDS)
                ready = {a['id'] for a in accounts if a.get('active') and a.get('availability', {}).get('state') == 'ready'}
                if not ready - {account_id}:
                    raise signin.AccessError('last_ready')
            # Re-read after usage checks; a replacement must never receive this PATCH.
            with routing_policy.controller_flock(timeout=deadline.timeout(LOCK_WAIT_SECONDS)):
                record = self.signins.resolve(account_id, identity, call)
                if not enabled:
                    self.signins.suppress_enable(account_id)
                account_access.set_disabled_verified(record, not enabled, call)
                self.signins.routing_set(account_id)
        threading.Thread(target=self.signin_changed, args=(account_id,), daemon=True).start()
        return {'enabled': enabled}

    def set_claude_routing_enabled(self, account_id, enabled, confirmed=False, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(api)
        with deadline.lock(self.lifecycle_lock):
            with routing_policy.controller_flock(timeout=deadline.timeout(LOCK_WAIT_SECONDS),
                                                  state_dir=routing_policy.state_path('claude')):
                rows = claude_routing_records(call)
                record = next((r for r in rows.values()
                               if account_access.claude_routing_id(r) == account_id), None)
                if record is None:
                    raise signin.AccessError('account_changed')
                watched = None
                if not enabled and record.get('disabled') is not True and not confirmed:
                    watched = watch_accounts()
                # Revalidate after reading usage; never PATCH a replacement identity.
                rows = claude_routing_records(call)
                current = rows.get(record['email'])
                if not current or account_access.claude_routing_id(current) != account_id:
                    raise signin.AccessError('account_changed')
                if not enabled and current.get('disabled') is not True and not confirmed:
                    now = time.time()
                    observed = {'gateway': {'online': True, 'checked_at': routing_policy.stamp(now),
                                            # The target's quota does not establish whether
                                            # the other credential remains routable.
                                            'claude_accounts': [dict(r, disabled=True) if r is current else r
                                                                for r in rows.values()]},
                                'watch_accounts': watched if watched is not None else watch_accounts()}
                    try:
                        ready, _ = routing_policy.eligible_accounts(observed, now, 'claude')
                    except routing_policy.Hold:
                        ready = {}
                    if not set(ready) - {record['email']}:
                        raise signin.AccessError('last_ready')
                # Quota evaluation can take time; bind the GET immediately before PATCH.
                current = claude_routing_records(call).get(record['email'])
                if (not current or current.get('type') != record.get('type')
                        or current.get('email') != record.get('email')
                        or account_access.claude_routing_id(current) != account_id):
                    raise signin.AccessError('account_changed')
                call('/auth-files/status', {'name': current['name'], 'auth_index': current['auth_index'],
                                           'disabled': not enabled}, method='PATCH')
                actual_rows = claude_routing_records(call)
                actual = actual_rows.get(record['email'])
                if (not actual or account_access.claude_routing_id(actual) != account_id
                        or not account_access.runtime_matches(actual, {'disabled': not enabled})):
                    raise routing_policy.Hold('Account status readback did not match')
                # Publish the confirmed readback without waiting for the quota poll.
                projected = claude_routing_projection(list(actual_rows.values()), utcnow())
                with deadline.lock(self.lock):
                    self.data['claude_accounts'] = projected
        if (routing_policy.state_path('claude') / 'selection.json').exists():
            threading.Thread(target=self.claude_routing_changed, daemon=True).start()
        return {'enabled': enabled}

    def claude_routing_changed(self):
        try:
            deadline = RequestDeadline()
            routing_policy.reconcile({'gateway': self.snapshot(), 'watch_accounts': watch_accounts()},
                                     call=deadline.wrap(routing_policy.request, maximum=15), provider='claude')
        except Exception:
            pass  # The confirmed switch stands; routing reports holds and the timer retries.

    def set_fast_mode(self, account_id, allowed):
        if type(allowed) is not bool:
            raise ValueError('Allowed must be true or false')
        files = [f for f in api('/auth-files').get('files', []) if f.get('type') == 'codex']
        with self.lock:
            plans = {a['id']: a.get('plan', '') for a in self.data['accounts']}
        accounts = [dict(id=f['auth_index'], email=f['email'], name=f['name'],
                         supports_ultrafast='(25x)' in plans.get(f['auth_index'], '').lower())
                    for f in files if all(isinstance(f.get(k), str) and f[k]
                                         for k in ('auth_index', 'email', 'name'))]
        return self.fast_modes.set_allowed(account_id, allowed, accounts)

    def refresh(self):
        with self.lock:
            if self.running or time.monotonic() - self.last_attempt < 30:
                return False
            self.running = True
            self.last_attempt = time.monotonic()
        try:
            threading.Thread(target=self._fetch, daemon=True).start()
        except Exception:
            with self.lock:
                self.running = False
            return False
        return True

    def _fetch(self):
        try:
            with self.lifecycle_lock:
                self._fetch_locked()
        except Exception:
            with self.lock:
                self.data.update(online=False, error='Gateway temporarily unreachable')
        finally:
            with self.lock:
                self.running = False

    def _fetch_locked(self):
        try:
            with self.lock:
                updates = dict(self.account_updates)
            # Date the priority read before quota polling can delay publication.
            priority_checked_at = utcnow()
            all_files = api('/auth-files').get('files', [])
            claude_accounts = claude_routing_projection(all_files, priority_checked_at)
            files = [f for f in all_files if f.get('type') == 'codex']
            self.signins.prune(files)
            files = self.access.reconcile(files, api)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                accounts = list(pool.map(lambda f: fetch_account(f, self.resets), files))
            for file, account in zip(files, accounts):
                self.access.project(file, account)
                self.access.maybe_refresh(file, account.pop('_usage_plan', None), api)
                account['priority_checked_at'] = priority_checked_at
            config_error = None
            try:
                config = api('/config')
                routing = config.get('routing') or {}
                safe_routing = {k: routing.get(k) for k in ('strategy', 'session-affinity', 'session-affinity-ttl', 'session-affinity-subagents')}
            except Exception:
                # A separate config failure must not discard fresh access exclusions.
                with self.lock:
                    safe_routing = self.data['routing']
                config_error = 'Routing configuration temporarily unavailable'
            paths = move_available()
            with self.lock:
                # Only positive evidence turns moves off; a failed read keeps the last answer.
                available = (paths and move_tested(config) if config_error is None else
                             paths and self.data['move_available'])
                old = {a['id']: a for a in self.data['accounts']}
                before = self.eligibility(self.data['accounts'])
                accounts = [old[a['id']] if a['id'] in old and
                            updates.get(a['id'], 0) != self.account_updates.get(a['id'], 0) else a
                            for a in accounts]
                present = {a['id'] for a in accounts}
                accounts.extend(a for key, a in old.items() if key not in present and
                                updates.get(key, 0) != self.account_updates.get(key, 0))
                accounts.sort(key=codex_account_order)
                for account in accounts:
                    previous = old.get(account['id'])
                    if account['quota_error'] and previous and account.get('access_identity') == previous.get('access_identity'):
                        account['windows'] = previous['windows']
                        account['quota_checked_at'] = previous['quota_checked_at']
                self.data = {'online': True, 'checked_at': utcnow(), 'accounts': accounts,
                             'routing': safe_routing, 'error': config_error, 'claude_accounts': claude_accounts,
                             'move_available': available}
                changed = before != self.eligibility(accounts)
            if changed:
                try:
                    routing_policy.reconcile(routing_policy.observations(self.snapshot()))
                except Exception:
                    pass  # Routing reports its hold separately; keep the account snapshot.
        except Exception:
            with self.lock:
                self.data.update(online=False, error='Gateway temporarily unreachable')

    @staticmethod
    def eligibility(accounts):
        return {(a['id'], a.get('access_identity'), bool(a.get('active')),
                 routing_policy.ineligible_reason(a)) for a in accounts}

    def t3_usage_cache(self, now=None):
        """Read cached observations only; bypass the dashboard's enriching snapshot."""
        with self.lock:
            result = {key: copy.deepcopy(self.data.get(key)) for key in ('online', 'checked_at')}
            result['accounts'] = [{key: copy.deepcopy(a.get(key)) for key in (
                'id', 'email', 'disabled', 'access_identity', 'quota_error', 'quota_checked_at', '_t3_usage')}
                for a in self.data.get('accounts', [])]
            result['claude_accounts'] = [{key: copy.deepcopy(a.get(key)) for key in (
                'auth_index', 'email', 'disabled', 'routing_id')}
                for a in self.data.get('claude_accounts', [])]
        for account in result['accounts'] + result['claude_accounts']:
            account['email'] = email_address(account.get('email'))
        result['watch_accounts'] = [account for path, identity, label, resets in (
            (CLAUDE_USAGE, 'claude', 'Claude A', True),
            (CLAUDE_USAGE.with_name('claude-b.json'), 'claude-b', 'Claude B', False))
            if (account := watch_account(path, identity, label, resets, now, usage_only=True)) is not None]
        return result

    def snapshot(self):
        with self.lock:
            result = {**copy.deepcopy(self.data), 'refreshing': self.running}
        result['pool'] = pool_status(result)
        result['preference'] = preference_status(result['accounts'])
        result['claude_preference'] = preference_status(result.get('claude_accounts', []), provider='claude')
        result['routing_linked'] = routing_policy.routing_linked()
        records = billing_records()
        fast_accounts = [dict(id=a['id'], email=a['email'], name=a['_fast_file'],
                              supports_ultrafast='(25x)' in a.get('plan', '').lower())
                         for a in result['accounts'] if a.get('_fast_file')]
        fast_views = self.fast_modes.views(fast_accounts, DB) if fast_accounts else {}
        for account in result['accounts']:
            account.pop('_t3_usage', None)
            account.pop('_fast_file', None)
            account['fast_mode'] = fast_views.get(account.get('id'), {'allowed': True, 'changed_at': None, 'enforcement': 'unknown'})
            account['billing'] = billing_status(account['email'], account.pop('subscription_period_ends_at', None), records)
            account['resets'] = self.resets.view(account['email'])
            account['signin'] = self.signins.view(account.get('id'), account.get('access_identity'))
        return result


def usage_summary(start, db=DB, fast_modes=None, gateway=None):
    result = {'available': False, 'since': None, 'last_collected_at': None, 'accounts': [],
              'recent': [], 'hours': [], 'models': [],
              'claude': {'since': None, 'accounts': [], 'models': [], 'hours': []}, 'error': None}
    if not db.exists():
        return result
    con = None
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        with con:
            # Keep both providers and recent requests on one read-only snapshot.
            con.execute('BEGIN')
            columns = {row[1] for row in con.execute('PRAGMA table_info(usage)')}
            # The gateway queue is shared across providers. Keep pre-provider
            # history readable, but never present Claude traffic as Codex usage.
            codex = "(provider = 'codex' OR provider IS NULL OR provider = '')" if 'provider' in columns else '1'
            bounds = con.execute('SELECT min(timestamp), max(collected_at) FROM usage WHERE ' + codex).fetchone()
            result['since'], result['last_collected_at'] = bounds
            where = ' WHERE ' + codex + ' AND julianday(timestamp) >= julianday(?)'
            cache_fields = ('cache_read_tokens', 'cache_creation_tokens')
            # NULL sums mean unknown; counts expose partial coverage of old rows.
            cache_totals = ', '.join(
                f'sum({field if field in columns else "NULL"}) AS {field}, '
                f'count({field if field in columns else "NULL"}) AS {field.removesuffix("_tokens")}_requests'
                for field in cache_fields)
            totals = '''count(*) AS requests, sum(failed) AS failures,
                sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                sum(cached_tokens) AS cached_tokens, sum(total_tokens) AS total_tokens,
                avg(latency_ms) AS latency_ms, ''' + cache_totals
            result['accounts'] = [dict(r) for r in con.execute('SELECT auth_index,' + totals + ' FROM usage' + where + ' GROUP BY auth_index', (start,))]
            result['models'] = [dict(r) for r in con.execute('SELECT model,' + totals + ' FROM usage' + where + ' GROUP BY model ORDER BY requests DESC', (start,))]
            if 'provider' in columns:
                first = con.execute("SELECT timestamp FROM usage WHERE provider = 'claude' "
                                    "AND julianday(timestamp) IS NOT NULL "
                                    "ORDER BY julianday(timestamp), timestamp LIMIT 1").fetchone()
                result['claude']['since'] = first[0] if first else None
                claude_where = " WHERE provider = 'claude' AND julianday(timestamp) >= julianday(?)"
                result['claude']['accounts'] = [dict(r) for r in con.execute('SELECT auth_index,' + totals + ' FROM usage' + claude_where + ' GROUP BY auth_index', (start,))]
                result['claude']['models'] = [dict(r) for r in con.execute('SELECT model,' + totals + ' FROM usage' + claude_where + ' GROUP BY model ORDER BY requests DESC', (start,))]
                result['claude']['hours'] = [dict(r) for r in con.execute("SELECT strftime('%Y-%m-%dT%H:00:00Z',timestamp) AS hour, auth_index, count(*) AS requests FROM usage" + claude_where + ' GROUP BY hour,auth_index ORDER BY hour', (start,))]
            # Permit dashboard/collector deployment in either order and legacy DBs.
            effort = 'reasoning_effort' if 'reasoning_effort' in columns else 'NULL AS reasoning_effort'
            tiers = [name if name in columns else 'NULL AS ' + name
                     for name in ('service_tier', 'response_service_tier')]
            cache = [name if name in columns else 'NULL AS ' + name for name in cache_fields]
            provider = "coalesce(nullif(provider, ''), 'codex')" if 'provider' in columns else "'codex'"
            recent_where = ' WHERE ' + provider + " IN ('codex', 'claude') AND julianday(timestamp) >= julianday(?)"
            limit = 12
            candidates = con.execute('SELECT id, timestamp, auth_index, model, failed, latency_ms, total_tokens, ' + provider + ' AS provider, ' + ', '.join([effort, *tiers, *cache]) + ' FROM usage' + recent_where + ' ORDER BY id DESC LIMIT ?', (start, max(200, 10 * limit)))
            ordered = []
            for row in candidates:
                key = timestamp_key(row['timestamp'])
                if key is not None:
                    ordered.append((key, row['id'], dict(row)))
            ordered.sort(key=lambda item: item[:2], reverse=True)
            result['recent'] = [row for _, _, row in ordered[:limit]]
            emails = {}
            for name, accounts in (('codex', (gateway or {}).get('accounts', [])),
                                   ('claude', (gateway or {}).get('claude_accounts', []))):
                for account in accounts:
                    account_id = account.get('auth_index') if name == 'claude' else account.get('id')
                    emails[(name, account_id)] = email_address(account.get('email'))
            for row in result['claude']['accounts']:
                row['email'] = emails.get(('claude', row['auth_index']))
            for row in result['recent']:
                row.pop('id')
                row['email'] = emails.get((row['provider'], row['auth_index']))
                if row['provider'] == 'claude':
                    row.update(fast_mode_allowed=None, forwarded_service_tier=None)
                else:
                    row.update(fast_modes.request_view(row['auth_index'], row['timestamp'], row['service_tier'])
                               if fast_modes else {'fast_mode_allowed': True,
                                                   'forwarded_service_tier': forwarded_tier(row['service_tier'])})
            result['hours'] = [dict(r) for r in con.execute("SELECT strftime('%Y-%m-%dT%H:00:00Z',timestamp) AS hour, auth_index, count(*) AS requests FROM usage" + where + ' GROUP BY hour,auth_index ORDER BY hour', (start,))]
            result['available'] = True
    except sqlite3.Error:
        result['error'] = 'Usage history is temporarily unavailable'
    finally:
        if con is not None:
            con.close()
    return result


def valid_host(host):
    try:
        parsed = urlsplit('http://' + host)
        return parsed.hostname in ALLOWED_HOSTS and parsed.port in (None, 80, 8320, 18320) and not parsed.username and not parsed.password
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = 'PersonalRouter'

    def read_body(self, size, deadline=None):
        deadline = deadline or RequestDeadline()
        body = bytearray()
        try:
            while len(body) < size:
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                # read1 performs at most one underlying read, so a steady drip
                # cannot hide inside a buffered read beyond the absolute deadline.
                chunk = self.rfile.read1(min(8192, size - len(body)))
                deadline.timeout(LOCK_WAIT_SECONDS)
                if not chunk:
                    raise ValueError('Incomplete request body')
                body.extend(chunk)
        except TimeoutError:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            raise
        return bytes(body)

    def log_message(self, *_):
        pass

    def send(self, status, body, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store' if content_type.startswith('application/json') else 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, api_request=False):
        host = self.headers.get('Host', '')
        if not valid_host(host):
            self.send(421, b'{"error":"Unrecognized host"}')
            return False
        if api_request:
            origin = self.headers.get('Origin')
            same_origin = origin is None or origin == 'http://' + host
            if self.headers.get('X-Router-Request') != '1' or not same_origin or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send(403, b'{"error":"Same-origin request required"}')
                return False
        return True

    def do_GET(self):
        if t3_usage.handle(self, 'GET'):
            return
        url = urlsplit(self.path)
        is_api = url.path.startswith('/api/')
        if not self.allowed(is_api):
            return
        if url.path == '/api/overview':
            try:
                start = parse_qs(url.query).get('since', ['1970-01-01T00:00:00Z'])[0]
                parsed = dt.datetime.fromisoformat(start.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError()
            except ValueError:
                return self.send(400, b'{"error":"Invalid date"}')
            gateway = self.server.state.snapshot()
            watched = watch_accounts()
            expected = dict(zip(('claude', 'claude-b'), routing_policy.identities('claude')))
            for account in watched:
                matches = [r for r in gateway.get('claude_accounts', [])
                           if r.get('email') == account.get('email') == expected.get(account.get('id'))]
                runtime = matches[0] if len(matches) == 1 else {}
                account.update(routing_id=runtime.get('routing_id'),
                               routing_enabled=runtime.get('routing_enabled'),
                               runtime_available=routing_policy.claude_runtime_available(runtime) if runtime else None)
            data = {'generated_at': utcnow(), 'gateway': gateway,
                    'watch_accounts': watched,
                    'read_only': self.headers.get('X-Router-Read-Only') == '1',
                    'access_transport': 'tailscale' if self.headers.get('X-Router-Transport') == 'tailscale' else 'local',
                    'health': health(gateway), 'notifications': self.server.state.notices.view(),
                    'usage': usage_summary(start, DB, self.server.state.fast_modes, gateway), 'machine': 'user-Desktop'}
            return self.send(200, json.dumps(data, allow_nan=False).encode())
        if url.path == '/api/health':
            status = health(self.server.state.snapshot())
            return self.send(200, json.dumps({'ok': status['usage_collection']['status'] == 'healthy',
                                             'health': status}, allow_nan=False).encode())
        filename = 'index.html' if url.path == '/' else url.path.lstrip('/')
        path = (ROOT / 'static' / filename).resolve()
        if path.parent != (ROOT / 'static').resolve() or not path.is_file():
            return self.send(404, b'{"error":"Not found"}')
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix == '.js':
            content_type = 'text/javascript'
        self.send(200, path.read_bytes(), content_type)

    def apply_routing_selection(self, value, provider, deadline, call):
        """Shared controller and response contract for single and linked changes."""
        try:
            deadline.timeout(LOCK_WAIT_SECONDS)
            gateway = self.server.state.snapshot()
            if provider == 'claude':
                observed = {'gateway': gateway, 'watch_accounts': watch_accounts()}
                result = routing_policy.reconcile(observed, value, call=call, provider=provider)
            else:
                result = routing_policy.reconcile(routing_policy.observations(gateway), value, call=call)
            deadline.timeout(LOCK_WAIT_SECONDS)
            if result.get('status') in ('failed', 'unconfirmed', 'error'):
                return 503, result
            if result.get('status') in ('held', 'pending', 'applying') or result.get('pending_plan'):
                result = dict(result, message='Selection saved; applying pending because ' +
                              (result.get('hold_reason') or 'routing confirmation is pending'))
                return 202, result
            return 200, result
        except (ValueError, TypeError, UnicodeError):
            return 400, {'error': 'Invalid routing selection'}
        except Exception:
            return 503, {'error': 'Routing change not confirmed. Refresh and try again.'}

    def _move_result(self, results, codes, providers, deadline):
        """Move only after every requested provider applied the selection."""
        try:
            if any(code != 200 or not isinstance(results.get(provider), dict)
                   or results[provider].get('status') != 'active' for code, provider in zip(codes, providers)):
                return move_payload('not_moved', 'not_applied')
            return move_conversations(deadline, call=api)
        except Exception:
            return move_payload('uncertain', 'error')

    def do_POST(self):
        if t3_usage.handle(self, 'POST'):
            return
        if not self.allowed(True):
            return
        if self.headers.get('X-Router-Read-Only') == '1' and self.path != '/api/refresh':
            return self.send(403, b'{"error":"Management is available on your computer"}')
        if self.path == '/api/claude/accounts/routing':
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                if (not isinstance(data, dict) or set(data) != {'account_id', 'enabled', 'confirmed'}
                        or not isinstance(data['account_id'], str)
                        or not re.fullmatch(r'[a-f0-9]{64}', data['account_id'])
                        or type(data['enabled']) is not bool or type(data['confirmed']) is not bool):
                    raise ValueError()
                result = self.server.state.set_claude_routing_enabled(
                    data['account_id'], data['enabled'], data['confirmed'], deadline=deadline)
                deadline.timeout(LOCK_WAIT_SECONDS)
                return self.send(200, json.dumps(result).encode())
            except signin.AccessError as error:
                code = error.code if error.code in ('last_ready', 'account_changed') else 'unavailable'
                return self.send(409, json.dumps({'error': code}).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"Invalid account request"}')
            except Exception:
                return self.send(503, b'{"error":"Account change could not be confirmed. Try again."}')
        if self.path in ('/api/accounts/signin/start', '/api/accounts/signin/cancel', '/api/accounts/routing'):
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                extra = ({'handle'} if self.path.endswith('/cancel') else
                         {'enabled', 'confirmed'} if self.path.endswith('/routing') else set())
                if (not isinstance(data, dict) or set(data) != {'account_id', 'identity'} | extra
                        or not isinstance(data['account_id'], str) or not 1 <= len(data['account_id']) <= 80
                        or not isinstance(data['identity'], str) or not re.fullmatch(r'[a-f0-9]{64}', data['identity'])):
                    raise ValueError()
                if self.path.endswith('/start'):
                    result = self.server.state.start_signin(data['account_id'], data['identity'], deadline=deadline)
                elif self.path.endswith('/cancel'):
                    if not isinstance(data['handle'], str) or not 20 <= len(data['handle']) <= 100:
                        raise ValueError()
                    result = self.server.state.signins.cancel(data['account_id'], data['identity'], data['handle'], deadline=deadline)
                else:
                    if type(data['enabled']) is not bool or type(data['confirmed']) is not bool:
                        raise ValueError()
                    result = self.server.state.set_routing_enabled(data['account_id'], data['identity'],
                                                                  data['enabled'], data['confirmed'], deadline=deadline)
                deadline.timeout(LOCK_WAIT_SECONDS)
                return self.send(200, json.dumps(result).encode())
            except signin.AccessError as error:
                codes = {'account_changed', 'session_changed', 'already_finishing', 'signin_not_required', 'last_ready'}
                code = error.code if error.code in codes else 'unavailable'
                return self.send(409, json.dumps({'error': code}).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"Invalid account request"}')
            except Exception:
                return self.send(503, b'{"error":"Account change could not be confirmed. Try again."}')
        if self.path == '/api/fast-mode':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size))
                if (not isinstance(data, dict) or set(data) != {'account_id', 'allowed'}
                        or not isinstance(data['account_id'], str) or not 1 <= len(data['account_id']) <= 80
                        or type(data['allowed']) is not bool):
                    raise ValueError()
                result = self.server.state.set_fast_mode(data['account_id'], data['allowed'])
                return self.send(200, json.dumps(result).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"Invalid fast mode request or unavailable account"}')
            except Exception:
                return self.send(503, b'{"error":"Fast mode change was not saved. Try again."}')
        if self.path in ('/api/claude/resets/consume', '/api/claude/resets/policy'):
            policy = self.path.endswith('/policy')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size))
                if not isinstance(data, dict):
                    raise ValueError()
                if policy:
                    if set(data) != {'enabled'} or type(data['enabled']) is not bool:
                        raise ValueError()
                    request = {'action': 'policy', 'enabled': data['enabled']}
                else:
                    if (set(data) != {'grant_id', 'confirmed'} or data['confirmed'] is not True or
                            not isinstance(data['grant_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data['grant_id'])):
                        raise ValueError()
                    request = {'action': 'consume', **data}
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"invalid"}')
            except TimeoutError:
                return
            try:
                result = claude_request(request)
                if policy and result.get('error') == 'locked':
                    return self.send(423, b'{"error":"locked"}')
                if policy and type(result.get('automatic')) is bool:
                    return self.send(200, json.dumps({'automatic': result['automatic']}).encode())
                if not policy:
                    if result.get('outcome') == 'reset':
                        return self.send(200, b'{"outcome":"reset"}')
                    if result.get('outcome') == 'uncertain' and result.get('grant_id') == data['grant_id']:
                        return self.send(200, json.dumps({'outcome': 'uncertain', 'grant_id': data['grant_id']}).encode())
                    code = result.get('code')
                    if (result.get('outcome') == 'rejected' and isinstance(code, str) and
                            code in CLAUDE_RESULT_CODES):
                        return self.send(409, json.dumps({'outcome': 'rejected', 'code': code}).encode())
            except (OSError, ValueError, TypeError, UnicodeError):
                pass
            return self.send(503, b'{"error":"unavailable"}' if policy else b'{"outcome":"unavailable"}')
        if self.path in ('/api/routing', '/api/claude-routing', '/api/routing-all', '/api/routing-link'):
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                with deadline.lock(ROUTING_SELECTION_LOCK):
                    if self.path == '/api/routing-link':
                        if not isinstance(data, dict) or set(data) != {'linked'} or type(data['linked']) is not bool:
                            raise ValueError()
                        result = routing_policy.set_routing_linked(data['linked'], deadline)
                        return self.send(200, json.dumps(result).encode())
                    providers = (('codex', 'claude') if self.path == '/api/routing-all' else
                                 ('claude',) if self.path == '/api/claude-routing' else ('codex',))
                    if not isinstance(data, dict) or ('move' in data and type(data['move']) is not bool):
                        raise ValueError()
                    move = data.get('move') is True
                    # Validate against both providers before either controller can save or PATCH.
                    selections = {provider: routing_policy.validate_selection(data, provider) for provider in providers}
                    call = deadline.wrap(routing_policy.request, maximum=15)
                    results, codes = {}, []
                    for provider, value in selections.items():
                        code, results[provider] = self.apply_routing_selection(value, provider, deadline, call)
                        codes.append(code)
                    # Only a 200 or 202 response carries move; the frontend treats any other as not confirmed.
                    if len(providers) == 1:
                        if move and codes[0] < 400:
                            results[providers[0]]['move'] = self._move_result(results, codes, providers, deadline)
                        return self.send(codes[0], json.dumps(results[providers[0]]).encode())
                    code = 503 if any(c >= 400 for c in codes) else 202 if 202 in codes else 200
                    if move and code < 400:
                        results['move'] = self._move_result(results, codes, providers, deadline)
                    return self.send(code, json.dumps(results).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"Invalid routing selection"}')
            except Exception:
                return self.send(503, b'{"error":"Routing change not confirmed. Refresh and try again."}')
        if self.path in ('/api/resets/policy', '/api/resets/consume'):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Expected a small JSON request')
                data = json.loads(self.read_body(size))
                if not isinstance(data, dict) or not isinstance(data.get('account_id'), str):
                    raise ValueError('Account required')
                if self.path.endswith('/policy'):
                    result = self.server.state.resets.policy(data['account_id'], data.get('enabled'))
                else:
                    if data.get('confirmed') is not True or not isinstance(data.get('credit_id'), str) or not 1 <= len(data['credit_id']) <= 200:
                        raise ValueError('Confirm one specific reset credit')
                    result = self.server.state.resets.consume(data['account_id'], data['credit_id'])
                return self.send(200, json.dumps(result).encode())
            except (ValueError, TypeError):
                return self.send(400, b'{"error":"Invalid reset request or unavailable account"}')
            except Exception:
                return self.send(503, b'{"error":"Reset request could not be confirmed. Refresh before retrying the same credit."}')
        if self.path != '/api/refresh':
            return self.send(404, b'{"error":"Not found"}')
        accepted = self.server.state.refresh()
        self.send(202 if accepted else 200, json.dumps({'refreshing': accepted or self.server.state.snapshot()['refreshing']}).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8320)
    args = parser.parse_args()
    state = GatewayState()
    def poll():
        while True:
            state.refresh()
            time.sleep(60)
    threading.Thread(target=poll, daemon=True).start()
    def reset_poll():
        while True:
            try:
                state.resets.tick()
            except Exception:
                pass  # A provider outage must not stop future expiry checks.
            try:
                state.notices.update(state.snapshot())
            except Exception:
                pass
            time.sleep(10)
    threading.Thread(target=reset_poll, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.state = state
    try:
        server.serve_forever()
    finally:
        state.signins.close()
        server.server_close()


if __name__ == '__main__':
    main()
