"""Token-only fields PATCH regressions against the pinned, synthetic gateway."""
import copy
import datetime as dt
import fcntl
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import routing_policy as routing
import server as app
import signin
from signin_fixture import NAME, FakeGateway, auth_file_row, jwt
from codex_fixture import use_codex_pair


def setUpModule():
    use_codex_pair()


class FieldsImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.isolation = patch.object(routing, 'STATE', self.root)
        self.isolation.start()
        self.addCleanup(self.isolation.stop)
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.manager = signin.SigninManager(self.gateway.call, threading.RLock(), Mock())
        self.addCleanup(self.manager.close)

    def outcome(self):
        return self.manager.view('account-a', self.gateway.identity)['last_outcome']

    def start(self):
        with patch.object(signin.threading, 'Thread'):
            self.manager.start('account-a', self.gateway.identity)
        return self.manager.sessions['account-a']

    def test_patch_allowlist_and_live_metadata_preservation_with_stale_disk(self):
        live = {'priority': 100, 'prefix': 'live-prefix', 'weight': 2, 'websockets': True,
                'request_retry': 4, 'note': 'live note', 'project_id': 'project',
                'quota_probe': {'enabled': True}, 'unknown_metadata': {'keep': [1, 2]}}
        self.gateway.runtime.update(copy.deepcopy(live))
        self.gateway.row['priority'] = 100
        self.gateway.saved.update(priority=0, disabled=False, prefix='stale-prefix')
        self.gateway.tokens.update(email='untrusted', type='untrusted', priority=-100)
        session = self.start()
        self.manager.suppress_enable('account-a')
        self.manager.complete(session, self.gateway.tokens)
        body, = self.gateway.field_patches
        self.assertEqual(set(body), {'name', 'access_token', 'refresh_token', 'id_token',
                                     'expired', 'last_refresh', 'plan_type'})
        self.assertEqual(body['name'], NAME)
        for field, expected in live.items():
            self.assertEqual(self.gateway.saved[field], expected, field)
            self.assertNotIn(field, body)
        self.assertTrue(self.gateway.saved['disabled'])
        self.assertEqual(self.gateway.row['priority'], 100)
        self.assertEqual(self.gateway.row['auth_index'], 'account-a')
        self.assertEqual(self.gateway.row['last_refresh'], '2025-12-31T00:00:00Z')
        self.assertFalse(self.gateway.patches)
        dt.datetime.strptime(body['last_refresh'], '%Y-%m-%dT%H:%M:%SZ')
        self.assertEqual(self.outcome(), {'kind': 'signed_in', 'at': ANY, 'enabled': False})

    def test_prefix_omitted_disabled_and_string_or_fractional_priority_remain_valid(self):
        for priority in (' +100 ', 23.9, None):
            with self.subTest(priority=priority):
                self.gateway.runtime.pop('disabled', None)
                self.gateway.runtime.pop('priority', None)
                if priority is not None:
                    self.gateway.runtime['priority'] = priority
                self.gateway.row = auth_file_row(self.gateway.runtime, NAME, 'account-a')
                self.gateway.row.update(path=self.gateway.path, status_message='refresh_token_invalidated')
                self.manager.complete(self.start(), self.gateway.tokens)
                self.assertNotIn('prefix', self.gateway.row)
                self.assertEqual(self.gateway.saved['prefix'], 'acct-a')
                self.assertEqual(self.gateway.saved.get('priority'), priority)
                self.assertFalse(self.gateway.saved['disabled'])

    def test_plan_metadata_is_explicitly_replaced_for_model_gating(self):
        self.gateway.runtime['plan_type'] = self.gateway.saved['plan_type'] = 'free'
        self.manager.complete(self.start(), self.gateway.tokens)
        self.assertEqual(self.gateway.runtime_plan, 'pro')
        self.assertEqual(self.gateway.saved['plan_type'], 'pro')
        self.assertFalse(self.gateway.saved['disabled'])

    def test_unconfirmed_runtime_keeps_saved_tokens_without_enable_or_rollback(self):
        self.gateway.stale_patches = 2
        session = self.start()
        self.manager.complete(session, self.gateway.tokens)
        self.assertEqual(self.outcome()['kind'], 'saved_unconfirmed')
        self.assertEqual(self.gateway.saved['refresh_token'], self.gateway.tokens['refresh_token'])
        self.assertEqual(self.gateway.runtime['access_token'], 'FAKE_OLD_ACCESS')
        self.assertEqual(len(self.gateway.field_patches), 2)
        self.assertFalse(self.gateway.patches)

    def test_final_enable_must_be_persisted_too(self):
        original = self.manager.call
        def lose_status_save(path, body=None, method=None, timeout=None):
            result = original(path, body, method)
            if path == '/auth-files/status' and not body['disabled']:
                self.gateway.saved['disabled'] = True
            return result
        self.manager.call = lose_status_save
        self.manager.complete(self.start(), self.gateway.tokens)
        # Saved and confirmed; an unpersisted enable leaves routing off, not a failed sign-in.
        self.assertEqual(self.outcome(), {'kind': 'signed_in', 'at': ANY, 'enabled': False, 'enable_failed': True})
        self.assertEqual(len(self.gateway.field_patches), 1)

    def test_free_import_preserves_preference_until_verified_enable(self):
        self.gateway.tokens['id_token'] = jwt(plan='free')
        self.manager.complete(self.start(), self.gateway.tokens)
        body, = self.gateway.field_patches
        self.assertNotIn('disabled', body)
        self.assertFalse(self.gateway.row['disabled'] or self.gateway.saved['disabled'])
        self.assertEqual([body['disabled'] for body in self.gateway.patches], [False])

    def test_identity_replacement_after_fields_patch_fails_without_enable(self):
        original = self.manager.call
        def replace_identity(path, body=None, method=None, timeout=None):
            result = original(path, body, method)
            if path == '/auth-files/fields':
                self.gateway.row['auth_index'] = 'replacement'
            return result
        self.manager.call = replace_identity
        self.manager.complete(self.start(), self.gateway.tokens)
        self.assertEqual(self.manager.outcomes['account-a']['kind'], 'saved_unconfirmed')
        self.assertFalse(self.gateway.patches)

    def test_config_preflight_runs_once_not_per_provider_poll(self):
        session = self.start()
        self.gateway.proxy = 'http://changed-after-start.invalid'
        self.manager.relay(signin.DEVICE_TOKEN, {'device_auth_id': session['device_auth_id'],
                                               'user_code': session['user_code']})
        self.assertEqual(sum(path == '/config' for path, _, _ in self.gateway.calls), 1)

    def test_flock_covers_patch_and_readback_but_not_initial_download(self):
        original = self.manager.call
        imported = False
        def inspect(path, body=None, method=None, timeout=None):
            nonlocal imported
            imported = imported or path == '/auth-files/fields'
            with (self.root / 'controller.lock').open('a') as lock:
                if imported:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(path, body, method)
        self.manager.call = inspect
        self.manager.complete(self.start(), self.gateway.tokens)

    def test_external_flock_timeout_preserves_save_without_later_enable(self):
        script = ('import fcntl,sys; handle=open(sys.argv[1],"a"); '
                  'fcntl.flock(handle,fcntl.LOCK_EX); print("locked",flush=True); sys.stdin.read()')
        session = self.start()
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root / 'controller.lock')],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with patch.object(routing.time, 'monotonic', side_effect=[0, 11]):
                self.manager.complete(session, self.gateway.tokens)
            self.assertEqual(self.outcome()['kind'], 'saved_unconfirmed')
            self.assertFalse(self.gateway.field_patches or self.gateway.patches)
        finally:
            child.communicate('', timeout=3)
        # Polling never turns this informational outcome into a later enable.
        self.gateway.row['status_message'] = ''
        calls = len(self.gateway.calls)
        with self.assertRaisesRegex(signin.AccessError, 'signin_not_required'):
            self.manager.start('account-a', self.gateway.identity)
        self.assertFalse(any(c[0] == '/api-call' and c[1]['url'].startswith('https://auth.openai.com')
                             for c in self.gateway.calls[calls:]))
        self.assertEqual(self.outcome(), {'kind': 'saved_unconfirmed', 'at': ANY})
        self.assertFalse(self.gateway.field_patches or self.gateway.patches)
        self.assertEqual(self.gateway.saved['access_token'], 'FAKE_NEW_ACCESS')

    def test_overlapping_controller_cannot_replace_token_patch_clone(self):
        self.gateway.row['priority'] = self.gateway.runtime['priority'] = 100
        self.gateway.row['email'] = self.gateway.runtime['email'] = self.gateway.saved['email'] = routing.EMAILS[0]
        self.gateway.saved['id_token'] = jwt(email=routing.EMAILS[0])
        self.gateway.tokens['id_token'] = jwt(email=routing.EMAILS[0])
        self.gateway.other_rows = [dict(copy.deepcopy(self.gateway.row), name='second.json',
                                       auth_index='account-b', email=routing.EMAILS[1])]
        def controller(path, body=None, method=None, timeout=None):
            if path == '/config':
                return {'routing': {'strategy': 'round-robin'}}
            if path == '/auth-files/fields' and body['name'] == 'second.json':
                self.gateway.other_rows[0]['priority'] = body['priority']
                return {}
            return self.gateway.call(path, body, method)
        def overlap():
            with self.assertRaises(routing.Hold):
                routing.reconcile({}, {'mode': 'round-robin'}, controller, self.root)
        self.gateway.fields_hook = overlap
        self.manager.complete(self.start(), self.gateway.tokens)
        self.gateway.fields_hook = None
        routing.reconcile({}, {'mode': 'round-robin'}, controller, self.root)
        self.assertEqual(self.gateway.runtime['priority'], 0)
        self.assertEqual(self.gateway.saved['refresh_token'], self.gateway.tokens['refresh_token'])

    def test_cancellation_during_import_download_is_already_finishing(self):
        session = self.start()
        def cancel():
            with self.assertRaisesRegex(signin.AccessError, 'already_finishing'):
                self.manager.cancel('account-a', self.gateway.identity, session['handle'])
        self.gateway.download_hook = cancel
        self.manager.complete(session, self.gateway.tokens)
        self.assertEqual(len(self.gateway.field_patches), 1)

    def test_fetch_lock_failure_clears_running_and_next_refresh_recovers(self):
        state = app.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        state.running = True
        lock = state.lifecycle_lock
        broken = Mock()
        broken.__enter__ = Mock(side_effect=TimeoutError('synthetic lock failure'))
        broken.__exit__ = Mock(return_value=False)
        state.lifecycle_lock = broken
        state._fetch()
        self.assertFalse(state.running)
        state.lifecycle_lock = lock
        self.gateway.row['disabled'] = False
        self.gateway.row['id_token']['plan_type'] = 'free'
        with patch.object(app, 'api', side_effect=self.gateway.call), patch.object(routing, 'reconcile'):
            state._fetch()
        self.assertTrue(state.data['online'])
        self.assertFalse(self.gateway.row['disabled'])

    def test_start_and_cancel_do_not_acquire_lifecycle_lock(self):
        self.manager.lifecycle = Mock()
        self.manager.lifecycle.__enter__ = Mock(side_effect=AssertionError('unexpected lifecycle lock'))
        session = self.start()
        self.manager.cancel('account-a', self.gateway.identity, session['handle'])
        self.assertFalse(self.manager.sessions)

    def test_routing_refresh_is_background_and_lock_wait_is_bounded(self):
        state = app.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        state.lifecycle_lock = Mock()
        state.lifecycle_lock.acquire.return_value = False
        with self.assertRaises(TimeoutError):
            state.set_routing_enabled('account-a', self.gateway.identity, False, True)
        state.lifecycle_lock.acquire.assert_called_once_with(timeout=10)
        self.assertFalse(self.gateway.calls)
        state.lifecycle_lock = threading.RLock()
        with patch.object(app, 'api', side_effect=self.gateway.call), patch.object(app.threading, 'Thread') as launcher:
            self.assertEqual(state.set_routing_enabled('account-a', self.gateway.identity, False, True), {'enabled': False})
        launcher.assert_called_once_with(target=state.signin_changed, args=('account-a',), daemon=True)
        launcher.return_value.start.assert_called_once_with()

    def test_off_downgrade_after_quota_checks_stays_within_seven_calls(self):
        self.gateway.row['disabled'] = False
        self.gateway.other_rows = [dict(copy.deepcopy(self.gateway.row), name='second.json',
                                       auth_index='account-b', email='two@example.com')]
        state = app.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        def quota(record, call):
            call('/api-call', {'url': 'https://chatgpt.com/backend-api/wham/usage'})
            if record['auth_index'] == 'account-b':
                self.gateway.row['id_token']['plan_type'] = 'free'
            return {'id': record['auth_index'], 'active': True, 'availability': {'state': 'ready'}}
        with patch.object(app, 'api', side_effect=self.gateway.call), patch.object(app, 'fetch_account', side_effect=quota), patch.object(app.threading, 'Thread'):
            self.assertEqual(state.set_routing_enabled('account-a', self.gateway.identity, False), {'enabled': False})
        self.assertEqual(len(self.gateway.calls), 7)
        self.assertTrue(self.gateway.row['disabled'])
        self.assertEqual([body['disabled'] for body in self.gateway.patches], [True])
