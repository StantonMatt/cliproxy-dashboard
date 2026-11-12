from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
import copy
import json
from pathlib import Path
import sys
import threading
import tempfile
import unittest
from unittest.mock import ANY, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import signin
import account_access
import server as app
from signin_fixture import FakeGateway, jwt
from request_deadline import RequestDeadline


def setUpModule():
    from codex_fixture import use_codex_pair
    use_codex_pair()
    directory = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(directory.cleanup)
    previous = app.routing_policy.STATE
    app.routing_policy.STATE = Path(directory.name)
    unittest.addModuleCleanup(setattr, app.routing_policy, 'STATE', previous)


class ClockEvent:
    def __init__(self, clock):
        self.clock, self.stopped, self.waits, self.hook = clock, False, [], None

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock[0] += seconds
        if self.hook:
            self.hook(seconds)
        return self.stopped


class SigninTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.clock = [1800000000.0]
        self.changed = Mock()
        self.manager = signin.SigninManager(self.gateway.call, threading.RLock(), self.changed,
                                            clock=lambda: self.clock[0])
        self.addCleanup(self.manager.close)

    def start(self):
        with patch.object(signin.threading, 'Thread'):
            result = self.manager.start('account-a', self.gateway.identity)
        if result['session']:
            self.session = self.manager.sessions['account-a']
            self.event = ClockEvent(self.clock)
            self.session['stop'] = self.event
        return result

    def run_flow(self):
        self.start()
        self.manager.run(self.session)
        return self.manager.view('account-a', self.session['identity'])['last_outcome']

    def assert_no_writes(self):
        self.assertEqual(self.gateway.field_patches, [])
        self.assertEqual(self.gateway.patches, [])

    def test_start_join_cancel_from_another_device_and_opaque_handle(self):
        first = self.start()
        second = self.manager.start('account-a', self.gateway.identity)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first['session']['handle']), 32)
        self.assertEqual(first['session']['verification_url'], signin.VERIFY + '?user_code=ABCD-EFGH')
        self.assertEqual(sum(bool(c[1]) and c[1].get('url') == signin.USERCODE for c in self.gateway.calls), 1)
        with self.assertRaises(signin.AccessError):
            self.manager.cancel('account-a', self.gateway.identity, 'a-different-session-handle')
        self.manager.cancel('account-a', self.gateway.identity, first['session']['handle'])
        self.assertIsNone(self.manager.view('account-a', self.gateway.identity)['session'])
        self.assertTrue(self.event.is_set())
        self.assert_no_writes()

    def test_expiry_cleanup_outcomes_and_restart(self):
        self.start()
        self.clock[0] += 901
        result = self.manager.view('account-a', self.gateway.identity)
        self.assertEqual(result['last_outcome']['kind'], 'expired')
        self.assertTrue(self.event.is_set())
        self.clock[0] += 600
        self.assertIsNone(self.manager.view('account-a', self.gateway.identity)['last_outcome'])
        self.start()
        restarted = signin.SigninManager(self.gateway.call, threading.RLock(), self.changed)
        self.addCleanup(restarted.close)
        self.assertNotEqual(self.manager.instance, restarted.instance)
        self.assertIsNone(restarted.view('account-a', self.gateway.identity)['session'])
        self.manager.close()
        self.assertTrue(self.event.is_set())
        self.assertEqual(self.manager.sessions, {})
        self.assert_no_writes()

    def test_no_signin_on_healthy_card_but_observed_401_is_allowed(self):
        self.gateway.row.update(disabled=False, status_message='')
        with self.assertRaises(signin.AccessError):
            self.start()
        with patch.object(signin.threading, 'Thread'):
            result = self.manager.start('account-a', self.gateway.identity, observed_required=True)
        self.assertIsNotNone(result['session'])

    def test_nonempty_proxy_refuses_without_relay(self):
        self.gateway.proxy = 'http://proxy.invalid'
        self.assertEqual(self.start()['last_outcome']['kind'], 'start_failed')
        self.assertFalse(any(c[0] == '/api-call' for c in self.gateway.calls))
        self.assert_no_writes()

    def test_interval_defaults_and_upstream_alternate_usercode(self):
        for interval in (None, 'nonsense', -4, 0):
            with self.subTest(interval=interval):
                self.manager.close()
                self.manager = signin.SigninManager(self.gateway.call, threading.RLock(), self.changed,
                                                    clock=lambda: self.clock[0])
                self.gateway.code.update(interval=interval, usercode='ABCD-EFGH')
                self.gateway.code.pop('user_code', None)
                self.start()
                self.assertEqual(self.session['interval'], 5)

    def test_pending_polling_uses_server_interval_then_paid_enables_via_helper(self):
        self.gateway.code['interval'] = '7'
        self.gateway.polls = [(403, {}), (404, {}), (400, {'error': 'authorization_pending'}),
                              (200, {'authorization_code': 'FAKE_CODE', 'code_verifier': 'FAKE_VERIFIER'})]
        with patch.object(account_access, 'enable_after_signin', wraps=account_access.enable_after_signin) as enable:
            outcome = self.run_flow()
        self.assertEqual(outcome['kind'], 'signed_in')
        self.assertEqual(self.event.waits, [7, 7, 7, 7])
        enable.assert_called_once()
        self.assertFalse(self.gateway.row['disabled'])
        self.changed.assert_called_once_with('account-a')


    def test_free_signin_enables_unless_explicit_off_supersedes(self):
        for superseded in (False, True):
            with self.subTest(superseded=superseded):
                self.setUp()
                self.gateway.tokens['id_token'] = jwt(plan='free')
                self.start()
                if superseded:
                    self.manager.suppress_enable('account-a')
                self.manager.run(self.session)
                outcome = self.manager.view('account-a', self.session['identity'])['last_outcome']
                self.assertEqual(outcome['kind'], 'signed_in')
                self.assertEqual(outcome['enabled'], not superseded)
                self.assertEqual(self.gateway.row['disabled'], superseded)
                self.assertNotIn('disabled', self.gateway.field_patches[0])

    def test_new_plan_tokens_validate_save_and_enable(self):
        for plan in ('prolite', 'future_super_plan', 'unknown', 'free'):
            with self.subTest(plan=plan):
                self.setUp()
                self.gateway.tokens['id_token'] = jwt(plan=plan)
                self.assertEqual(signin.validate_tokens(self.gateway.tokens, self.gateway.saved,
                                                        self.gateway.row, self.clock[0]), plan)
                outcome = self.run_flow()
                self.assertEqual(outcome['kind'], 'signed_in')
                self.assertTrue(outcome['enabled'])
                self.assertFalse(self.gateway.row['disabled'])
                self.assertEqual(self.gateway.row['id_token']['plan_type'], plan)
                self.assertEqual(self.gateway.saved['plan_type'], plan)

    def test_identity_rejections_write_nothing(self):
        cases = [
            ('wrong_account', jwt(email='other@example.com')),
            ('wrong_workspace', jwt(workspace='workspace-other')),
            ('invalid_identity', jwt(iss='https://evil.invalid')),
            ('invalid_identity', jwt(aud='wrong-client')),
            ('invalid_identity', jwt(exp=1)),
            ('invalid_identity', jwt(exp=True)),
            ('invalid_identity', jwt(exp=float('inf'))),
            ('invalid_identity', jwt(sub='different-user')),
            ('invalid_identity', jwt(**{signin.AUTH: {'chatgpt_account_id': 'workspace-a', 'chatgpt_plan_type': 'pro'}})),
            *[('invalid_identity', jwt(plan=plan)) for plan in (None, '', '   ', [], {}, 123, True)],
        ]
        for expected, token in cases:
            with self.subTest(expected=expected, token_type=expected):
                self.setUp()
                self.gateway.tokens['id_token'] = token
                self.assertEqual(self.run_flow()['kind'], expected)
                self.assert_no_writes()

    def test_missing_saved_account_id_refuses_before_requesting_code(self):
        self.gateway.saved['id_token'] = jwt(**{signin.AUTH: {'chatgpt_plan_type': 'pro'}})
        self.assertEqual(self.start()['last_outcome']['kind'], 'identity_unavailable')
        self.assertFalse(any(c[0] == '/api-call' for c in self.gateway.calls))
        self.assert_no_writes()

    def test_identity_rechecked_after_approval(self):
        self.start()
        self.gateway.row['email'] = 'replacement@example.com'
        self.manager.run(self.session)
        result = self.manager.view('account-a', self.session['identity'])
        self.assertEqual(result['last_outcome']['kind'], 'save_failed')
        self.assert_no_writes()

    def test_legitimate_post_import_refresh_is_never_overwritten(self):
        def refresh_after_completion(_):
            self.gateway.saved.update(id_token=jwt(jti='legitimate-refresh'),
                                      access_token='FAKE_ROTATED_ACCESS', refresh_token='FAKE_ROTATED_REFRESH')
        self.changed.side_effect = refresh_after_completion
        self.assertEqual(self.run_flow()['kind'], 'signed_in')
        self.assertEqual(self.gateway.saved['refresh_token'], 'FAKE_ROTATED_REFRESH')
        self.assertEqual(len(self.gateway.field_patches), 1)
        self.assertEqual(self.event.waits, [5])

    def test_saved_fields_and_runtime_attestation_report_unconfirmed_without_rollback(self):
        for field in ('access_token', 'refresh_token', 'id_token', 'expired', 'last_refresh', 'plan_type',
                      'listing', 'runtime'):
            with self.subTest(field=field):
                self.setUp()
                original, replace = self.gateway.call, signin.credential_replace.replace
                def corrupt(path, body=None, method=None, timeout=None):
                    result = original(path, body, method)
                    if method == 'PATCH' and path == '/auth-files/fields' and field == 'listing':
                        self.gateway.row['id_token'] = dict(self.gateway.row['id_token'], plan_type='team')
                    return result
                def overwrite(*args, **kwargs):
                    replace(*args, **kwargs)
                    if field not in ('listing', 'runtime'):
                        self.gateway.saved[field] = 'FAKE_CORRUPTION'
                self.manager.call = corrupt
                self.gateway.stale_patches = 2 if field == 'runtime' else 0
                with patch.object(signin.credential_replace, 'replace', side_effect=overwrite):
                    self.assertEqual(self.run_flow()['kind'], 'saved_unconfirmed')
                self.assertFalse(self.gateway.patches)
                self.assertEqual(len(self.gateway.field_patches), 0 if field not in ('listing', 'runtime') else 2)


    def test_off_during_pending_or_finishing_suppresses_enable(self):
        for when in ('pending', 'finishing'):
            with self.subTest(when=when):
                self.setUp()
                self.start()
                if when == 'pending':
                    self.manager.suppress_enable('account-a')
                else:
                    original = self.manager.relay
                    def suppress_on_exchange(url, data, form=False):
                        response = original(url, data, form)
                        if url == signin.TOKEN:
                            self.manager.suppress_enable('account-a')
                        return response
                    self.manager.relay = suppress_on_exchange
                self.manager.run(self.session)
                outcome = self.manager.view('account-a', self.gateway.identity)['last_outcome']
                self.assertEqual(outcome['kind'], 'signed_in')
                self.assertFalse(outcome['enabled'])
                self.assertEqual(self.gateway.patches, [])
                self.assertTrue(self.gateway.row['disabled'])

    def test_cancel_after_approval_is_rejected_and_completion_continues(self):
        self.start()
        original = self.gateway.call
        def cancel_on_exchange(path, body=None, method=None, timeout=None):
            response = original(path, body, method)
            if path == '/api-call' and body['url'] == signin.TOKEN:
                with self.assertRaisesRegex(signin.AccessError, 'already_finishing'):
                    self.manager.cancel('account-a', self.gateway.identity, self.session['handle'])
            return response
        self.manager.call = cancel_on_exchange
        self.manager.run(self.session)
        self.assertEqual(len(self.gateway.field_patches), 1)
        self.assertEqual(self.manager.view('account-a', self.gateway.identity)['last_outcome']['kind'], 'signed_in')

    def test_provider_failure_declined_expired_and_slow_down(self):
        for response, expected in [((400, {'error': 'access_denied'}), 'declined'),
                ((400, {'error': {'code': 'user_denied'}}), 'declined'),
                ((400, {'error': 'expired_token'}), 'expired'),
                ((500, {'error': 'FAKE_SECRET_ERROR'}), 'provider_error')]:
            with self.subTest(expected=expected):
                self.setUp()
                self.gateway.polls = [response]
                self.assertEqual(self.run_flow()['kind'], expected)
                self.assert_no_writes()
        self.setUp()
        self.gateway.polls.insert(0, (429, {'error': 'slow_down'}))
        self.run_flow()
        self.assertEqual(self.event.waits[:2], [5, 10])

    def test_pending_until_expiry_and_expiry_during_inflight_poll(self):
        self.start()
        self.clock[0] += 899
        self.manager.run(self.session)
        self.assertEqual(self.event.waits, [1])
        self.assertEqual(self.manager.view('account-a', self.gateway.identity)['last_outcome']['kind'], 'expired')
        self.assert_no_writes()

    def test_secret_allowlist_and_provider_errors_sanitized(self):
        result = self.start()
        self.assertEqual(set(result['session']), {'handle', 'state', 'user_code', 'verification_url',
                                                'expected_email', 'started_at', 'expires_at'})
        self.assertNotIn('FAKE_', json.dumps(result))
        self.manager.call = Mock(side_effect=RuntimeError('FAKE_SECRET_EXCEPTION'))
        self.manager.run(self.session)
        result = self.manager.view('account-a', self.gateway.identity)
        self.assertNotIn('FAKE_', json.dumps(result))
        self.assertNotIn('device_auth_id', json.dumps(result))

    def test_verification_url_stays_on_fixed_openai_page(self):
        for url in ('https://auth.openai.com.evil/codex/device', 'http://auth.openai.com/codex/device',
                    'https://user:pass@auth.openai.com/codex/device', 'javascript:alert(1)',
                    'https://auth.openai.com/other', signin.VERIFY+'#fragment', signin.VERIFY+'?device_auth_id=private'):
            self.assertEqual(signin.verification_url(url), signin.VERIFY)

    def test_pending_view_is_safe_during_start_and_same_session_joins_while_finishing(self):
        original = self.manager.relay
        observed = []
        def relay(url, data, form=False, call=None):
            if url == signin.USERCODE:
                observed.append(self.manager.view('account-a', self.gateway.identity))
            return original(url, data, form, call=call)
        self.manager.relay = relay
        first = self.start()
        self.assertIsNone(observed[0]['session'])
        self.session['state'] = 'finishing'
        second = self.manager.start('account-a', self.gateway.identity)
        self.assertEqual(first['session']['handle'], second['session']['handle'])
        self.assertEqual(second['session']['state'], 'finishing')


    def test_expiry_during_inflight_poll_refuses_token_exchange(self):
        self.start()
        original = self.gateway.call
        def slow_poll(path, body=None, method=None, timeout=None):
            result = original(path, body, method)
            if path == '/api-call' and body['url'] == signin.DEVICE_TOKEN:
                self.clock[0] += 901
            return result
        self.manager.call = slow_poll
        self.manager.run(self.session)
        self.assertEqual(self.manager.view('account-a', self.gateway.identity)['last_outcome']['kind'], 'expired')
        self.assertFalse(any(c[1] and c[1].get('url') == signin.TOKEN for c in self.gateway.calls))
        self.assert_no_writes()


    def test_saved_user_ids_and_missing_token_fields_fail_closed(self):
        for field, value in [('user_id', 'other-user'), ('chatgpt_user_id', 'other-user'), ('sub', 'other-user')]:
            with self.subTest(field=field):
                self.setUp()
                self.gateway.saved[field] = value
                self.assertEqual(self.run_flow()['kind'], 'invalid_identity')
                self.assert_no_writes()
        self.setUp()
        self.gateway.tokens.pop('refresh_token')
        self.assertEqual(self.run_flow()['kind'], 'invalid_identity')
        self.assert_no_writes()

    def test_callback_refreshes_card_while_session_is_still_finishing(self):
        self.changed.side_effect = lambda _: self.assertEqual(
            self.manager.view('account-a', self.gateway.identity)['session']['state'], 'finishing')
        self.assertEqual(self.run_flow()['kind'], 'signed_in')


    def test_overview_snapshot_never_serializes_credentials_or_device_secret(self):
        self.start()
        state = app.GatewayState(resets=Mock(), notices=Mock(), fast_modes=Mock())
        state.notices.claude_view.return_value = dict(low=[], out=None, recovered=None)
        self.addCleanup(state.signins.close)
        state.signins = self.manager
        state.resets.view.return_value = {}
        state.fast_modes.views.return_value = {}
        with patch.object(app, 'api', side_effect=self.gateway.call):
            account = app.fetch_account(self.gateway.row)
        state.access.project(self.gateway.row, account)
        state.data['accounts'] = [account]
        with patch.object(app, 'billing_records', return_value={}), patch.object(app, 'preference_status', return_value={}):
            snapshot = state.snapshot()
        public = json.dumps(snapshot)
        self.assertNotIn('FAKE_', public)
        self.assertNotIn('id_token', public)
        self.assertNotIn('device_auth_id', public)
        self.assertNotIn('code_verifier', public)
        self.assertIn('ABCD-EFGH', public)

    def test_paid_notice_only_for_completed_enabled_outcome_and_disabled_health_is_neutral(self):
        import operations
        now = self.clock[0]
        account = {'email': CODEX_PAIR[0], 'access_identity': 'a' * 64,
            'active': False, 'disabled': True, 'quota_attempted_at': app.routing_policy.stamp(now),
            'signin': {'last_outcome': {'kind': 'signed_in', 'at': now, 'enabled': False}}}
        gateway = {'online': True, 'checked_at': app.routing_policy.stamp(now), 'accounts': [account]}
        self.assertEqual(operations.account_health(account, gateway, now), 'intentionally_disabled')
        self.assertFalse(any(n['kind'] == 'signin_enabled' for n in operations.candidates(gateway, now).values()))
        account['signin']['last_outcome']['enabled'] = True
        notices = [n for n in operations.candidates(gateway, now).values() if n['kind'] == 'signin_enabled']
        self.assertEqual([n['message'] for n in notices], ['Signed in · account enabled'])


    def test_sign_in_notice_follows_a_fresh_sign_in_requirement_not_a_stale_one(self):
        import operations
        now = self.clock[0]
        def gateway(attempted, disabled, enable_failed):
            outcome = {'kind': 'signed_in', 'at': now, 'enabled': False}
            if enable_failed:
                outcome['enable_failed'] = True
            account = {'email': CODEX_PAIR[0], 'access_identity': 'a' * 64, 'active': not disabled,
                       'disabled': disabled, 'auth_state': 'sign_in_required',
                       'quota_attempted_at': app.routing_policy.stamp(attempted), 'signin': {'last_outcome': outcome}}
            return account, {'online': True, 'checked_at': app.routing_policy.stamp(now), 'accounts': [account]}
        for disabled, enable_failed in ((False, False), (True, False), (True, True)):
            with self.subTest(disabled=disabled, enable_failed=enable_failed):
                # Read before the confirmed sign-in: stale, no notice.
                account, view = gateway(now - 5, disabled, enable_failed)
                kinds = [n['kind'] for n in operations.candidates(view, now).values()]
                self.assertNotIn('sign_in', kinds)
                self.assertNotEqual(operations.account_health(account, view, now), 'sign_in_required')
                # Read after it: the account really needs a new sign-in, even after a failed enable.
                account, view = gateway(now + 5, disabled, enable_failed)
                kinds = [n['kind'] for n in operations.candidates(view, now + 5).values()]
                self.assertIn('sign_in', kinds)


