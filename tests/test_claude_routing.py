from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
"""Provider routing contracts with synthetic usage and gateway records only."""
import copy
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import routing_policy as p
import server as app

from codex_fixture import CODEX_PAIR, use_codex_pair
A, B = CODEX_PAIR
NOW = 1700000000


def setUpModule():
    use_codex_pair()


def overview():
    return {'gateway': {'online': True, 'checked_at': p.stamp(NOW), 'claude_accounts': [dict(r) for r in CODEX_INVENTORY if r['type']=='claude']}, 'watch_accounts': [
        {'id': 'claude' if i == 0 else 'claude-b', 'provider': 'claude', 'email': email,
         'status': 'ok', 'checked_at': p.stamp(NOW), 'windows': [
             {'kind': 'five_hour', 'remaining': 40+i*20, 'resets_at': NOW+10000},
             {'kind': 'seven_day', 'remaining': 20+i*40, 'resets_at': NOW+100000+i*50000}]}
        for i, email in enumerate((A, B))]}


class ClaudeRoutingTests(unittest.TestCase):
    def test_weekly_only_is_eligible_and_ranked_but_present_invalid_five_hour_holds(self):
        data = overview()
        for row in data['watch_accounts']:
            row['windows'] = [w for w in row['windows'] if w['kind'] == 'seven_day']
        self.assertEqual(p.choose(data, NOW, 'highest-remaining-quota', provider='claude')['preferred_email'], B)
        self.assertEqual(p.choose(data, NOW, 'earliest-weekly-reset', provider='claude')['preferred_email'], A)
        data['watch_accounts'][0]['windows'].append(dict(kind='five_hour', remaining=None, resets_at=NOW + 5000))
        with self.assertRaises(p.DataHold):
            p.choose(data, NOW, 'highest-remaining-quota', provider='claude')

    def test_aged_exhausted_watch_deferral_preserves_routing_until_expiry(self):
        data = overview()
        first = data['watch_accounts'][0]
        first.update(checked_at=p.stamp(NOW - 1800), fresh_until=p.stamp(NOW + 60))
        first['windows'][0].update(used=100, remaining=0)
        candidates, excluded = p.eligible_accounts(data, NOW, provider='claude')
        self.assertEqual(set(candidates), {B})
        self.assertEqual(excluded, {A: 'no remaining allowance'})
        data['gateway']['checked_at'] = p.stamp(NOW + 10000)
        with self.assertRaises(p.DataHold):
            p.eligible_accounts(data, NOW + 10000, provider='claude')

    def test_deferred_weekly_exhaustion_never_infers_elapsed_window_recovery(self):
        data = overview()
        first = data['watch_accounts'][0]
        first.update(checked_at=p.stamp(NOW - 1800), fresh_until=p.stamp(NOW + 60))
        first['windows'][0].update(used=100, remaining=0, resets_at=NOW - 60)
        first['windows'][1].update(used=100, remaining=0)
        with self.assertRaises(p.DataHold):
            p.eligible_accounts(data, NOW, provider='claude')
        # A normally fresh reading with an elapsed reset still waits for a new observation.
        first.update(checked_at=p.stamp(NOW - 60))
        with self.assertRaises(p.DataHold):
            p.eligible_accounts(data, NOW, provider='claude')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [{'type': provider, 'email': email, 'name': provider+str(i),
                      'auth_index': provider+str(i),
                      'note': 'Claude '+chr(65+i) if provider == 'claude' else '',
                      'priority': 100 if i == 0 else 0, 'disabled': False, 'status': 'active'}
                     for provider in ('codex', 'claude') for i, email in enumerate((A, B))]
        self.writes = []
        manager = patch.object(p, 'request', side_effect=self.call)
        manager.start()
        self.addCleanup(manager.stop)
        original_snapshot = p.inventory_snapshot
        manager = patch.object(p, 'inventory_snapshot', side_effect=lambda call=None: original_snapshot(self.call if call is None else call))
        manager.start()
        self.addCleanup(manager.stop)

    def call(self, path, body=None, method=None, **kwargs):
        if path == '/config':
            return {'routing': {'strategy': 'round-robin'}}
        if path == '/auth-files':
            return {'files': copy.deepcopy(self.rows)}
        if path == '/auth-files/status':
            self.assertEqual(method, 'PATCH')
            self.writes.append(body.copy())
            row = next(r for r in self.rows if r['name'] == body['name'])
            self.assertEqual(row['auth_index'], body['auth_index'])
            row['disabled'] = body['disabled']
            return {'status': 'ok'}
        self.assertEqual((path, method), ('/auth-files/fields', 'PATCH'))
        self.writes.append(body.copy())
        next(r for r in self.rows if r['name'] == body['name']).update(priority=body['priority'])
        return {'status': 'ok'}

    def reconcile(self, choice=None, data=None):
        with patch.object(p.time, 'time', return_value=NOW):
            return p.reconcile(overview() if data is None else data, choice, self.call,
                               self.root / 'claude', provider='claude')

    def test_runtime_unavailable_other_requires_last_ready_confirmation(self):
        state = self.state()
        target = app.account_access.claude_routing_id(self.rows[2])
        for change in ({'status': 'error'}, {'unavailable': True}, {'runtime_available': False}):
            self.rows[2]['disabled'] = False
            self.rows[3].update({'status': 'active', 'unavailable': False, **change})
            with self.subTest(change=change), patch.object(p, 'STATE', self.root), \
                    patch.object(app, 'api', side_effect=self.call), \
                    patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                    patch.object(p.time, 'time', return_value=NOW):
                with self.assertRaises(app.signin.AccessError) as failure:
                    state.set_claude_routing_enabled(target, False)
                self.assertEqual(failure.exception.code, 'last_ready')
                self.assertEqual(self.writes, [])

    def test_raw_and_projected_runtime_availability_agree_on_eligibility(self):
        for change in ({}, {'unavailable': True}, {'status': 'error'}, {'runtime_available': False}):
            files = copy.deepcopy(self.rows)
            files[3].update(change)
            for projected in (False, True):
                data = overview()
                data['gateway']['claude_accounts'] = (app.claude_routing_projection(files, p.stamp(NOW))
                                                      if projected else files[2:])
                with self.subTest(change=change, projected=projected):
                    observed = p.claude_observations(data, NOW)['gateway']['accounts'][1]
                    candidates, excluded = p.eligible_accounts(data, NOW, 'claude')
                    self.assertEqual(observed['active'], not bool(change))
                    self.assertEqual(B in candidates, not bool(change))
                    if change:
                        self.assertEqual(excluded[B], 'provider_unavailable')
                    else:
                        self.assertNotIn(B, excluded)

    def test_timer_reconciles_unavailable_claude_from_projected_overview(self):
        (self.root/'claude').mkdir()
        for change in ({'unavailable': True}, {'runtime_available': False}):
            self.rows[2]['priority'], self.rows[3]['priority'] = 0, 100
            self.rows[3].update({'unavailable': False, 'runtime_available': True, **change})
            data = overview()
            data['gateway']['claude_accounts'] = app.claude_routing_projection(self.rows, p.stamp(NOW))
            self.writes.clear()
            p.write_json(self.root/'claude/selection.json',
                         {'mode': 'preferred-account', 'preferred_email': B})
            with self.subTest(change=change), patch.object(p, 'STATE', self.root), \
                    patch.object(p, 'urlopen', return_value=io.BytesIO(json.dumps(data).encode())), \
                    patch.object(p, 'request', side_effect=self.call), patch.object(p.time, 'time', return_value=NOW), \
                    patch.object(sys, 'argv', ['prefer-earliest-reset.py', '--apply', '--all-providers']), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(p.main(), 0)
                self.assertEqual(json.loads(output.getvalue())['claude']['preferred_email'], A)
                self.assertEqual([r['priority'] for r in self.rows[2:]], [100, 0])
                self.assertTrue(self.writes)

    def test_raw_malformed_resets_hold_after_watch_projection(self):
        path = self.root / 'usage.json'
        for value in (False, 'malformed', [], {}):
            for resets_enabled in (True, False):
                data = overview()
                raw = {'email': A, 'status': 'ok', 'checked_at': p.stamp(NOW), 'windows': [
                    {'kind': 'five_hour', 'used': 0, 'resets_at': value},
                    {'kind': 'seven_day', 'used': 0, 'resets_at': NOW+100000}]}
                path.write_text(json.dumps(raw))
                data['watch_accounts'][0] = app.watch_account(path, 'claude', 'Claude A', resets_enabled, NOW)
                with self.subTest(value=value, resets=resets_enabled), self.assertRaises(p.DataHold):
                    p.claude_observations(data, NOW)
        for missing in (True, False):
            raw['windows'][0].pop('resets_at', None)
            if not missing: raw['windows'][0]['resets_at'] = None
            path.write_text(json.dumps(raw))
            data['watch_accounts'][0] = app.watch_account(path, 'claude', 'Claude A', True, NOW)
            projected = p.claude_observations(data, NOW)
            self.assertIsNone(projected['gateway']['accounts'][0]['windows'][0]['resets_at'])

    def test_collector_and_cached_snapshots_preserve_malformed_reset_marker(self):
        spec = importlib.util.spec_from_file_location('reset_collector',
            Path(__file__).resolve().parents[1] / 'scripts/collect-claude-usage.py')
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        path = self.root / 'collector.json'
        for reset in (False, 'malformed', [], {}):
            for for_b in (False, True):
                windows, _ = collector.parse_usage({
                    'five_hour': {'utilization': 0, 'resets_at': reset},
                    'seven_day': {'utilization': 0, 'resets_at': p.stamp(NOW+100000)}}, for_b=for_b)
                raw = {'windows': windows, 'email': A, 'status': 'ok', 'checked_at': p.stamp(NOW)}
                path.write_text(json.dumps(raw))
                for cached in (False, True):
                    if cached:
                        path.write_text(json.dumps(collector.previous_snapshot(path)))
                    data = overview()
                    data['watch_accounts'][0] = app.watch_account(path, 'claude', 'Claude A', True, NOW)
                    with self.subTest(reset=reset, b=for_b, cached=cached), self.assertRaises(p.DataHold):
                        p.claude_observations(data, NOW)

    def test_priority_targets_reject_cross_provider_filename_collision(self):
        self.rows[0]['name'] = self.rows[3]['name']
        with self.assertRaises(p.Hold):
            self.reconcile({'mode': 'preferred-account', 'preferred_email': B})
        self.assertEqual(self.writes, [])

    def test_priority_identity_replacement_stops_before_any_further_write(self):
        for stage in ('before', 'after'):
            for field, value in (('auth_index', 'replacement'), ('email', 'other@example.com'), ('type', 'codex')):
                self.rows[2].update(priority=100)
                self.rows[3].update(type='claude', email=B, auth_index='claude1', priority=0)
                self.writes.clear()
                reads = 0
                def call(path, body=None, method=None, **kwargs):
                    nonlocal reads
                    if path == '/auth-files':
                        reads += 1
                        # One operation reads membership once; the second read verifies the bound mutation target.
                        if stage == 'before' and reads == 2: self.rows[3][field] = value
                    result = self.call(path, body, method, **kwargs)
                    if stage == 'after' and method == 'PATCH': self.rows[3][field] = value
                    return result
                state_dir = self.root / (stage + field)
                with self.subTest(stage=stage, field=field), patch.object(p.time, 'time', return_value=NOW):
                    with self.assertRaises(p.Hold):
                        p.reconcile(overview(), {'mode': 'preferred-account', 'preferred_email': B},
                                    call, state_dir, provider='claude')
                    self.assertEqual(len(self.writes), 0 if stage == 'before' else 1)
                    self.assertEqual(json.loads((state_dir/'status.json').read_text())['status'], 'held')

    def test_routing_requests_serialize_complete_multi_provider_selection(self):
        for second_path in ('/api/routing-all', '/api/routing', '/api/claude-routing', '/api/routing-link'):
            first_entered, release, second_attempted = (threading.Event() for _ in range(3))
            lock = threading.Lock()
            class ObservedLock:
                def acquire(self, **kwargs):
                    if threading.current_thread().name == 'second': second_attempted.set()
                    return lock.acquire(**kwargs)
                def release(self): lock.release()
            events = []
            def apply(handler, value, provider, deadline, call, inventory=None):
                events.append((threading.current_thread().name, provider))
                if threading.current_thread().name == 'first' and provider == 'codex':
                    first_entered.set()
                    if not release.wait(2): raise AssertionError('first request never released')
                return 200, {'status': 'active'}
            first = self.handler({'mode': 'round-robin'})
            first.path = '/api/routing-all'
            second = self.handler({'linked': False} if second_path.endswith('/routing-link') else
                                  {'mode': 'highest-remaining-quota'})
            second.path = second_path
            workers = [threading.Thread(target=h.do_POST, name=name) for name, h in
                       (('first', first), ('second', second))]
            with self.subTest(path=second_path), patch.object(app, 'ROUTING_SELECTION_LOCK', ObservedLock(), create=True), \
                    patch.object(app.Handler, 'apply_routing_selection', apply), \
                    patch.object(p, 'set_routing_linked', side_effect=lambda *args: events.append(('second', 'link')) or {'linked': False}):
                try:
                    workers[0].start()
                    self.assertTrue(first_entered.wait(2))
                    workers[1].start()
                    self.assertTrue(second_attempted.wait(1), 'second request did not acquire the request lock')
                    self.assertEqual(events, [('first', 'codex')])
                finally:
                    release.set()
                    for worker in workers:
                        if worker.ident is not None: worker.join(3)
                self.assertEqual(events[:2], [('first', 'codex'), ('first', 'claude')])
                self.assertEqual(first.send.call_args.args[0], 200)
                self.assertEqual(second.send.call_args.args[0], 200)

    def test_claude_toggle_rechecks_identity_after_quota_evaluation(self):
        state = self.state()
        target = app.account_access.claude_routing_id(self.rows[2])
        original = p.eligible_accounts
        def evaluate(*args):
            result = original(*args)
            self.rows[2]['auth_index'] = 'replacement'
            return result
        with patch.object(p, 'STATE', self.root), patch.object(app, 'api', side_effect=self.call), \
                patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                patch.object(p.time, 'time', return_value=NOW), patch.object(p, 'eligible_accounts', side_effect=evaluate):
            with self.assertRaises(app.signin.AccessError):
                state.set_claude_routing_enabled(target, False)
        self.assertEqual(self.writes, [])

    def test_default_and_all_modes_and_provider_isolation(self):
        self.assertEqual(p.selection(self.root, provider='claude', inventory=CODEX_INVENTORY),
                         {'mode': 'earliest-weekly-reset', 'preferred_email': None})
        original = copy.deepcopy(self.rows[:2])
        for choice, wanted in ((None, A), ({'mode': 'round-robin'}, None),
                ({'mode': 'earliest-weekly-reset'}, A), ({'mode': 'highest-remaining-quota'}, B),
                ({'mode': 'preferred-account', 'preferred_email': B}, B)):
            result = self.reconcile(choice)
            self.assertEqual(result['status'], 'active')
            self.assertEqual(result['preferred_email'], wanted)
            self.assertEqual(self.rows[:2], original)
        self.assertTrue(self.writes)
        self.assertTrue(all(w['name'].startswith('claude') for w in self.writes))
        original = copy.deepcopy(self.rows[2:])
        p.reconcile({}, {'mode': 'round-robin'}, self.call, self.root)
        self.assertEqual(self.rows[2:], original)
        self.assertNotEqual(p.selection(self.root, inventory=CODEX_INVENTORY), p.selection(self.root/'claude', provider='claude', inventory=CODEX_INVENTORY))

    def test_uncertain_usage_holds_even_after_ten_minutes(self):
        self.reconcile()
        self.writes.clear()
        for mode in ('earliest-weekly-reset', 'highest-remaining-quota', 'preferred-account'):
            for change in ('stale', 'unknown', 'reset'):
                data = overview()
                b = data['watch_accounts'][1]
                if change == 'stale': b['checked_at'] = p.stamp(NOW+1)
                if change == 'missing': data['watch_accounts'].pop()
                if change == 'unknown': b['windows'][0]['remaining'] = None
                if change == 'reset': b['windows'][0]['resets_at'] = NOW-1
                if change == 'duplicate': b['windows'].append(copy.deepcopy(b['windows'][0]))
                if change == 'identity': b['email'] = A
                result = self.reconcile({'mode': mode, 'preferred_email': B}, data)
                self.assertEqual(result['status'], 'held', (mode, change))
                status = p.read_status(self.root/'claude')
                status['held_since'] = p.stamp(NOW-1000)
                p.write_json(self.root/'claude/status.json', status)
                self.assertEqual(self.reconcile(data=data)['status'], 'held')
        self.assertEqual(self.writes, [])

    def test_refuses_ambiguous_records_and_manual_priority(self):
        for change in ('extra', 'manual'):
            old = copy.deepcopy(self.rows)
            if change == 'missing': self.rows.pop()
            if change == 'extra': self.rows.append(dict(self.rows[-1], name='extra', email=self.rows[-1]['email']))
            if change == 'manual': self.rows[-1]['priority'] = 42
            if change == 'note': self.rows[-1]['note'] = 'Claude A'
            with self.assertRaises(p.Hold): self.reconcile({'mode': 'round-robin'})
            self.assertFalse(self.writes)
            self.rows = old

    def test_window_freshness_preserves_observation_and_validates_both_limits(self):
        data = overview()
        data['watch_accounts'][1]['checked_at'] = p.stamp(NOW-600)
        observations = p.claude_observations(data, NOW)
        self.assertEqual(observations['gateway']['accounts'][1]['quota_checked_at'], p.stamp(NOW-600))
        self.assertEqual(p.choose(data, NOW, 'highest-remaining-quota', provider='claude')['preferred_email'], B)
        for change in ('missing', 'unknown', 'expired', 'error', 'nan', 'future'):
            data = overview()
            row = data['watch_accounts'][1]
            if change == 'missing': row['windows'].pop(1)
            if change == 'unknown': row['windows'][0]['remaining'] = None
            if change == 'expired': row['windows'][1]['resets_at'] = NOW
            if change == 'error': row['error'] = 'network'
            if change == 'nan': row['windows'][0]['remaining'] = float('nan')
            if change == 'future': row['checked_at'] = p.stamp(NOW+31)
            with self.assertRaises(p.DataHold):
                p.choose(data, NOW, 'highest-remaining-quota', provider='claude')

    def test_disabled_and_exhausted_preference_fails_over(self):
        for cause in ('disabled', 'five_hour', 'seven_day'):
            data = overview()
            if cause == 'disabled':
                self.rows[2]['disabled'] = True
            else:
                next(w for w in data['watch_accounts'][0]['windows'] if w['kind'] == cause)['remaining'] = 0
            self.assertEqual(self.reconcile({'mode': 'preferred-account', 'preferred_email': A}, data)['preferred_email'], B)
            self.rows[2]['disabled'] = False

    def test_unstarted_five_hour_window_routes_live_quota_case(self):
        data = overview()
        windows = data['watch_accounts'][0]['windows']
        windows[0].update(remaining=100, resets_at=None)
        windows[1]['remaining'] = 2
        windows.append(dict(kind='weekly_model', model='Fable', remaining=87, resets_at=NOW+100000))
        original = copy.deepcopy(data)
        result = self.reconcile({'mode': 'highest-remaining-quota'}, data)
        self.assertEqual(result['status'], 'active')
        self.assertIsNone(result['hold_reason'])
        self.assertEqual(result['preferred_email'], B)
        self.assertEqual(data, original)

    def test_unstarted_weekly_window_ranks_full_quota_and_latest_reset(self):
        for missing in (False, True):
            data = overview()
            for window in data['watch_accounts'][0]['windows']:
                window.update(remaining=100, resets_at=None)
                if missing: window.pop('resets_at')
            data['watch_accounts'][1]['windows'][1]['remaining'] = 90
            for mode, wanted in (('highest-remaining-quota', A), ('earliest-weekly-reset', B)):
                with self.subTest(missing=missing, mode=mode):
                    result = self.reconcile({'mode': mode}, data)
                    self.assertEqual(result['status'], 'active')
                    self.assertEqual(result['preferred_email'], wanted)
                    if wanted == A: self.assertIsNone(result['weekly_resets_at'])

    def test_unstarted_windows_use_their_period_and_accept_zero_used(self):
        data = overview()
        windows = data['watch_accounts'][0]['windows']
        windows.append(dict(kind='weekly_model', model='Fable', seconds=604800))
        for window in windows:
            window.pop('remaining', None)
            window.update(used=0, resets_at=None)
        observed = p.claude_observations(data, NOW)['gateway']['accounts'][0]['windows']
        self.assertEqual([w['remaining'] for w in observed], [100, 100, 100])
        self.assertEqual([w['resets_at'] for w in observed], [None, None, None])
        self.assertNotIn('seconds', observed[2])
        self.assertEqual(p.choose(data, NOW, 'highest-remaining-quota', provider='claude')['preferred_email'], A)
        for window in windows: window['remaining'] = None
        self.assertEqual(p.choose(data, NOW, 'highest-remaining-quota', provider='claude')['preferred_email'], A)

    def test_unstarted_exception_keeps_invalid_allowances_held(self):
        invalid = [dict(remaining=r, resets_at=None) for r in
                   (99, None, True, -1, 101, float('nan'), float('inf'))]
        invalid += [dict(remaining=100, resets_at=r) for r in
                    (False, float('nan'), float('inf'), NOW-1, NOW, NOW+8*86400+1)]
        invalid += [dict(remaining=99, used=0, resets_at=None),
                    dict(remaining=float('nan'), used=0, resets_at=None)]
        for kind in ('five_hour', 'seven_day', 'weekly_model'):
            for change in invalid:
                data = overview()
                window = dict(kind=kind, **change)
                windows = data['watch_accounts'][0]['windows']
                if kind == 'weekly_model': windows.append(window)
                else: windows[0 if kind == 'five_hour' else 1] = window
                with self.subTest(kind=kind, change=change):
                    result = self.reconcile({'mode': 'highest-remaining-quota'}, data)
                    self.assertEqual(result['status'], 'held')
                    self.assertFalse(self.writes)

    def test_moving_unstarted_resets_do_not_flap_on_minute_ticks(self):
        for mode in ('earliest-weekly-reset', 'highest-remaining-quota'):
            self.rows[2]['priority'], self.rows[3]['priority'] = 0, 100
            self.writes.clear()
            for minute in range(16):
                now = NOW+minute*60
                data = overview()
                data['gateway']['checked_at'] = p.stamp(now)
                for account in data['watch_accounts']:
                    account['checked_at'] = p.stamp(now)
                    for window in account['windows']:
                        window.update(remaining=100, resets_at=None)
                with self.subTest(mode=mode, minute=minute), patch.object(p.time, 'time', return_value=now):
                    result = p.reconcile(data, {'mode': mode} if minute == 0 else None,
                                         self.call, self.root/'claude', provider='claude')
                    self.assertEqual(result['status'], 'active')
                    self.assertEqual(result['preferred_email'], B)
                    self.assertIsNone(result['weekly_resets_at'])
                    self.assertFalse(self.writes)

    def test_unstarted_window_preserves_quota_and_reset_stability_margins(self):
        data = overview()
        for window in data['watch_accounts'][0]['windows']:
            window.update(remaining=100, resets_at=None)
        weekly = data['watch_accounts'][1]['windows'][1]
        for remaining, winner in ((96, B), (95, A)):
            weekly['remaining'] = remaining
            self.assertEqual(p.choose(data, NOW, 'highest-remaining-quota', current=B,
                                      provider='claude')['preferred_email'], winner)
        # An unknown reset cannot establish a time-based stability margin.
        for lead, winner in ((900, B), (901, B)):
            weekly['resets_at'] = NOW+604800-lead
            self.assertEqual(p.choose(data, NOW, current=A, provider='claude')['preferred_email'], winner)

    def test_disabled_account_ignores_stale_usage_and_never_promotes(self):
        self.rows[2]['disabled'] = True
        data = overview()
        data['watch_accounts'][0].update(status='error', error='network', checked_at=p.stamp(NOW-9999), windows=[])
        self.assertEqual(self.reconcile({'mode': 'preferred-account', 'preferred_email': A}, data)['preferred_email'], B)
        self.assertFalse(any(w['name'] == 'claude0' and w.get('priority') == 100 for w in self.writes))
        data['watch_accounts'].pop(0)
        self.assertEqual(self.reconcile({'mode': 'preferred-account', 'preferred_email': A}, data)['preferred_email'], B)
        self.writes.clear()
        with self.assertRaises(p.Hold):
            p.apply_plan(p.priority_plan(A, provider='claude', inventory=CODEX_INVENTORY), self.call, self.root/'claude', 'claude')
        self.assertFalse(self.writes)

    def test_unconfirmed_plan_rechecks_usage_before_retry_and_preserves_codex(self):
        def fail(path, body=None, method=None):
            if method == 'PATCH' and body['name'] == 'claude0':
                raise OSError('private')
            return self.call(path, body, method)
        with patch.object(p.time, 'time', return_value=NOW), self.assertRaises(OSError):
            p.reconcile(overview(), {'mode': 'preferred-account', 'preferred_email': B},
                        fail, self.root/'claude', provider='claude')
        self.assertTrue(p.read_status(self.root/'claude')['pending_plan'])
        before = copy.deepcopy(self.rows)
        self.writes.clear()
        stale = overview()
        stale['watch_accounts'][0]['checked_at'] = p.stamp(NOW+1)
        self.assertEqual(self.reconcile(data=stale)['status'], 'held')
        self.assertEqual(self.rows, before)
        self.assertFalse(self.writes)
        self.assertEqual(self.reconcile()['status'], 'active')
        self.assertEqual([r['priority'] for r in self.rows[2:]], [0, 100])
        self.assertEqual(self.rows[:2], before[:2])
        self.assertFalse(p.read_status(self.root/'claude')['pending_plan'])

    def test_baseline_identity_and_readback_fail_closed(self):
        self.reconcile()
        self.rows[3]['name'] = 'replacement'
        with self.assertRaises(p.Hold): self.reconcile({'mode': 'round-robin'})
        self.assertFalse(self.writes)
        self.rows[3]['name'] = 'claude1'
        def unconfirmed(path, body=None, method=None):
            return {'status': 'ok'} if method == 'PATCH' else self.call(path, body, method)
        with patch.object(p.time, 'time', return_value=NOW), self.assertRaises(p.Hold):
            p.reconcile(overview(), {'mode': 'round-robin'}, unconfirmed,
                        self.root/'claude', provider='claude')
        self.assertTrue(p.read_status(self.root/'claude')['pending_plan'])

    def test_existing_timer_reconciles_both_but_unsaved_claude_is_passive(self):
        payload = json.dumps(overview()).encode()
        with patch.object(p, 'STATE', self.root), patch.object(p, 'urlopen', return_value=io.BytesIO(payload)), \
                patch.object(p, 'reconcile', return_value={'status': 'active'}) as reconcile:
            result, code = p.reconcile_providers()
            self.assertEqual(code, 0)
            self.assertEqual(result['claude']['status'], 'unchanged')
            self.assertEqual([c.kwargs['provider'] for c in reconcile.call_args_list], ['codex'])
            self.assertFalse((self.root/'claude').exists())
        (self.root/'claude').mkdir()
        p.write_json(self.root/'claude/selection.json', {'mode': 'highest-remaining-quota'})
        with patch.object(p, 'STATE', self.root), patch.object(p, 'urlopen', return_value=io.BytesIO(payload)), \
                patch.object(p, 'reconcile', side_effect=[OSError('private'), {'status': 'held'}]) as reconcile:
            result, code = p.reconcile_providers()
            self.assertEqual(code, 1)
            self.assertEqual([c.kwargs['provider'] for c in reconcile.call_args_list], ['codex', 'claude'])
            self.assertEqual(reconcile.call_args_list[0].args[0], p.observations(overview()['gateway']))
            self.assertEqual(reconcile.call_args_list[1].args[0], overview())
            self.assertNotIn('private', json.dumps(result))
        with patch.object(p, 'STATE', self.root), patch.object(p, 'urlopen', return_value=io.BytesIO(payload)), \
                patch.object(p, 'request', side_effect=self.call), patch.object(p.time, 'time', return_value=NOW):
            result, code = p.reconcile_providers()
            self.assertEqual(code, 0)
            self.assertEqual(result['claude']['preferred_email'], B)
            self.assertEqual([r['priority'] for r in self.rows[2:]], [0, 100])

    def handler(self, value, headers=None):
        handler = object.__new__(app.Handler)
        handler.path = '/api/claude-routing'
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        handler.headers = {'Host': '127.0.0.1:8320', 'X-Router-Request': '1',
                           'Content-Type': 'application/json', 'Content-Length': str(len(raw)), **(headers or {})}
        handler.rfile = io.BytesIO(raw)
        handler.connection = Mock()
        handler.server = SimpleNamespace(state=SimpleNamespace(snapshot=lambda: overview()['gateway']))
        handler.send = Mock()
        return handler

    def test_endpoint_gates_and_results(self):
        choice = {'mode': 'round-robin'}
        for headers, code in (({'Host': 'evil.test'}, 421), ({'X-Router-Request': ''}, 403),
                ({'Origin': 'http://evil.test'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
                ({'X-Router-Read-Only': '1'}, 403), ({'Content-Length': '1025'}, 400),
                ({'Content-Type': 'text/plain'}, 400)):
            h = self.handler(choice, headers)
            with patch.object(p, 'reconcile') as reconcile:
                h.do_POST()
                self.assertEqual(h.send.call_args.args[0], code)
                reconcile.assert_not_called()
        for value in ({'mode': 'invalid'}, [], b'{', b'\xff'):
            h = self.handler(value)
            h.do_POST()
            self.assertEqual(h.send.call_args.args[0], 400)
        with patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                patch.object(p, 'request', side_effect=self.call), patch.object(p, 'STATE', self.root), \
                patch.object(p.time, 'time', return_value=NOW):
            for value, code in ((choice, 200), ({'mode': 'earliest-weekly-reset'}, 200)):
                h = self.handler(value)
                h.do_POST()
                self.assertEqual(h.send.call_args.args[0], code, h.send.call_args)
            with patch.object(app, 'watch_accounts', return_value=[]):
                h = self.handler({'mode': 'highest-remaining-quota'})
                h.do_POST()
                self.assertEqual(h.send.call_args.args[0], 202)
            with patch.object(p, 'request', side_effect=OSError('private')):
                h = self.handler(choice)
                h.do_POST()
                self.assertEqual(h.send.call_args.args[0], 503)
                self.assertNotIn(b'private', h.send.call_args.args[1])

    def test_overview_exposes_independent_saved_claude_status_and_drift(self):
        self.reconcile({'mode': 'preferred-account', 'preferred_email': B})
        accounts = [dict(r, priority_checked_at=p.stamp(NOW+1)) for r in self.rows[2:]]
        accounts[0]['priority'] = 100
        state = app.GatewayState(Mock(), Mock())
        state.notices.view.return_value = {}
        state.notices.claude_view.return_value = dict(low=[], out=None, recovered=None)
        self.addCleanup(state.signins.close)
        state.data = dict(overview()['gateway'], accounts=[], claude_accounts=accounts)
        handler = object.__new__(app.Handler)
        handler.path = '/api/overview'
        handler.headers = {'Host': '127.0.0.1:8320', 'X-Router-Request': '1'}
        handler.server = SimpleNamespace(state=state)
        handler.send = Mock()
        with patch.object(app, 'PREFERENCE', self.root/'status.json'), \
                patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                patch.object(app, 'billing_records', return_value={}), \
                patch.object(app, 'usage_summary', return_value={}), \
                patch.object(app, 'health', return_value={}), patch.object(p.time, 'time', return_value=NOW):
            handler.do_GET()
        self.assertEqual(handler.send.call_args.args[0], 200)
        data = json.loads(handler.send.call_args.args[1])
        self.assertIsNone(data['gateway']['preference'])
        claude = data['gateway']['claude_preference']
        self.assertEqual(claude['saved_preferred_email'], B)
        self.assertIs(claude['drift'], True)
        self.assertEqual(data['gateway']['accounts'], [])
        self.assertEqual(len(data['watch_accounts']), 2)

    def test_status_defaults_saved_selection_and_drift(self):
        with patch.object(app, 'PREFERENCE', self.root/'status.json'), patch.object(p.time, 'time', return_value=NOW):
            initial = app.preference_status([], provider='claude')
            self.assertIsNone(initial['saved_preferred_email'])
            self.reconcile({'mode': 'preferred-account', 'preferred_email': B})
            accounts = [dict(r, priority_checked_at=p.stamp(NOW+1)) for r in self.rows[2:]]
            self.assertIs(app.preference_status(accounts, provider='claude')['drift'], False)
            accounts[0]['priority'] = 100
            self.assertIs(app.preference_status(accounts, provider='claude')['drift'], True)
            accounts[0]['priority_checked_at'] = p.stamp(NOW)
            self.assertIsNone(app.preference_status(accounts, provider='claude')['drift'])

    def test_linked_default_persistence_and_no_routing_mutation(self):
        with patch.object(p, 'STATE', self.root), patch.object(p, 'reconcile') as reconcile:
            self.assertIs(p.routing_linked(), True)
            for linked in (False, True):
                h = self.handler({'linked': linked})
                h.path = '/api/routing-link'
                h.do_POST()
                self.assertEqual(h.send.call_args.args, (200, json.dumps({'linked': linked}).encode()))
                self.assertIs(p.routing_linked(), linked)
                self.assertEqual((self.root/'linked.json').stat().st_mode & 0o777, 0o600)
            reconcile.assert_not_called()
            self.assertFalse((self.root/'selection.json').exists())
            self.assertFalse((self.root/'claude').exists())

    def test_new_endpoint_gates_and_validation(self):
        values = {'/api/routing-link': {'linked': True}, '/api/routing-all': {'mode': 'round-robin'},
                  '/api/claude/accounts/routing': {'account_id': 'a'*64, 'enabled': False, 'confirmed': False}}
        for path, value in values.items():
            for headers, code in (({'Host': 'evil.test'}, 421), ({'X-Router-Request': ''}, 403),
                    ({'Origin': 'http://evil.test'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
                    ({'X-Router-Read-Only': '1'}, 403), ({'Content-Length': '1025'}, 400),
                    ({'Content-Type': 'text/plain'}, 400)):
                with self.subTest(path=path, headers=headers):
                    h = self.handler(value, headers)
                    h.path = path
                    with patch.object(p, 'reconcile') as reconcile, patch.object(app, 'api') as api:
                        h.do_POST()
                        self.assertEqual(h.send.call_args.args[0], code)
                        reconcile.assert_not_called()
                        api.assert_not_called()
            invalid = ([{}, {'linked': 1}, {'linked': True, 'extra': False}] if path.endswith('link') else
                       [{}, {'account_id': 'a'*64, 'enabled': 1, 'confirmed': False},
                        {'account_id': 'a'*64, 'enabled': False},
                        {'account_id': 'a'*64, 'enabled': False, 'confirmed': False, 'identity': 'x'}]
                       if path.endswith('/routing') else [{'mode': 'bad'}, {'mode': 'preferred-account', 'preferred_email': 'other'}])
            for value in [[], b'{', b'\xff', *invalid]:
                with self.subTest(path=path, value=value):
                    h = self.handler(value)
                    h.path = path
                    h.do_POST()
                    self.assertEqual(h.send.call_args.args[0], 400)

    def test_routing_all_results_and_partial_failure(self):
        error = {'error': 'Routing change not confirmed. Refresh and try again.'}
        active = {'status': 'active', 'policy': 'round-robin'}
        held = {'status': 'held', 'hold_reason': 'stale observations'}
        pending = dict(held, message='Selection saved; applying pending because stale observations')
        for sides, expected, code in (([active, active], [active, active], 200),
                ([active, held], [active, pending], 202), ([held, active], [pending, active], 202),
                ([active, OSError('private')], [active, error], 503),
                ([OSError('private'), active], [error, active], 503),
                ([held, OSError('private')], [pending, error], 503)):
            with self.subTest(sides=sides), patch.object(p, 'reconcile', side_effect=copy.deepcopy(sides)) as reconcile:
                h = self.handler({'mode': 'round-robin'})
                h.path = '/api/routing-all'
                h.do_POST()
                self.assertEqual(h.send.call_args.args[0], code)
                self.assertEqual(json.loads(h.send.call_args.args[1]), dict(zip(('codex', 'claude'), expected)))
                self.assertEqual(reconcile.call_count, 2)
                self.assertNotIn('provider', reconcile.call_args_list[0].kwargs)
                self.assertEqual(reconcile.call_args_list[1].kwargs['provider'], 'claude')
                self.assertIs(reconcile.call_args_list[0].kwargs['call'], reconcile.call_args_list[1].kwargs['call'])

    def test_routing_all_validates_both_before_mutating(self):
        original = p.validate_selection
        def validate(value, provider='codex'):
            if provider == 'claude':
                raise ValueError('unsupported')
            return original(value, provider)
        h = self.handler({'mode': 'round-robin'})
        h.path = '/api/routing-all'
        with patch.object(p, 'validate_selection', side_effect=validate), patch.object(p, 'reconcile') as reconcile:
            h.do_POST()
        self.assertEqual(h.send.call_args.args[0], 400)
        reconcile.assert_not_called()

    def test_routing_all_shared_deadline_retains_confirmed_codex_result(self):
        clock = [0]
        deadline = app.RequestDeadline(clock=lambda: clock[0])
        active = {'status': 'active', 'policy': 'round-robin'}
        snapshots = 0
        def snapshot():
            nonlocal snapshots
            snapshots += 1
            if snapshots == 2:
                clock[0] = 46
            return overview()['gateway']
        def reconcile(observed, value, call, **kwargs):
            if kwargs.get('provider') == 'claude':
                call('/config')
            return active
        h = self.handler({'mode': 'round-robin'})
        h.path = '/api/routing-all'
        h.server.state.snapshot = snapshot
        with patch.object(app, 'RequestDeadline', return_value=deadline), \
                patch.object(p, 'reconcile', side_effect=reconcile), patch.object(p, 'request', side_effect=self.call) as request:
            h.do_POST()
        self.assertEqual(h.send.call_args.args[0], 503)
        result = json.loads(h.send.call_args.args[1])
        self.assertEqual(result['codex'], active)
        self.assertIn('error', result['claude'])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], '/auth-files')

    def test_routing_all_real_controllers_and_failed_claude_readback(self):
        original = copy.deepcopy(self.rows[:2])
        def call(path, body=None, method=None, **kwargs):
            if method == 'PATCH' and body['name'] == 'claude1':
                return {'status': 'ok'}  # Do not apply; the controller must reject the readback.
            return self.call(path, body, method)
        h = self.handler({'mode': 'preferred-account', 'preferred_email': B})
        h.path = '/api/routing-all'
        gateway = dict(overview()['gateway'], accounts=[
            dict(id=r['auth_index'], email=r['email'], active=True, quota_checked_at=p.stamp(NOW),
                 windows=[dict(seconds=604800, remaining=40, resets_at=NOW+100000)])
            for r in self.rows[:2]])
        h.server.state.snapshot = lambda: gateway
        with patch.object(p, 'STATE', self.root), patch.object(p, 'request', side_effect=call), \
                patch.object(p.time, 'time', return_value=NOW), \
                patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']):
            h.do_POST()
        self.assertEqual(h.send.call_args.args[0], 503)
        result = json.loads(h.send.call_args.args[1])
        self.assertEqual(result['codex']['status'], 'active')
        self.assertEqual(result['codex']['preferred_email'], B)
        self.assertIn('error', result['claude'])
        self.assertEqual([r['priority'] for r in self.rows[:2]], [0, 100])
        self.assertNotEqual(self.rows[:2], original)
        self.assertEqual(p.selection(self.root/'claude', 'claude', inventory=CODEX_INVENTORY)['preferred_email'], B)

    def state(self):
        state = app.GatewayState(Mock(), Mock())
        state.notices.view.return_value = {}
        state.notices.claude_view.return_value = dict(low=[], out=None, recovered=None)
        self.addCleanup(state.signins.close)
        return state

    def test_claude_toggle_scope_readback_and_confirmation(self):
        state = self.state()
        codex = copy.deepcopy(self.rows[:2])
        with patch.object(p, 'STATE', self.root), patch.object(app, 'api', side_effect=self.call), \
                patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                patch.object(p.time, 'time', return_value=NOW), patch.object(p, 'reconcile'):
            ids = [app.account_access.claude_routing_id(r) for r in self.rows[2:]]
            self.assertEqual(state.set_claude_routing_enabled(ids[0], False), {'enabled': False})
            self.assertTrue(self.rows[2]['disabled'])
            with self.assertRaises(app.signin.AccessError) as failure:
                state.set_claude_routing_enabled(ids[1], False)
            self.assertEqual(failure.exception.code, 'last_ready')
            self.assertFalse(self.rows[3]['disabled'])
            self.assertEqual(state.set_claude_routing_enabled(ids[1], False, True), {'enabled': False})
            self.assertEqual(state.set_claude_routing_enabled(ids[0], True), {'enabled': True})
            self.assertEqual(self.rows[:2], codex)
            self.assertTrue(all(w['name'].startswith('claude') for w in self.writes))
            self.assertTrue(all('priority' not in w for w in self.writes))

    def test_claude_toggle_refuses_ambiguous_replaced_and_codex_identities(self):
        state = self.state()
        original = copy.deepcopy(self.rows)
        target = app.account_access.claude_routing_id(self.rows[2])
        for cause in ('codex', 'extra', 'replacement', 'duplicate-index', 'cross-provider-name'):
            self.rows = copy.deepcopy(original)
            account_id = target
            if cause == 'codex': account_id = app.account_access.identity(self.rows[0])
            if cause == 'extra': self.rows.append(dict(self.rows[2], name='extra'))
            if cause == 'note': self.rows[2]['note'] = 'Claude B'
            if cause == 'replacement': self.rows[2]['name'] = 'replacement'
            if cause == 'missing': self.rows.pop()
            if cause == 'duplicate-index': self.rows[3]['auth_index'] = self.rows[2]['auth_index']
            if cause == 'cross-provider-name': self.rows[0]['name'] = self.rows[2]['name']
            with self.subTest(cause=cause), patch.object(p, 'STATE', self.root), \
                    patch.object(app, 'api', side_effect=self.call):
                with self.assertRaises((p.Hold, app.signin.AccessError)):
                    state.set_claude_routing_enabled(account_id, False, True)
                self.assertFalse(self.writes)

    def test_claude_toggle_revalidates_identity_and_last_ready_after_usage(self):
        state = self.state()
        target = app.account_access.claude_routing_id(self.rows[2])
        original = copy.deepcopy(self.rows)
        for cause in ('replacement', 'disabled-other'):
            self.rows = copy.deepcopy(original)
            def watched():
                if cause == 'replacement': self.rows[2]['name'] = 'replacement'
                else: self.rows[3]['disabled'] = True
                return overview()['watch_accounts']
            with self.subTest(cause=cause), patch.object(p, 'STATE', self.root), \
                    patch.object(app, 'api', side_effect=self.call), patch.object(app, 'watch_accounts', side_effect=watched), \
                    patch.object(p.time, 'time', return_value=NOW):
                with self.assertRaises(app.signin.AccessError) as failure:
                    state.set_claude_routing_enabled(target, False)
                self.assertEqual(failure.exception.code, 'account_changed' if cause == 'replacement' else 'last_ready')
                self.assertFalse(self.writes)

    def test_claude_toggle_last_ready_requires_confirm_on_unknown_or_exhausted_other(self):
        state = self.state()
        target = app.account_access.claude_routing_id(self.rows[2])
        for cause in ('stale', 'quota', 'unavailable'):
            data = overview()['watch_accounts']
            if cause == 'stale': data[1]['checked_at'] = p.stamp(NOW+1)
            if cause == 'quota': data[1]['windows'][0]['remaining'] = 0
            if cause == 'unavailable': data[1]['error'] = 'network'
            with self.subTest(cause=cause), patch.object(p, 'STATE', self.root), \
                    patch.object(app, 'api', side_effect=self.call), patch.object(app, 'watch_accounts', return_value=data), \
                    patch.object(p.time, 'time', return_value=NOW):
                with self.assertRaises(app.signin.AccessError) as failure:
                    state.set_claude_routing_enabled(target, False)
                self.assertEqual(failure.exception.code, 'last_ready')
                self.assertFalse(self.writes)

    def test_claude_toggle_reconciles_saved_preference_after_confirmed_switch(self):
        state = self.state()
        state.data.update(online=True, checked_at=p.stamp(NOW))
        original = copy.deepcopy(self.rows[:2])
        account_id = app.account_access.claude_routing_id(self.rows[2])
        (self.root/'claude').mkdir()
        p.write_json(self.root/'claude/selection.json', {'mode': 'preferred-account', 'preferred_email': A})
        # Run the follow-up in this test's mock scope, without starting a real thread.
        thread = Mock(side_effect=lambda target, **_: SimpleNamespace(start=target))
        with patch.object(p, 'STATE', self.root), patch.object(app, 'api', side_effect=self.call), \
                patch.object(p, 'request', side_effect=self.call), patch.object(app.threading, 'Thread', thread), \
                patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                patch.object(p.time, 'time', return_value=NOW):
            self.assertEqual(state.set_claude_routing_enabled(account_id, False), {'enabled': False})
            self.assertTrue(self.rows[2]['disabled'])
            self.assertEqual([r['priority'] for r in self.rows[2:]], [0, 100])
            self.assertEqual(state.set_claude_routing_enabled(account_id, True), {'enabled': True})
            self.assertEqual([r['priority'] for r in self.rows[2:]], [100, 0])
        self.assertEqual(thread.call_count, 2)
        self.assertEqual(self.rows[:2], original)

    def test_claude_toggle_readback_identity_change_and_deadline(self):
        state = self.state()
        account_id = app.account_access.claude_routing_id(self.rows[2])
        def changed(path, body=None, method=None, **kwargs):
            result = self.call(path, body, method)
            if method == 'PATCH': self.rows[2]['auth_index'] = 'replacement'
            return result
        with patch.object(p, 'STATE', self.root), patch.object(app, 'api', side_effect=changed):
            with self.assertRaises(p.Hold):
                state.set_claude_routing_enabled(account_id, False, True)
        before = len(self.writes)
        deadline = app.RequestDeadline(seconds=0)
        with patch.object(p, 'STATE', self.root), patch.object(app, 'api') as api:
            with self.assertRaises(TimeoutError):
                state.set_claude_routing_enabled(account_id, False, True, deadline)
            api.assert_not_called()
        self.assertEqual(len(self.writes), before)

    def test_claude_projection_fails_closed_for_unverified_pair(self):
        for cause in ('missing-name', 'extra'):
            files = copy.deepcopy(self.rows)
            if cause == 'unknown': files[2]['email'] = 'other@example.test'
            if cause == 'missing-name': files[2].pop('name')
            if cause == 'note': files[2]['note'] = 'Claude B'
            if cause == 'extra': files.append(dict(files[2], name='extra'))
            with self.subTest(cause=cause):
                projected = app.claude_routing_projection(files, p.stamp(NOW))
                self.assertTrue(all(r['routing_enabled'] is None and r['routing_id'] is None for r in projected))

    def test_overview_claude_runtime_and_unknown_routing_projection(self):
        for change, available in (({}, True), ({'unavailable': True}, False),
                                  ({'status': 'error'}, False), ({'status': 'disabled'}, True)):
            files = copy.deepcopy(self.rows)
            files[3].update(change)
            projected = app.claude_routing_projection(files, p.stamp(NOW))
            with self.subTest(change=change):
                data = self.claude_overview_projection(projected)
                a, b = data['watch_accounts']
                self.assertIs(a['runtime_available'], True)
                self.assertIs(b['runtime_available'], available)
                self.assertIs(b['routing_enabled'], change.get('status') != 'disabled')
                self.assertIs(projected[1]['runtime_available'], available)
        for cause in ('unverified', 'missing', 'duplicate', 'wrong-email'):
            files = copy.deepcopy(self.rows)
            if cause == 'unverified': files[2].pop('name')
            projected = app.claude_routing_projection(files, p.stamp(NOW))
            if cause == 'missing': projected = projected[1:]
            if cause == 'duplicate': projected.append(dict(projected[0]))
            if cause == 'wrong-email': projected[0]['email'] = 'other@example.test'
            with self.subTest(cause=cause):
                data = self.claude_overview_projection(projected)
                a = data['watch_accounts'][0]
                self.assertIsNone(a['routing_enabled'])
                self.assertIsNone(a['routing_id'])
                self.assertIs(a['runtime_available'], True if cause == 'unverified' else None)

    def claude_overview_projection(self, accounts):
        h = self.handler({})
        h.path = '/api/overview'
        h.server.state = SimpleNamespace(snapshot=lambda: {'claude_accounts': accounts},
                                        notices=SimpleNamespace(view=lambda: {}), fast_modes={})
        with patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                patch.object(app, 'usage_summary', return_value={}), patch.object(app, 'health', return_value={}):
            h.do_GET()
        self.assertEqual(h.send.call_args.args[0], 200)
        return json.loads(h.send.call_args.args[1])

    def test_claude_toggle_requires_readback_and_handles_endpoint_errors(self):
        state = self.state()
        account_id = app.account_access.claude_routing_id(self.rows[2])
        def unconfirmed(path, body=None, method=None, **kwargs):
            if method == 'PATCH': return {'status': 'ok'}
            return self.call(path, body, method)
        with patch.object(p, 'STATE', self.root), patch.object(app, 'api', side_effect=unconfirmed):
            with self.assertRaises(p.Hold):
                state.set_claude_routing_enabled(account_id, False, True)
        for failure, code, result in ((None, 200, {'enabled': False}),
                (app.signin.AccessError('last_ready'), 409, {'error': 'last_ready'}),
                (app.signin.AccessError('account_changed'), 409, {'error': 'account_changed'}),
                (OSError('private'), 503, {'error': 'Account change could not be confirmed. Try again.'})):
            h = self.handler({'account_id': account_id, 'enabled': False, 'confirmed': False})
            h.path = '/api/claude/accounts/routing'
            h.server.state = Mock()
            h.server.state.set_claude_routing_enabled.side_effect = failure
            h.server.state.set_claude_routing_enabled.return_value = {'enabled': False}
            h.do_POST()
            self.assertEqual(h.send.call_args.args[0], code)
            self.assertEqual(json.loads(h.send.call_args.args[1]), result)

    def test_claude_projection_and_linked_overview_fields(self):
        state = self.state()
        with patch.object(p, 'STATE', self.root), patch.object(app, 'api', side_effect=self.call), \
                patch.object(p, 'reconcile'), patch.object(app, 'fetch_account', return_value={}), \
                patch.object(app.account_access.AccountAccess, 'project'):
            # Only Claude records are needed; keep unrelated Codex fetching out of this assertion.
            self.rows = self.rows[2:]
            self.rows[0]['disabled'] = True
            state._fetch()
            gateway = state.snapshot()
            self.assertIs(gateway['routing_linked'], True)
            accounts = gateway['claude_accounts']
            self.assertEqual([a['routing_enabled'] for a in accounts], [False, True])
            self.assertTrue(all(len(a['routing_id']) == 64 for a in accounts))
            self.assertEqual([a['key'] for a in accounts], [r['name'] for r in self.rows])
            h = self.handler({})
            h.path = '/api/overview'
            h.server.state = state
            with patch.object(app, 'watch_accounts', return_value=overview()['watch_accounts']), \
                    patch.object(app, 'usage_summary', return_value={}), patch.object(app, 'health', return_value={}):
                h.do_GET()
            data = json.loads(h.send.call_args.args[1])
            self.assertEqual([(a['routing_id'], a['routing_enabled']) for a in data['watch_accounts']],
                             [(a['routing_id'], a['routing_enabled']) for a in accounts])


if __name__ == '__main__':
    unittest.main()
