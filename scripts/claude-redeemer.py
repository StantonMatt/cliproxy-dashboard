#!/usr/bin/env python3
"""User-owned Claude reset selector; credentials never cross the UNIX socket."""
import argparse
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import struct
import threading
import time
from urllib.error import HTTPError
import uuid

spec = importlib.util.spec_from_file_location('claude_usage', Path(__file__).with_name('collect-claude-usage.py'))
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)
STATE = Path.home() / '.local/state/personal-codex-router/claude-resets.json'
MASTER = Path.home() / '.config/personal-codex-router/claude-auto-reset-disabled'
PUBLIC = Path('/var/lib/personal-codex-router-watch/claude-resets.json')
SOCKET = PUBLIC.with_name('claude-redeem.sock')
MAX_REQUEST = 4096
LOCK_WAIT_TIMEOUT = 20
ORG = re.compile(r'[A-Za-z0-9_-]{1,128}')
REFUSAL_RESULTS = {'already_used', 'not_limited', 'cooldown', 'ineligible', 'unavailable'}
INDETERMINATE_RESULTS = {'stamp_indeterminate', 'reset_unconfirmed'}
# Public consume/event code contract, also imported by the dashboard.
CODES = frozenset({'not_usable', 'pending', 'retry_expired', 'budget', 'cooldown', 'next_grant',
                   'expired_token', 'credentials', 'provider', 'locked', 'confirmed', 'surface',
                   'ambiguous', 'local_reset', 'local_not_reset', 'account_changed', 'account_unbound'} |
                  {'http_' + str(n) for n in range(400, 500)} |
                  {'result_' + value for value in REFUSAL_RESULTS | INDETERMINATE_RESULTS})


def strict_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('invalid')
            result[key] = value
        return result
    return json.loads(content, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid')))


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('invalid')
    action = data.get('action')
    if action == 'consume' and set(data) == {'action', 'grant_id', 'confirmed'}:
        if usage.grant_id(data['grant_id']) and data['confirmed'] is True:
            return data
    elif action == 'policy' and set(data) == {'action', 'enabled'}:
        if type(data['enabled']) is bool:
            return data
    elif action == 'status' and set(data) == {'action'}:
        return data
    raise ValueError('invalid')


def response_cooldown(value):
    deadline = usage.reset_time(value)
    # Accept ISO dates or epoch seconds, bounded to representable UTC dates.
    if usage.number(deadline) and 0 <= deadline < 253402300800:
        return deadline
    return None


def retry_after_deadline(value, now):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r'[0-9]{1,12}', value):
            return response_cooldown(now + int(value))
        date = parsedate_to_datetime(value)
        return response_cooldown(date.timestamp()) if date.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def provider_constraints(raw, grant_id=None):
    """Allowlist constraints, never provider reason text or outcome guesses.

    An explicit fresh value supersedes a POST constraint; omission does not.
    POST grant fields refer to the grant we submitted, not an untrusted ID.
    """
    program, grants = {}, {}
    for key in ('eligible', 'at_limit'):
        if type(raw.get(key)) is bool:
            program[key] = raw[key]
    if isinstance(raw.get('exhausted'), list):
        program['exhausted'] = usage.grant_scopes(raw['exhausted'])
    if 'ineligible_reason' in raw:
        reason = raw['ineligible_reason']
        if reason is None or isinstance(reason, str):
            program['ineligible'] = bool(reason)
    elif raw.get('eligible') is True:
        program['ineligible'] = False
    if 'next_grant_id' in raw and (raw['next_grant_id'] is None or usage.grant_id(raw['next_grant_id'])):
        program['next_grant_id'] = raw['next_grant_id']
    rows = [dict(raw, id=grant_id)] if grant_id else raw.get('grants', [])
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not usage.grant_id(row.get('id')):
            continue
        fields = {}
        for key in ('usable_now', 'paused', 'use_requires_limit'):
            if type(row.get(key)) is bool:
                fields[key] = row[key]
        for key in ('resets_left', 'resets_total'):
            if type(row.get(key)) is int and 0 <= row[key] <= 1000000:
                fields[key] = row[key]
        if not grant_id and 'resets_left' in fields and 'resets_total' in fields:
            fields['resets_left'] = min(fields['resets_left'], fields['resets_total'])
        for key in ('clears', 'blocking'):
            if isinstance(row.get(key), list):
                fields[key] = usage.grant_scopes(row[key])
        end = response_cooldown(row.get('ends_at')) if grant_id else usage.epoch(row.get('ends_at'))
        if 'ends_at' in row and (row['ends_at'] is None or end is not None):
            fields['ends_at'] = end
        if fields:
            grants[row['id']] = fields
    return {'program': program, 'grants': grants}