class RoutingSwitchTests(unittest.TestCase):
    def test_a_deliberate_routing_change_clears_a_failed_automatic_enable(self):
        identity = self.gateway.identity
        self.gateway.row['status_message'] = ''
        self.gateway.other_rows = [dict(self.gateway.row, auth_index='account-b', name='second.json',
                                        email='two@example.com', disabled=False)]
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.state.signins.outcomes['account-a'] = {'kind': 'signed_in', 'at': 1.0, 'identity': identity,
                                                            'enabled': False, 'enable_failed': True}
                self.state.signins.clock = lambda: 2.0
                self.assertEqual(self.state.set_routing_enabled('account-a', identity, enabled, True)['enabled'], enabled)
                self.assertEqual(self.state.signins.view('account-a', identity)['last_outcome'],
                                 {'kind': 'signed_in', 'at': 1.0, 'enabled': False})
        # A routing change that fails readback keeps it.
        self.state.signins.outcomes['account-a']['enable_failed'] = True
        with patch.object(app.account_access, 'set_disabled_verified', side_effect=ValueError('synthetic')), \
                self.assertRaises(ValueError):
            self.state.set_routing_enabled('account-a', identity, True, True)
        self.assertTrue(self.state.signins.outcomes['account-a']['enable_failed'])

    def setUp(self):
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.state = app.GatewayState(resets=Mock(), notices=Mock(), fast_modes=Mock())
        self.changed = patch.object(self.state, 'signin_changed').start()
        self.addCleanup(patch.stopall)
        self.call = patch.object(app, 'api', side_effect=self.gateway.call).start()
        self.addCleanup(self.state.signins.close)

    def test_free_enable_succeeds_and_stale_identity_writes_nothing(self):
        self.gateway.row['id_token']['plan_type'] = 'free'
        identity = self.gateway.identity
        self.assertTrue(self.state.set_routing_enabled('account-a', identity, True)['enabled'])
        self.assertFalse(self.gateway.row['disabled'])
        self.gateway.patches.clear()
        self.gateway.row['email'] = 'replacement@example.com'
        with self.assertRaises(signin.AccessError):
            self.state.set_routing_enabled('account-a', identity, False, True)
        self.assertEqual(self.gateway.patches, [])

    def test_last_ready_requires_confirmation_then_disables_and_suppresses_enable(self):
        self.gateway.row.update(disabled=False, status_message='')
        with self.assertRaisesRegex(signin.AccessError, 'last_ready'):
            self.state.set_routing_enabled('account-a', self.gateway.identity, False)
        self.assertEqual(self.gateway.patches, [])
        with patch.object(self.state.signins, 'suppress_enable') as suppress:
            result = self.state.set_routing_enabled('account-a', self.gateway.identity, False, True)
        self.assertFalse(result['enabled'])
        self.assertTrue(self.gateway.row['disabled'])
        suppress.assert_called_once_with('account-a')
        self.changed.assert_called_once_with('account-a')

    def test_second_ready_needs_no_confirmation_and_enable_reads_back(self):
        self.gateway.row.update(disabled=False, status_message='')
        self.gateway.other_rows = [dict(self.gateway.row, auth_index='account-b', name='second.json', email='two@example.com')]
        self.state.set_routing_enabled('account-a', self.gateway.identity, False)
        self.state.set_routing_enabled('account-a', self.gateway.identity, True)
        self.assertEqual([p['disabled'] for p in self.gateway.patches], [True, False])

    def test_identity_rechecked_immediately_before_patch(self):
        self.gateway.row.update(disabled=False, status_message='')
        count = [0]
        def replace():
            count[0] += 1
            if count[0] == 3:
                self.gateway.row['email'] = 'replacement@example.com'
        self.gateway.list_hook = replace
        with self.assertRaises(signin.AccessError):
            self.state.set_routing_enabled('account-a', self.gateway.identity, False)
        self.assertEqual(self.gateway.patches, [])

    def test_off_intent_supersedes_signin_even_when_patch_readback_fails(self):
        with patch.object(signin.threading, 'Thread'):
            self.state.start_signin('account-a', self.gateway.identity)
        self.call.side_effect = lambda path, *args, **kwargs: ({'files': [self.gateway.row]}
            if path == '/auth-files' else {'status': 'ok'})
        self.gateway.row['disabled'] = False
        with self.assertRaises(ValueError):
            self.state.set_routing_enabled('account-a', self.gateway.identity, False, True)
        self.assertFalse(self.state.signins.sessions['account-a']['enable'])

    def test_free_ready_sibling_avoids_confirmation(self):
        self.gateway.row.update(disabled=False, status_message='')
        self.gateway.other_rows = [dict(self.gateway.row, auth_index='account-b', name='second.json',
            email='two@example.com', id_token={'plan_type': 'free', 'chatgpt_account_id': 'workspace-b'})]
        self.assertFalse(self.state.set_routing_enabled('account-a', self.gateway.identity, False)['enabled'])
        self.assertEqual([body['disabled'] for body in self.gateway.patches], [True])


class SigninHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = patch.object(app.routing_policy, 'STATE', Path(directory.name))
        manager.start()
        self.addCleanup(manager.stop)
        import http.client
        self.http_client = http.client
        self.httpd = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.state = self.httpd.state = Mock()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)
        self.headers = {'Host': 'router.localhost', 'X-Router-Request': '1', 'Content-Type': 'application/json'}
        self.payload = {'account_id': 'account-a', 'identity': 'a' * 64}

    def cleanup(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)

    def request(self, path, payload=None, headers=None):
        connection = self.http_client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=5)
        data = json.dumps(payload if payload is not None else self.payload).encode()
        connection.request('POST', path, data, headers=self.headers if headers is None else headers)
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def test_new_endpoints_reject_cross_site_host_origin_and_readonly_without_dispatch(self):
        for path in ('/api/accounts/signin/start', '/api/accounts/signin/cancel', '/api/accounts/routing'):
            for extra, expected in [({'Host': 'evil.invalid'}, 421), ({'X-Router-Request': '0'}, 403),
                    ({'Origin': 'http://evil.invalid'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
                    ({'X-Router-Read-Only': '1'}, 403)]:
                with self.subTest(path=path, headers=extra):
                    status, headers, _ = self.request(path, headers={**self.headers, **extra})
                    self.assertEqual(status, expected)
                    self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.state.mock_calls, [])

    def test_bounded_json_schema_and_exact_endpoints(self):
        for path in ('/api/accounts/signin/start', '/api/accounts/signin/cancel', '/api/accounts/routing'):
            for payload in ([], {}, {'account_id': 'a', 'identity': 'bad'},
                            {**self.payload, 'arbitrary_url': 'https://evil.invalid'}, {'oversize': 'a' * 2049}):
                self.assertEqual(self.request(path, payload)[0], 400)
        self.assertEqual(self.request('/api/accounts/signin/start/extra')[0], 404)
        self.assertEqual(self.request('/api/accounts/signin/start', headers={**self.headers, 'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.state.mock_calls, [])

    def test_dispatch_session_cancel_and_routing_fixed_errors(self):
        self.state.start_signin.return_value = {'session': None, 'last_outcome': None}
        status, headers, _ = self.request('/api/accounts/signin/start')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.state.start_signin.assert_called_once_with('account-a', 'a' * 64, deadline=ANY)
        self.state.signins.cancel.return_value = {'session': None, 'last_outcome': None}
        self.assertEqual(self.request('/api/accounts/signin/cancel', {**self.payload, 'handle': 'b' * 32})[0], 200)
        self.state.signins.cancel.assert_called_once_with('account-a', 'a' * 64, 'b' * 32, deadline=ANY)
        self.state.signins.cancel.side_effect = signin.AccessError('already_finishing')
        status, _, body = self.request('/api/accounts/signin/cancel', {**self.payload, 'handle': 'b' * 32})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)['error'], 'already_finishing')
        self.state.set_routing_enabled.return_value = {'enabled': False}
        routing = {**self.payload, 'enabled': False, 'confirmed': True}
        self.assertEqual(self.request('/api/accounts/routing', routing)[0], 200)
        self.state.set_routing_enabled.assert_called_once_with('account-a', 'a' * 64, False, True, deadline=ANY)
        self.state.set_routing_enabled.side_effect = signin.AccessError('last_ready')
        status, _, body = self.request('/api/accounts/routing', routing)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)['error'], 'last_ready')
        self.state.set_routing_enabled.side_effect = app.routing_policy.Hold('PRIVATE_LOCK_DETAILS')
        status, _, body = self.request('/api/accounts/routing', routing)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)['error'], 'Account change could not be confirmed. Try again.')
        self.assertNotIn(b'PRIVATE_LOCK_DETAILS', body)

    def test_expired_account_requests_use_sanitized_unavailable_responses(self):
        for path, payload, action in (
                ('/api/accounts/signin/start', self.payload, self.state.start_signin),
                ('/api/accounts/signin/cancel', {**self.payload, 'handle': 'b' * 32}, self.state.signins.cancel),
                ('/api/accounts/routing', {**self.payload, 'enabled': False, 'confirmed': True}, self.state.set_routing_enabled)):
            with self.subTest(path=path):
                action.side_effect = TimeoutError('PRIVATE_TIMEOUT_DETAILS')
                status, headers, body = self.request(path, payload)
                self.assertEqual(status, 503)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(json.loads(body), {'error': 'Account change could not be confirmed. Try again.'})

    def test_routing_selector_uses_one_deadline_with_fifteen_second_calls(self):
        now, timeouts = [0], []
        gateway = FakeGateway()
        self.addCleanup(gateway.close)
        gateway.row.update(email=CODEX_PAIR[0], priority=0)
        gateway.other_rows = [dict(copy.deepcopy(gateway.row), email=CODEX_PAIR[1],
                                  name='second.json', auth_index='account-b')]
        def call(path, body=None, method=None, timeout=None):
            timeouts.append(timeout)
            now[0] += timeout
            if path == '/config':
                return {'routing': {'strategy': 'round-robin'}}
            return gateway.call(path, body, method)
        self.state.snapshot.return_value = {'accounts': []}
        with patch.object(app, 'RequestDeadline', side_effect=lambda: RequestDeadline(clock=lambda: now[0])), \
                patch.object(app.routing_policy, 'request', side_effect=call):
            status, _, body = self.request('/api/routing', {'mode': 'round-robin', 'preferred_email': None})
        self.assertEqual(status, 503, body)
        self.assertEqual(timeouts, [15, 15, 15])
        self.assertEqual(now[0], 45)
        self.assertFalse(gateway.field_patches)
        self.state.start_signin.side_effect = RuntimeError('FAKE_SECRET_TOKEN')
        status, _, body = self.request('/api/accounts/signin/start')
        self.assertEqual(status, 503)
        self.assertNotIn(b'FAKE_SECRET', body)


if __name__ == '__main__':
    unittest.main()
