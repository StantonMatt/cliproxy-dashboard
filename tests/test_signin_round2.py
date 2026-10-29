"""Lock recovery and CLIProxyAPI v8.0.7 management-contract regressions."""
import contextlib
import copy
import fcntl
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import account_access as access
import routing_policy as routing
import server as app
import signin
from signin_fixture import FakeGateway, auth_file_row, jwt
from test_signin import ClockEvent


class RuntimeContractTests(unittest.TestCase):
    def test_fixture_projects_only_upstream_management_fields(self):
        """v8.0.7 internal/api/handlers/management/auth_files.go:655-785,907-934."""
        gateway = FakeGateway()
        self.addCleanup(gateway.close)
        self.assertNotIn('prefix', gateway.row)
        self.assertNotIn('access_token', gateway.row)
        self.assertNotIn('refresh_token', gateway.row)
        self.assertNotIn('unknown_metadata', gateway.row)
        self.assertEqual(set(gateway.row['id_token']), {'plan_type', 'chatgpt_account_id'})
        self.assertIs(type(gateway.row['disabled']), bool)
        self.assertIs(type(gateway.row['priority']), int)
        for value in (' 100 ', '+100', 100, 100.9):
            with self.subTest(priority=value):
                row = auth_file_row(dict(gateway.saved, priority=value), 'test.json', 'test')
                self.assertEqual(row['priority'], 100)
                self.assertIs(type(row['priority']), int)
        row = auth_file_row({'email': 'one@example.com'}, 'test.json', 'test')
        self.assertEqual((row['type'], row['provider']), ('unknown', 'unknown'))
        self.assertIs(row['disabled'], False)
        self.assertNotIn('priority', row)
        self.assertNotIn('id_token', row)

    def test_effective_defaults_and_priority_types(self):
        for priority, runtime in ((None, 0), ('100', 100), (' +100 ', 100), (100.9, 100),
                                  ('-100', -100), (-100.9, -100)):
            with self.subTest(priority=priority):
                self.assertTrue(access.runtime_matches({'disabled': False, 'priority': runtime},
                    {'disabled': None, 'priority': priority}))
        self.assertTrue(access.runtime_matches({'disabled': False}, {'disabled': False, 'priority': 0}))
        for disabled in (None, False, 'false', 'true', 1):
            with self.subTest(disabled=disabled):
                self.assertTrue(access.runtime_matches({'disabled': False}, {'disabled': disabled}))
        for runtime in ({}, {'disabled': 0}, {'disabled': 'false'}, {'disabled': None}):
            self.assertFalse(access.runtime_matches(runtime, {'disabled': False}))
        for priority in (True, 'bad', float('inf')):
            self.assertFalse(access.runtime_matches({'priority': 100}, {'priority': priority}))
        self.assertFalse(access.runtime_matches({}, {'priority': 100}))
        self.assertFalse(access.runtime_matches({}, {'prefix': 'acct-a'}))

    def test_selected_claim_comparisons_fail_closed(self):
        expected = {'id_token': {'plan_type': 'pro', 'chatgpt_account_id': 'workspace-a',
                                'chatgpt_subscription_active_until': 1800000000}}
        self.assertTrue(access.runtime_matches(copy.deepcopy(expected), expected))
        for field in expected['id_token']:
            observed = copy.deepcopy(expected)
            observed['id_token'].pop(field)
            self.assertFalse(access.runtime_matches(observed, expected))


class RoundTwoRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        isolation = patch.object(routing, 'STATE', self.root)
        isolation.start()
        self.addCleanup(isolation.stop)
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.state = app.GatewayState(Mock(), Mock(), Mock())
        self.manager = self.state.signins
        self.manager.changed = Mock()
        self.addCleanup(self.manager.close)
        transport = patch.object(app, 'api', side_effect=self.gateway.call)
        transport.start()
        self.addCleanup(transport.stop)

    def start(self):
        with patch.object(signin.threading, 'Thread'):
            self.manager.start('account-a', self.gateway.identity)
        return self.manager.sessions['account-a']

    def worker(self, action):
        failures = []
        def run():
            try:
                action()
            except Exception as error:
                failures.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(worker.join, 3)
        return worker, failures

    @contextlib.contextmanager
    def held_file_lock(self):
        with (self.root / 'controller.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def assert_worker_finished(self, worker, failures):
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

    def assert_paid(self, session):
        self.manager.complete(session, self.gateway.tokens)
        self.assertEqual(self.manager.view('account-a', session['identity'])['last_outcome']['kind'], 'signed_in')
        self.assertEqual(len(self.gateway.field_patches), 1)


    def test_subscription_claims_are_projected_and_verified(self):
        claims = signin.decode(self.gateway.tokens['id_token'])[signin.AUTH]
        claims.update(chatgpt_subscription_active_start='2026-10-01T00:00:00Z',
                      chatgpt_subscription_active_until=1800000000)
        self.gateway.tokens['id_token'] = jwt(**{signin.AUTH: claims})
        self.assert_paid(self.start())
        self.assertEqual(self.gateway.row['id_token']['chatgpt_subscription_active_until'], 1800000000)


    def test_every_status_entrypoint_requires_boolean_runtime_readback(self):
        for entrypoint in ('signin', 'routing'):
            for malformed in (None, 0, 'true'):
                with self.subTest(entrypoint=entrypoint, malformed=malformed):
                    gateway = FakeGateway()
                    self.addCleanup(gateway.close)
                    def corrupt(path, body=None, method=None, timeout=None):
                        result = gateway.call(path, body, method)
                        if path == '/auth-files/status':
                            if malformed is None:
                                gateway.row.pop('disabled')
                            else:
                                gateway.row['disabled'] = malformed
                        return result
                    with patch.object(app, 'api', side_effect=corrupt):
                        with self.assertRaises(ValueError):
                            if entrypoint == 'routing':
                                self.state.set_routing_enabled('account-a', gateway.identity, True)
                            else:
                                access.enable_after_signin(gateway.row, corrupt)
                    self.assertEqual(len(gateway.patches), 1)

    def test_poll_thread_launch_failure_clears_running_and_retries(self):
        for constructor_fails in (False, True):
            with self.subTest(constructor_fails=constructor_fails):
                self.state.last_attempt = 0
                with patch.object(app.threading, 'Thread') as launcher:
                    if constructor_fails:
                        launcher.side_effect = RuntimeError('synthetic thread failure')
                    else:
                        launcher.return_value.start.side_effect = RuntimeError('synthetic thread failure')
                    self.assertFalse(self.state.refresh())
                self.assertFalse(self.state.running)
        self.state.last_attempt = 0
        with patch.object(app.threading, 'Thread'):
            self.assertTrue(self.state.refresh())
        self.state._fetch()
        self.assertFalse(self.state.running)

    def test_signin_worker_launch_failure_discards_session_and_allows_retry(self):
        for constructor_fails in (False, True):
            with self.subTest(constructor_fails=constructor_fails):
                with patch.object(signin.threading, 'Thread') as launcher:
                    if constructor_fails:
                        launcher.side_effect = RuntimeError('synthetic thread failure')
                    else:
                        launcher.return_value.start.side_effect = RuntimeError('synthetic thread failure')
                    result = self.manager.start('account-a', self.gateway.identity)
                self.assertIsNone(result['session'])
                self.assertEqual(result['last_outcome']['kind'], 'start_failed')
                self.assertFalse(self.manager.sessions)
        self.assertEqual(self.start()['state'], 'pending')


    def test_failed_session_lock_body_releases_both_locks_for_retry(self):
        with patch.object(self.manager, 'view', side_effect=ValueError('synthetic view failure')):
            with self.assertRaises(ValueError):
                self.manager.start('account-a', self.gateway.identity)
        self.assertFalse(self.manager.sessions)
        def acquire():
            with self.state.lifecycle_lock:
                acquired = self.manager.lock.acquire(timeout=0.1)
                if acquired:
                    self.manager.lock.release()
                self.assertTrue(acquired)
        worker, failures = self.worker(acquire)
        self.assert_worker_finished(worker, failures)
        self.assertEqual(self.start()['state'], 'pending')

    def test_failed_cancel_transport_preserves_session_and_allows_retry(self):
        session = self.start()
        with patch.object(self.manager, 'resolve', side_effect=TimeoutError('synthetic read timeout')):
            with self.assertRaises(TimeoutError):
                self.manager.cancel('account-a', self.gateway.identity, session['handle'])
        self.assertIsNotNone(self.manager.view('account-a', self.gateway.identity)['session'])
        self.manager.cancel('account-a', self.gateway.identity, session['handle'])
        self.assertFalse(self.manager.sessions)


    def test_nested_lifecycle_body_error_preserves_outer_lock_then_releases(self):
        acquired = threading.Event()
        def acquire():
            with self.state.lifecycle_lock:
                acquired.set()
        with self.state.lifecycle_lock:
            with self.assertRaises(ValueError):
                with self.state.lifecycle_lock:
                    raise ValueError('synthetic body failure')
            worker, failures = self.worker(acquire)
            self.assertFalse(acquired.wait(0.04))
        self.assert_worker_finished(worker, failures)
        self.assertTrue(acquired.is_set())

    def test_cancel_resolve_does_not_hold_in_memory_session_lock(self):
        session = self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        resolve = self.manager.resolve
        def paused_resolve(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('synthetic resolve timeout')
            return resolve(*args)
        with patch.object(self.manager, 'resolve', side_effect=paused_resolve):
            worker, failures = self.worker(lambda: self.manager.cancel(
                'account-a', self.gateway.identity, session['handle']))
            try:
                self.assertTrue(entered.wait(2))
                acquired = self.manager.lock.acquire(timeout=0.1)
                if acquired:
                    self.manager.lock.release()
                self.assertTrue(acquired)
                self.assertIsNotNone(self.manager.view('account-a', self.gateway.identity)['session'])
            finally:
                release.set()
            self.assert_worker_finished(worker, failures)
        self.assertFalse(self.manager.sessions)


if __name__ == '__main__':
    unittest.main()
