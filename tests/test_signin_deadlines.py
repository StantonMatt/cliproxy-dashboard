"""Synthetic intent barriers and monotonic account-request deadlines."""
import copy
import fcntl
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import server as app
import signin
from request_deadline import RequestDeadline
from signin_fixture import FakeGateway


class AccountDeadlineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.state = app.GatewayState(Mock(), Mock(), Mock())
        self.manager = self.state.signins
        self.manager.call = self.gateway.call
        self.manager.run = Mock()
        self.manager.changed = Mock()
        self.state.signin_changed = Mock()
        self.addCleanup(self.manager.close)
        self.now = [0.0]
        self.timeouts = []
        for context in (patch.object(app, 'api', side_effect=self.gateway.call),
                        patch.object(app.routing_policy, 'STATE', self.root)):
            context.start()
            self.addCleanup(context.stop)

    def deadline(self, seconds=45):
        return RequestDeadline(seconds, clock=lambda: self.now[0])

    def delayed(self, path, body=None, method=None, timeout=None):
        self.timeouts.append(timeout)
        self.now[0] += min(18, timeout)
        return self.gateway.call(path, body, method)

    def start(self):
        self.manager.start('account-a', self.gateway.identity)
        return self.manager.sessions['account-a']

    def stalled_start(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        results, errors = [], []
        original = self.manager.resolve
        def resolve(*args, **kwargs):
            record = original(*args, **kwargs)
            if threading.current_thread().name == 'stalled-start':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Start barrier was not released')
            return record
        context = patch.object(self.manager, 'resolve', side_effect=resolve)
        context.start()
        self.addCleanup(context.stop)
        def run():
            try:
                results.append(self.manager.start('account-a', self.gateway.identity))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run, name='stalled-start')
        worker.start()
        self.addCleanup(worker.join, 4)
        self.assertTrue(entered.wait(2))
        return worker, release, results, errors

    def test_off_before_initial_lookup_registers_cannot_be_undone_by_paid_approval(self):
        worker, release, results, errors = self.stalled_start()
        self.state.set_routing_enabled('account-a', self.gateway.identity, False, True)
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNotNone(results[0]['session'])
        session = self.manager.sessions['account-a']
        self.assertFalse(session['enable'])
        self.manager.complete(session, self.gateway.tokens)
        self.assertTrue(self.gateway.row['disabled'])
        self.assertFalse(any(not patch['disabled'] for patch in self.gateway.patches))

    def test_cancel_joined_session_does_not_let_older_start_recreate_it(self):
        worker, release, results, errors = self.stalled_start()
        session = self.start()
        self.manager.cancel('account-a', self.gateway.identity, session['handle'])
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(results[0]['session'])
        self.assertFalse(self.manager.sessions)
        self.assertEqual(sum(body and body.get('url') == signin.USERCODE
                             for _, body, _ in self.gateway.calls if body), 1)

    def test_new_start_after_off_is_a_new_intent_and_later_off_still_wins(self):
        self.state.set_routing_enabled('account-a', self.gateway.identity, False, True)
        session = self.start()
        self.assertTrue(self.manager.can_enable(session))
        self.state.set_routing_enabled('account-a', self.gateway.identity, True)
        self.assertFalse(self.gateway.row['disabled'])
        self.state.set_routing_enabled('account-a', self.gateway.identity, False, True)
        self.assertFalse(self.manager.can_enable(session))
        self.manager.complete(session, self.gateway.tokens)
        self.assertTrue(self.gateway.row['disabled'])

    def test_stale_cancel_is_not_an_accepted_cancellation(self):
        session = self.start()
        with self.assertRaisesRegex(signin.AccessError, 'session_changed'):
            self.manager.cancel('account-a', self.gateway.identity, 'stale-handle')
        self.assertTrue(self.manager.can_enable(session))

    def test_start_has_one_deadline_and_does_not_launch_after_expiry(self):
        self.manager.call = self.delayed
        with self.assertRaises(TimeoutError):
            self.state.start_signin('account-a', self.gateway.identity, deadline=self.deadline())
        self.assertEqual(self.timeouts, [18, 18, 9])
        self.assertEqual(self.now[0], 45)
        self.assertEqual([path for path, _, _ in self.gateway.calls],
                         ['/auth-files', '/auth-files/download?' + urlencode({'name': self.gateway.row['name']}), '/config'])
        self.assertFalse(self.manager.sessions)
        self.assertEqual(self.manager.view('account-a', self.gateway.identity)['last_outcome']['kind'], 'start_failed')
        self.manager.run.assert_not_called()

    def test_cancel_lookup_and_session_lock_share_deadline(self):
        session = self.start()
        self.manager.call = self.delayed
        with self.assertRaises(TimeoutError):
            self.manager.cancel('account-a', self.gateway.identity, session['handle'], deadline=self.deadline(3))
        self.assertEqual(self.timeouts, [3])
        self.assertTrue(self.manager.active(session))

    def test_lifecycle_wait_reduces_remaining_management_timeout(self):
        lock = Mock()
        def acquire(timeout):
            self.assertEqual(timeout, 10)
            self.now[0] += 10
            return True
        lock.acquire.side_effect = acquire
        self.state.lifecycle_lock = lock
        with patch.object(app, 'api', side_effect=self.delayed), self.assertRaises(TimeoutError):
            self.state.set_routing_enabled('account-a', self.gateway.identity, False, True, deadline=self.deadline())
        self.assertEqual(self.timeouts, [18, 17])
        lock.release.assert_called_once_with()
        self.assertFalse(self.gateway.patches)

    def test_expiry_after_lock_acquisition_releases_without_any_call(self):
        lock = Mock()
        def acquire(timeout):
            self.now[0] += timeout
            return True
        lock.acquire.side_effect = acquire
        self.state.lifecycle_lock = lock
        with self.assertRaises(TimeoutError):
            self.state.set_routing_enabled('account-a', self.gateway.identity, False, True, deadline=self.deadline(2))
        lock.release.assert_called_once_with()
        self.assertFalse(self.gateway.calls)

    def test_controller_flock_wait_counts_against_whole_request(self):
        with (self.root / 'controller.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            started = time.monotonic()
            with self.assertRaises(app.routing_policy.Hold):
                self.state.set_routing_enabled('account-a', self.gateway.identity, False, True,
                                               deadline=RequestDeadline(.02))
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.gateway.patches)

    def test_quota_calls_share_deadline_and_expiry_does_not_become_last_ready(self):
        self.gateway.row['disabled'] = False
        self.gateway.other_rows = [dict(copy.deepcopy(self.gateway.row), name='second.json',
                                       auth_index='account-b', email='two@example.com')]
        def call(path, body=None, method=None, timeout=None):
            self.timeouts.append(timeout)
            self.now[0] += min(9, timeout)
            return self.gateway.call(path, body, method)
        with patch.object(app, 'api', side_effect=call), self.assertRaises(TimeoutError):
            self.state.set_routing_enabled('account-a', self.gateway.identity, False, deadline=self.deadline())
        self.assertEqual(self.timeouts, [18, 18, 18, 18, 9])
        self.assertEqual(self.now[0], 45)
        self.assertFalse(self.gateway.patches)

    def test_expired_deadline_never_invokes_transport(self):
        deadline = self.deadline(0)
        call = Mock()
        with self.assertRaises(TimeoutError):
            deadline.wrap(call)('/auth-files/status', {}, method='PATCH')
        call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
