"""Single Codex account behavior with synthetic gateway/state only."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import operations
import routing_policy as policy
import server
from codex_fixture import CODEX_PAIR

A, B = CODEX_PAIR
NOW = 1700000000


class SingleCodexTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / '.cache/agent-scratch/personal-codex-router/single-codex'
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for module in (policy, operations):
            manager = patch.object(module, 'EMAILS', (A,))
            manager.start()
            self.addCleanup(manager.stop)
        self.rows = [dict(type='codex', email=A, name='codex-a.json', auth_index='codex-a',
                          priority=100, disabled=False),
                     dict(type='codex', email=B, name='retired.json', auth_index='retired',
                          priority=100, disabled=True),
                     *[dict(type='claude', email=email, name=f'claude-{i}.json',
                            auth_index=f'claude-{i}', priority=100 if i == 0 else 0,
                            disabled=False) for i, email in enumerate(CODEX_PAIR)]]
        self.writes = []

    def call(self, path, body=None, method=None):
        if path == '/config':
            return {'routing': {'strategy': 'round-robin'}}
        if path == '/auth-files':
            return {'files': copy.deepcopy(self.rows)}
        self.assertEqual((path, method), ('/auth-files/fields', 'PATCH'))
        self.writes.append(copy.deepcopy(body))
        next(row for row in self.rows if row['name'] == body['name']).update(priority=body['priority'])
        return {'status': 'ok'}

    def exhausted_gateway(self):
        stamp = policy.stamp(NOW)
        return dict(online=True, checked_at=stamp, accounts=[dict(
            id='codex-a', email=A, active=True, disabled=False, auth_state='authenticated',
            quota_attempted_at=stamp, quota_checked_at=stamp,
            availability={'state': 'quota_exhausted', 'scope': 'account'},
            windows=[dict(seconds=604800, remaining=0, resets_at=NOW + 3600)],
            resets=dict(available=1, eligible=True, credits=[dict(expires_at=NOW + 300)]))])

    def test_all_modes_are_neutral_without_usage_and_do_not_touch_other_credentials(self):
        untouched = copy.deepcopy(self.rows[1:])
        for mode in policy.MODES:
            with self.subTest(mode=mode):
                choice = dict(mode=mode, preferred_email=A)
                plan = policy.selected_plan(choice, {}, NOW)
                self.assertEqual(plan['priorities'], {A: 0})
                self.assertIsNone(plan['preferred_email'])
                result = policy.reconcile({}, choice, self.call, self.root)
                self.assertEqual(result['status'], 'active')
                self.assertIsNone(result['hold_reason'])
                self.assertEqual(result['intended_priorities'], {A: 0})
                self.assertEqual(policy.reconcile({}, call=self.call, state_dir=self.root)['status'], 'active')
        self.assertEqual(self.writes, [dict(name='codex-a.json', priority=0)])
        self.assertEqual(self.rows[1:], untouched)
        self.assertEqual(policy.identities('claude'), CODEX_PAIR)

    def test_disabled_remaining_account_is_not_enabled_or_held_for_quota(self):
        self.rows[0]['disabled'] = True
        result = policy.reconcile({}, {'mode': 'highest-remaining-quota'}, self.call, self.root)
        self.assertEqual(result['status'], 'active')
        self.assertTrue(self.rows[0]['disabled'])

    def test_records_ignore_retired_codex_files_present_or_removed(self):
        for extra in (self.rows[1], dict(self.rows[1], disabled=False), None):
            with self.subTest(extra=extra):
                rows = self.rows[:1] + self.rows[2:] + ([extra] if extra else [])
                result = policy.records(lambda _: {'files': rows})
                self.assertEqual(set(result), {A})
                self.assertEqual(set(policy.records(lambda _: {'files': rows}, 'claude')), set(CODEX_PAIR))

    def test_records_still_reject_missing_or_duplicate_configured_identity(self):
        for rows in (self.rows[1:], self.rows + [dict(self.rows[0], name='duplicate.json', disabled=True)]):
            with self.subTest(rows=rows), self.assertRaisesRegex(policy.Hold, 'accounts are incomplete or ambiguous'):
                policy.records(lambda _: {'files': rows})

    def test_saved_removed_preference_and_pending_pair_plan_converge(self):
        policy.write_json(self.root / 'selection.json', dict(mode='preferred-account', preferred_email=B))
        policy.write_json(self.root / 'status.json', dict(
            policy='preferred-account', selected_email=B, preferred_email=B, pending_plan=True,
            intended_priorities={A: 0, B: 100}, plan_status='active'))
        policy.write_json(self.root / 'baseline.json', {
            A: dict(name='codex-a.json', priority=100), B: dict(name='retired.json', priority=0)})
        self.rows.pop(1)
        result = policy.reconcile({}, call=self.call, state_dir=self.root)
        self.assertEqual(result['status'], 'active')
        self.assertEqual(result['selected_email'], A)
        self.assertEqual(result['intended_priorities'], {A: 0})
        self.assertFalse(policy.read_status(self.root)['pending_plan'])
        self.assertEqual(self.writes, [dict(name='codex-a.json', priority=0)])
        with self.assertRaises(ValueError):
            policy.validate_selection(dict(mode='preferred-account', preferred_email=B))

    def test_rollback_with_old_pair_baseline_or_without_baseline(self):
        untouched = copy.deepcopy(self.rows[1:])
        for recorded in (False, True):
            with self.subTest(recorded=recorded):
                state = self.root / str(recorded)
                state.mkdir()
                if recorded:
                    policy.write_json(state / 'baseline.json', {
                        A: dict(name='codex-a.json', priority=100), B: dict(name='gone.json', priority=0)})
                policy.reconcile({}, {'mode': 'earliest-weekly-reset'}, self.call, state)
                # Without an old baseline, reconcile captures the current priority.
                result = policy.rollback(self.call, state)
                self.assertEqual(result, dict(status='restored', policy='baseline'))
                self.assertEqual(self.rows[0]['priority'], 100)
                self.assertEqual(policy.read_status(state)['intended_priorities'], {A: 100})
                self.assertTrue(policy.suspended(state))
        self.assertEqual(self.rows[1:], untouched)

    def test_adding_a_second_identity_after_single_account_baseline(self):
        policy.reconcile({}, {'mode': 'earliest-weekly-reset'}, self.call, self.root)
        self.assertEqual(set(json.loads((self.root / 'baseline.json').read_text())), {A})
        self.rows[1].update(disabled=False, priority=0)
        with patch.object(policy, 'EMAILS', (A, B)):
            policy.apply_plan(policy.priority_plan(B), self.call, self.root)
            self.assertEqual((self.rows[0]['priority'], self.rows[1]['priority']), (0, 100))
            baseline = json.loads((self.root / 'baseline.json').read_text())
            self.assertEqual(baseline[A], dict(name='codex-a.json', priority=100))
            self.assertEqual(baseline[B], dict(name='retired.json', priority=0))
            self.assertEqual(policy.rollback(self.call, self.root)['status'], 'restored')
            self.assertEqual((self.rows[0]['priority'], self.rows[1]['priority']), (100, 0))

    def test_rollback_without_any_previous_policy(self):
        self.assertEqual(policy.rollback(self.call, self.root), dict(status='restored', policy='round-robin'))
        self.assertEqual(self.rows[0]['priority'], 0)
        self.assertEqual(self.writes, [dict(name='codex-a.json', priority=0)])

    def test_exhaustion_is_critical_no_fallback_with_reset_action_and_recovery(self):
        gateway = self.exhausted_gateway()
        # A retired account, even if apparently ready, is not a fallback.
        gateway['accounts'].append(dict(copy.deepcopy(gateway['accounts'][0]), id='retired', email=B))
        gateway['accounts'][1]['windows'][0]['remaining'] = 50
        store = operations.NoticeStore(self.root / 'notices.json', self.root)
        store.update(gateway, NOW)
        item, = store.view()['items']
        self.assertEqual(item['email'], A)
        self.assertEqual(item['kind'], 'weekly_exhausted')
        self.assertEqual(item['urgency'], 'critical')
        self.assertEqual(item['message'], 'Weekly limit reached · no account can serve · resets in 1h 0m')
        self.assertEqual(item['action'], dict(type='redeem_reset', account_id='codex-a', label=A))
        self.assertEqual(item['accounts'][0]['reset_credits_available'], 1)
        reloaded = operations.NoticeStore(store.path, self.root)
        reloaded.update(gateway, NOW + 10)
        self.assertEqual(reloaded.view()['items'][0]['id'], item['id'])
        gateway['accounts'][0]['windows'][0]['remaining'] = 50
        reloaded.update(gateway, NOW + 20)
        self.assertEqual(reloaded.view()['items'], [])

    def test_retired_notices_and_pair_episode_are_closed(self):
        store = operations.NoticeStore(self.root / 'notices.json', self.root)
        for key, email in (('exhausted:pool', 'Both Codex accounts'), ('exhausted:' + B, B),
                           ('signin:' + B, B), ('free:retired', B)):
            store.rows[key] = dict(id=key, email=email, kind='legacy', message='legacy', active=True,
                                   dismissed=False, created_at=NOW)
        store.update(self.exhausted_gateway(), NOW)
        item, = store.view()['items']
        self.assertEqual(item['email'], A)
        self.assertFalse(item.get('hidden'))
        self.assertTrue(all(not row['active'] for key, row in store.rows.items() if row['kind'] == 'legacy'))

    def test_retained_notice_does_not_claim_a_retired_fallback_during_outage(self):
        store = operations.NoticeStore(self.root / 'notices.json', self.root)
        store.rows['exhausted:' + A] = dict(
            id='existing', email=A, kind='weekly_exhausted', message='using the other account',
            urgency='normal', active=True, dismissed=False, created_at=NOW,
            accounts=[dict(account_id='codex-a', label=A, weekly_resets_at=NOW + 3600)])
        store.update(dict(online=False, accounts=[]), NOW)
        item, = store.view()['items']
        self.assertEqual(item['id'], 'existing')
        self.assertEqual(item['urgency'], 'critical')
        self.assertEqual(item['message'], 'Weekly limit reached · no account can serve · resets in 1h 0m')

    def test_overview_count_and_remaining_account_controls(self):
        resets, fast = Mock(), Mock()
        resets.view.return_value = {'available': 2}
        fast.views.return_value = {'codex-a': {'allowed': False}}
        state = server.GatewayState(resets, Mock(), fast)
        self.addCleanup(state.signins.close)
        state.data = self.exhausted_gateway()
        state.data['accounts'][0].update(_fast_file='codex-a.json', access_identity='synthetic')
        with patch.object(policy, 'STATE', self.root), patch.object(server, 'billing_records', return_value={}), \
                patch.object(server, 'preference_status', return_value=None):
            result = state.snapshot()
        self.assertEqual(result['codex_account_count'], 1)
        self.assertIs(type(result['codex_account_count']), int)
        self.assertEqual(result['accounts'][0]['fast_mode'], {'allowed': False})
        self.assertEqual(result['accounts'][0]['resets'], {'available': 2})
        resets.view.assert_called_once_with(A)
        self.assertEqual(fast.views.call_args.args[0][0]['email'], A)

    def test_single_account_poll_reset_refresh_fast_mode_and_t3_projection(self):
        self.rows.pop(1)  # Credential deletion leaves one Codex and both Claude accounts.
        self.rows[0]['id_token'] = {'plan_type': 'pro', 'chatgpt_account_id': 'synthetic-workspace'}
        resets, fast = Mock(), Mock()
        state = server.GatewayState(resets, Mock(), fast)
        self.addCleanup(state.signins.close)

        def call(path, body=None, method=None, **kwargs):
            if path == '/api-call':
                self.assertEqual(body['auth_index'], 'codex-a')
                return dict(status_code=200, body=dict(plan_type='pro', rate_limit=dict(
                    primary_window=dict(used_percent=12, reset_at=NOW + 86400, limit_window_seconds=604800))))
            return self.call(path, body, method)

        reconcile = policy.reconcile
        def reconcile_locally(overview):
            return reconcile(overview, call=call, state_dir=self.root)

        with patch.object(server, 'api', side_effect=call), patch.object(policy, 'STATE', self.root), \
                patch.object(policy, 'reconcile', side_effect=reconcile_locally), \
                patch.object(policy, 'urlopen', side_effect=AssertionError('Unmocked transport')), \
                patch.object(server, 'PREFERENCE', self.root / 'status.json'), \
                patch.object(server, 'utcnow', return_value=policy.stamp(NOW)), \
                patch.object(policy.time, 'time', return_value=NOW), \
                patch.object(server, 'billing_records', return_value={}), patch.object(server, 'move_available', return_value=False):
            state._fetch()
            self.assertTrue(state.data['online'])
            self.assertEqual([a['email'] for a in state.data['accounts']], [A])
            self.assertEqual([a['email'] for a in state.data['claude_accounts']], list(CODEX_PAIR))
            self.assertEqual(state.after_reset('codex-a'), dict(quota_refresh='ok', routing_reconcile='ok'))
            state.set_fast_mode('codex-a', False)
            cache = state.t3_usage_cache()
            projected = server.t3_usage.accounts(cache, NOW)
        codex, = [(public, body) for public, body in projected if public['provider'] == 'codex']
        self.assertEqual(codex[0]['email'], A)
        self.assertEqual(codex[1]['rate_limit']['primary_window']['used_percent'], 12)
        self.assertEqual({call.args[0]['type'] for call in resets.fetch.call_args_list}, {'codex'})
        self.assertEqual(fast.set_allowed.call_args.args, (
            'codex-a', False, [dict(id='codex-a', email=A, name='codex-a.json', supports_ultrafast=False)]))

    def test_single_account_cli_dry_run_needs_no_dashboard_or_management_requests(self):
        with patch.object(policy, 'STATE', self.root), patch.object(policy, 'urlopen') as transport, \
                patch.object(sys, 'argv', ['routing_policy.py']), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(policy.main(), 0)
        transport.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['priorities'], {A: 0})

    def test_legacy_status_drift_accepts_single_neutral_priority(self):
        policy.write_json(self.root / 'status.json', dict(policy='round-robin', status='active',
                                                        checked_at=policy.stamp(NOW)))
        rows = [dict(email=A, priority=0, priority_checked_at=policy.stamp(NOW + 1))]
        with patch.object(server, 'PREFERENCE', self.root / 'status.json'), \
                patch.object(policy.time, 'time', return_value=NOW + 2):
            self.assertFalse(server.preference_status(rows)['drift'])
            rows[0]['priority'] = 100
            self.assertTrue(server.preference_status(rows)['drift'])

    def test_verification_scripts_skip_before_binary_credentials_or_launch(self):
        for name in ('verify-account-failover', 'verify-reset-routing'):
            spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(name=name), patch.object(module.gateway_config, 'resolve_gateway_binary') as resolve, \
                    patch.object(module.Path, 'read_text', side_effect=AssertionError('Credential read')), \
                    patch.object(module.subprocess, 'Popen') as launch, \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(module.main([]), 0)
                self.assertIn('Skipped:', output.getvalue())
                self.assertIn('1 configured', output.getvalue())
                resolve.assert_not_called()
                launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
