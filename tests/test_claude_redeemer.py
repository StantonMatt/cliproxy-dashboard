from contextlib import contextmanager, redirect_stdout, redirect_stderr
from copy import deepcopy
from email.utils import formatdate
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('claude_redeemer', ROOT / 'scripts/claude-redeemer.py')
redeem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(redeem)


class RedeemerTests(unittest.TestCase):
    def test_exported_code_contract_covers_fixed_emitters(self):
        expected = {'not_usable', 'pending', 'retry_expired', 'budget', 'cooldown', 'next_grant',
                    'expired_token', 'credentials', 'provider', 'locked', 'confirmed', 'surface',
                    'ambiguous', 'local_reset', 'local_not_reset', 'account_changed', 'account_unbound'}
        expected.update('http_' + str(n) for n in range(400, 500))
        expected.update('result_' + value for value in (
            'already_used', 'not_limited', 'cooldown', 'ineligible', 'unavailable',
            'stamp_indeterminate', 'reset_unconfirmed'))
        self.assertEqual(redeem.CODES, expected)
        self.assertIsInstance(redeem.CODES, frozenset)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = 1790790000
        self.secret = 'FAKE_ACCESS_SECRET'
        self.credentials = self.root / 'credentials.json'
        self.write_credentials()
        self.profile = patch.object(redeem.usage, 'fetch_profile', return_value={
            'organization': {'uuid': 'org-private'}, 'account': {'uuid': 'user-private'}}).start()
        self.fingerprint = hashlib.sha256(b'["org-private","user-private"]').hexdigest()
        self.addCleanup(patch.stopall)
        self.payload = {'five_hour': {'utilization': 100}, 'seven_day': {'utilization': 10}, 'cedar_ember': {
            'eligible': True, 'at_limit': True, 'exhausted': ['five_hour'], 'next_grant_id': 'grant1',
            'cooldown_until': None, 'grants': [{'id': 'grant1', 'clears': ['five_hour', 'seven_day'],
                'resets_left': 2, 'resets_total': 3, 'paused': False, 'usable_now': True,
                'use_requires_limit': True, 'ends_at': redeem.usage.stamp(self.now + 250)}]}}
        self.fetch = patch.object(redeem.usage, 'fetch_usage', side_effect=lambda _: self.payload).start()
        self.post = patch.object(redeem.usage, 'fetch_json', return_value={'result': 'reset'}).start()
        self.daemon = self.new_daemon()

    def new_daemon(self):
        return redeem.Redeemer(self.root / 'state/claude-resets.json', self.root / 'public.json',
                               self.root / 'master', self.credentials, lambda: self.now)

    def write_credentials(self, expiry=None):
        self.credentials.write_text(json.dumps({'claudeAiOauth': {
            'accessToken': self.secret, 'refreshToken': 'FAKE_REFRESH_SECRET',
            'expiresAt': (self.now + 3600 if expiry is None else expiry) * 1000}}))

    def consume(self, grant='grant1'):
        return self.daemon.dispatch({'action': 'consume', 'grant_id': grant, 'confirmed': True})

    def state(self):
        return json.loads(self.daemon.path.read_text())

    def change_state(self, **values):
        if values.get('automatic') is True:
            values.setdefault('automatic_account', self.fingerprint)
        with self.daemon.transaction():
            self.daemon.state.update(values)
            self.daemon.save()

    def test_peer_uid_rejected_before_reading(self):
        conn = Mock()
        conn.__enter__ = Mock(return_value=conn)
        conn.__exit__ = Mock(return_value=False)
        conn.getsockopt.return_value = struct.pack('3i', 999, 123, 456)
        redeem.serve_connection(conn, self.daemon, 124)
        conn.recv.assert_not_called()
        conn.sendall.assert_not_called()
        conn.getsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)

    def test_surface_refuses_manual_and_automatic_consume_without_post(self):
        for empty in (False, True):
            for automatic in (False, True):
                with self.subTest(empty=empty, automatic=automatic):
                    self.daemon = self.new_daemon()
                    self.change_state(automatic=True)
                    block = self.payload['cedar_ember']
                    block.update(eligible=False, ineligible_reason='surface')
                    if empty:
                        block.update(grants=[], next_grant_id=None)
                    if automatic:
                        with self.daemon.transaction(), self.assertRaises(redeem.Rejection) as error:
                            self.daemon.consume('grant1', automatic=True)
                        self.assertEqual(error.exception.code, 'surface')
                    else:
                        self.assertEqual(self.consume(), {'outcome': 'rejected', 'code': 'surface'})
                    self.post.assert_not_called()
                    self.assertEqual(self.state()['posts'], [])
                    self.assertIsNone(self.state()['pending'])

    def test_surface_tick_is_idle_and_resumes_when_eligible(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        block = self.payload['cedar_ember']
        block.update(eligible=False, ineligible_reason='surface')
        for empty in (False, True):
            with self.subTest(empty=empty):
                grants = block['grants']
                if empty:
                    block.update(grants=[], next_grant_id=None)
                event = self.state()['last_event']
                with patch.object(self.daemon, 'consume', wraps=self.daemon.consume) as consume:
                    self.daemon.tick()
                    consume.assert_not_called()
                self.post.assert_not_called()
                self.assertEqual(self.state()['last_event'], event)
                self.assertTrue(self.state()['automatic'])
                self.assertEqual(self.state()['posts'], [])
                self.assertIsNone(self.state()['pending'])
                block.update(grants=grants, next_grant_id='grant1')
        block.update(eligible=True, ineligible_reason=None)
        self.daemon.tick()
        self.post.assert_called_once()
        self.assertEqual(self.state()['last_event']['outcome'], 'reset')

    def test_status_uses_persisted_account_pin_without_network(self):
        status = self.daemon.dispatch({'action': 'status'})
        self.assertIsNone(status['account_pin_match'])
        self.assertIsNone(status['account_pin_checked_at'])
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        self.daemon = self.new_daemon()
        self.profile.reset_mock()
        self.fetch.reset_mock()
        self.post.reset_mock()
        status = self.daemon.dispatch({'action': 'status'})
        self.assertTrue(status['automatic'])
        self.assertTrue(status['account_pin_match'])
        self.assertEqual(status['account_pin_checked_at'], redeem.usage.stamp(self.now))
        self.assertEqual(self.state()['account_pin_match'], status['account_pin_match'])
        self.profile.assert_not_called()
        self.fetch.assert_not_called()
        self.post.assert_not_called()

    def test_status_does_not_wait_behind_provider_work(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.daemon.transaction():
                held.set()
                release.wait(2)
        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(held.wait(1))
            self.profile.reset_mock()
            self.assertTrue(self.daemon.dispatch({'action': 'status'})['account_pin_match'])
            self.profile.assert_not_called()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_transaction_bounds_both_lock_waits(self):
        with patch.object(self.daemon, 'lock') as lock:
            lock.acquire.return_value = False
            with self.assertRaises(TimeoutError):
                with self.daemon.transaction():
                    self.fail('lock acquired')
            lock.acquire.assert_called_once_with(timeout=redeem.LOCK_WAIT_TIMEOUT)
        with patch.object(redeem.fcntl, 'flock', side_effect=BlockingIOError), \
                patch.object(redeem.time, 'monotonic', side_effect=[100, 100 + redeem.LOCK_WAIT_TIMEOUT]):
            with self.assertRaises(TimeoutError):
                with self.daemon.transaction():
                    self.fail('file lock acquired')

    def connection(self, content):
        conn = Mock()
        conn.__enter__ = Mock(return_value=conn)
        conn.__exit__ = Mock(return_value=False)
        conn.getsockopt.return_value = struct.pack('3i', 999, 123, 456)
        conn.recv.side_effect = [content, b'']
        redeem.serve_connection(conn, self.daemon, 123)
        return json.loads(conn.sendall.call_args.args[0])

    def test_strict_inputs_and_size_limit_never_echo_input(self):
        invalid = [[], {}, {'action': 'consume', 'grant_id': 'grant1', 'confirmed': 1},
                   {'action': 'consume', 'grant_id': '../bad', 'confirmed': True},
                   {'action': 'consume', 'grant_id': 'g' * 129, 'confirmed': True},
                   {'action': 'consume', 'grant_id': 'grant1', 'confirmed': True,
                    'request_id': self.secret}, {'action': 'policy', 'enabled': 1},
                   {'action': 'policy', 'enabled': True, 'version': 4},
                   {'action': 'status', 'token': self.secret}, {'action': 'resolve'}]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.connection(json.dumps(value).encode()), {'error': 'invalid'})
        for content in (b'{} {}', b'{"action":"status","action":"status"}', b'NaN', b'\xff', b'x' * 4097):
            self.assertEqual(self.connection(content), {'error': 'invalid'})
        self.post.assert_not_called()

    def test_request_generated_and_durable_before_post(self):
        before = self.credentials.read_bytes()
        def post(token, url, body):
            saved = self.state()
            self.assertEqual(saved['pending']['state'], 'pending')
            self.assertEqual(saved['pending']['request_id'], body['request_id'])
            self.assertEqual(saved['pending']['resets_left_before'], 2)
            self.assertEqual(saved['pending']['account_fingerprint'], self.fingerprint)
            self.assertEqual(saved['posts'], [self.now])
            self.assertEqual(body['program'], 'cedar_ember')
            self.assertEqual(url, 'https://api.anthropic.com/api/organizations/org-private/reset_rate_limits')
            self.assertEqual(self.daemon.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.daemon.path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.daemon.public.stat().st_mode & 0o777, 0o640)
            import uuid
            self.assertEqual(uuid.UUID(body['request_id']).version, 4)
            return {'result': 'reset'}
        self.post.side_effect = post
        self.assertEqual(self.consume(), {'outcome': 'reset'})
        self.assertEqual(self.credentials.read_bytes(), before)
        self.assertIsNone(self.state()['pending'])
        self.assertEqual(self.state()['last_success'], self.now)

    def test_crash_before_and_after_post_recover_as_uncertain(self):
        original = self.daemon.publish
        def crash_before():
            if self.daemon.state['pending']:
                raise SystemExit('crash')
            original()
        with patch.object(self.daemon, 'publish', side_effect=crash_before), self.assertRaises(SystemExit):
            self.consume()
        self.post.assert_not_called()
        request_id = self.state()['pending']['request_id']
        self.daemon = self.new_daemon()
        self.assertEqual(self.state()['pending']['state'], 'uncertain')
        self.post.side_effect = SystemExit('crash after dispatch')
        with self.assertRaises(SystemExit):
            self.consume()
        self.daemon = self.new_daemon()
        self.assertEqual(self.state()['pending']['state'], 'uncertain')
        self.assertEqual(self.state()['pending']['request_id'], request_id)
        self.assertEqual(self.post.call_args.args[2]['request_id'], request_id)

    def test_uncertain_blocks_new_grant_retry_same_id_and_local_resolution(self):
        self.post.side_effect = TimeoutError('private')
        self.assertEqual(self.consume(), {'outcome': 'uncertain', 'grant_id': 'grant1'})
        original = self.state()['pending']['request_id']
        self.assertEqual(self.consume('different')['code'], 'pending')
        self.now += 100
        self.post.side_effect = None
        self.assertEqual(self.consume()['outcome'], 'reset')
        self.assertEqual(self.post.call_args.args[2]['request_id'], original)
        self.assertEqual(self.post.call_count, 2)
        self.now += 600
        self.payload['cedar_ember']['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 1000)
        self.post.side_effect = TimeoutError()
        self.consume()
        self.now += 600
        self.assertEqual(self.consume()['code'], 'retry_expired')
        count = self.post.call_count
        self.daemon.resolve('not_reset')
        self.post.side_effect = None
        self.assertEqual(self.consume()['outcome'], 'reset')
        self.assertEqual(self.post.call_count, count + 1)
        self.assertNotEqual(self.post.call_args.args[2]['request_id'], original)

    def test_account_switch_refuses_uncertain_retry_without_post_and_keeps_resolution(self):
        for profile in ({'organization': {'uuid': 'other-org'}, 'account': {'uuid': 'user-private'}},
                        {'organization': {'uuid': 'org-private'}, 'account': {'uuid': 'other-user'}}):
            with self.subTest(profile=profile):
                self.profile.return_value = {'organization': {'uuid': 'org-private'},
                                             'account': {'uuid': 'user-private'}}
                self.prepare_attempt('retry')
                original = self.state()['pending']
                self.profile.return_value = profile
                self.daemon = self.new_daemon()
                self.profile.reset_mock()
                self.fetch.reset_mock()
                self.assertEqual(self.consume(), {'outcome': 'rejected', 'code': 'account_changed'})
                self.profile.assert_called_once_with(self.secret)
                self.fetch.assert_not_called()
                self.post.assert_not_called()
                self.assertEqual(self.state()['pending'], original)
                self.assertEqual(self.consume('different')['code'], 'pending')
                self.daemon.resolve('not_reset')
                self.assertIsNone(self.state()['pending'])

    def test_automatic_account_switch_publishes_off_and_enable_repins(self):
        self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': True}), {'automatic': True})
        self.assertEqual(self.state()['automatic_account'], self.fingerprint)
        self.profile.return_value = {'organization': {'uuid': 'other-org'}, 'user': {'id': 'other-user'}}
        for daemon in (self.daemon, self.new_daemon()):
            self.daemon = daemon
            self.daemon.tick()
            self.post.assert_not_called()
            status = self.daemon.dispatch({'action': 'status'})
            self.assertFalse(status['automatic'])
            self.assertIs(status['account_pin_match'], False)
            self.assertIs(self.state()['account_pin_match'], False)
            self.assertEqual(status['last_event']['code'], 'account_changed')
            public = json.loads(self.daemon.public.read_text())
            self.assertFalse(public['automatic'])
            self.assertEqual(public['last_event']['code'], 'account_changed')
            self.assertEqual(self.state()['automatic_account'], self.fingerprint)
        self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': True}), {'automatic': True})
        repinned = hashlib.sha256(b'["other-org","other-user"]').hexdigest()
        self.assertEqual(self.state()['automatic_account'], repinned)
        self.daemon.tick()
        self.post.assert_called_once()
        self.assertIn('/organizations/other-org/', self.post.call_args.args[1])
        for secret in (self.fingerprint, repinned, 'org-private', 'user-private', 'other-org', 'other-user'):
            self.assertNotIn(secret, self.daemon.public.read_text())

    def test_account_switch_between_tick_selection_and_consume_never_posts(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        self.profile.side_effect = [self.profile.return_value,
                                   {'organization': {'uuid': 'other-org'}, 'account': {'uuid': 'other-user'}}]
        self.daemon.tick()
        self.post.assert_not_called()
        self.assertIsNone(self.state()['pending'])
        public = json.loads(self.daemon.public.read_text())
        self.assertFalse(public['automatic'])
        self.assertEqual(public['last_event']['code'], 'account_changed')

    def test_legacy_operation_without_fingerprint_requires_local_resolution(self):
        for state in ('pending', 'uncertain'):
            with self.subTest(state=state):
                self.prepare_attempt('retry')
                saved = self.state()
                saved['pending'].pop('account_fingerprint')
                saved['pending']['state'] = state
                redeem.usage.publish(self.daemon.path, saved, 0o600)
                self.daemon = self.new_daemon()
                original = self.state()['pending']
                self.assertEqual(self.daemon.dispatch({'action': 'status'})['pending']['state'], 'uncertain')
                self.assertEqual(self.consume(), {'outcome': 'rejected', 'code': 'account_unbound'})
                self.daemon.tick()
                self.post.assert_not_called()
                self.assertEqual(self.state()['pending'], original)
                self.assertNotIn('account_fingerprint', self.state()['pending'])
                self.daemon.resolve('not_reset')
                self.assertIsNone(self.state()['pending'])

    def test_legacy_automatic_policy_requires_explicit_enable(self):
        self.change_state(automatic=True)
        saved = self.state()
        saved.pop('automatic_account')
        redeem.usage.publish(self.daemon.path, saved, 0o600)
        self.daemon = self.new_daemon()
        self.daemon.tick()
        self.post.assert_not_called()
        self.assertFalse(self.daemon.dispatch({'action': 'status'})['automatic'])
        public = json.loads(self.daemon.public.read_text())
        self.assertFalse(public['automatic'])
        self.assertEqual(public['last_event']['code'], 'account_unbound')
        self.assertNotIn('automatic_account', self.state())
        self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': True}), {'automatic': True})
        self.daemon.tick()
        self.post.assert_called_once()

    def test_unverifiable_account_keeps_automatic_off_and_failed_enable_preserves_pin(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        before = self.state()['automatic_account']
        self.profile.side_effect = TimeoutError('private provider failure')
        self.daemon.tick()
        self.post.assert_not_called()
        self.assertFalse(self.daemon.dispatch({'action': 'status'})['automatic'])
        self.assertFalse(json.loads(self.daemon.public.read_text())['automatic'])
        self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': True}),
                         {'outcome': 'rejected', 'code': 'provider'})
        self.assertEqual(self.state()['automatic_account'], before)
        self.profile.side_effect = None
        self.payload['cedar_ember']['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 1000)
        self.daemon.tick()
        self.assertTrue(self.daemon.dispatch({'action': 'status'})['automatic'])
        self.post.assert_not_called()

    def test_profile_without_user_id_binds_organization_and_keeps_hash_private(self):
        self.profile.return_value = {'organization': {'uuid': 'org-private'}}
        fingerprint = hashlib.sha256(b'["org-private",null]').hexdigest()
        self.post.side_effect = TimeoutError()
        self.consume()
        self.assertEqual(self.state()['pending']['account_fingerprint'], fingerprint)
        self.assertNotIn(fingerprint, self.daemon.public.read_text())
        self.post.side_effect = None
        self.assertEqual(self.consume()['outcome'], 'reset')

    def test_http_error_retry_does_not_clear_original_uncertainty(self):
        for status in range(400, 500):
            with self.subTest(status=status):
                before = self.prepare_attempt('retry')
                self.post.side_effect = HTTPError('https://api.anthropic.com', status, 'private', {}, None)
                self.assertEqual(self.consume()['outcome'], 'uncertain')
                self.assertEqual(self.state()['pending']['state'], 'uncertain')
                self.assertEqual(self.state()['pending']['request_id'], before['request_id'])
                self.assertEqual(self.state()['last_event']['code'], f'http_{status}')
                self.assertEqual(self.consume('different')['code'], 'pending')

    def test_first_attempt_http_client_outcomes(self):
        for mode in ('manual', 'auto'):
            for status in range(400, 500):
                with self.subTest(mode=mode, status=status):
                    self.prepare_attempt(mode)
                    self.post.side_effect = HTTPError('https://api.anthropic.com', status, 'private', {}, None)
                    expected = 'rejected' if status in (400, 401, 403, 404, 422) else 'uncertain'
                    result = self.attempt(mode)
                    self.assertEqual(result['outcome'], expected)
                    saved = self.state()
                    self.assertEqual(saved['last_event']['outcome'], expected)
                    self.assertEqual(saved['last_event']['code'], f'http_{status}')
                    self.assertEqual(saved, self.daemon.state)
                    if expected == 'rejected':
                        self.assertIsNone(saved['pending'])
                        if mode == 'manual':
                            self.assertEqual(result['code'], f'http_{status}')
                        self.post.side_effect = None
                        self.assertEqual(self.consume()['outcome'], 'reset')
                    else:
                        self.assertEqual(saved['pending']['state'], 'uncertain')

    def test_http_and_malformed_outcomes(self):
        for outcome, side_effect, result in (
                ('reset', None, {'result': 'reset'}),
                ('uncertain', None, {}),
                ('uncertain', None, {'success': True}),
                ('uncertain', HTTPError('https://api.anthropic.com', 500, self.secret, {}, None), {}),
                ('uncertain', HTTPError('https://api.anthropic.com', 503, self.secret, {}, None), {}),
                ('uncertain', HTTPError('https://api.anthropic.com', 599, self.secret, {}, None), {}),
                ('uncertain', TimeoutError(self.secret), {}),
                ('uncertain', ConnectionError(self.secret), {}),
                ('uncertain', ValueError(self.secret), {}),
                ('uncertain', None, []), ('uncertain', None, {'error': self.secret}),
                ('uncertain', None, {'type': 'error'})):
            self.change_state(pending=None, posts=[], last_success=None, cooldown_until=None)
            self.post.side_effect, self.post.return_value = side_effect, result
            self.assertEqual(self.consume()['outcome'], outcome)

    def test_provider_refusals_first_attempt_and_uncertain_retry(self):
        for mode in ('manual', 'auto', 'retry'):
            for value in ('already_used', 'not_limited', 'cooldown', 'ineligible', 'unavailable'):
                with self.subTest(mode=mode, value=value):
                    before = self.prepare_attempt(mode)
                    self.post.return_value = {'result': value}
                    result = self.attempt(mode)
                    expected = 'uncertain' if mode == 'retry' else 'rejected'
                    self.assertEqual(result['outcome'], expected)
                    saved = self.state()
                    self.assertEqual(saved, self.daemon.state)
                    self.assertEqual(saved['last_event']['outcome'], expected)
                    self.assertEqual(saved['last_event']['code'], 'result_' + value)
                    self.assertIsNone(saved['last_success'])
                    self.assertEqual(saved['episodes'], [])
                    if mode == 'retry':
                        self.assertEqual(saved['pending']['state'], 'uncertain')
                        self.assertEqual(saved['pending']['request_id'], before['request_id'])
                    else:
                        self.assertIsNone(saved['pending'])
                        if mode == 'manual':
                            self.assertEqual(result['code'], 'result_' + value)
                        # The same automatic episode is still available after refusal.
                        self.post.return_value = {'result': 'reset'}
                        self.assertEqual(self.attempt(mode)['outcome'], 'reset')
                        if mode == 'auto':
                            self.assertEqual(self.state()['episodes'], [['grant1', self.now + 250]])

    def test_unconfirmed_provider_bodies_remain_uncertain(self):
        bodies = [{}, {'success': True}, {'result': self.secret}, {'result': None},
                  {'result': []}, {'result': {}}, {'result': True}]
        for value in ('stamp_indeterminate', 'reset_unconfirmed'):
            bodies.append({'result': value})
            for result in ('reset', 'unavailable'):
                bodies.append({'result': result, 'reason': value})
        for mode in ('manual', 'auto', 'retry'):
            for body in bodies:
                with self.subTest(mode=mode, body=body):
                    before = self.prepare_attempt(mode)
                    self.post.return_value = body
                    self.assertEqual(self.attempt(mode)['outcome'], 'uncertain')
                    saved = self.state()
                    self.assertEqual(saved, self.daemon.state)
                    self.assertEqual(saved['pending']['state'], 'uncertain')
                    self.assertIsNone(saved['last_success'])
                    self.assertEqual(saved['episodes'], [])
                    value = body.get('reason', body.get('result'))
                    code = ('result_' + value if isinstance(value, str) and
                            value in ('stamp_indeterminate', 'reset_unconfirmed') else 'ambiguous')
                    self.assertEqual(saved['last_event']['code'], code)
                    if before:
                        self.assertEqual(saved['pending']['request_id'], before['request_id'])
                    self.assertNotIn(self.secret, self.daemon.public.read_text())
                    self.daemon = self.new_daemon()
                    self.assertEqual(self.state(), saved)
                    self.daemon.tick()
                    self.post.assert_called_once()

    def test_budget_cooldown_caps_and_limit_condition(self):
        block = self.payload['cedar_ember']
        block['at_limit'] = False
        self.assertEqual(self.consume()['code'], 'not_usable')
        del block['grants'][0]['use_requires_limit']
        self.assertEqual(self.consume()['code'], 'not_usable')
        self.post.assert_not_called()
        block['grants'][0]['use_requires_limit'] = False
        self.post.side_effect = HTTPError('https://api.anthropic.com', 400, 'private', {}, None)
        for _ in range(3):
            self.assertEqual(self.consume()['outcome'], 'rejected')
            self.assertEqual(self.state()['last_event']['code'], 'http_400')
        self.assertEqual(self.consume()['code'], 'budget')
        self.assertIsNone(self.state()['cooldown_until'])
        self.assertEqual(json.loads(self.daemon.public.read_text())['cooldown_until'], self.now + 600)
        self.now += 600
        self.assertIsNone(self.daemon.dispatch({'action': 'status'})['cooldown_until'])
        self.assertIsNone(json.loads(self.daemon.public.read_text())['cooldown_until'])
        block['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 300)
        self.post.side_effect = None
        block['cooldown_until'] = redeem.usage.stamp(self.now + 1)
        self.assertEqual(self.consume()['code'], 'cooldown')
        block['cooldown_until'] = None
        self.assertEqual(self.consume()['outcome'], 'reset')
        self.assertEqual(self.consume()['code'], 'cooldown')
        self.assertEqual(self.post.call_count, 4)

    def test_status_cooldown_combines_success_and_rolling_budget(self):
        self.change_state(last_success=self.now - 50, cooldown_until=self.now + 100,
                          posts=[self.now - 10, self.now - 100, self.now - 60, self.now - 700])
        self.assertEqual(self.daemon.dispatch({'action': 'status'})['cooldown_until'], self.now + 550)
        self.change_state(last_success=None, cooldown_until=None)
        self.assertEqual(self.daemon.dispatch({'action': 'status'})['cooldown_until'], self.now + 500)
        self.now += 500
        self.assertIsNone(self.daemon.dispatch({'action': 'status'})['cooldown_until'])

    def test_next_grant_and_eligibility_paused_usable_validation(self):
        block = self.payload['cedar_ember']
        block['next_grant_id'] = 'different'
        self.assertEqual(self.consume()['code'], 'next_grant')
        block['next_grant_id'] = None
        for parent, key, value in ((block, 'eligible', False), (block, 'eligible', None),
                                   (block['grants'][0], 'paused', True),
                                   (block['grants'][0], 'usable_now', False)):
            original = parent[key]
            parent[key] = value
            self.assertEqual(self.consume()['code'], 'not_usable')
            parent[key] = original
        self.post.assert_not_called()

    def test_response_cooldown_survives_restart_and_missing_or_stale_usage(self):
        deadline = self.now + 1800
        for value in (deadline, float(deadline), redeem.usage.stamp(deadline),
                      redeem.usage.stamp(deadline).replace('+00:00', 'Z')):
            for mode in ('manual', 'retry', 'auto'):
                for usage_cooldown in ('missing', None, deadline - 1801):
                    with self.subTest(value=value, mode=mode, usage=usage_cooldown):
                        self.now = deadline - 1800
                        block = self.payload['cedar_ember']
                        block['cooldown_until'] = None
                        block['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
                        self.prepare_attempt(mode)
                        self.post.return_value = {'result': 'reset', 'cooldown_until': value}
                        self.assertEqual(self.attempt(mode)['outcome'], 'reset')
                        self.assertEqual(self.state()['cooldown_until'], deadline)
                        self.assertEqual(json.loads(self.daemon.public.read_text())['cooldown_until'], deadline)
                        self.assertIsNone(self.state()['pending'])
                        self.daemon = self.new_daemon()
                        self.now += 601
                        block['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
                        if usage_cooldown == 'missing':
                            block.pop('cooldown_until')
                        else:
                            block['cooldown_until'] = redeem.usage.stamp(usage_cooldown) if usage_cooldown is not None else None
                        self.daemon.tick()
                        self.assertEqual(self.consume()['code'], 'cooldown')
                        self.post.assert_called_once()
                        self.now = deadline
                        block['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
                        block['cooldown_until'] = None
                        self.assertEqual(self.consume()['outcome'], 'reset')
                        self.assertEqual(self.post.call_count, 2)

    def test_missing_invalid_or_short_response_cooldown_uses_local_cap(self):
        responses = [{}] + [{'cooldown_until': value} for value in (
            None, True, False, -1, 0, self.now - 1, self.now + 1,
            redeem.usage.stamp(self.now + 1), float('nan'), float('inf'),
            float('-inf'), 10 ** 400, 253402300800, 'invalid', '',
            '2026-09-30T00:00:00', '1969-12-31T23:59:59Z',
            '9999-12-31T23:59:59-01:00', [], {})]
        for fields in responses:
            with self.subTest(fields=fields):
                self.now = 1790790000
                self.payload['cedar_ember']['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
                self.prepare_attempt('manual')
                self.post.return_value = {'result': 'reset', **fields}
                self.assertEqual(self.consume()['outcome'], 'reset')
                self.assertEqual(self.state()['cooldown_until'], self.now + 600)
                self.assertEqual(json.loads(self.daemon.public.read_text())['cooldown_until'], self.now + 600)
                self.daemon = self.new_daemon()
                self.now += 599
                self.payload['cedar_ember']['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
                self.assertEqual(self.consume()['code'], 'cooldown')
                self.post.assert_called_once()
                self.now += 1
                self.assertEqual(self.consume()['outcome'], 'reset')
                self.assertEqual(self.post.call_count, 2)

    def test_all_refusal_deadlines_block_manual_retry_and_auto_after_restart(self):
        start = self.now
        for result in redeem.REFUSAL_RESULTS:
            for mode in ('manual', 'retry', 'auto'):
                for encode in (lambda at: at, redeem.usage.stamp):
                    with self.subTest(result=result, mode=mode, encode=encode):
                        self.now = start
                        block = self.payload['cedar_ember']
                        block['cooldown_until'] = None
                        block['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
                        before = self.prepare_attempt(mode)
                        deadline = self.now + 1800
                        self.post.return_value = {'result': result, 'cooldown_until': encode(deadline)}
                        expected = 'uncertain' if mode == 'retry' else 'rejected'
                        self.assertEqual(self.attempt(mode)['outcome'], expected)
                        self.assertEqual(self.state()['cooldown_until'], deadline)
                        self.assertIsNone(self.state()['last_success'])
                        if before:
                            self.assertEqual(self.state()['pending']['request_id'], before['request_id'])
                        self.daemon = self.new_daemon()
                        block.pop('cooldown_until')
                        self.change_state(automatic=True)
                        self.daemon.tick()
                        self.assertEqual(self.consume()['code'], 'cooldown')
                        self.post.assert_called_once()
                        self.assertEqual(self.daemon.dispatch({'action': 'status'})['cooldown_until'], deadline)
                        self.assertEqual(json.loads(self.daemon.public.read_text())['cooldown_until'], deadline)

    def test_retry_after_seconds_and_http_date_survive_restart(self):
        start = self.now
        for value in ('120', formatdate(start + 120, usegmt=True)):
            for mode in ('manual', 'retry', 'auto'):
                with self.subTest(value=value, mode=mode):
                    self.now = start
                    self.prepare_attempt(mode)
                    self.post.side_effect = HTTPError('https://api.anthropic.com', 429, 'private',
                                                     {'Retry-After': value}, io.BytesIO(b'bad JSON'))
                    self.assertEqual(self.attempt(mode)['outcome'], 'uncertain')
                    self.assertEqual(self.state()['last_event']['code'], 'http_429')
                    self.assertEqual(self.state()['cooldown_until'], start + 120)
                    request_id = self.state()['pending']['request_id']
                    self.daemon = self.new_daemon()
                    self.now += 119
                    self.assertEqual(self.consume()['code'], 'cooldown')
                    self.daemon.tick()
                    self.post.assert_called_once()
                    self.assertEqual(json.loads(self.daemon.public.read_text())['cooldown_until'], start + 120)
                    self.now += 1
                    self.post.side_effect = None
                    self.assertEqual(self.consume()['outcome'], 'reset')
                    self.assertEqual(self.post.call_args.args[2]['request_id'], request_id)

    def test_deadline_max_merge_and_invalid_retry_after(self):
        for old, new, success, retry, expected in (
                (1800, 900, None, None, 1800),
                (900, 1800, None, None, 1800),
                (1800, 900, 0, None, 1800),
                (None, 20, 0, None, 600),
                (None, 1800, 0, None, 1800),
                (900, 1800, None, '2400', 2400),
                (None, None, None, '0', 0)):
            with self.subTest(old=old, new=new, success=success, retry=retry):
                candidate = {'cooldown_until': self.now + old if old is not None else None}
                self.daemon.merge_provider_deadline(candidate,
                    {'cooldown_until': self.now + new if new is not None else None}, retry,
                    self.now + success if success is not None else None)
                self.assertEqual(candidate['cooldown_until'], self.now + expected)
        for value in (None, True, 120, '', 'bad', '-1', '1.5', 'NaN', 'inf', '9' * 400,
                      'Fri, 01 Jan 10000 00:00:00 GMT'):
            with self.subTest(retry_after=value):
                self.assertIsNone(redeem.retry_after_deadline(value, self.now))

    def test_unknown_and_error_responses_still_preserve_constraints_and_deadlines(self):
        deadline = self.now + 120
        responses = [
            {'result': 'unknown', 'cooldown_until': deadline},
            {'result': 'reset', 'reason': 'stamp_indeterminate', 'cooldown_until': deadline},
            {'type': 'error', 'cooldown_until': deadline},
            HTTPError('https://api.anthropic.com', 429, 'private', {'Retry-After': '60'},
                      io.BytesIO(json.dumps({'cooldown_until': deadline}).encode()))]
        for response in responses:
            with self.subTest(response=response):
                self.prepare_attempt('manual')
                if isinstance(response, HTTPError):
                    self.post.side_effect = response
                else:
                    self.post.return_value = response
                self.assertEqual(self.consume()['outcome'], 'uncertain')
                self.assertEqual(self.state()['cooldown_until'], deadline)
                self.assertEqual(self.consume()['code'], 'cooldown')
                self.post.assert_called_once()

    def test_usage_deadline_is_published_by_the_shared_check(self):
        self.payload['cedar_ember']['cooldown_until'] = redeem.usage.stamp(self.now + 1200)
        self.assertEqual(self.consume()['code'], 'cooldown')
        self.assertEqual(self.daemon.dispatch({'action': 'status'})['cooldown_until'], self.now + 1200)
        self.assertEqual(json.loads(self.daemon.public.read_text())['cooldown_until'], self.now + 1200)
        self.post.assert_not_called()

    def test_refusal_deadline_retained_after_failed_outcome_save(self):
        deadline = self.now + 1200
        self.post.return_value = {'result': 'cooldown', 'cooldown_until': deadline}
        original = self.daemon.save
        def save():
            if self.daemon.state['last_event'] and self.daemon.state['last_event']['outcome'] == 'rejected':
                raise OSError('rejection save failed')
            original()
        with patch.object(self.daemon, 'save', side_effect=save):
            self.assertEqual(self.consume()['outcome'], 'uncertain')
        self.assertEqual(self.state()['cooldown_until'], deadline)
        self.daemon = self.new_daemon()
        self.assertEqual(self.consume()['code'], 'cooldown')
        self.post.assert_called_once()

    def test_response_constraints_survive_omission_and_clear_on_explicit_usage(self):
        original = deepcopy(self.payload['cedar_ember'])
        cases = [('eligible', False, 'program', 'not_usable'),
                 ('at_limit', False, 'program', 'not_usable'),
                 ('ineligible_reason', 'private reason', 'program', 'not_usable'),
                 ('next_grant_id', 'different', 'program', 'next_grant'),
                 ('usable_now', False, 'grant', 'not_usable'),
                 ('paused', True, 'grant', 'not_usable'),
                 ('resets_left', 0, 'grant', 'not_usable'),
                 ('ends_at', self.now - 1, 'grant', 'not_usable'),
                 ('use_requires_limit', True, 'grant', 'not_usable')]
        for key, value, scope, code in cases:
            with self.subTest(key=key):
                self.payload['cedar_ember'] = deepcopy(original)
                block = self.payload['cedar_ember']
                if key == 'use_requires_limit':
                    block['at_limit'] = False
                    block['grants'][0][key] = False
                self.change_state(provider_constraints={'program': {}, 'grants': {}})
                self.prepare_attempt('manual')
                self.post.return_value = {'result': 'unavailable', key: value}
                self.assertEqual(self.consume()['outcome'], 'rejected')
                self.assertNotIn('private reason', self.daemon.path.read_text())
                target = block if scope == 'program' else block['grants'][0]
                target.pop(key, None)
                if key == 'ineligible_reason':
                    block.pop('eligible')
                self.daemon = self.new_daemon()
                self.assertEqual(self.consume()['code'], code)
                self.change_state(automatic=True)
                self.daemon.tick()
                self.post.assert_called_once()
                self.assertNotIn('provider_constraints', json.loads(self.daemon.public.read_text()))
                self.payload['cedar_ember'] = deepcopy(original)
                self.payload['cedar_ember']['ineligible_reason'] = None
                self.post.return_value = {'result': 'reset'}
                self.assertEqual(self.consume()['outcome'], 'reset')
                self.assertEqual(self.post.call_count, 2)

    def test_usage_ineligibility_reason_and_empty_balance_block(self):
        block = self.payload['cedar_ember']
        block['ineligible_reason'] = 'private reason'
        self.assertEqual(self.consume()['code'], 'not_usable')
        block['ineligible_reason'] = None
        block['grants'][0]['resets_left'] = 0
        self.assertEqual(self.consume()['code'], 'not_usable')
        self.post.assert_not_called()

    def test_auto_window_one_success_per_episode_and_no_uncertain_retry(self):
        self.daemon.tick()
        self.post.assert_not_called()
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        g = self.payload['cedar_ember']['grants'][0]
        for end in (None, self.now, self.now + 301):
            g['ends_at'] = redeem.usage.stamp(end) if end is not None else None
            self.daemon.tick()
        self.post.assert_not_called()
        g['ends_at'] = redeem.usage.stamp(self.now + 300)
        self.daemon.tick()
        self.post.assert_called_once()
        self.change_state(last_success=None, cooldown_until=None, posts=[])
        self.daemon.tick()
        self.assertEqual(self.post.call_count, 1)
        # A different end time is a new episode.
        g['ends_at'] = redeem.usage.stamp(self.now + 299)
        self.post.side_effect = TimeoutError()
        self.daemon.tick()
        self.daemon.tick()
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.state()['pending']['state'], 'uncertain')
        self.daemon.resolve('reset')
        self.assertIn(['grant1', self.now + 299], self.state()['episodes'])

    def test_auto_selects_provider_next_grant_before_consuming(self):
        block = self.payload['cedar_ember']
        block['grants'].append({**block['grants'][0], 'id': 'grant2'})
        block['next_grant_id'] = 'grant2'
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        with patch.object(self.daemon, 'consume', wraps=self.daemon.consume) as consume:
            self.daemon.tick()
            consume.assert_called_once_with('grant2', automatic=True)
        self.assertEqual(self.post.call_args.args[2]['grant_id'], 'grant2')
        self.assertEqual(self.state()['episodes'], [['grant2', self.now + 250]])

        # Other near-expiry grants remain ineligible if the next grant is not due.
        self.change_state(last_success=None, cooldown_until=None, posts=[], episodes=[])
        block['grants'][1]['ends_at'] = redeem.usage.stamp(self.now + 301)
        self.daemon.tick()
        self.assertEqual(self.post.call_count, 1)

    def test_auto_missing_next_never_falls_back_to_another_grant(self):
        block = self.payload['cedar_ember']
        block['next_grant_id'] = None
        block['grants'].append({**block['grants'][0], 'id': 'grant2'})
        block['grants'][0]['paused'] = True
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        with patch.object(self.daemon, 'consume', wraps=self.daemon.consume) as consume:
            self.daemon.tick()
            consume.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.state()['posts'], [])

    def test_all_dispatch_paths_obey_client_rules_without_spending_budget(self):
        original = deepcopy(self.payload['cedar_ember'])
        cases = [
            # The recurring finding: a session grant cannot refill a weekly wall.
            ({'exhausted': ['seven_day']}, {'clears': ['five_hour'], 'blocking': ['seven_day']}),
            ({'exhausted': ['seven_day']}, {'clears': ['five_hour'], 'blocking': []}),
            ({'exhausted': []}, {}), ({'at_limit': False}, {}),
            ({'next_grant_id': None}, {}), ({}, {'resets_total': 0}),
            ({}, {'resets_left': 0}), ({}, {'paused': True}),
            ({}, {'usable_now': False}), ({}, {'ends_at': None}),
            ({}, {'ends_at': self.now + 250}),
            ({'cooldown_until': redeem.usage.stamp(self.now - 1)}, {})]
        for mode in ('manual', 'retry', 'auto'):
            for program_fields, grant_fields in cases:
                with self.subTest(mode=mode, program=program_fields, grant=grant_fields):
                    self.payload['cedar_ember'] = deepcopy(original)
                    self.prepare_attempt(mode)
                    before = self.state()
                    block = self.payload['cedar_ember']
                    block.update(program_fields)
                    block['grants'][0].update(grant_fields)
                    with patch.object(self.daemon, 'consume', wraps=self.daemon.consume) as consume:
                        if mode == 'auto':
                            for _ in range(4):
                                self.daemon.tick()
                            consume.assert_not_called()
                        else:
                            self.assertEqual(self.consume()['outcome'], 'rejected')
                    self.post.assert_not_called()
                    after = self.state()
                    self.assertEqual(after['posts'], before['posts'])
                    self.assertEqual(after['pending'], before['pending'])

    def test_uncovered_post_blocker_survives_omission_until_fresh_reconciliation(self):
        self.payload['cedar_ember']['grants'][0]['clears'] = ['five_hour']
        self.post.return_value = {'result': 'unavailable', 'blocking': ['seven_day']}
        self.assertEqual(self.consume()['outcome'], 'rejected')
        self.assertEqual(self.consume()['code'], 'not_usable')
        self.post.assert_called_once()
        self.payload['cedar_ember']['grants'][0]['blocking'] = []
        self.post.return_value = {'result': 'reset'}
        self.assertEqual(self.consume()['outcome'], 'reset')

    def test_saved_post_fields_cannot_lift_fresh_client_rejection(self):
        original = deepcopy(self.payload['cedar_ember'])
        cases = [('next_grant_id', 'grant1', 'program'),
                 ('exhausted', ['five_hour'], 'program'),
                 ('clears', ['five_hour', 'seven_day'], 'grant'),
                 ('resets_total', 3, 'grant'),
                 ('ends_at', redeem.usage.stamp(self.now + 250), 'grant')]
        for key, value, scope in cases:
            with self.subTest(key=key):
                self.payload['cedar_ember'] = deepcopy(original)
                self.prepare_attempt('manual')
                self.post.return_value = {'result': 'unavailable', key: value}
                self.assertEqual(self.consume()['outcome'], 'rejected')
                block = self.payload['cedar_ember']
                target = block if scope == 'program' else block['grants'][0]
                target.pop(key)
                self.assertEqual(self.consume()['code'], 'not_usable')
                self.change_state(automatic=True)
                self.daemon.tick()
                self.post.assert_called_once()
                self.assertEqual(len(self.state()['posts']), 1)

    def test_auto_publication_failure_recovers_durable_pending(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        original = self.daemon.publish
        for error_type in (OSError, RuntimeError):
            with self.subTest(error_type=error_type):
                self.change_state(pending=None, posts=[], last_event=None)
                persisted = []
                def fail_pending():
                    if self.daemon.state['pending']['state'] == 'pending':
                        persisted.append(self.state()['pending'])
                        raise error_type('publication failed')
                    original()
                with patch.object(self.daemon, 'publish', side_effect=fail_pending):
                    self.daemon.tick()
                saved = self.state()
                self.assertEqual(saved['pending']['state'], 'uncertain')
                self.assertEqual(saved['pending']['request_id'], persisted[0]['request_id'])
                self.assertEqual(saved['last_event']['outcome'], 'uncertain')
                self.assertEqual(json.loads(self.daemon.public.read_text())['pending']['state'], 'uncertain')
                self.daemon.tick()
                self.post.assert_not_called()
                self.daemon.resolve('not_reset')

    def test_auto_outcome_save_failure_retries_confirmed_success(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        original = self.daemon.save
        saves = []
        def fail_outcome():
            saves.append(self.daemon.state['pending'])
            if len(saves) == 2:
                raise OSError('outcome save failed')
            original()
        with patch.object(self.daemon, 'save', side_effect=fail_outcome):
            self.daemon.tick()
        self.post.assert_called_once()
        self.assertIsNone(saves[1])
        self.assertIsNone(self.state()['pending'])
        self.assertEqual(self.state()['last_success'], self.now)
        self.assertEqual(self.state()['episodes'], [['grant1', self.now + 250]])
        self.daemon.tick()
        self.post.assert_called_once()

    def test_auto_recovery_save_failure_retained_until_storage_recovers(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        original = self.daemon.save
        def fail_recovery():
            if self.daemon.state['pending']['state'] == 'uncertain':
                raise OSError('recovery save failed')
            original()
        with patch.object(self.daemon, 'publish', side_effect=OSError('publication failed')), \
                patch.object(self.daemon, 'save', side_effect=fail_recovery):
            self.daemon.tick()
            self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
            with self.assertRaises(OSError):
                self.daemon.tick()
        self.assertEqual(self.state()['pending']['state'], 'pending')
        self.post.assert_not_called()
        self.daemon.tick()
        self.assertEqual(self.state()['pending']['state'], 'uncertain')

    def prepare_attempt(self, mode):
        self.change_state(automatic=mode == 'auto', pending=None, posts=[],
                          last_success=None, cooldown_until=None, episodes=[], last_event=None)
        self.post.side_effect = None
        self.post.return_value = {'result': 'reset'}
        if mode == 'retry':
            self.post.side_effect = TimeoutError()
            self.consume()
            self.post.side_effect = None
        self.post.reset_mock()
        return self.state()['pending']

    def attempt(self, mode):
        if mode == 'auto':
            self.daemon.tick()
            return {'outcome': self.daemon.state['last_event']['outcome']}
        return self.consume()

    def test_attempt_save_and_publish_failure_matrix(self):
        # Include failures after replace: a failed save need not mean no file exists.
        faults = ('request_save', 'request_fsync', 'request_publish',
                  'outcome_save', 'outcome_fsync', 'outcome_publish')
        for mode in ('manual', 'retry', 'auto'):
            for fault in faults:
                for error_type in (OSError, RuntimeError):
                    with self.subTest(mode=mode, fault=fault, error=error_type):
                        before = self.prepare_attempt(mode)
                        original_save, original_publish = self.daemon.save, self.daemon.publish
                        saves, publishes = 0, 0
                        def save():
                            nonlocal saves
                            saves += 1
                            failed = ((fault.startswith('request_') and saves == 1) or
                                      (fault.startswith('outcome_') and saves == 2))
                            if failed and fault.endswith('save'):
                                raise error_type('save failed')
                            original_save()
                            if failed and fault.endswith('fsync'):
                                raise error_type('directory fsync failed')
                        def publish():
                            nonlocal publishes
                            publishes += 1
                            if fault.endswith('publish') and publishes == (1 if fault.startswith('request_') else 2):
                                raise error_type('publish failed')
                            original_publish()
                        with patch.object(self.daemon, 'save', side_effect=save), \
                                patch.object(self.daemon, 'publish', side_effect=publish):
                            if mode == 'auto' and fault == 'request_save':
                                self.daemon.tick()
                                result = None  # No operation was ever persisted.
                            else:
                                result = self.attempt(mode)
                        saved = self.state()
                        self.assertEqual(saved, self.daemon.state)
                        self.assertFalse(saved['pending'] and saved['pending']['state'] == 'pending')
                        sent = fault.startswith('outcome_')
                        self.assertEqual(self.post.call_count, int(sent))
                        if sent:
                            self.assertEqual(result['outcome'], 'reset')
                            self.assertEqual(saved['last_success'], self.now)
                            self.assertEqual(saved['last_event']['outcome'], 'reset')
                            self.assertIsNone(saved['pending'])
                            if mode == 'auto':
                                self.assertEqual(saved['episodes'], [['grant1', self.now + 250]])
                            if before:
                                self.assertEqual(self.post.call_args.args[2]['request_id'], before['request_id'])
                        elif mode == 'manual' and fault == 'request_save':
                            self.assertEqual(result['outcome'], 'rejected')
                            self.assertIsNone(saved['pending'])
                        elif result:
                            self.assertEqual(result['outcome'], 'uncertain')
                            if before:
                                self.assertEqual(saved['pending']['request_id'], before['request_id'])

    def test_definitive_refusal_save_and_publish_failures(self):
        for mode in ('manual', 'auto'):
            for status in (400, 401, 403, 404, 422, 'already_used', 'not_limited',
                           'cooldown', 'ineligible', 'unavailable'):
                for fault in ('outcome_save', 'persistent_save', 'outcome_fsync', 'outcome_publish'):
                    with self.subTest(mode=mode, status=status, fault=fault):
                        self.prepare_attempt(mode)
                        if isinstance(status, int):
                            self.post.side_effect = HTTPError('https://api.anthropic.com', status, 'private', {}, None)
                            code = f'http_{status}'
                        else:
                            self.post.return_value = {'result': status}
                            code = 'result_' + status
                        original_save, original_publish = self.daemon.save, self.daemon.publish
                        def save():
                            state = self.daemon.state
                            rejected = state['last_event'] and state['last_event']['outcome'] == 'rejected'
                            if fault == 'outcome_save' and rejected:
                                raise OSError('rejection save failed')
                            if fault == 'persistent_save' and not (state['pending'] and state['pending']['state'] == 'pending'):
                                raise OSError('storage unavailable')
                            original_save()
                            if fault == 'outcome_fsync' and rejected:
                                raise OSError('directory fsync failed')
                        def publish():
                            if fault == 'outcome_publish' and self.daemon.state['pending'] is None:
                                raise OSError('publish failed')
                            original_publish()
                        with patch.object(self.daemon, 'save', side_effect=save), \
                                patch.object(self.daemon, 'publish', side_effect=publish):
                            result = self.attempt(mode)
                            expected = 'uncertain' if fault in ('outcome_save', 'persistent_save') else 'rejected'
                            self.assertEqual(result['outcome'], expected)
                            self.assertEqual(self.daemon.state['last_event']['outcome'], expected)
                            self.assertEqual(self.daemon.state['last_event']['code'], code)
                            saved = self.state()
                            if expected == 'rejected':
                                self.assertIsNone(saved['pending'])
                                self.assertEqual(saved['last_event']['outcome'], 'rejected')
                            else:
                                self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
                                self.assertIsNotNone(saved['pending'])
                                self.assertEqual(saved['pending']['request_id'], self.daemon.state['pending']['request_id'])
                                if fault == 'persistent_save':
                                    self.assertEqual(saved['pending']['state'], 'pending')
                                    with self.assertRaises(OSError):
                                        self.daemon.dispatch({'action': 'status'})
                                else:
                                    self.assertEqual(saved['pending']['state'], 'uncertain')
                        self.daemon.dispatch({'action': 'status'})
                        self.assertEqual(self.state(), self.daemon.state)
                        self.assertFalse(self.state()['pending'] and self.state()['pending']['state'] == 'pending')
                        self.post.assert_called_once()

    def test_confirmed_outcome_retained_across_persistent_save_failure(self):
        for mode in ('manual', 'retry', 'auto'):
            with self.subTest(mode=mode):
                before = self.prepare_attempt(mode)
                deadline = self.now + 1800
                self.post.return_value = {'result': 'reset', 'cooldown_until': deadline}
                original = self.daemon.save
                def fail_outcome():
                    if self.daemon.state['last_success'] is not None:
                        raise OSError('storage unavailable')
                    original()
                with patch.object(self.daemon, 'save', side_effect=fail_outcome):
                    self.assertEqual(self.attempt(mode)['outcome'], 'reset')
                    durable_pending = self.state()['pending']
                    self.assertEqual(durable_pending['state'], 'pending')
                    self.assertIsNone(self.daemon.state['pending'])
                    self.assertEqual(self.daemon.state['last_success'], self.now)
                    self.assertEqual(self.daemon.state['cooldown_until'], deadline)
                    self.assertEqual(self.daemon.state['last_event']['outcome'], 'reset')
                    with self.assertRaises(OSError):
                        self.daemon.dispatch({'action': 'status'})
                    self.assertIsNone(self.daemon.state['pending'])
                    self.assertEqual(self.daemon.state['last_success'], self.now)
                self.daemon.dispatch({'action': 'status'})
                self.assertEqual(self.state(), self.daemon.state)
                self.assertIsNone(self.state()['pending'])
                self.assertEqual(self.state()['last_event']['outcome'], 'reset')
                self.assertEqual(self.state()['last_success'], self.now)
                self.assertEqual(self.state()['cooldown_until'], deadline)
                if before:
                    self.assertEqual(durable_pending['request_id'], before['request_id'])
                self.daemon.tick()
                self.post.assert_called_once()

    def test_provider_failure_and_unsaved_uncertainty_never_rejected(self):
        for mode in ('manual', 'retry', 'auto'):
            for provider_error in (TimeoutError(), HTTPError('https://api.anthropic.com', 409, 'private', {}, None)):
                with self.subTest(mode=mode, error=type(provider_error)):
                    self.prepare_attempt(mode)
                    self.post.side_effect = provider_error
                    original = self.daemon.save
                    def fail_uncertain():
                        if self.daemon.state['pending']['state'] == 'uncertain':
                            raise OSError('uncertain save failed')
                        original()
                    with patch.object(self.daemon, 'save', side_effect=fail_uncertain):
                        self.assertEqual(self.attempt(mode)['outcome'], 'uncertain')
                        self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
                        self.assertEqual(self.daemon.state['last_event']['outcome'], 'uncertain')
                        self.assertEqual(self.state()['pending']['state'], 'pending')
                    request_id = self.state()['pending']['request_id']
                    self.daemon.dispatch({'action': 'status'})
                    self.assertEqual(self.state()['pending']['state'], 'uncertain')
                    self.assertEqual(self.state()['pending']['request_id'], request_id)
                    self.post.assert_called_once()

    def test_abort_save_failure_never_leaves_live_pending(self):
        for mode in ('manual', 'retry', 'auto'):
            with self.subTest(mode=mode):
                self.write_credentials()
                self.payload['cedar_ember']['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
                self.prepare_attempt(mode)
                original_publish, original_save = self.daemon.publish, self.daemon.save
                def expire():
                    original_publish()
                    if self.daemon.state['pending']['state'] == 'pending':
                        self.now += 3600
                def fail_abort():
                    if self.daemon.state['pending']['state'] == 'uncertain':
                        raise OSError('abort save failed')
                    original_save()
                with patch.object(self.daemon, 'publish', side_effect=expire), \
                        patch.object(self.daemon, 'save', side_effect=fail_abort):
                    self.assertEqual(self.attempt(mode)['outcome'], 'uncertain')
                    self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
                self.post.assert_not_called()
                self.daemon.dispatch({'action': 'status'})
                self.assertEqual(self.state()['pending']['state'], 'uncertain')
                self.assertEqual(len(self.state()['posts']), int(mode == 'retry'))

    def test_request_publish_and_recovery_save_failure_matrix(self):
        for mode in ('manual', 'retry', 'auto'):
            with self.subTest(mode=mode):
                self.prepare_attempt(mode)
                original_save = self.daemon.save
                def fail_recovery():
                    if self.daemon.state['pending']['state'] == 'uncertain':
                        raise OSError('recovery save failed')
                    original_save()
                with patch.object(self.daemon, 'publish', side_effect=OSError('publication failed')), \
                        patch.object(self.daemon, 'save', side_effect=fail_recovery):
                    self.assertEqual(self.attempt(mode)['outcome'], 'uncertain')
                    self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
                    with self.assertRaises(OSError):
                        self.daemon.dispatch({'action': 'status'})
                    self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
                request_id = self.state()['pending']['request_id']
                self.daemon.tick()
                self.assertEqual(self.state()['pending']['state'], 'uncertain')
                self.assertEqual(self.state()['pending']['request_id'], request_id)
                self.post.assert_not_called()

    def test_deferred_success_preserves_external_policy_and_blocks_conflicting_resolution(self):
        self.prepare_attempt('manual')
        original_save = self.daemon.save
        def fail_outcome():
            if self.daemon.state['last_success'] is not None:
                raise OSError('storage unavailable')
            original_save()
        with patch.object(self.daemon, 'save', side_effect=fail_outcome):
            self.assertEqual(self.consume()['outcome'], 'reset')
        # Model another process changing policy under the same file lock.
        durable = self.state()
        durable['automatic'], durable['version'] = True, 7
        durable['automatic_account'] = self.fingerprint
        redeem.usage.publish(self.daemon.path, durable, 0o600)
        self.daemon.dispatch({'action': 'status'})
        self.assertTrue(self.state()['automatic'])
        self.assertEqual(self.state()['version'], 7)
        self.assertEqual(self.state()['last_event']['outcome'], 'reset')
        self.assertIsNone(self.state()['pending'])

        self.prepare_attempt('manual')
        with patch.object(self.daemon, 'save', side_effect=fail_outcome):
            self.assertEqual(self.consume()['outcome'], 'reset')
        durable = self.state()
        durable['pending'] = None
        durable['last_event'] = {'at': 'external', 'outcome': 'rejected',
                                 'code': 'local_not_reset', 'grant_id': 'grant1'}
        redeem.usage.publish(self.daemon.path, durable, 0o600)
        with self.assertRaisesRegex(OSError, 'state conflict'):
            self.daemon.dispatch({'action': 'status'})
        self.assertEqual(self.daemon.state['last_event']['outcome'], 'reset')
        self.assertEqual(self.state(), durable)
        self.post.assert_called_once()

    def test_rejection_event_save_failure_rolls_back(self):
        self.prepare_attempt('retry')
        before = self.state()
        with patch.object(self.daemon, 'save', side_effect=OSError('event save failed')), self.assertRaises(OSError):
            self.consume('different')
        self.assertEqual(self.daemon.state, before)
        self.assertEqual(self.state(), before)
        self.post.assert_not_called()

    def test_policy_failed_save_rolls_back_and_publication_does_not_mask_success(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.change_state(automatic=not enabled)
                before = self.state()
                original = self.daemon.save
                calls = 0
                def fail_policy():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError('policy save failed')
                    original()
                with patch.object(self.daemon, 'save', side_effect=fail_policy):
                    self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': enabled})['outcome'], 'rejected')
                self.assertEqual(self.state()['automatic'], before['automatic'])
                self.assertEqual(self.state()['version'], before['version'])
                self.assertEqual(self.daemon.state, self.state())
                with patch.object(self.daemon, 'publish', side_effect=OSError('public unavailable')):
                    self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': enabled}), {'automatic': enabled})
                self.assertEqual(self.state()['automatic'], enabled)
                self.assertGreater(self.state()['version'], before['version'])

    def test_startup_recovery_failure_retains_uncertainty(self):
        self.prepare_attempt('manual')
        original = self.daemon.publish
        def crash():
            if self.daemon.state['pending']:
                raise SystemExit('crash')
            original()
        with patch.object(self.daemon, 'publish', side_effect=crash), self.assertRaises(SystemExit):
            self.consume()
        request_id = self.state()['pending']['request_id']
        with patch.object(redeem.Redeemer, 'save', side_effect=OSError('recovery save failed')):
            self.daemon = self.new_daemon()
            self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
            with self.assertRaises(OSError):
                self.daemon.tick()
            self.assertEqual(self.daemon.state['pending']['state'], 'uncertain')
        self.daemon.dispatch({'action': 'status'})
        self.assertEqual(self.state()['pending']['state'], 'uncertain')
        self.assertEqual(self.state()['pending']['request_id'], request_id)
        self.post.assert_not_called()

    def test_resolve_failed_save_rolls_back_and_failed_publish_preserves_resolution(self):
        for outcome in ('reset', 'not_reset'):
            with self.subTest(outcome=outcome):
                self.prepare_attempt('retry')
                before = self.state()
                with patch.object(self.daemon, 'save', side_effect=OSError('resolve save failed')), self.assertRaises(OSError):
                    self.daemon.resolve(outcome)
                self.assertEqual(self.daemon.state, before)
                self.assertEqual(self.state(), before)
                with patch.object(self.daemon, 'publish', side_effect=OSError('public unavailable')):
                    self.daemon.resolve(outcome)
                self.assertIsNone(self.state()['pending'])
                self.assertEqual(self.state()['last_event']['code'], 'local_' + outcome)
                self.assertEqual(self.daemon.state, self.state())
                self.post.assert_not_called()

    def test_status_publication_failure_preserves_state_and_response(self):
        before = self.daemon.dispatch({'action': 'status'})
        with patch.object(self.daemon, 'publish', side_effect=OSError('public unavailable')):
            self.assertEqual(self.daemon.dispatch({'action': 'status'}), before)
            self.daemon.tick()
        self.assertIsNone(self.daemon.state['pending'])
        self.post.assert_not_called()

    def test_master_lock_and_expired_token_never_auto_dispatch(self):
        self.daemon.master.touch()
        self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': True}), {'error': 'locked'})
        self.assertTrue(self.daemon.dispatch({'action': 'status'})['automatic_locked'])
        self.daemon.master.unlink()
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        self.profile.reset_mock()
        self.write_credentials(expiry=self.now + 60)
        self.daemon.tick()
        self.fetch.assert_not_called()
        self.profile.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.consume()['code'], 'expired_token')
        self.write_credentials()
        self.daemon.master.touch()
        self.daemon.tick()
        self.post.assert_not_called()
        self.assertFalse(self.daemon.dispatch({'action': 'status'})['automatic'])
        self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': False}), {'automatic': False})

    def test_expiry_and_master_rechecked_after_durable_write_before_post(self):
        original = self.daemon.publish
        def expire():
            original()
            if self.daemon.state['pending']:
                self.now += 3600
        with patch.object(self.daemon, 'publish', side_effect=expire):
            self.assertEqual(self.consume()['outcome'], 'uncertain')
        self.post.assert_not_called()
        self.assertEqual(self.state()['pending']['state'], 'uncertain')
        self.assertEqual(self.state()['last_event']['code'], 'expired_token')
        self.assertEqual(self.state()['posts'], [])
        self.daemon.resolve('not_reset')
        self.write_credentials()
        self.payload['cedar_ember']['grants'][0]['ends_at'] = redeem.usage.stamp(self.now + 250)
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        def lock_master():
            original()
            if self.daemon.state['pending']:
                self.daemon.master.touch()
        with patch.object(self.daemon, 'publish', side_effect=lock_master):
            self.daemon.tick()
        self.post.assert_not_called()
        self.assertEqual(self.state()['pending']['state'], 'uncertain')
        self.assertEqual(self.state()['last_event']['code'], 'locked')
        self.assertEqual(self.state()['posts'], [])

    def test_policy_version_older_enable_cannot_override_later_disable(self):
        arrived, release = threading.Event(), threading.Event()
        original = self.daemon.transaction
        @contextmanager
        def transaction():
            if threading.current_thread().name == 'old-enable':
                arrived.set()
                release.wait(2)
            with original():
                yield
        with patch.object(self.daemon, 'transaction', transaction):
            old = threading.Thread(name='old-enable', target=lambda: self.daemon.dispatch({'action': 'policy', 'enabled': True}))
            old.start()
            self.assertTrue(arrived.wait(2))
            self.assertEqual(self.daemon.dispatch({'action': 'policy', 'enabled': False}), {'automatic': False})
            release.set()
            old.join(2)
        self.assertFalse(old.is_alive())
        self.assertFalse(self.state()['automatic'])
        self.assertEqual(self.state()['version'], 2)
        self.daemon = self.new_daemon()
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        self.assertEqual(self.state()['version'], 3)

    def test_disable_serialized_with_dispatch_and_profile_not_cached(self):
        self.daemon.dispatch({'action': 'policy', 'enabled': True})
        entered, release = threading.Event(), threading.Event()
        def post(*_):
            entered.set()
            release.wait(3)
            return {'result': 'reset'}
        self.post.side_effect = post
        auto = threading.Thread(target=self.daemon.tick)
        auto.start()
        self.assertTrue(entered.wait(2))
        done = threading.Event()
        off = threading.Thread(target=lambda: (self.daemon.dispatch({'action': 'policy', 'enabled': False}), done.set()))
        off.start()
        self.assertFalse(done.wait(.05))
        release.set()
        auto.join(2)
        off.join(2)
        self.assertTrue(done.is_set())
        self.change_state(posts=[], last_success=None, cooldown_until=None)
        g = self.payload['cedar_ember']['grants'][0]
        g['ends_at'] = redeem.usage.stamp(redeem.usage.epoch(g['ends_at']) + 1)
        self.daemon.tick()
        self.post.assert_called_once()
        self.consume()
        self.assertEqual(self.profile.call_count, 4)

    def test_secret_and_org_never_published_or_logged(self):
        output = io.StringIO()
        self.post.side_effect = TimeoutError(self.secret)
        with redirect_stdout(output), redirect_stderr(output):
            self.consume()
        published = self.daemon.public.read_text() + self.daemon.path.read_text() + output.getvalue()
        for secret in (self.secret, 'FAKE_REFRESH_SECRET', 'org-private', 'user-private', 'accessToken', 'refreshToken'):
            self.assertNotIn(secret, published)
        self.assertNotIn(self.fingerprint, self.daemon.public.read_text())
        self.assertEqual(set(json.loads(self.daemon.public.read_text())),
                         {'checked_at', 'automatic', 'automatic_locked', 'pending', 'last_event', 'cooldown_until',
                          'account_pin_match', 'account_pin_checked_at'})

    def test_provider_echo_in_grant_rejected_without_publishing_token(self):
        self.payload['cedar_ember']['grants'][0]['id'] = self.secret
        self.assertEqual(self.consume()['code'], 'provider')
        self.assertNotIn(self.secret, self.daemon.public.read_text())
        self.post.assert_not_called()

    def test_corrupt_private_state_fails_closed(self):
        self.daemon.path.write_text('{}')
        with self.assertRaises(ValueError):
            self.consume()
        self.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
