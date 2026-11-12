"""Allowance forecasts use synthetic snapshots and private temporary files only."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import operations
import server
import usage_adapters as usage

NOW = 1800000000
RESET = NOW + 8 * 3600


def window(used=40, at=NOW, reset=RESET, **fields):
    return dict(kind='seven_day', label='Weekly', used=used, remaining=100 - used,
                resets_at=reset, measured_at=usage.stamp(at), **fields)


def account(windows, **fields):
    return dict(provider='claude', key='claude-a.json', auth_index='claude-a',
                status='ok', checked_at=usage.stamp(NOW), windows=windows, **fields)


class ForecastMathTests(unittest.TestCase):
    def forecast(self, used=40, samples=None, now=NOW, reset=RESET, **fields):
        return operations.allowance_forecast(window(used, reset=reset, **fields), None,
                                             samples or [], now)

    def test_endpoint_rate_and_predicted_exhaustion_use_fraction_units(self):
        result = self.forecast(samples=[(NOW - 3600, .2), (NOW, .4)])
        self.assertEqual(result['state'], 'runs_out')
        self.assertAlmostEqual(result['rate_per_hour'], .2)
        self.assertAlmostEqual(result['runs_out_at'], NOW + 3 * 3600)
        self.assertEqual(result['basis_minutes'], 60)
        self.assertEqual(result['observed_at'], NOW)

    def test_lasts_compares_estimate_to_reset_including_exact_boundary(self):
        for reset, expected in ((NOW + 10799, 'lasts'), (NOW + 10800, 'lasts'),
                                (NOW + 10801, 'runs_out')):
            with self.subTest(reset=reset):
                result = self.forecast(samples=[(NOW - 3600, .2), (NOW, .4)], reset=reset)
                self.assertEqual(result['state'], expected)
                self.assertEqual(result['runs_out_at'], NOW + 10800)

    def test_learning_until_minimum_span_and_change(self):
        for samples, reason in (([], 'insufficient_history'),
                                ([(NOW, .4)], 'insufficient_history'),
                                ([(NOW - 899, .1), (NOW, .4)], 'insufficient_history'),
                                ([(NOW - 1800, .395), (NOW, .4)], 'insufficient_change')):
            with self.subTest(samples=samples):
                result = self.forecast(samples=samples)
                self.assertEqual((result['state'], result['reason']), ('learning', reason))
                self.assertIsNone(result['rate_per_hour'])
                self.assertIsNone(result['runs_out_at'])
        self.assertEqual(self.forecast(samples=[(NOW - 900, .39), (NOW, .4)])['state'], 'lasts')

    def test_idle_requires_fresh_distinct_observations_over_thirty_minutes(self):
        for span, expected in ((900, 'learning'), (1799, 'learning'), (1800, 'idle')):
            result = self.forecast(samples=[(NOW - span, .4), (NOW, .4)])
            self.assertEqual(result['state'], expected)
            self.assertIsNone(result['runs_out_at'])
            self.assertEqual(result['rate_per_hour'], 0 if expected == 'idle' else None)

    def test_idle_after_thirty_unchanged_minutes_following_earlier_burn(self):
        burn_then_flat = [(NOW - 7200, .2), (NOW - 1800, .4), (NOW, .4)]
        result = self.forecast(samples=burn_then_flat)
        self.assertEqual((result['state'], result['rate_per_hour']), ('idle', 0))
        self.assertIsNone(result['runs_out_at'])
        recent_flat = [(NOW - 7200, .2), (NOW - 1799, .4), (NOW, .4)]
        self.assertEqual(self.forecast(samples=recent_flat)['state'], 'runs_out')

    def test_recent_horizon_and_uneven_sampling_do_not_weight_poll_frequency(self):
        samples = [(NOW - 4 * 3600, 0), (NOW - 3600, .2)]
        samples += [(NOW - seconds, .3) for seconds in range(1500, 0, -60)]
        samples.append((NOW, .4))
        result = self.forecast(samples=samples)
        self.assertEqual(result['basis_minutes'], 60)
        self.assertAlmostEqual(result['rate_per_hour'], .2)
        self.assertEqual(self.forecast(samples=[(NOW - 10801, .1), (NOW, .4)])['state'], 'learning')

    def test_fast_burn_keeps_subminute_prediction_and_does_not_claim_exhaustion(self):
        result = self.forecast(99, [(NOW - 900, .1), (NOW, .99)])
        self.assertEqual(result['state'], 'runs_out')
        self.assertGreater(result['rate_per_hour'], 1)
        self.assertTrue(NOW < result['runs_out_at'] < NOW + 11)
        result = self.forecast(99, [(NOW - 900, .1), (NOW, .99)], now=NOW + 11)
        self.assertEqual((result['state'], result['reason']), ('stale', 'projection_elapsed'))
        self.assertIsNone(result['runs_out_at'])

    def test_prediction_is_anchored_to_measurement_not_request_time(self):
        result = self.forecast(samples=[(NOW - 3600, .2), (NOW, .4)], now=NOW + 300)
        self.assertEqual(result['runs_out_at'], NOW + 10800)

    def test_exhaustion_needs_no_history_but_stale_data_cannot_claim_out(self):
        result = self.forecast(100)
        self.assertEqual((result['state'], result['reason']), ('out', 'exhausted'))
        self.assertIsNone(result['runs_out_at'])
        self.assertIsNone(result['rate_per_hour'])
        self.assertEqual(self.forecast(100, now=NOW + 901)['state'], 'stale')
        self.assertEqual(self.forecast(100, reset=NOW)['reason'], 'reset_elapsed')

    def test_unknown_reset_allows_estimate_but_never_lasts(self):
        result = self.forecast(samples=[(NOW - 3600, .2), (NOW, .4)], reset=None)
        self.assertEqual(result['state'], 'runs_out')
        self.assertEqual(result['runs_out_at'], NOW + 10800)
        self.assertEqual(self.forecast(100, reset=None)['state'], 'out')
        self.assertEqual(self.forecast(reset=None)['state'], 'learning')

    def test_invalid_reset_clock_skew_missing_time_and_bad_percentages(self):
        cases = [({'invalid_reset': True}, 'invalid_reset'),
                 ({'resets_at': 'bad'}, 'invalid_reset'),
                 ({'measured_at': usage.stamp(NOW + 1)}, 'clock_skew'),
                 ({'measured_at': None}, 'missing_observation_time'),
                 ({'used': None}, 'missing_usage'), ({'used': True}, 'missing_usage'),
                 ({'used': float('nan')}, 'missing_usage'), ({'used': -1}, 'missing_usage'),
                 ({'used': 101}, 'missing_usage')]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                result = operations.allowance_forecast(window() | overrides, usage.stamp(NOW), [], NOW)
                self.assertEqual((result['state'], result['reason']), ('unavailable', reason))
                self.assertIsNone(result['rate_per_hour'])
        result = operations.allowance_forecast(dict(used=1, resets_at=None), usage.stamp(NOW), [], NOW)
        self.assertEqual(result['observed_at'], NOW)


class ForecastStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state' / 'allowance-samples.json'
        self.store = operations.AllowanceForecastStore(self.path)

    def observe(self, used, at, reset=RESET, *, store=None, **account_fields):
        item = account([window(used, at, reset)]) | account_fields
        (store or self.store).decorate({}, [], [dict(id='claude', accounts=[item])], now=at)
        return item['windows'][0]['forecast']

    def row(self):
        return next(iter(self.store.series.values()))

    def test_round_trip_atomic_private_file_and_missing_parent(self):
        self.assertEqual(self.observe(20, NOW - 3600)['state'], 'learning')
        restored = operations.AllowanceForecastStore(self.path)
        result = self.observe(40, NOW, store=restored)
        self.assertEqual(result['state'], 'runs_out')
        self.assertAlmostEqual(result['rate_per_hour'], .2)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertNotIn('claude-a.json', self.path.read_text())
        self.assertNotIn('@', self.path.read_text())
        self.path.chmod(0o644)
        self.observe(50, NOW + 300, store=restored)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_identical_cached_snapshots_never_add_time_or_writes(self):
        self.observe(20, NOW)
        before = self.path.read_bytes()
        with patch.object(self.store, '_save', wraps=self.store._save) as save:
            item = account([window(20)])
            for elapsed in (30, 300, 900, 901, 1800):
                self.store.decorate({}, [], [dict(id='claude', accounts=[item])], NOW + elapsed)
                expected = 'stale' if elapsed > 900 else 'learning'
                self.assertEqual(item['windows'][0]['forecast']['state'], expected)
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.row()['samples'], [[NOW, .2]])

    def test_unchanged_new_measurements_are_sampled_every_five_minutes(self):
        self.observe(20, NOW - 1800)
        with patch.object(self.store, '_save', wraps=self.store._save) as save:
            self.observe(20, NOW - 1501)
            save.assert_not_called()
            self.observe(20, NOW - 1500)
            self.assertEqual(save.call_count, 1)
        self.assertEqual(self.observe(20, NOW)['state'], 'idle')
        self.assertEqual(self.observe(21, NOW + 1)['state'], 'lasts')
        self.assertEqual(self.row()['samples'][-1], [NOW + 1, .21])

    def test_reset_change_drop_and_missing_reset_transition_start_new_series(self):
        for new_used, new_reset in ((50, RESET + 3600), (10, RESET), (50, None)):
            with self.subTest(new_used=new_used, new_reset=new_reset):
                self.store = operations.AllowanceForecastStore(self.path.with_name(str(new_used) + str(new_reset)))
                self.observe(20, NOW - 1800)
                self.observe(40, NOW)
                result = self.observe(new_used, NOW + 60, new_reset)
                self.assertEqual(result['state'], 'learning')
                self.assertEqual(result['basis_minutes'], 0)
                self.assertEqual(self.row()['samples'], [[NOW + 60, new_used / 100]])

    def test_out_of_order_or_contradictory_readings_cannot_roll_history_back(self):
        self.observe(20, NOW - 1800)
        self.observe(40, NOW)
        before = copy.deepcopy(self.store.series)
        for used, at, reset in ((10, NOW - 10, RESET + 300), (10, NOW, RESET), (40, NOW, None)):
            result = self.observe(used, at, reset)
            self.assertEqual((result['state'], result['reason']), ('learning', 'superseded_observation'))
            self.assertEqual(self.store.series, before)
        self.assertEqual(self.observe(40, NOW + 300)['state'], 'runs_out')

    def test_different_accounts_models_slots_and_durations_are_isolated(self):
        rows = [account([window()]), account([window(model='Fable')]),
                account([window(model='Sonnet')]), account([window(slot='primary_window')]),
                account([window(seconds=604800)]), account([window()], id='another-id')]
        rows += [account([window()]) | dict(key='other.json'),
                 account([window()]) | dict(auth_index='replacement')]
        self.store.decorate({}, [], [dict(id='claude', accounts=rows)], NOW)
        # Model windows also remain independent when returned on the same card.
        combined = account([item['windows'][0] for item in rows[:5]])
        self.store.decorate({}, [], [dict(id='claude', accounts=[combined] + rows[6:])], NOW)
        self.assertEqual(len(self.store.series), 7)

    def test_reload_rejects_clock_rollback_until_new_measurements_catch_up(self):
        self.observe(20, NOW - 1800)
        self.observe(40, NOW)
        restored = operations.AllowanceForecastStore(self.path)
        before = copy.deepcopy(restored.series)
        result = self.observe(10, NOW - 60, store=restored)
        self.assertEqual(result['reason'], 'superseded_observation')
        self.assertEqual(restored.series, before)
        self.assertEqual(self.observe(50, NOW + 60, store=restored)['state'], 'runs_out')

    def test_concurrent_overview_reads_share_one_sample_and_atomic_file(self):
        barrier = threading.Barrier(8)
        errors = []

        def observe():
            try:
                barrier.wait(timeout=5)
                self.observe(40, NOW)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=observe) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        restored = operations.AllowanceForecastStore(self.path)
        self.assertEqual(len(restored.series), 1)
        self.assertEqual(next(iter(restored.series.values()))['samples'], [[NOW, .4]])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupt_missing_oversized_or_invalid_file_relearns(self):
        self.path.parent.mkdir()
        invalid_rows = [None, {}, [], {'version': 2, 'series': {}},
                        {'version': 1, 'series': {'a' * 64: dict(reset=None, latest_at=NOW,
                                                               samples=[[NOW, 1.1]])}},
                        {'version': 1, 'series': {'a' * 64: dict(reset=None, latest_at=NOW,
                                                               samples=[[NOW - 1, .5], [NOW, .4]])}},
                        {'version': 1, 'series': {'a' * 64: dict(reset=None, latest_at=NOW,
                                                               samples=[[NOW, .4], [NOW, .5]])}},
                        {'version': 1, 'series': {'a' * 64: dict(latest_at=NOW, samples=[[NOW, .4]])}}]
        for value in invalid_rows:
            self.path.write_text(json.dumps(value))
            store = operations.AllowanceForecastStore(self.path)
            self.assertEqual(store.series, {})
            self.assertEqual(self.observe(20, NOW, store=store)['state'], 'learning')
        self.path.write_bytes(b'{broken')
        self.assertEqual(operations.AllowanceForecastStore(self.path).series, {})
        self.path.write_bytes(b' ' * (operations.FORECAST_MAX_BYTES + 1))
        self.assertEqual(operations.AllowanceForecastStore(self.path).series, {})

    def test_samples_series_and_retention_are_bounded(self):
        for index in range(300):
            self.observe(index / 10, NOW - 300 + index)
        self.assertEqual(len(self.row()['samples']), operations.FORECAST_MAX_SAMPLES)
        rows = [account([window()]) | dict(key=f'account-{index}.json') for index in range(300)]
        self.store.decorate({}, [], [dict(id='claude', accounts=rows)], NOW)
        self.assertEqual(len(self.store.series), operations.FORECAST_MAX_SERIES)
        self.assertLess(self.path.stat().st_size, operations.FORECAST_MAX_BYTES)
        self.store.decorate({}, [], [], NOW + operations.FORECAST_RETENTION + 1)
        self.assertEqual(self.store.series, {})
        self.assertEqual(operations.AllowanceForecastStore(self.path).series, {})

    def test_stale_invalid_future_or_error_observations_do_not_pollute_history(self):
        self.observe(20, NOW - 1800)
        self.observe(40, NOW)
        before = copy.deepcopy(self.store.series)
        for overrides, fields, expected in ((dict(invalid_reset=True), {}, 'unavailable'),
                                           (dict(measured_at=usage.stamp(NOW + 1)), {}, 'unavailable'),
                                           (dict(measured_at=usage.stamp(NOW - 901)), {}, 'stale'),
                                           ({}, dict(quota_error='unavailable'), 'stale'),
                                           ({}, dict(status='error'), 'stale')):
            item = account([window() | overrides]) | fields
            self.store.decorate({}, [], [dict(id='claude', accounts=[item])], NOW)
            self.assertEqual(item['windows'][0]['forecast']['state'], expected)
            self.assertEqual(self.store.series, before)

    def test_codex_routing_cooldown_does_not_hide_fresh_exhaustion(self):
        item = dict(provider='codex', key='codex.json', id='codex-index', status='error',
                    quota_error=None, quota_checked_at=usage.stamp(NOW), windows=[window(100)])
        self.store.decorate({}, [], [dict(id='codex', accounts=[item])], NOW)
        self.assertEqual(item['windows'][0]['forecast']['state'], 'out')

    def test_failed_atomic_write_preserves_previous_file_and_retries_later(self):
        self.observe(20, NOW - 1800)
        before = self.path.read_bytes()
        with patch.object(operations.os, 'replace', side_effect=OSError('disk full')) as replace:
            self.assertEqual(self.observe(40, NOW)['state'], 'runs_out')
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])
            self.observe(40, NOW + 1)
            replace.assert_called_once()
        self.assertTrue(self.store.dirty)
        self.observe(40, NOW + 300)
        self.assertFalse(self.store.dirty)
        self.assertNotEqual(self.path.read_bytes(), before)
        self.assertEqual(len(operations.AllowanceForecastStore(self.path).series), 1)

    def test_older_watch_mirror_cannot_reset_selected_gateway_series(self):
        self.observe(20, NOW - 3600)
        selected = account([window(40)])
        old = account([window(10, NOW - 300, RESET - 300)])
        self.store.decorate({}, [old], [dict(id='claude', accounts=[selected])], NOW)
        self.assertEqual(selected['windows'][0]['forecast']['state'], 'runs_out')
        self.assertEqual(old['windows'][0]['forecast']['reason'], 'superseded_observation')
        self.assertEqual(self.row()['reset'], RESET)
        self.assertEqual(self.row()['samples'][-1], [NOW, .4])

    def test_unmatched_watch_account_learns_without_gateway_inventory(self):
        watched = account([window(20, NOW - 3600)]) | dict(key=None, id='claude-b')
        self.store.decorate({}, [watched], [], NOW - 3600)
        watched['windows'] = [window(40)]
        self.store.decorate({}, [watched], [], NOW)
        self.assertEqual(watched['windows'][0]['forecast']['state'], 'runs_out')


class ForecastOverviewTests(unittest.TestCase):
    def test_main_background_observer_learns_with_no_browser_or_notice_service(self):
        with tempfile.TemporaryDirectory() as directory:
            store = operations.AllowanceForecastStore(Path(directory) / 'samples.json')
            watched = [account([window(40)])]
            state = SimpleNamespace(snapshot=lambda: {}, forecasts=store,
                                    notices=Mock(), resets=Mock(), signins=Mock())
            state.notices.update.side_effect = OSError('notice store unavailable')
            with patch.object(sys, 'argv', ['dashboard']), \
                    patch.object(server, 'AllowanceForecastStore', return_value=store), \
                    patch.object(server, 'GatewayState', return_value=state) as gateway_state, \
                    patch.object(server.subscriptions, 'CodexBilling') as billing, \
                    patch.object(server.threading, 'Thread') as thread, \
                    patch.object(server, 'ThreadingHTTPServer'), \
                    patch.object(server, 'watch_accounts', return_value=watched), \
                    patch.object(operations.time, 'time', return_value=NOW):
                server.main()
                billing.assert_called_once_with(server.BILLING.with_name('codex-billing.json'))
                gateway_state.assert_called_once_with(forecasts=store, codex_billing=billing.return_value)
                self.assertEqual(thread.call_count, 2)  # Reuses existing pollers.
                observer = thread.call_args_list[1].kwargs['target']
                with patch.object(server.time, 'sleep', side_effect=StopIteration):
                    with self.assertRaises(StopIteration):
                        observer()
            self.assertEqual(len(operations.AllowanceForecastStore(store.path).series), 1)
            state.resets.tick.assert_called_once_with()
            state.notices.update.assert_called_once_with({})
            state.signins.close.assert_called_once_with()

    def test_http_overview_forecasts_codex_and_selected_claude_windows_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'allowance-samples.json'
            store = operations.AllowanceForecastStore(path)
            codex = dict(provider='codex', key='codex.json', id='codex-index',
                         quota_checked_at=usage.stamp(NOW - 3600), quota_error=None,
                         windows=server.quota_windows(dict(rate_limit=dict(
                             allowed=True, primary_window=dict(used_percent=20, reset_at=RESET,
                                                                limit_window_seconds=18000),
                             secondary_window=dict(used_percent=30, reset_at=RESET + 86400,
                                                                  limit_window_seconds=604800)))))
            claude = dict(provider='claude', key='claude-a.json', auth_index='claude-a',
                          email='a@example.test', status='active')
            gateway = dict(online=True, accounts=[codex], claude_accounts=[claude],
                           inventory=[dict(type='codex', name='codex.json', auth_index='codex-index'),
                                      dict(type='claude', name=claude['key'], auth_index='claude-a',
                                           email=claude['email'])])
            watched = [account([window(20, NOW - 3600)], email=claude['email'], id='claude')]
            groups = server.providers.groups(gateway, watched)
            store.decorate(copy.deepcopy(gateway), copy.deepcopy(watched), groups, NOW - 3600)
            # Exercise a restart between observations, including Codex's checked-at fallback.
            store = operations.AllowanceForecastStore(path)
            codex['quota_checked_at'] = usage.stamp(NOW)
            codex['windows'][0].update(used=40, remaining=60)
            codex['windows'][1].update(used=40, remaining=60)
            watched[0]['windows'] = [window(40)]
            watched[0]['checked_at'] = usage.stamp(NOW)
            before = copy.deepcopy(gateway)
            state = SimpleNamespace(snapshot=lambda: copy.deepcopy(gateway), forecasts=store,
                                    notices=SimpleNamespace(view=lambda: []), fast_modes=None)
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.state = state
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            with patch.object(server, 'watch_accounts', side_effect=lambda: copy.deepcopy(watched)), \
                    patch.object(server, 'usage_summary', return_value={}), \
                    patch.object(server, 'billing_records', return_value={}), \
                    patch.object(server, 'health', return_value={}), \
                    patch.object(server.provider_status, 'read_snapshot', return_value={}), \
                    patch.object(server, 'layout_view', return_value={}), \
                    patch.object(operations.time, 'time', return_value=NOW):
                thread.start()
                try:
                    request = Request(f'http://127.0.0.1:{httpd.server_port}/api/overview',
                                      headers={'X-Router-Request': '1', 'Host': 'router.localhost'})
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        data = json.load(response)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(gateway, before)
            cards = {group['id']: group['accounts'][0] for group in data['providers']}
            for row in cards['codex']['windows'] + cards['claude']['windows']:
                self.assertEqual(set(row['forecast']), {'state', 'reason', 'runs_out_at',
                                                       'rate_per_hour', 'basis_minutes', 'observed_at'})
                self.assertEqual(row['forecast']['state'], 'runs_out')
                self.assertEqual(row['forecast']['basis_minutes'], 60)
            self.assertAlmostEqual(cards['codex']['windows'][0]['forecast']['rate_per_hour'], .2)
            self.assertAlmostEqual(cards['codex']['windows'][1]['forecast']['rate_per_hour'], .1)
            self.assertEqual(data['gateway']['accounts'][0]['windows'][0]['forecast'],
                             cards['codex']['windows'][0]['forecast'])
            self.assertEqual(data['watch_accounts'][0]['windows'][0]['forecast'],
                             cards['claude']['windows'][0]['forecast'])
            self.assertEqual(cards['claude']['windows'][0]['used'], 40)


if __name__ == '__main__':
    unittest.main()
