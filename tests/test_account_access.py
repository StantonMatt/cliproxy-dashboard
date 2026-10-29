import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import account_access as access
import operations
import routing_policy
import server as app


def setUpModule():
    directory = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(directory.cleanup)
    previous = routing_policy.STATE
    routing_policy.STATE = Path(directory.name)
    unittest.addModuleCleanup(setattr, routing_policy, 'STATE', previous)


class AccountAccessTests(unittest.TestCase):
    def setUp(self):
        self.record = {'type': 'codex', 'name': 'account.json', 'auth_index': 'a',
                       'email': routing_policy.EMAILS[0], 'disabled': False,
                       'id_token': {'plan_type': 'free', 'chatgpt_account_id': 'workspace-a'}}
        self.rows = [self.record]
        self.calls = []
        self.access = access.AccountAccess()

    def call(self, path, body=None, method=None):
        self.calls.append((path, body, method))
        if path == '/auth-files':
            return {'files': copy.deepcopy(self.rows)}
        if path == '/config':
            return {'routing': {}}
        if path == '/auth-files/status':
            self.assertEqual(method, 'PATCH')
            self.assertEqual(body['name'], self.record['name'])
            self.assertEqual(body['auth_index'], self.record['auth_index'])
            self.record['disabled'] = body['disabled']
            return {'status': 'ok'}
        if path.startswith('/auth-files/refresh?'):
            self.assertEqual(method, 'POST')
            return {'status': 'ok'}
        if path == '/api-call':
            return {'status_code': 200, 'body': {'plan_type': 'pro', 'rate_limit': {
                'primary_window': {'used_percent': 30, 'limit_window_seconds': 604800,
                                   'reset_at': 1800000000}}}}
        self.fail('Unexpected management endpoint: ' + path)

    def project(self, record):
        with patch.object(app, 'api', side_effect=self.call):
            account = app.fetch_account(record)
        self.access.project(record, account)
        account.pop('_usage_plan', None)
        return account

    def test_free_stays_enabled_across_polls(self):
        for _ in range(3):
            record = self.access.reconcile(copy.deepcopy(self.rows), self.call)[0]
            self.assertFalse(record['disabled'])
        self.assertEqual(self.calls, [])
        account = self.project(record)
        self.assertTrue(account['active'])
        self.assertEqual(account['plan'], 'Free')
        self.assertIsNone(routing_policy.ineligible_reason(account))
        self.assertIsNone(account['access_reason'])

    def test_poll_preserves_free_preference_and_signed_out_exclusion(self):
        for disabled, signed_out in ((False, False), (True, False), (False, True)):
            with self.subTest(disabled=disabled, signed_out=signed_out):
                self.record['disabled'] = disabled
                state = app.GatewayState(Mock(), Mock(), Mock())
                self.addCleanup(state.signins.close)
                def call(path, body=None, method=None):
                    if path == '/api-call':
                        return {'status_code': 401 if signed_out else 200,
                                'body': {'plan_type': 'free'}}
                    return self.call(path, body, method)
                with patch.object(app, 'api', side_effect=call), patch.object(routing_policy, 'reconcile'):
                    for _ in range(3):
                        state._fetch()
                        account = state.data['accounts'][0]
                        self.assertEqual(account['disabled'], disabled)
                        self.assertEqual(routing_policy.ineligible_reason(account),
                                         'disabled' if disabled else 'auth_required' if signed_out else None)
                self.assertFalse(any(method == 'PATCH' for _, _, method in self.calls))

    def test_paid_unknown_missing_and_defaulted_top_level_never_disable(self):
        for token in ({'plan_type': 'pro'}, {'plan_type': 'unknown'}, {}, None,
                      {'plan_type': 'Free'}, {'plan_type': ''}, {'plan_type': ['free']}, 'raw.jwt.token'):
            with self.subTest(token=token):
                self.record.update(id_token=token, plan_type='free')
                self.access.reconcile(self.rows, self.call)
                self.assertEqual(self.calls, [])
                self.assertFalse(self.record['disabled'])

    def test_usage_is_not_a_plan_label_or_disable_signal(self):
        self.record['id_token'] = {}
        account = self.project(self.record)
        self.assertEqual(account['plan'], 'Unknown')
        self.assertIsNone(account['plan_type'])
        self.assertTrue(account['active'])
        self.assertFalse(any(method == 'PATCH' for _, _, method in self.calls))

    def test_plan_projection_preserves_new_nonempty_claims(self):
        for plan, label in (('prolite', 'Pro 100'), ('future_super_plan', 'Future Super Plan'),
                            ('future-super-plan', 'Future Super Plan'), ('unknown', 'Unknown')):
            with self.subTest(plan=plan):
                self.record['id_token']['plan_type'] = plan
                account = self.project(self.record)
                self.assertEqual(account['plan_type'], plan)
                self.assertEqual(account['plan'], label)
                self.assertTrue(account['active'])

    def test_missing_or_malformed_plans_remain_unknown_and_do_not_refresh(self):
        for plan in (None, '', '   ', [], {}, 123, True):
            with self.subTest(plan=plan):
                self.record['id_token']['plan_type'] = plan
                account = self.project(self.record)
                self.assertIsNone(account['plan_type'])
                self.assertEqual(account['plan'], 'Unknown')
                call = Mock()
                self.access.maybe_refresh(self.record, 'prolite', call)
                self.record['id_token']['plan_type'] = 'prolite'
                self.access.maybe_refresh(self.record, plan, call)
                call.assert_not_called()

    def test_usage_plan_retains_new_values_without_changing_claim_label(self):
        for plan in ('prolite', 'future_super_plan', 'free', None, '', '   ', [], {}, 123):
            with self.subTest(plan=plan):
                response = {'status_code': 200, 'body': {'plan_type': plan}}
                account = app.fetch_account(self.record, call=Mock(return_value=response))
                expected = plan if isinstance(plan, str) and plan.strip() else None
                self.assertEqual(account['_usage_plan'], expected)
                self.assertEqual(account['plan'], 'Free')

    def test_existing_free_disabled_account_is_never_auto_enabled(self):
        self.record['disabled'] = True
        for _ in range(3):
            record = self.access.reconcile(copy.deepcopy(self.rows), self.call)[0]
            self.assertTrue(record['disabled'])
        self.assertEqual(self.calls, [])

    def test_disabled_paid_is_plain_disabled_and_polling_never_enables(self):
        self.record.update(disabled=True, id_token={'plan_type': 'pro'})
        record = self.access.reconcile(self.rows, self.call)[0]
        account = self.project(record)
        self.assertEqual(account['access_state'], 'disabled')
        self.assertEqual(account['availability']['state'], 'disabled')
        self.assertIsNone(account['access_reason'])
        self.assertFalse(any(method == 'PATCH' for _, _, method in self.calls))

    def test_refresh_disagreement_both_directions_throttled_even_on_failure(self):
        for saved, usage in (('free', 'pro'), ('pro', 'free'), ('pro', 'prolite'),
                             ('prolite', 'pro'), ('pro', 'future_super_plan'),
                             ('future_super_plan', 'free')):
            self.setUp()
            self.record['id_token']['plan_type'] = saved
            call = Mock(side_effect=OSError('private'))
            for now in (0, 60, 599, 600):
                self.access.maybe_refresh(self.record, usage, call, now)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.args[0], '/auth-files/refresh?name=account.json')
            self.assertEqual(call.call_args.kwargs, {'method': 'POST'})
            self.access.maybe_refresh(self.record, saved, call, 1200)
            self.access.maybe_refresh(self.record, None, call, 1200)
            self.assertEqual(call.call_count, 2)

    def test_enable_after_signin_rechecks_identity_claim_and_status(self):
        for plan in ('free', None, '', '   ', ['pro'], {}, 123, 'unknown',
                     'pro', 'plus', 'prolite', 'future_super_plan'):
            with self.subTest(plan=plan):
                self.setUp()
                self.record.update(disabled=True, id_token={'plan_type': plan})
                if isinstance(plan, str) and plan.strip():
                    self.assertFalse(access.enable_after_signin(self.record, self.call)['disabled'])
                    self.assertEqual(self.calls[1][1]['disabled'], False)
                else:
                    with self.assertRaises(ValueError):
                        access.enable_after_signin(self.record, self.call)
                    self.assertEqual(len(self.calls), 1)
        original = copy.deepcopy(self.record)
        self.record['email'] = 'replacement@example.test'
        with self.assertRaises(ValueError):
            access.enable_after_signin(original, self.call)

    def test_refresh_throttle_survives_temporary_absence_of_same_identity(self):
        call = Mock()
        with patch.object(access.time, 'monotonic', return_value=100):
            self.access.maybe_refresh(self.record, 'pro', call)
        with patch.object(access.time, 'monotonic', return_value=200):
            self.access.reconcile([], call)
            self.access.maybe_refresh(self.record, 'pro', call)
        self.assertEqual(call.call_count, 1)

    def test_enable_helper_requires_readback(self):
        self.record.update(disabled=True, id_token={'plan_type': 'pro'})
        def fail(path, body=None, method=None):
            if method == 'PATCH':
                return {'status': 'ok'}
            return self.call(path, body, method)
        with self.assertRaises(ValueError):
            access.enable_after_signin(self.record, fail)

    def test_notice_episode_identity_legacy_dismissal_close_and_recur(self):
        account = self.project(self.record)
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        gateway = {'online': True, 'checked_at': app.utcnow(), 'accounts': [account]}
        with tempfile.TemporaryDirectory() as directory:
            store = operations.NoticeStore(Path(directory) / 'notices.json', Path(directory))
            def free_notices():
                return [n for n in store.view()['items'] if n['kind'] == 'free_plan']
            store.update(gateway, now)
            first = free_notices()[0]
            self.assertEqual(first['urgency'], 'info')
            self.assertEqual(first['message'], 'ChatGPT reports a Free plan')
            self.assertEqual(operations.account_health(account, gateway, now), 'healthy')
            self.assertIsNone(operations.health(gateway, now, Path(directory))['accounts'][0]['reason'])
            # Honor dismissal flags persisted by older dashboards.
            store.rows[next(k for k, r in store.rows.items() if r['id'] == first['id'])]['dismissed'] = True
            store.save()
            store.update(gateway, now + 1)
            self.assertFalse(free_notices())
            account['plan_type'] = 'pro'
            store.update(gateway, now + 2)
            self.assertFalse(any(r['active'] for k, r in store.rows.items() if k.startswith('free:')))
            account['plan_type'] = 'free'
            store.update(gateway, now + 3)
            second = free_notices()[0]
            self.assertNotEqual(first['id'], second['id'])
            store.rows[next(k for k, r in store.rows.items() if r['id'] == second['id'])]['dismissed'] = True
            store.save()
            replacement = copy.deepcopy(self.record)
            replacement['id_token']['chatgpt_account_id'] = 'new'
            gateway['accounts'] = [self.project(replacement)]
            store.update(gateway, now + 4)
            self.assertEqual(len(free_notices()), 1)
            self.assertNotEqual(free_notices()[0]['id'], second['id'])
            self.assertEqual(sum(r['active'] for k, r in store.rows.items() if k.startswith('free:')), 1)

    def test_poll_reconciles_immediately_when_eligibility_changes_and_not_on_401_refresh(self):
        state = app.GatewayState(Mock(), Mock(), Mock())
        with patch.object(app, 'api', side_effect=self.call), \
                patch.object(app.routing_policy, 'reconcile', return_value={'status': 'active'}) as reconcile:
            state._fetch()
            self.assertFalse(state.data['accounts'][0]['disabled'])
            reconcile.assert_called_once()
            observed = reconcile.call_args.args[0]['gateway']['accounts'][0]
            self.assertTrue(observed['active'])
            reconcile.reset_mock()
            state._fetch()
            reconcile.assert_not_called()
        for response in (401, 200):
            self.setUp()
            self.record['id_token']['plan_type'] = 'pro'
            state = app.GatewayState(Mock(), Mock(), Mock())
            state.data['accounts'] = [self.project(self.record)]
            self.calls.clear()
            def call(path, body=None, method=None):
                if path == '/api-call':
                    return {'status_code': response, 'body': {'plan_type': 'free'}}
                return self.call(path, body, method)
            with patch.object(app, 'api', side_effect=call), patch.object(app.routing_policy, 'reconcile') as reconcile:
                state._fetch()
            refreshes = [p for p, _, _ in self.calls if p.startswith('/auth-files/refresh?')]
            self.assertEqual(len(refreshes), 0 if response == 401 else 1)
            if response == 401:
                reconcile.assert_called_once()
                self.assertEqual(routing_policy.ineligible_reason(state.data['accounts'][0]), 'auth_required')

    def test_old_poll_preserves_newer_access_state_even_when_new_identity_was_not_listed(self):
        for listed in (True, False):
            with self.subTest(listed=listed):
                self.record.update(disabled=True)
                state = app.GatewayState(Mock(), Mock(), Mock())
                newer = self.project(self.record)
                newer.update(access_state='disabled', disabled=True, active=False)
                original = self.call
                def call(path, body=None, method=None):
                    if path == '/config':
                        with state.lock:
                            state.account_updates['a'] = 1
                            state.data['accounts'] = [newer]
                    if path == '/auth-files' and not listed:
                        return {'files': []}
                    return original(path, body, method)
                with patch.object(app, 'api', side_effect=call), patch.object(app.routing_policy, 'reconcile'):
                    state._fetch()
                self.assertEqual(state.data['accounts'][0]['access_state'], 'disabled')

    def test_config_failure_does_not_discard_free_enabled_projection(self):
        state = app.GatewayState(Mock(), Mock(), Mock())
        def call(path, body=None, method=None):
            if path == '/config' or method == 'PATCH':
                raise OSError('PRIVATE_TOKEN')
            return self.call(path, body, method)
        with patch.object(app, 'api', side_effect=call), patch.object(app.routing_policy, 'reconcile') as reconcile:
            state._fetch()
        self.assertTrue(state.data['online'])
        self.assertEqual(state.data['accounts'][0]['access_state'], 'enabled')
        self.assertTrue(state.data['accounts'][0]['active'])
        self.assertNotIn('PRIVATE', json.dumps(state.data))
        reconcile.assert_called_once()

    def test_lifecycle_lock_serializes_reset_refresh_with_poll(self):
        state = app.GatewayState(Mock(), Mock(), Mock())
        started = threading.Event()
        finished = threading.Event()
        def reset():
            started.set()
            state.after_reset('a')
            finished.set()
        with patch.object(app, 'api', side_effect=self.call), patch.object(app.routing_policy, 'reconcile'):
            with state.lifecycle_lock:
                worker = threading.Thread(target=reset)
                worker.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(0.05))
                self.assertFalse(self.calls)
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())


if __name__ == '__main__':
    unittest.main()
