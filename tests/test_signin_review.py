import base64
import copy
import json
from pathlib import Path
import subprocess
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
from signin_fixture import FakeGateway, jwt


class ReviewRegressionTests(unittest.TestCase):
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
        self.addCleanup(self.manager.close)
        transport = patch.object(app, 'api', side_effect=self.gateway.call)
        transport.start()
        self.addCleanup(transport.stop)
        self.manager.changed = Mock()

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
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 3)
        return thread, failures

    def test_plan_change_to_free_during_enable_succeeds_without_compensation(self):
        for signin_helper in (False, True):
            with self.subTest(signin_helper=signin_helper):
                gateway = FakeGateway()
                self.addCleanup(gateway.close)
                def downgrade(path, body=None, method=None, timeout=None):
                    if path == '/auth-files/status':
                        gateway.row['id_token']['plan_type'] = 'free'
                    return gateway.call(path, body, method)
                with patch.object(app, 'api', side_effect=downgrade), patch.object(self.state, 'signin_changed'):
                    if signin_helper:
                        access.enable_after_signin(gateway.row, downgrade)
                    else:
                        self.state.set_routing_enabled('account-a', gateway.identity, True)
                self.assertFalse(gateway.row['disabled'])
                self.assertEqual([body['disabled'] for body in gateway.patches], [False])

    def test_preexisting_enabled_free_stays_enabled_at_both_entrypoints(self):
        self.gateway.row.update(disabled=False, status_message='')
        self.gateway.row['id_token']['plan_type'] = 'free'
        with patch.object(self.state, 'signin_changed'):
            self.state.set_routing_enabled('account-a', self.gateway.identity, True)
        access.enable_after_signin(self.gateway.row, self.gateway.call)
        self.assertFalse(self.gateway.row['disabled'])
        self.assertTrue(all(body['disabled'] is False for body in self.gateway.patches))
        with self.assertRaisesRegex(signin.AccessError, 'signin_not_required'):
            self.start()

    def test_unknown_readiness_cannot_waive_last_account_confirmation(self):
        self.gateway.row['disabled'] = False
        unknown = {'id': 'account-a', 'active': False, 'availability': {'state': 'provider_unavailable'}}
        with patch.object(app, 'fetch_account', return_value=unknown):
            with self.assertRaisesRegex(signin.AccessError, 'last_ready'):
                self.state.set_routing_enabled('account-a', self.gateway.identity, False)
        self.assertFalse(self.gateway.patches)

    def test_negative_identity_matrix_writes_nothing(self):
        claims = signin.decode(jwt())
        missing = []
        for field in ('iss', 'aud', 'exp', 'email'):
            value = copy.deepcopy(claims)
            value.pop(field)
            missing.append(value)
        workspace = copy.deepcopy(claims)
        workspace[signin.AUTH].pop('chatgpt_account_id')
        missing.append(workspace)
        missing.append(dict(claims, email='One@example.com'))
        session = self.start()
        for value in missing:
            token = 'synthetic.' + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=') + '.synthetic'
            with self.subTest(fields=sorted(value)), self.assertRaises(signin.IdentityError):
                self.manager.save(session, dict(self.gateway.tokens, id_token=token))
        self.assertFalse(self.gateway.field_patches)
        self.assertFalse(self.gateway.patches)

    def test_outcome_churn_is_bounded_globally_expired_and_retired(self):
        now = [1800000000]
        self.manager.clock = lambda: now[0]
        for index in range(300):
            session = {'account_id': str(index), 'identity': str(index), 'stop': threading.Event()}
            self.manager.sessions[str(index)] = session
            self.manager.finish(session, 'save_failed')
        self.assertLessEqual(len(self.manager.outcomes), 128)
        now[0] += 601
        self.manager.view('unrelated-account', 'unrelated-identity')
        self.assertFalse(self.manager.outcomes)
        session = self.start()
        self.manager.finish(session, 'save_failed')
        self.manager.prune([dict(self.gateway.row, auth_index='replacement')])
        self.assertFalse(self.manager.outcomes)


    def test_start_join_cancel_are_serialized_real_threads(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.manager.relay
        def paused_relay(url, data, form=False, call=None):
            if url == signin.USERCODE:
                entered.set()
                self.assertTrue(release.wait(3))
            return original(url, data, form, call=call)
        self.manager.relay = paused_relay
        first, failures = self.worker(lambda: self.manager.start('account-a', self.gateway.identity))
        self.assertTrue(entered.wait(2))
        joined = []
        second, second_failures = self.worker(lambda: joined.append(self.manager.start('account-a', self.gateway.identity)))
        second.join(1)
        self.assertFalse(second.is_alive())
        release.set()
        first.join(3)
        second.join(3)
        self.assertEqual(failures + second_failures, [])
        handle = self.manager.view('account-a', self.gateway.identity)['session']['handle']
        self.assertEqual(sum(bool(body) and body.get('url') == signin.USERCODE
                             for _, body, _ in self.gateway.calls), 1)
        canceller, cancel_failures = self.worker(lambda: self.manager.cancel('account-a', self.gateway.identity, handle))
        canceller.join(3)
        self.assertEqual(cancel_failures, [])
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.gateway.field_patches or self.gateway.patches)

    def test_off_and_cancel_before_completion_win_real_threads(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.gateway.row['status_message'] = 'refresh_token_invalidated'
                session = self.start()
                session['state'] = 'pending' if cancel else 'finishing'
                waiting, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                def completion():
                    waiting.set()
                    self.assertTrue(release.wait(3))
                    self.manager.complete(session, self.gateway.tokens)
                thread, failures = self.worker(completion)
                self.assertTrue(waiting.wait(2))
                with patch.object(self.state, 'signin_changed'):
                    if cancel:
                        self.manager.cancel('account-a', self.gateway.identity, session['handle'])
                    else:
                        self.state.set_routing_enabled('account-a', self.gateway.identity, False, True)
                release.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertTrue(self.gateway.row['disabled'])

    def test_poll_and_reset_refresh_wait_for_same_mutation_lock(self):
        for operation in (self.state._fetch, lambda: self.state.after_reset('account-a')):
            with self.subTest(operation=str(operation)), patch.object(routing, 'reconcile'):
                self.gateway.calls.clear()
                with self.state.lifecycle_lock:
                    worker, failures = self.worker(operation)
                    self.assertTrue(worker.is_alive())
                    time.sleep(0.03)
                    self.assertFalse(self.gateway.calls)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])

    def test_completion_callback_runs_after_import_lock_release(self):
        def callback(_):
            completed = threading.Event()
            def acquire():
                with self.state.lifecycle_lock:
                    completed.set()
            worker, failures = self.worker(acquire)
            self.assertTrue(completed.wait(2))
            worker.join(3)
            self.assertEqual(failures, [])
        self.manager.changed = callback
        self.manager.complete(self.start(), self.gateway.tokens)


    def test_cancel_during_inflight_start_immediately_cancels_real_session(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.manager.relay
        def paused(url, data, form=False, call=None):
            if url == signin.USERCODE:
                entered.set()
                self.assertTrue(release.wait(3))
            return original(url, data, form, call=call)
        self.manager.relay = paused
        starter, failures = self.worker(lambda: self.manager.start('account-a', self.gateway.identity))
        self.assertTrue(entered.wait(2))
        session = self.manager.sessions['account-a']
        cancelled = threading.Event()
        def cancel():
            self.manager.cancel('account-a', self.gateway.identity, session['handle'])
            cancelled.set()
        canceller, cancel_failures = self.worker(cancel)
        self.assertTrue(cancelled.wait(1))
        release.set()
        starter.join(3)
        canceller.join(3)
        self.assertFalse(starter.is_alive() or canceller.is_alive())
        self.assertEqual(failures + cancel_failures, [])
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.manager.sessions or self.gateway.field_patches or self.gateway.patches)


if __name__ == '__main__':
    unittest.main()