class Rejection(Exception):
    def __init__(self, code):
        self.code = code


class Redeemer:
    def __init__(self, state=STATE, public=PUBLIC, master=MASTER,
                 credentials=usage.CREDENTIALS, clock=time.time):
        self.path, self.public, self.master = state, public, master
        self.credentials, self.clock = credentials, clock
        self.lock = threading.RLock()
        self.version_lock = threading.Lock()
        self.version = 0
        self.unsaved = None
        self.latest_usage_cooldown = None
        self.current_account = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise ValueError('state')
        self.path.parent.chmod(0o700)
        with self.transaction():
            self.version = self.state['version']
            self.publish()

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {'automatic': False, 'version': 0, 'pending': None, 'last_event': None,
                    'posts': [], 'last_success': None, 'cooldown_until': None, 'episodes': []}
        with os.fdopen(fd) as source:
            value = strict_json(source.read())
        # Damaged private state must fail closed, never mint a new operation.
        if (not isinstance(value, dict) or type(value.get('automatic')) is not bool or
                type(value.get('version')) is not int or value['version'] < 0 or
                not isinstance(value.get('posts'), list) or not isinstance(value.get('episodes'), list) or
                'pending' not in value or 'last_success' not in value or 'cooldown_until' not in value):
            raise ValueError('state')
        pending = value['pending']
        fingerprints = [value.get('automatic_account')]
        if isinstance(pending, dict):
            fingerprints.append(pending.get('account_fingerprint'))
        if any(v is not None and (not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v))
               for v in fingerprints):
            raise ValueError('state')
        if pending is not None and (not isinstance(pending, dict) or
                pending.get('state') not in ('pending', 'uncertain') or
                not usage.grant_id(pending.get('grant_id')) or
                not usage.number(pending.get('at')) or not isinstance(pending.get('request_id'), str)):
            raise ValueError('state')
        return value

    @contextmanager
    def transaction(self, timeout=LOCK_WAIT_TIMEOUT):
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=timeout):
            raise TimeoutError('lock')
        try:
            fd = os.open(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('lock') from None
                        time.sleep(min(.05, remaining))
                durable = self.load()  # CLI resolution and daemon share this lock.
                if self.unsaved:
                    before, after = self.unsaved
                    candidate = deepcopy(durable)
                    for key in after:
                        if before.get(key) != after[key]:
                            # Never overwrite a conflicting CLI resolution or policy edit.
                            if durable.get(key) not in (before.get(key), after[key]):
                                raise OSError('state conflict')
                            candidate[key] = deepcopy(after[key])
                    self.state = candidate
                    self.save()  # Keep the known outcome in memory if this still fails.
                    self.unsaved = None
                    durable = candidate
                self.state = durable
                self.recover_pending()
                yield
        finally:
            self.lock.release()

    def save(self):
        usage.publish(self.path, self.state, 0o600)

    def persist(self, candidate, retain=False):
        before = self.state
        self.state = candidate
        try:
            self.save()
        except Exception:
            if not retain:
                # replace() may have succeeded before the directory fsync failed.
                self.state = before
                self.state = self.load()
                self.recover_pending()
                raise
            # The provider outcome must survive a failed write and a later reload.
            self.unsaved = (before, candidate)
            try:
                self.save()
            except Exception:
                return
        self.unsaved = None

    def recover_pending(self):
        pending = self.state['pending']
        if pending and pending['state'] == 'pending':
            candidate = deepcopy(self.state)
            candidate['pending']['state'] = 'uncertain'
            self.event('uncertain', 'ambiguous' if pending.get('account_fingerprint') else 'account_unbound',
                       pending['grant_id'], candidate)
            self.persist(candidate, retain=True)

    def publish_status(self):
        try:
            # Cache-only checks must not add a write before durable dispatch.
            if not self.unsaved:
                durable = self.load()
                if any(durable.get(key) != self.state.get(key)
                       for key in ('account_pin_match', 'account_pin_checked_at')):
                    self.save()
        except Exception:
            pass  # A cache write failure must not suppress the public heartbeat.
        try:
            self.publish()
        except Exception:
            pass  # A public snapshot failure must not change the private outcome.

    def master_locked(self):
        return self.master.exists() or self.master.is_symlink()

    def effective_cooldown(self, now):
        deadlines = [response_cooldown(self.state['cooldown_until']), self.latest_usage_cooldown]
        if self.state['last_success'] is not None:
            deadlines.append(self.state['last_success'] + 600)
        posts = sorted(at for at in self.state['posts'] if now - at < 600)
        if len(posts) >= 3:
            # The rolling budget opens when fewer than three POSTs remain.
            deadlines.append(posts[-3] + 600)
        deadline = max((at for at in deadlines if at is not None and at > now), default=None)
        # Preserve the existing budget code when it alone blocks dispatch.
        code = 'cooldown' if any(at is not None and at > now for at in deadlines[:2]) or (
            self.state['last_success'] is not None and self.state['last_success'] + 600 > now) else 'budget'
        return deadline, code

    def status(self):
        pending = self.state['pending']
        cooldown, _ = self.effective_cooldown(self.clock())
        return {'automatic': self.state['automatic'] and not self.master_locked() and
                            bool(self.state.get('automatic_account')) and
                            self.state.get('account_pin_match') is True,
                'automatic_locked': self.master_locked(),
                'account_pin_match': self.state.get('account_pin_match'),
                'account_pin_checked_at': self.state.get('account_pin_checked_at'),
                'cooldown_until': cooldown,
                'pending': {'grant_id': pending['grant_id'], 'state': pending['state'],
                            'since': usage.stamp(pending['at'])} if pending else None,
                'last_event': self.state.get('last_event')}

    def publish(self):
        usage.publish(self.public, {'checked_at': usage.stamp(self.clock()), **self.status()})

    def event(self, outcome, code, grant, state=None):
        state = self.state if state is None else state
        state['last_event'] = {'at': usage.stamp(self.clock()), 'outcome': outcome,
                                    'code': code, 'grant_id': grant}

    def merge_provider_deadline(self, candidate, response=None, retry_after=None, success_at=None):
        """Persist every provider deadline together with the durable outcome."""
        deadlines = [response_cooldown(candidate['cooldown_until'])]
        if isinstance(response, dict):
            deadlines.append(response_cooldown(response.get('cooldown_until')))
        deadlines.append(retry_after_deadline(retry_after, self.clock()))
        if success_at is not None:
            deadlines.append(success_at + 600)
        candidate['cooldown_until'] = max((at for at in deadlines if at is not None), default=None)

    def merge_constraints(self, candidate, constraints, fresh=False):
        saved = candidate.setdefault('provider_constraints', {'program': {}, 'grants': {}})
        def merge(target, fields):
            for key, value in fields.items():
                if fresh and key not in target:
                    continue  # Usage reconciles POST constraints; it is not a history cache.
                blocks = (value is False if key in ('eligible', 'at_limit', 'usable_now') else
                          value is True if key in ('ineligible', 'paused', 'use_requires_limit') else
                          value == 0 if key == 'resets_left' else value is not None)
                if blocks:
                    target[key] = value
                else:
                    target.pop(key, None)
        merge(saved['program'], constraints['program'])
        for grant_id, fields in constraints['grants'].items():
            if fresh and grant_id not in saved['grants']:
                continue
            merge(saved['grants'].setdefault(grant_id, {}), fields)
            if not saved['grants'][grant_id]:
                del saved['grants'][grant_id]
        if not any(saved.values()):
            candidate.pop('provider_constraints')

    def token(self):
        try:
            raw = json.loads(self.credentials.read_text())
            oauth = raw.get('claudeAiOauth') if isinstance(raw, dict) else None
            if not isinstance(oauth, dict):
                raise Rejection('credentials')
            expiry = oauth.get('expiresAt')
            if not usage.number(expiry) or expiry <= (self.clock() + 60) * 1000:
                raise Rejection('expired_token')
            token = oauth.get('accessToken')
            if not isinstance(token, str) or not token:
                raise Rejection('credentials')
            return token, expiry
        except (OSError, ValueError, TypeError):
            raise Rejection('credentials') from None

    def account(self, token):
        self.current_account = None
        try:
            return self.read_account(token)
        finally:
            self.cache_account_pin(self.state)

    def cache_account_pin(self, candidate):
        pin = candidate.get('automatic_account')
        candidate['account_pin_match'] = (pin == self.current_account
                                          if pin and self.current_account else None)
        candidate['account_pin_checked_at'] = usage.stamp(self.clock())

    def read_account(self, token):
        # Hash unambiguous identity fields; raw IDs never enter either state file.
        profile = usage.fetch_profile(token)
        org = profile.get('organization') if isinstance(profile, dict) else None
        org_id = org.get('uuid') if isinstance(org, dict) else None
        if not isinstance(org_id, str) or not ORG.fullmatch(org_id):
            raise Rejection('provider')
        account_id = None
        for key in ('account', 'user'):
            identity = profile.get(key)
            if isinstance(identity, dict):
                for field in ('uuid', 'id'):
                    value = identity.get(field)
                    if isinstance(value, str) and value:
                        account_id = value
                        break
            if account_id is not None:
                break
        if account_id is None:
            for key in ('account_id', 'user_id'):
                value = profile.get(key)
                if isinstance(value, str) and value:
                    account_id = value
                    break
        fingerprint = hashlib.sha256(json.dumps([org_id, account_id], separators=(',', ':')).encode()).hexdigest()
        self.current_account = fingerprint
        return org_id, fingerprint

    def automatic_account_code(self):
        if not self.state.get('automatic_account'):
            return 'account_unbound'
        if self.current_account is None:
            return 'provider'
        if self.state['automatic_account'] != self.current_account:
            return 'account_changed'
        return None

    def refresh_automatic_account(self):
        if not self.state['automatic'] or self.master_locked():
            return
        self.current_account = None
        if self.state.get('automatic_account'):
            try:
                token, _ = self.token()
                self.account(token)
            except Exception:
                pass  # Unverifiable accounts never enable automatic dispatch.
        code = self.automatic_account_code()
        candidate = deepcopy(self.state)
        self.cache_account_pin(candidate)
        if code and (self.state.get('last_event') or {}).get('code') != code:
            self.event('rejected', code, None, candidate)
            self.persist(candidate)
        else:
            self.state = candidate

    def fresh(self, token):
        payload = usage.fetch_usage(token)
        usage.parse_usage(payload)  # Reject 200 error envelopes.
        block = payload.get('cedar_ember')
        if not isinstance(block, dict):
            raise Rejection('provider')
        for key in ('next_grant_id', 'cooldown_until'):
            value = block.get(key)
            if value is not None and ((key == 'next_grant_id' and not usage.grant_id(value)) or
                                      (key == 'cooldown_until' and response_cooldown(value) is None)):
                raise Rejection('provider')
        for grant in block.get('grants', []) if isinstance(block.get('grants'), list) else []:
            if isinstance(grant, dict):
                for key in ('paused', 'use_requires_limit', 'usable_now'):
                    if key in grant and type(grant[key]) is not bool:
                        raise Rejection('provider')
        grants = usage.parse_grants(block, self.clock())
        if token in json.dumps(grants):
            raise Rejection('provider')
        self.latest_usage_cooldown = response_cooldown(grants['cooldown_until'])
        constraints = provider_constraints(block)
        # Only persist fresh constraints when reconciling a saved POST response.
        if 'provider_constraints' in self.state:
            candidate = deepcopy(self.state)
            self.merge_constraints(candidate, constraints, fresh=True)
            if candidate != self.state:
                self.persist(candidate)
            saved = candidate.get('provider_constraints', {'program': {}, 'grants': {}})
            constraints['program'].update(saved['program'])
            for grant_id, fields in saved['grants'].items():
                constraints['grants'].setdefault(grant_id, {}).update(fields)
        grants.update({key: value for key, value in constraints['program'].items() if key != 'ineligible'})
        if constraints['program'].get('ineligible'):
            grants['eligible'] = False
        for grant in grants['grants']:
            source_usable = grant['usable']
            grant.update(constraints['grants'].get(grant['id'], {}))
            # Retained POST fields may only tighten the fresh client's decision.
            grant['usable'] = source_usable and usage.grant_usable(grants, grant['id'], self.clock())
        return grants

    def validate_grant(self, grants, grant_id, now):
        if grants['eligible'] is False and grants['ineligible_reason'] == 'surface':
            raise Rejection('surface')
        matches = [g for g in grants['grants'] if g['id'] == grant_id]
        if len(matches) != 1:
            raise Rejection('not_usable')
        grant = matches[0]
        cooldown, code = self.effective_cooldown(now)
        if cooldown is not None:
            raise Rejection(code)
        if grants['next_grant_id'] is not None and grants['next_grant_id'] != grant_id:
            raise Rejection('next_grant')
        if grant['usable'] is not True or not usage.grant_usable(grants, grant_id, now):
            raise Rejection('not_usable')
        return grant

    def consume(self, grant_id, automatic=False):
        now = self.clock()
        pending = deepcopy(self.state['pending'])
        retrying = pending is not None
        if pending:
            if automatic or pending['grant_id'] != grant_id or pending['state'] != 'uncertain':
                raise Rejection('pending')
            if not 0 <= now - pending['at'] < 600:
                raise Rejection('retry_expired')
        token, expiry = self.token()
        org_id, fingerprint = self.account(token)
        if pending:
            if not pending.get('account_fingerprint'):
                raise Rejection('account_unbound')
            if pending['account_fingerprint'] != fingerprint:
                raise Rejection('account_changed')
        if automatic and self.automatic_account_code():
            raise Rejection(self.automatic_account_code())
        grants = self.fresh(token)  # Always re-read after profile, even for auto dispatch.
        now = self.clock()
        grant = self.validate_grant(grants, grant_id, now)
        episode = [grant_id, grant['ends_at']]
        if automatic:
            if (not self.state['automatic'] or self.master_locked() or
                    grant['ends_at'] is None or not 0 < grant['ends_at'] - now <= 300 or
                    episode in self.state['episodes']):
                raise Rejection('not_usable')
        if expiry <= (now + 60) * 1000:
            raise Rejection('expired_token')
        if pending and not 0 <= now - pending['at'] < 600:
            raise Rejection('retry_expired')
        if pending is None:
            pending = {'id': str(uuid.uuid4()), 'grant_id': grant_id, 'request_id': str(uuid.uuid4()),
                       'resets_left_before': grant['resets_left'],
                       'cooldown_until_before': grants['cooldown_until'], 'at': now,
                       'state': 'pending', 'episode': episode if automatic else None,
                       'account_fingerprint': fingerprint}
        else:
            pending['state'] = 'pending'
        candidate = deepcopy(self.state)
        candidate['pending'] = pending
        candidate['posts'] = [at for at in candidate['posts'] if now - at < 600] + [now]
        try:
            self.persist(candidate)  # File AND parent directory fsynced before POST.
            self.publish()
        except Exception:
            # A failed fsync may still leave a durable request; never reject it.
            self.recover_pending()
            if self.state['pending']:
                return {'outcome': 'uncertain', 'grant_id': grant_id}
            raise
        # Recheck local conditions after potentially slow durable writes.
        dispatch_now = self.clock()
        abort = ('expired_token' if expiry <= (dispatch_now + 60) * 1000 else
                 'locked' if automatic and self.master_locked() else
                 'not_usable' if grant['ends_at'] is not None and grant['ends_at'] <= dispatch_now else
                 'retry_expired' if retrying and not 0 <= dispatch_now - pending['at'] < 600 else None)
        if abort:
            candidate = deepcopy(self.state)
            candidate['pending']['state'] = 'uncertain'
            candidate['posts'].pop()  # This attempt never reached provider dispatch.
            self.event('uncertain', abort, grant_id, candidate)
            self.persist(candidate, retain=True)
            return {'outcome': 'uncertain', 'grant_id': grant_id}
        outcome, code = 'uncertain', 'ambiguous'
        result, retry_after = None, None
        try:
            result = usage.fetch_json(token, 'https://api.anthropic.com/api/organizations/' + org_id + '/reset_rate_limits',
                                      {'program': 'cedar_ember', 'grant_id': grant_id,
                                       'request_id': pending['request_id']})
            if not isinstance(result, dict) or 'error' in result or result.get('type') == 'error':
                raise ValueError('provider')
            value, reason = result.get('result'), result.get('reason')
            # Indeterminate provider reasons also occur under "unavailable".
            if isinstance(reason, str) and reason in INDETERMINATE_RESULTS:
                code = 'result_' + reason
            elif isinstance(value, str) and value in INDETERMINATE_RESULTS:
                code = 'result_' + value
            elif value == 'reset':
                outcome, code = 'reset', 'confirmed'
            elif isinstance(value, str) and value in REFUSAL_RESULTS:
                code = 'result_' + value
                if not retrying:
                    outcome = 'rejected'
        except HTTPError as error:
            if error.code == 429 and error.headers is not None:
                retry_after = error.headers.get('Retry-After')
            try:
                content = error.read(1024 * 1024 + 1)
                if len(content) <= 1024 * 1024:
                    result = strict_json(content)
            except Exception:
                pass  # A malformed error body must not discard Retry-After.
            if 400 <= error.code < 500:
                code = 'http_' + str(error.code)
                if not retrying and error.code in (400, 401, 403, 404, 422):
                    outcome = 'rejected'
            error.close()
        except Exception:
            pass  # Fixed outcomes only; never exception/provider text.
        candidate = deepcopy(self.state)
        self.event(outcome, code, grant_id, candidate)
        if outcome == 'reset':
            candidate['last_success'] = self.clock()
            if pending.get('episode') and pending['episode'] not in candidate['episodes']:
                candidate['episodes'].append(pending['episode'])
            candidate['pending'] = None
        self.merge_provider_deadline(candidate, result, retry_after,
                                     candidate['last_success'] if outcome == 'reset' else None)
        if isinstance(result, dict):
            constraints = provider_constraints(result, grant_id)
            if any(constraints.values()) and token not in json.dumps(constraints):
                self.merge_constraints(candidate, constraints)
        if outcome == 'rejected':
            candidate['pending'] = None
            try:
                self.persist(candidate)
            except Exception:
                if self.state['pending']:
                    # Failed rejection writes must keep the operation unresolved.
                    candidate = deepcopy(candidate)
                    candidate['pending'] = deepcopy(self.state['pending'])
                    candidate['pending']['state'] = 'uncertain'
                    self.event('uncertain', code, grant_id, candidate)
                    if self.unsaved:
                        # Keep the durable base of a failed pending recovery.
                        self.state = self.unsaved[0]
                    self.persist(candidate, retain=True)
                    return {'outcome': 'uncertain', 'grant_id': grant_id}
                # A write may have succeeded before reporting a local failure.
            return {'outcome': 'rejected', 'code': code}
        elif outcome != 'reset':
            candidate['pending']['state'] = 'uncertain'
        self.persist(candidate, retain=True)
        return {'outcome': outcome, **({'grant_id': grant_id} if outcome == 'uncertain' else {})}

    def dispatch(self, data):
        data = validate(data)
        if data['action'] == 'status':
            # Read cached state immediately while a tick/consume owns the lock.
            try:
                with self.transaction(timeout=0):
                    self.publish_status()
                    return self.status()
            except TimeoutError:
                return self.status()
        version = None
        if data['action'] == 'policy':
            # Reserve ordering before waiting for the redemption/dispatch lock.
            with self.version_lock:
                self.version += 1
                version = self.version
        with self.transaction():
            try:
                if data['action'] == 'policy':
                    if version <= self.state['version']:
                        return {'automatic': self.status()['automatic']}
                    candidate = deepcopy(self.state)
                    candidate['version'] = version
                    if data['enabled'] and self.master_locked():
                        self.persist(candidate)
                        return {'error': 'locked'}
                    candidate['automatic'] = data['enabled']
                    if data['enabled']:
                        token, _ = self.token()
                        _, candidate['automatic_account'] = self.account(token)
                        self.cache_account_pin(candidate)
                    else:
                        candidate.pop('automatic_account', None)
                        candidate['account_pin_match'] = None
                        candidate['account_pin_checked_at'] = None
                    self.persist(candidate)
                    return {'automatic': self.status()['automatic']}
                return self.consume(data['grant_id'])
            except Rejection as error:
                candidate = deepcopy(self.state)
                self.event('rejected', error.code, data.get('grant_id'), candidate)
                self.persist(candidate)
                return {'outcome': 'rejected', 'code': error.code}
            except Exception:
                self.recover_pending()
                pending = self.state['pending']
                if pending:
                    return {'outcome': 'uncertain', 'grant_id': pending['grant_id']}
                candidate = deepcopy(self.state)
                self.event('error', 'provider', data.get('grant_id'), candidate)
                self.persist(candidate)
                return {'outcome': 'rejected', 'code': 'provider'}
            finally:
                self.publish_status()

    def tick(self):
        with self.transaction():
            try:
                self.refresh_automatic_account()
                if not self.status()['automatic'] or self.state['pending']:
                    return
                token, _ = self.token()
                grants = self.fresh(token)
                if grants['eligible'] is False and grants['ineligible_reason'] == 'surface':
                    return
                now = self.clock()
                if self.effective_cooldown(now)[0] is not None:
                    return
                for grant in grants['grants']:
                    end = grant['ends_at']
                    try:
                        self.validate_grant(grants, grant['id'], now)
                    except Rejection:
                        continue
                    if (end is not None and 0 < end - now <= 300 and
                            [grant['id'], end] not in self.state['episodes']):
                        try:
                            self.consume(grant['id'], automatic=True)
                        except Rejection as error:
                            if self.state['pending'] or error.code in ('account_changed', 'account_unbound'):
                                raise
                            continue
                        break
            except Rejection as error:
                candidate = deepcopy(self.state)
                self.event('rejected', error.code, None, candidate)
                self.persist(candidate)
            except Exception:
                self.recover_pending()
            finally:
                self.publish_status()

    def resolve(self, outcome):
        with self.transaction():
            pending = self.state['pending']
            if not pending or pending['state'] != 'uncertain':
                raise Rejection('pending')
            candidate = deepcopy(self.state)
            if outcome == 'reset':
                candidate['last_success'] = self.clock()
                self.merge_provider_deadline(candidate, {'cooldown_until': pending['cooldown_until_before']},
                                             success_at=candidate['last_success'])
                if pending.get('episode') and pending['episode'] not in candidate['episodes']:
                    candidate['episodes'].append(pending['episode'])
            self.event('reset' if outcome == 'reset' else 'rejected', 'local_' + outcome,
                       pending['grant_id'], candidate)
            candidate['pending'] = None
            self.persist(candidate)
            self.publish_status()


def serve_connection(conn, redeemer, allowed_uid):
    with conn:
        conn.settimeout(2)
        # Authenticate before reading even one byte.
        _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != allowed_uid:
            return
        try:
            content = bytearray()
            while True:
                part = conn.recv(MAX_REQUEST + 1 - len(content))
                if not part:
                    break
                content.extend(part)
                if len(content) > MAX_REQUEST:
                    raise ValueError('invalid')
            data = validate(strict_json(content))
            result = redeemer.dispatch(data)
        except (ValueError, UnicodeError, TypeError):
            result = {'error': 'invalid'}
        except Exception:
            result = {'error': 'unavailable'}
        try:
            conn.sendall(json.dumps(result).encode())
        except OSError:
            pass


def serve(redeemer, path=SOCKET):
    allowed_uid = pwd.getpwnam('cliproxyapi').pw_uid
    # Prevent a second instance from unlinking a live listener.
    fd = os.open(redeemer.path.with_suffix('.daemon.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as process_lock:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('socket')
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            mask = os.umask(0o117)
            try:
                listener.bind(str(path))
            finally:
                os.umask(mask)
            listener.listen(4)
            listener.settimeout(1)
            stop = threading.Event()
            workers = threading.BoundedSemaphore(4)
            arrivals = deque()
            def poll():
                while not stop.wait(30):
                    try:
                        redeemer.tick()
                    except Exception:
                        stop.set()  # Fail closed; main exits so systemd can recover pending state.
            timer = threading.Thread(target=poll, daemon=True)
            timer.start()
            def handle(conn):
                try:
                    serve_connection(conn, redeemer, allowed_uid)
                finally:
                    workers.release()
            try:
                while not stop.is_set():
                    try:
                        conn, _ = listener.accept()
                    except TimeoutError:
                        continue
                    now = time.monotonic()
                    while arrivals and now - arrivals[0] >= 60:
                        arrivals.popleft()
                    if len(arrivals) >= 60 or not workers.acquire(blocking=False):
                        conn.close()
                        continue
                    arrivals.append(now)
                    threading.Thread(target=handle, args=(conn,), daemon=True).start()
            finally:
                stop.set()
                path.unlink(missing_ok=True)
            if stop.is_set():
                raise RuntimeError('unavailable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve')
    resolve = commands.add_parser('resolve')
    resolve.add_argument('--as', dest='outcome', choices=('reset', 'not_reset'), required=True)
    args = parser.parse_args()
    try:
        daemon = Redeemer()
        if args.command == 'resolve':
            daemon.resolve(args.outcome)
            print('Claude reset attempt resolved locally')
        else:
            serve(daemon)
    except Exception:
        print('Claude reset operation unavailable')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
