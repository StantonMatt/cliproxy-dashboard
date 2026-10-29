import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('claude_b_usage', ROOT / 'scripts/collect-claude-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class ClaudeBUsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token = self.root / 'token'
        self.token.write_text('private-test-token\n')
        self.token.chmod(0o600)
        self.snapshot = self.root / 'claude-b.json'
        self.now = 1791990000
        self.event = {'type': 'rate_limit_event', 'rate_limit_info': {
            'status': 'allowed', 'rateLimitType': 'seven_day', 'utilization': .16,
            'resetsAt': self.now + 300000, 'unifiedWindows': {
                'five_hour': {'utilization': .04, 'resetsAt': self.now + 14000},
                'seven_day': {'utilization': .16, 'resetsAt': self.now + 300000}}}}
        self.windows = collector.parse_rate_limit_events(json.dumps(self.event))

    def collect(self, now=None):
        return collector.collect_b(self.token, self.snapshot, self.now if now is None else now)

    def measured_windows(self, at):
        return [{**row, 'measured_at': collector.stamp(at)} for row in self.windows]

    def seed_known_usage(self):
        data = {'windows': self.measured_windows(self.now - 3600), 'status': 'ok', 'source': 'usage',
                'checked_at': collector.stamp(self.now - 3600),
                'usage_checked_at': collector.stamp(self.now - 3600)}
        self.snapshot.write_text(json.dumps(data))
        return data

    def fable_windows(self, reset=None):
        return self.windows + [{'kind': 'weekly_model', 'model': 'Fable', 'used': 13,
                                'resets_at': self.now + 10000 if reset is None else reset}]

    def test_cli_probes_share_collection_deadline_below_service_timeout(self):
        for overhead, expected_calls in ((0, 2), (8, 1)):
            previous = self.seed_known_usage()
            clock = [0.0]
            timeouts = []
            def endpoint(token):
                clock[0] += 10
                raise URLError('unavailable')
            def probe(token, model='haiku', timeout=90):
                timeouts.append(timeout)
                clock[0] += timeout + (overhead if len(timeouts) == 1 else 0)
                raise collector.UsageResponseError('cli_timeout')
            with self.subTest(overhead=overhead), patch.object(collector.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(collector, 'fetch_usage', side_effect=endpoint), \
                    patch.object(collector, 'probe_usage', side_effect=probe):
                result = self.collect()
                self.assertEqual(len(timeouts), expected_calls)
                self.assertLessEqual(clock[0], 110)
                self.assertEqual(result['windows'], previous['windows'])
                self.assertEqual(result['checked_at'], previous['checked_at'])
                self.assertEqual(result['error'], 'collection_deadline' if overhead else 'cli_timeout')
                if not overhead: self.assertEqual(timeouts, [90, 10])
        unit = (ROOT/'scripts/personal-codex-router-claude-b-usage.service').read_text()
        timeout = int(next(line.split('=')[1] for line in unit.splitlines() if line.startswith('TimeoutStartSec=')))
        self.assertGreater(timeout, 110)

    def test_fable_probes_are_hourly_and_haiku_preserves_measurement(self):
        def probe(token, model='haiku', timeout=90):
            if model == 'claude-fable-5-1':
                state = json.loads(self.snapshot.read_text())
                self.assertEqual(state['fable_probe_attempted_at'], collector.stamp(at))
                return self.fable_windows()
            return self.windows

        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                patch.object(collector, 'probe_usage', side_effect=probe) as cli:
            for at in (self.now, self.now + 300, self.now + 3300, self.now + 3600):
                result = self.collect(at)
                model = result['windows'][-1]
                measured = self.now if at < self.now + 3600 else at
                self.assertEqual((model['kind'], model['model'], model['used']), ('weekly_model', 'Fable', 13))
                self.assertEqual(model['measured_at'], collector.stamp(measured))
                self.assertEqual(collector.b_snapshot(self.snapshot, at)['windows'][-1], model)
                self.assertEqual(result['checked_at'], collector.stamp(at))
        self.assertEqual([call.kwargs.get('model', 'haiku') for call in cli.call_args_list],
                         ['claude-fable-5-1', 'haiku', 'haiku', 'claude-fable-5-1'])

    def test_expired_fable_window_clears_during_haiku_poll_without_early_retry(self):
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                patch.object(collector, 'probe_usage', return_value=self.fable_windows(self.now + 300)):
            self.collect()
        with patch.object(collector, 'fetch_usage') as fetch, \
                patch.object(collector, 'probe_usage', return_value=self.windows) as cli:
            result = self.collect(self.now + 300)
        fetch.assert_not_called()
        cli.assert_called_once_with(self.token.read_text().strip(), timeout=90)
        self.assertIsNone(result['windows'][-1]['used'])
        self.assertIsNone(result['windows'][-1]['remaining'])
        self.assertEqual(result['windows'][-1]['measured_at'], collector.stamp(self.now))
        self.assertEqual(result['fable_probe_attempted_at'], collector.stamp(self.now))

    def test_existing_unexpired_fable_window_seeds_initial_hourly_budget(self):
        previous = self.seed_known_usage()
        previous.update(endpoint_next_attempt_at=collector.stamp(self.now + 7200))
        previous['windows'].append({**self.fable_windows(self.now + 300)[-1],
                                    'measured_at': collector.stamp(self.now - 100)})
        self.snapshot.write_text(json.dumps(previous))
        with patch.object(collector, 'fetch_usage') as fetch, \
                patch.object(collector, 'probe_usage', return_value=self.windows) as cli:
            result = self.collect()
            cli.assert_called_once_with(self.token.read_text().strip(), timeout=90)
            self.assertIsNone(result['fable_probe_attempted_at'])
            # No Fable attempt was made; the expired cached window no longer defers one.
            result = self.collect(self.now + 300)
            self.assertEqual(cli.call_args.kwargs, {'model': 'claude-fable-5-1', 'timeout': 90})
            self.assertEqual(result['fable_probe_attempted_at'], collector.stamp(self.now + 300))
            self.assertIsNone(result['windows'][-1]['used'])
        fetch.assert_not_called()

    def test_fable_failure_falls_back_to_haiku_and_waits_an_hour_even_without_window(self):
        for failure in (collector.UsageResponseError('cli_timeout'),
                        collector.UsageResponseError('no_usage_data'), OSError('private')):
            with self.subTest(failure=type(failure).__name__):
                self.snapshot.unlink(missing_ok=True)
                def probe(token, model='haiku', timeout=90):
                    if model == 'claude-fable-5-1':
                        raise failure
                    return self.windows
                with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                        patch.object(collector, 'probe_usage', side_effect=probe) as cli:
                    for offset in (0, 300, 3300, 3600):
                        result = self.collect(self.now + offset)
                        self.assertEqual((result['status'], result['source'], result['error']), ('ok', 'cli', None))
                    self.assertEqual([call.kwargs.get('model', 'haiku') for call in cli.call_args_list],
                                     ['claude-fable-5-1', 'haiku', 'haiku', 'haiku', 'claude-fable-5-1', 'haiku'])
                self.assertEqual(result['fable_probe_attempted_at'], collector.stamp(self.now + 3600))

    def test_rejected_fable_cli_with_telemetry_still_falls_back_to_haiku(self):
        event = json.loads(json.dumps(self.event))
        event['rate_limit_info']['unifiedWindows']['seven_day_overage_included'] = {
            'utilization': 1, 'resetsAt': self.now + 10000}
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                patch.object(collector.subprocess, 'run', side_effect=[
                    SimpleNamespace(stdout=json.dumps(event).encode(), returncode=1),
                    SimpleNamespace(stdout=json.dumps(self.event).encode(), returncode=0)]) as run:
            result = self.collect()
        self.assertEqual([call.args[0][call.args[0].index('--model') + 1] for call in run.call_args_list],
                         ['claude-fable-5-1', 'haiku'])
        self.assertEqual((result['status'], result['source'], result['error']), ('ok', 'cli', None))
        self.assertEqual(result['windows'][-1]['used'], 100)
        self.assertEqual(result['windows'][-1]['measured_at'], collector.stamp(self.now))
        self.assertEqual(result['fable_probe_attempted_at'], collector.stamp(self.now))

    def test_endpoint_model_windows_survive_cli_fallback_and_reload(self):
        payload = {'five_hour': {'utilization': 4}, 'seven_day': {'utilization': 16},
                   'limits': [{'kind': 'weekly_scoped', 'percent': 13,
                               'resets_at': collector.stamp(self.now + 10000),
                               'scope': {'model': {'display_name': 'Fable'}}}]}
        with patch.object(collector, 'fetch_usage', return_value=payload):
            result = self.collect()
        model = next(row for row in result['windows'] if row['kind'] == 'weekly_model')
        self.assertEqual((model['model'], model['label'], model['used']), ('Fable', 'Weekly · Fable', 13))
        self.assertEqual(model['measured_at'], collector.stamp(self.now))
        def rate_limited(token):
            raise HTTPError(collector.ENDPOINT, 429, '', {'Retry-After': '7200'}, None)
        for offset in (3600, 7200, 10000, 10300):
            with patch.object(collector, 'fetch_usage', side_effect=rate_limited), \
                    patch.object(collector, 'probe_usage', return_value=self.windows):
                result = self.collect(self.now + offset)
            retained = next(row for row in result['windows'] if row['kind'] == 'weekly_model')
            self.assertEqual(retained['measured_at'], model['measured_at'])
            self.assertEqual(retained['used'], 13 if offset < 10000 else None)
            self.assertEqual(collector.b_snapshot(self.snapshot, self.now + offset)['windows'][-1], retained)
            self.assertEqual(result['checked_at'], collector.stamp(self.now + offset))

    def test_cli_fable_bucket_is_parsed_and_refreshed_independently(self):
        event = json.loads(json.dumps(self.event))
        event['rate_limit_info']['unifiedWindows']['seven_day_overage_included'] = {
            'utilization': .13, 'resetsAt': self.now + 10000}
        output = json.dumps(event)
        windows = collector.parse_rate_limit_events(output)
        self.assertEqual(windows[-1]['model'], 'Fable')
        self.assertEqual(windows[-1]['used'], 13)
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                patch.object(collector, 'probe_usage', return_value=windows):
            result = self.collect()
        self.assertEqual(result['windows'][-1]['measured_at'], collector.stamp(self.now))
        # The representative field in older clients carries the same bucket.
        event['rate_limit_info'] = {'rateLimitType': 'seven_day_overage_included',
                                  'utilization': .2, 'resetsAt': self.now + 10000,
                                  'unifiedWindows': self.event['rate_limit_info']['unifiedWindows']}
        self.assertEqual(collector.parse_rate_limit_events(json.dumps(event))[-1]['used'], 20)

    def test_endpoint_and_cli_fable_identity_merge_without_duplicate_rows(self):
        payload = {'five_hour': {'utilization': 4}, 'seven_day': {'utilization': 16},
                   'seven_day_overage_included': {'utilization': 99},
                   'limits': [{'kind': 'weekly_scoped', 'percent': 13,
                               'scope': {'model': {'display_name': 'Fable'}}}]}
        with patch.object(collector, 'fetch_usage', return_value=payload):
            result = self.collect()
        self.assertEqual([r['kind'] for r in result['windows']], ['five_hour', 'seven_day', 'weekly_model'])
        self.assertEqual(result['windows'][-1]['used'], 13)
        event = json.loads(json.dumps(self.event))
        event['rate_limit_info']['unifiedWindows']['seven_day_overage_included'] = {'utilization': .14}
        windows = collector.parse_rate_limit_events(json.dumps(event))
        with patch.object(collector, 'fetch_usage'), patch.object(collector, 'probe_usage', return_value=windows):
            result = self.collect(self.now + 900)
        self.assertEqual(len(result['windows']), 3)
        self.assertAlmostEqual(result['windows'][-1]['used'], 14)
        self.assertEqual(result['windows'][-1]['measured_at'], collector.stamp(self.now + 900))

    def test_optional_model_window_does_not_age_primary_success_or_force_cli_probe(self):
        previous = self.seed_known_usage()
        previous.update(checked_at=collector.stamp(self.now), usage_checked_at=collector.stamp(self.now),
                        endpoint_next_attempt_at=collector.stamp(self.now + 3600))
        previous['windows'] = self.measured_windows(self.now) + [
            {'kind': 'weekly_model', 'model': 'Fable', 'used': 13,
             'resets_at': self.now, 'measured_at': collector.stamp(self.now - 3600)}]
        self.snapshot.write_text(json.dumps(previous))
        with patch.object(collector, 'fetch_usage') as fetch, patch.object(collector, 'probe_usage') as probe:
            result = self.collect()
        fetch.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(result['checked_at'], collector.stamp(self.now))
        self.assertIsNone(result['windows'][-1]['used'])
        self.assertEqual(result['windows'][-1]['measured_at'], collector.stamp(self.now - 3600))

    def test_invalid_model_samples_cannot_replace_cached_measurement(self):
        cached = [{'kind': 'weekly_model', 'model': 'Fable', 'used': 13,
                   'resets_at': self.now + 10000, 'measured_at': collector.stamp(self.now - 100)}]
        for value in (None, True, -1, '13', float('nan'), float('inf')):
            with self.subTest(value=value):
                payload = {'five_hour': {'utilization': 4}, 'limits': [
                    {'kind': 'weekly_scoped', 'percent': value, 'scope': {'model': {'display_name': 'Fable'}}}]}
                rows, _ = collector.parse_usage(payload, for_b=True)
                result = collector.b_windows(rows, cached, measured_at=self.now)
                self.assertEqual(result[-1]['model'], 'Fable')
                self.assertEqual(result[-1]['used'], 13)
                self.assertEqual(result[-1]['measured_at'], cached[0]['measured_at'])

    def test_model_only_samples_are_kept_without_refreshing_primary_success(self):
        for source in ('usage', 'cli'):
            with self.subTest(source=source):
                previous = self.seed_known_usage()
                payload = {'limits': [{'kind': 'weekly_scoped', 'percent': 13,
                                      'scope': {'model': {'display_name': 'Fable'}}}]}
                output = json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
                    'unifiedWindows': {'seven_day_overage_included': {'utilization': .13}}}})
                with patch.object(collector, 'fetch_usage', return_value=payload if source == 'usage' else {},
                                  side_effect=URLError('private') if source == 'cli' else None), \
                        patch.object(collector.subprocess, 'run', return_value=SimpleNamespace(
                            stdout=output.encode() if source == 'cli' else b'')):
                    result = self.collect()
                self.assertEqual(result['windows'][:2], previous['windows'])
                model = result['windows'][-1]
                self.assertEqual((model['model'], model['used'], model['measured_at']),
                                 ('Fable', 13, collector.stamp(self.now)))
                for key in ('checked_at', 'usage_checked_at', 'source'):
                    self.assertEqual(result[key], previous[key])
                self.assertEqual(result['error'], 'no_usage_data')

    def test_model_only_endpoint_cannot_publish_token_as_model_name(self):
        payload = {'limits': [{'kind': 'weekly_scoped', 'percent': 13,
                              'scope': {'model': {'display_name': self.token.read_text().strip()}}}]}
        with patch.object(collector, 'fetch_usage', return_value=payload), \
                patch.object(collector, 'probe_usage', return_value=self.windows):
            result = self.collect()
        self.assertNotIn(self.token.read_text().strip(), self.snapshot.read_text())
        self.assertEqual(result['endpoint_error'], 'parse')

    def test_configured_email_published_and_validated_without_profile(self):
        boundary = 'a' * 64 + '@' + 'b' * 63 + '.' + 'c' * 63 + '.' + 'd' * 61
        for value, expected in ((' account-b@example.com ', 'account-b@example.com'),
                                 (boundary, boundary), (boundary + 'd', None),
                                 ('bad', None), ('a@example.com\nother', None), ('', None)):
            with self.subTest(value=value), patch.dict(os.environ, {'CLAUDE_B_EMAIL': value}), \
                    patch.object(collector, 'fetch_profile') as profile:
                # Metadata is published even when the token is missing or budgets skip requests.
                result = collector.collect_b(self.root / 'missing-token', self.snapshot, self.now)
                self.assertEqual(result['email'], expected)
                self.assertEqual(json.loads(self.snapshot.read_text())['email'], expected)
                with patch.object(collector, 'fetch_usage', return_value={'seven_day': {'utilization': 16}}):
                    result = self.collect()
                self.assertEqual(result['email'], expected)
                self.assertEqual(json.loads(self.snapshot.read_text())['email'], expected)
                profile.assert_not_called()

    def test_endpoint_without_primary_utilization_falls_back_before_success(self):
        payloads = [
            {'seven_day_sonnet': {'utilization': 30}},
            {'limits': [{'kind': 'weekly_scoped', 'scope': {'model': {'display_name': 'Sonnet'}},
                         'percent': 30}]},
            {'extra_usage': {'is_enabled': True}},
            {'five_hour': {'resets_at': collector.stamp(self.now + 100)}},
        ] + [{'five_hour': {'utilization': value}, 'seven_day': None}
             for value in (None, True, -1, '4', {}, float('nan'), float('inf'))]
        for payload in payloads:
            with self.subTest(payload=payload):
                previous = self.seed_known_usage()

                def probe(token, model='haiku', timeout=90):
                    state = json.loads(self.snapshot.read_text())
                    self.assertEqual(state['windows'][:2], previous['windows'])
                    for key in ('checked_at', 'usage_checked_at', 'source'):
                        self.assertEqual(state[key], previous[key])
                    self.assertEqual(state['endpoint_error'], 'no_usage_data')
                    self.assertEqual(collector.epoch(state['endpoint_next_attempt_at']), self.now + 3600)
                    self.assertEqual(collector.epoch(state['cli_next_attempt_at']), self.now + collector.CLI_PROBE_INTERVAL)
                    return self.windows

                with patch.object(collector, 'fetch_usage', return_value=payload), \
                        patch.object(collector, 'probe_usage', side_effect=probe) as cli:
                    result = self.collect()
                    cli.assert_called_once()
                self.assertEqual((result['status'], result['source']), ('ok', 'cli'))
                self.assertEqual(result['checked_at'], collector.stamp(self.now))
                self.assertEqual(result['usage_checked_at'], previous['usage_checked_at'])

    def test_unusable_endpoint_and_cli_retain_values_success_times_and_budgets(self):
        previous = self.seed_known_usage()
        with patch.object(collector, 'fetch_usage', return_value={'seven_day_sonnet': {'utilization': 30}}), \
                patch.object(collector, 'probe_usage', return_value=[]) as cli:
            result = self.collect()
            self.assertEqual((result['status'], result['error'], result['endpoint_error']),
                             ('error', 'no_usage_data', 'no_usage_data'))
            self.assertEqual(result['windows'][:2], previous['windows'])
            for key in ('checked_at', 'usage_checked_at', 'source'):
                self.assertEqual(result[key], previous[key])
            self.collect(self.now + collector.CLI_PROBE_INTERVAL - 1)
            self.assertEqual(cli.call_count, 2)
        self.assertEqual(collector.epoch(result['endpoint_next_attempt_at']), self.now + 3600)
        self.assertEqual(collector.epoch(result['cli_next_attempt_at']), self.now + collector.CLI_PROBE_INTERVAL)

    def test_partial_sources_preserve_missing_or_malformed_other_window(self):
        for source in ('usage', 'cli'):
            for kind in ('five_hour', 'seven_day'):
                other = 'seven_day' if kind == 'five_hour' else 'five_hour'
                for invalid in ('absent', None, True, -1, '4', float('nan'), float('inf')):
                    with self.subTest(source=source, kind=kind, invalid=invalid):
                        previous = self.seed_known_usage()
                        reset = self.now + 1000
                        payload = {kind: {'utilization': 0, 'resets_at': collector.stamp(reset)}}
                        rows = [{'kind': kind, 'used': 0, 'resets_at': reset}]
                        if invalid != 'absent':
                            payload[other] = {'utilization': invalid, 'resets_at': collector.stamp(reset)}
                            rows.append({'kind': other, 'used': invalid, 'resets_at': reset})
                        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')
                                          if source == 'cli' else None, return_value=payload), \
                                patch.object(collector, 'probe_usage', return_value=rows) as cli:
                            result = self.collect()
                        self.assertEqual((result['status'], result['source']), ('ok', source))
                        selected = next(row for row in result['windows'] if row['kind'] == kind)
                        self.assertEqual((selected['used'], selected['remaining'], selected['resets_at']), (0, 100, reset))
                        self.assertEqual(next(row for row in result['windows'] if row['kind'] == other),
                                         next(row for row in previous['windows'] if row['kind'] == other))
                        self.assertEqual(selected['measured_at'], collector.stamp(self.now))
                        self.assertEqual(result['checked_at'], previous['checked_at'])
                        self.assertEqual(cli.call_count, int(source == 'cli'))

    def test_repeated_partial_samples_age_oldest_window_and_do_not_suppress_probe(self):
        previous = self.seed_known_usage()
        partial = {'five_hour': {'utilization': 5, 'resets_at': collector.stamp(self.now + 14000)}}
        with patch.object(collector, 'fetch_usage', return_value=partial), \
                patch.object(collector, 'probe_usage', return_value=[{'kind': 'five_hour', 'used': 6}]) as cli:
            result = self.collect()
            self.assertEqual(result['checked_at'], previous['checked_at'])
            result = self.collect(self.now + 300)
            cli.assert_called_once()
            self.assertEqual(result['checked_at'], previous['checked_at'])
            self.assertEqual(result['windows'][0]['measured_at'], collector.stamp(self.now + 300))
            self.assertEqual(result['windows'][1]['measured_at'], previous['checked_at'])
            self.assertEqual(collector.b_snapshot(self.snapshot)['checked_at'], previous['checked_at'])

    def test_due_retained_window_becomes_unknown_and_stays_unknown_on_reload(self):
        for source in ('usage', 'cli'):
            for offset in (0, -1):
                with self.subTest(source=source, offset=offset):
                    previous = self.seed_known_usage()
                    previous['windows'][1]['resets_at'] = self.now + offset
                    self.snapshot.write_text(json.dumps(previous))
                    with patch.object(collector, 'fetch_usage', side_effect=URLError('private')
                                      if source == 'cli' else None,
                                      return_value={'five_hour': {'utilization': 5}}), \
                            patch.object(collector, 'probe_usage', return_value=[{'kind': 'five_hour', 'used': 5}]):
                        result = self.collect()
                    weekly = result['windows'][1]
                    self.assertIsNone(weekly['used'])
                    self.assertIsNone(weekly['remaining'])
                    self.assertEqual(weekly['measured_at'], previous['checked_at'])
                    self.assertEqual(weekly['resets_at'], self.now + offset)
                    self.assertEqual(collector.b_snapshot(self.snapshot, self.now + 1)['windows'][1], weekly)

    def test_due_window_cleared_on_budget_skip_and_failed_probe(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                previous = self.seed_known_usage()
                previous['windows'][1]['resets_at'] = self.now
                previous['endpoint_next_attempt_at'] = collector.stamp(self.now + 3600)
                previous['cli_next_attempt_at'] = collector.stamp(self.now if failure else self.now + 900)
                self.snapshot.write_text(json.dumps(previous))
                with patch.object(collector, 'fetch_usage') as fetch, \
                        patch.object(collector, 'probe_usage', side_effect=collector.UsageResponseError('cli_timeout')) as cli:
                    result = self.collect()
                fetch.assert_not_called()
                self.assertEqual(cli.call_count, 2 * int(failure))
                self.assertIsNone(result['windows'][1]['used'])
                self.assertEqual(result['checked_at'], previous['checked_at'])

    def test_retained_reset_can_pass_during_either_request(self):
        for source in ('usage', 'cli'):
            for kind in ('five_hour', 'seven_day'):
                with self.subTest(source=source, kind=kind):
                    other = 'seven_day' if kind == 'five_hour' else 'five_hour'
                    previous = self.seed_known_usage()
                    row = next(w for w in previous['windows'] if w['kind'] == kind)
                    row['resets_at'] = self.now + 1
                    self.snapshot.write_text(json.dumps(previous))
                    clock = [self.now]
                    def finish_request(payload):
                        clock[0] = self.now + 2
                        return payload
                    with patch.object(collector, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])), \
                            patch.object(collector, 'fetch_usage', side_effect=URLError('private')
                                          if source == 'cli' else lambda token: finish_request({other: {'utilization': 5}})), \
                            patch.object(collector, 'probe_usage', side_effect=lambda token, model='haiku', timeout=90: finish_request([{'kind': other, 'used': 5}])):
                        result = collector.collect_b(self.token, self.snapshot)
                    retained = next(w for w in result['windows'] if w['kind'] == kind)
                    self.assertIsNone(retained['used'])
                    self.assertEqual(retained['measured_at'], previous['checked_at'])
                    self.assertEqual(next(w for w in result['windows'] if w['kind'] == other)['measured_at'],
                                     collector.stamp(self.now + 2))

    def test_initial_partial_sample_cannot_date_an_unmeasured_window(self):
        with patch.object(collector, 'fetch_usage', return_value={'five_hour': {'utilization': 4}}):
            result = self.collect()
        self.assertIsNone(result['windows'][1]['measured_at'])
        self.assertIsNone(result['checked_at'])
        self.assertEqual(collector.b_snapshot(self.snapshot)['windows'][0]['measured_at'], collector.stamp(self.now))

    def test_full_sample_refreshes_both_measurements_and_oldest_success(self):
        for source in ('usage', 'cli'):
            with self.subTest(source=source):
                self.seed_known_usage()
                payload = {row['kind']: {'utilization': row['used']} for row in self.windows}
                with patch.object(collector, 'fetch_usage', side_effect=URLError('private')
                                  if source == 'cli' else None, return_value=payload), \
                        patch.object(collector, 'probe_usage', return_value=self.windows):
                    result = self.collect()
                self.assertEqual([row['measured_at'] for row in result['windows']],
                                 [collector.stamp(self.now)] * 2)
                self.assertEqual(result['checked_at'], collector.stamp(self.now))
                self.assertEqual(collector.b_snapshot(self.snapshot)['checked_at'], result['checked_at'])

    def test_missing_or_malformed_measurement_is_not_freshened_from_success(self):
        for measured in (None, 'private', True, {}, []):
            with self.subTest(measured=measured):
                previous = self.seed_known_usage()
                previous['windows'][1]['measured_at'] = measured
                previous['checked_at'] = collector.stamp(self.now)
                self.snapshot.write_text(json.dumps(previous))
                result = collector.b_snapshot(self.snapshot)
                self.assertIsNone(result['windows'][1]['measured_at'])
                self.assertIsNone(result['checked_at'])
                self.assertNotIn('private', json.dumps(result))

    def test_cli_without_primary_utilization_does_not_count_as_success(self):
        infos = [{}, {'status': 'allowed'}, {'unifiedWindows': {}},
                 {'rateLimitType': 'seven_day', 'resetsAt': self.now + 100},
                 {'unifiedWindows': {'five_hour': {'resetsAt': self.now + 100}}}]
        infos += [{'rateLimitType': 'five_hour', 'utilization': value, 'resetsAt': self.now + 100}
                  for value in (None, True, -1, '0.2', float('nan'), float('inf'), 1e308)]
        for info in infos:
            with self.subTest(info=info):
                previous = self.seed_known_usage()
                output = json.dumps({'type': 'rate_limit_event', 'rate_limit_info': info}).encode()
                with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                        patch.object(collector.subprocess, 'run', return_value=SimpleNamespace(stdout=output)):
                    result = self.collect()
                self.assertEqual((result['status'], result['error']), ('error', 'no_usage_data'))
                for key in ('windows', 'checked_at', 'usage_checked_at', 'source'):
                    self.assertEqual(result[key], previous[key])

    def test_later_empty_or_malformed_events_do_not_erase_valid_cli_sample(self):
        for info in ({}, {'unifiedWindows': {'five_hour': {'utilization': -1}}},
                     {'rateLimitType': 'seven_day', 'resetsAt': self.now + 100}):
            with self.subTest(info=info):
                output = '\n'.join(json.dumps(event) for event in
                    (self.event, {'type': 'rate_limit_event', 'rate_limit_info': info}))
                self.assertEqual(collector.parse_rate_limit_events(output), self.windows)

    def test_invalid_cached_primary_windows_do_not_suppress_probe(self):
        self.snapshot.write_text(json.dumps({'windows': [{'kind': 'five_hour', 'used': -1},
            {'kind': 'seven_day_sonnet', 'used': 30}], 'status': 'ok', 'source': 'usage',
            'usage_checked_at': collector.stamp(self.now),
            'endpoint_next_attempt_at': collector.stamp(self.now + 3600)}))
        with patch.object(collector, 'fetch_usage') as fetch, \
                patch.object(collector, 'probe_usage', return_value=self.windows) as cli:
            result = self.collect()
        fetch.assert_not_called()
        cli.assert_called_once()
        self.assertEqual((result['status'], result['source'], result['windows'][:2]), ('ok', 'cli', self.measured_windows(self.now)))
        self.assertEqual(result['windows'][-1]['kind'], 'seven_day_sonnet')
        self.assertIsNone(result['windows'][-1]['measured_at'])

    def test_endpoint_success_shared_parsing_no_profile_or_probe(self):
        payload = {'five_hour': {'utilization': 4, 'resets_at': collector.stamp(self.now + 14000)},
                   'seven_day': {'utilization': 16, 'resets_at': collector.stamp(self.now + 300000)},
                   'extra_usage': {'is_enabled': True, 'used_credits': 100},
                   'cedar_ember': {'eligible': True}, 'secret': 'private-test-token'}
        with patch.object(collector, 'fetch_usage', return_value=payload) as fetch, \
                patch.object(collector, 'fetch_profile') as profile, \
                patch.object(collector, 'probe_usage') as probe:
            result = self.collect()
            self.assertEqual(result['windows'], self.measured_windows(self.now))
            self.assertEqual((result['id'], result['label'], result['plan'], result['status'], result['source']),
                             ('claude-b', 'Claude B', None, 'ok', 'usage'))
            for offset in (300, 899):
                self.assertEqual(self.collect(self.now + offset)['checked_at'], result['checked_at'])
            self.assertEqual(fetch.call_count, 1)
            probe.assert_not_called()
            profile.assert_not_called()
            self.assertIsNone(result['fable_probe_attempted_at'])
        self.assertIsNone(result['extra_usage'])
        self.assertIsNone(result['resets'])
        self.assertIsNone(result['email'])
        self.assertNotIn('private-test-token', self.snapshot.read_text())
        self.assertEqual(self.snapshot.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.token.read_text(), 'private-test-token\n')

    def test_retry_after_and_cli_budgets_persist_before_dispatch(self):
        def endpoint(token):
            state = json.loads(self.snapshot.read_text())
            self.assertEqual(collector.epoch(state['endpoint_next_attempt_at']), self.now + 3600)
            raise HTTPError(collector.ENDPOINT, 429, '', {'Retry-After': '7200'}, None)

        def probe(token, model='haiku', timeout=90):
            state = json.loads(self.snapshot.read_text())
            self.assertEqual(collector.epoch(state['cli_next_attempt_at']), self.now + collector.CLI_PROBE_INTERVAL)
            return self.windows

        with patch.object(collector, 'fetch_usage', side_effect=endpoint) as fetch, \
                patch.object(collector, 'probe_usage', side_effect=probe) as cli:
            result = self.collect()
        self.assertEqual(result['endpoint_error'], 'http_429')
        self.assertEqual(collector.epoch(result['endpoint_next_attempt_at']), self.now + 7200)
        self.assertEqual((result['status'], result['source']), ('ok', 'cli'))
        with patch.object(collector, 'fetch_usage') as fetch, \
                patch.object(collector, 'probe_usage', return_value=self.windows) as cli:
            for offset in (1, collector.CLI_PROBE_INTERVAL - 1):
                self.assertEqual(self.collect(self.now + offset)['checked_at'], result['checked_at'])
            cli.assert_not_called()
            self.collect(self.now + collector.CLI_PROBE_INTERVAL)
            self.assertEqual(cli.call_count, 1)
            self.collect(self.now + 3600)
            self.assertEqual(cli.call_count, 2)
            fetch.assert_not_called()
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')) as fetch, \
                patch.object(collector, 'probe_usage', return_value=self.windows):
            self.collect(self.now + 7200)
            fetch.assert_called_once()

    def test_hourly_attempt_including_errors_and_endpoint_data_aging(self):
        with patch.object(collector, 'fetch_usage', return_value={'seven_day': {'utilization': 10}}) as fetch, \
                patch.object(collector, 'probe_usage', return_value=self.windows) as cli:
            self.collect()
            cli.assert_not_called()
            self.collect(self.now + collector.CLI_PROBE_INTERVAL)
            self.collect(self.now + 2 * collector.CLI_PROBE_INTERVAL - 1)
            self.assertEqual(cli.call_count, 1)
            self.collect(self.now + 2 * collector.CLI_PROBE_INTERVAL)
            self.assertEqual(cli.call_count, 2)
            self.assertEqual(fetch.call_count, 1)
            self.collect(self.now + 3600)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(cli.call_count, 2)

    def test_failed_probe_keeps_values_and_timestamp_and_throttles(self):
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                patch.object(collector, 'probe_usage', return_value=self.windows):
            success = self.collect()
        with patch.object(collector, 'fetch_usage') as fetch, \
                patch.object(collector, 'probe_usage', side_effect=collector.UsageResponseError('cli_timeout')) as cli:
            failed = self.collect(self.now + collector.CLI_PROBE_INTERVAL)
            self.assertEqual((failed['status'], failed['error']), ('error', 'cli_timeout'))
            self.assertEqual(failed['windows'], success['windows'])
            self.assertEqual(failed['checked_at'], success['checked_at'])
            self.collect(self.now + 2 * collector.CLI_PROBE_INTERVAL - 1)
            self.assertEqual(cli.call_count, 1)
            fetch.assert_not_called()

    def test_missing_empty_malformed_token_never_requests_and_preserves_budgets(self):
        with patch.object(collector, 'fetch_usage') as fetch, patch.object(collector, 'probe_usage') as cli:
            for contents in (None, '', '\n', 'one\ntwo\n'):
                if contents is None:
                    self.token.unlink()
                else:
                    self.token.write_text(contents)
                result = self.collect()
                self.assertEqual((result['status'], result['error']), ('missing_token', 'missing_token'))
                self.assertIsNone(result['endpoint_next_attempt_at'])
                self.assertIsNone(result['cli_next_attempt_at'])
                self.assertEqual([row['used'] for row in result['windows']], [None, None])
            fetch.assert_not_called()
            cli.assert_not_called()
        self.token.write_text('private-test-token\n')
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')), \
                patch.object(collector, 'probe_usage', return_value=self.windows):
            self.assertEqual(self.collect()['status'], 'ok')

    def test_retry_after_dates_invalid_and_short_values(self):
        date = dt.datetime.fromtimestamp(self.now + 7200, dt.timezone.utc)
        self.assertEqual(collector.retry_after(date.strftime('%a, %d %b %Y %H:%M:%S GMT'), self.now), self.now + 7200)
        for value in (None, 'private', '-2', 'nan', 'inf'):
            self.assertEqual(collector.retry_after(value, self.now), self.now)
        with patch.object(collector, 'fetch_usage', side_effect=HTTPError(
                collector.ENDPOINT, 429, '', {'Retry-After': '30'}, None)), \
                patch.object(collector, 'probe_usage', return_value=self.windows):
            self.assertEqual(collector.epoch(self.collect()['endpoint_next_attempt_at']), self.now + 3600)

    def test_event_fractions_unknowns_limits_and_other_output(self):
        self.assertEqual([row['used'] for row in self.windows], [4, 16])
        self.assertEqual([row['seconds'] for row in self.windows], [18000, 604800])
        event = {'type': 'rate_limit_event', 'rate_limit_info': {
            'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': self.now + 1}}
        with self.assertRaisesRegex(collector.UsageResponseError, 'no_usage_data'):
            collector.parse_rate_limit_events(json.dumps(event))
        event['rate_limit_info'].update(utilization=1.2)
        result = collector.parse_rate_limit_events('\n'.join(('not json', 'null',
            json.dumps({'type': 'assistant', 'message': {'text': 'private'}}), json.dumps(event))))
        self.assertEqual([row['used'] for row in result], [100, None])
        for value in (True, -1, '0.2', None):
            event['rate_limit_info']['utilization'] = value
            with self.assertRaisesRegex(collector.UsageResponseError, 'no_usage_data'):
                collector.parse_rate_limit_events(json.dumps(event))
        with self.assertRaisesRegex(collector.UsageResponseError, 'no_usage_data'):
            collector.parse_rate_limit_events('{"type":"result","result":"private"}')

    def test_cli_is_hardened_scrubbed_and_raw_output_never_saved(self):
        output = json.dumps(self.event).encode()
        def run(command, **kwargs):
            self.assertEqual(command[:6], [str(collector.CLAUDE), '-p', '--output-format', 'stream-json', '--verbose', '--model'])
            for option, value in (('--model', model), ('--tools', ''), ('--max-turns', '1'), ('--setting-sources', '')):
                self.assertEqual(command[command.index(option) + 1], value)
            for flag in ('--no-session-persistence', '--strict-mcp-config'):
                self.assertIn(flag, command)
            self.assertEqual(kwargs['env']['CLAUDE_CODE_OAUTH_TOKEN'], 'private-test-token')
            for key, value in (('CLAUDE_CODE_MAX_OUTPUT_TOKENS', '8'), ('MAX_THINKING_TOKENS', '0'),
                               ('CLAUDE_CODE_MAX_RETRIES', '0')):
                self.assertEqual(kwargs['env'][key], value)
            for key in ('ANTHROPIC_API_KEY', 'HTTPS_PROXY', 'CLAUDE_CODE_API_BASE_URL', 'CLAUDE_CONFIG_DIR'):
                self.assertNotIn(key, kwargs['env'])
            self.assertNotEqual(kwargs['cwd'], str(ROOT))
            self.assertTrue(Path(kwargs['cwd']).is_dir())
            self.assertEqual(kwargs['env']['TMPDIR'], kwargs['cwd'])
            self.assertEqual(kwargs['timeout'], 90)
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            return SimpleNamespace(stdout=output, returncode=0 if model == 'claude-fable-5-1' else 1)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'private', 'HTTPS_PROXY': 'private',
                                    'CLAUDE_CODE_API_BASE_URL': 'private', 'CLAUDE_CONFIG_DIR': 'private'}), \
                patch.object(collector.subprocess, 'run', side_effect=run):
            for model in ('haiku', 'claude-fable-5-1'):
                with self.subTest(model=model):
                    self.assertEqual(collector.probe_usage('private-test-token', model=model), self.windows)
        with patch.object(collector.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private', 90)):
            with self.assertRaisesRegex(collector.UsageResponseError, 'cli_timeout'):
                collector.probe_usage('private-test-token')
        for output in (b'private-test-token', b'x' * (1024 * 1024 + 1)):
            with patch.object(collector.subprocess, 'run', return_value=SimpleNamespace(stdout=output)):
                with self.assertRaisesRegex(collector.UsageResponseError, 'parse'):
                    collector.probe_usage('private-test-token')

    def test_plan_comes_from_validated_service_setting(self):
        for value, expected in ((' Max (5x) ', 'Max (5x)'), ('Pro', 'Pro'), ('Max (20x)', 'Max (20x)'),
                                 ('Max', None), ('pro', None), ('', None)):
            with self.subTest(value=value), patch.dict(os.environ, {'CLAUDE_B_PLAN': value}):
                result = collector.collect_b(self.root / 'missing-token', self.snapshot, self.now)
                self.assertEqual(result['plan'], expected)
                self.assertEqual(json.loads(self.snapshot.read_text())['plan'], expected)

    def test_saved_state_is_allowlisted(self):
        self.snapshot.write_text(json.dumps({'id': 'evil', 'label': 'evil', 'plan': 'Max',
            'email': 'other@example.com', 'source': 'evil', 'endpoint_error': 'private',
            'error': 'private', 'accessToken': 'private', 'extra_usage': {'enabled': True},
            'resets': {'eligible': True}, 'windows': [{'kind': 'seven_day', 'used': 16}],
            'endpoint_next_attempt_at': 'private', 'fable_probe_attempted_at': 'private'}))
        data = collector.b_snapshot(self.snapshot)
        self.assertEqual((data['id'], data['label'], data['plan']), ('claude-b', 'Claude B', None))
        for key in ('source', 'endpoint_error', 'error', 'email', 'extra_usage', 'resets',
                    'endpoint_next_attempt_at', 'fable_probe_attempted_at'):
            self.assertIsNone(data[key])
        self.assertNotIn('private', json.dumps(data))


if __name__ == '__main__':
    unittest.main()
