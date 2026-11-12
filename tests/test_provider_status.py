import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import provider_status

spec = importlib.util.spec_from_file_location('collect_provider_status', ROOT / 'scripts/collect-provider-status.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
NOW = 1793448000
STAMP = provider_status.stamp(NOW)


def sample(source):
    return json.loads((ROOT / 'tests/fixtures/provider-status' / (source + '.json')).read_text())


class ProviderStatusTests(unittest.TestCase):
    def setUp(self):
        # A forgotten mock must fail locally, never contact a real feed.
        guard = patch('urllib.request.HTTPSHandler.https_open', side_effect=AssertionError('Network forbidden in tests'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_saved_public_feed_shapes(self):
        for source in collector.FEEDS:
            with self.subTest(source=source):
                row = collector.normalize(source, sample(source), NOW)
                self.assertEqual(row['indicator'], 'operational')
                self.assertIsNone(row['incident'])

    def test_indicator_normalisation(self):
        for raw, expected in {
            'none': 'operational', 'operational': 'operational', 'minor': 'minor',
            'major': 'major', 'critical': 'critical', 'maintenance': 'maintenance',
            'degraded_performance': 'minor', 'partial_outage': 'major',
            'major_outage': 'critical', 'full_outage': 'critical', 'under_maintenance': 'maintenance',
            'new-upstream-state': 'unknown', None: 'unknown',
        }.items():
            with self.subTest(raw=raw):
                self.assertEqual(collector.indicator(raw), expected)

    def test_anthropic_filters_consumer_outage_and_incident(self):
        payload = sample('anthropic')
        payload['status']['indicator'] = 'critical'
        payload['components'][0]['status'] = 'major_outage'
        payload['incidents'] = [dict(name='Consumer chat outage', status='investigating', impact='critical',
                                     components=[payload['components'][0]])]
        row = collector.normalize('anthropic', payload, NOW)
        self.assertEqual(row['indicator'], 'operational')
        self.assertIsNone(row['incident'])

    def test_anthropic_chooses_worst_relevant_active_incident(self):
        payload = sample('anthropic')
        api, code = payload['components'][2:4]
        api['status'] = 'degraded_performance'
        code['status'] = 'partial_outage'
        payload['incidents'] = [dict(name='API latency', status='monitoring', impact='minor', components=[api]),
                                dict(name='Code outage', status='identified', impact='major', components=[code]),
                                dict(name='Resolved outage', status='resolved', impact='critical', components=[api])]
        row = collector.normalize('anthropic', payload, NOW)
        self.assertEqual(row['indicator'], 'major')
        self.assertEqual(row['incident'], 'Code outage')

    def test_statuspage_falls_back_to_overall_when_components_absent(self):
        for source in ('anthropic', 'moonshot', 'devin'):
            with self.subTest(source=source):
                payload = {'status': {'indicator': 'minor'}}
                self.assertEqual(collector.normalize(source, payload, NOW)['indicator'], 'minor')

    def test_statuspage_active_maintenance_but_not_future_maintenance(self):
        payload = sample('moonshot')
        payload['status']['indicator'] = 'maintenance'
        payload['scheduled_maintenances'] = [dict(name='Future work', status='scheduled', components=[]),
                                             dict(name='Current work', status='in_progress', components=[])]
        row = collector.normalize('moonshot', payload, NOW)
        self.assertEqual(row['indicator'], 'maintenance')
        self.assertEqual(row['incident'], 'Current work')

    def test_openai_includes_codex_absent_from_compatibility_summary(self):
        payload = sample('openai')
        codex = next(i['group'] for i in payload['summary']['structure']['items'] if i['group']['name'] == 'Codex')
        component = codex['components'][0]['component_id']
        payload['summary']['affected_components'] = [dict(component_id=component, status='full_outage')]
        payload['summary']['ongoing_incidents'] = [dict(name='Codex outage', status='investigating',
            affected_components=[dict(component_id=component, status='full_outage')], component_impacts=[
                dict(component_id=component, status='full_outage', start_at=provider_status.stamp(NOW - 60))])]
        row = collector.normalize('openai', payload, NOW)
        self.assertEqual(row['indicator'], 'critical')
        self.assertEqual(row['incident'], 'Codex outage')

    def test_openai_filters_chatgpt_incident_and_old_component_impacts(self):
        payload = sample('openai')
        chat = next(i['group'] for i in payload['summary']['structure']['items'] if i['group']['name'] == 'ChatGPT')
        component = chat['components'][0]['component_id']
        api = payload['summary']['structure']['items'][0]['group']['components'][0]['component_id']
        payload['summary']['affected_components'] = [dict(component_id=component, status='full_outage')]
        payload['summary']['ongoing_incidents'] = [dict(name='ChatGPT outage', status='monitoring',
            affected_components=[dict(component_id=component, status='full_outage')], component_impacts=[
                dict(component_id=api, status='full_outage', end_at=provider_status.stamp(NOW - 60))])]
        row = collector.normalize('openai', payload, NOW)
        self.assertEqual(row['indicator'], 'operational')
        self.assertIsNone(row['incident'])

    def test_openai_missing_groups_is_unknown_at_collection_boundary(self):
        row = collector.collect(load=lambda _: {'summary': {'structure': {'items': []}}}, clock=lambda: NOW)
        self.assertEqual(row['provider_status']['codex']['indicator'], 'unknown')

    def test_openai_ongoing_incident_with_expired_selected_impacts_is_ignored(self):
        payload = sample('openai')
        api = payload['summary']['structure']['items'][0]['group']['components'][0]['component_id']
        payload['summary']['ongoing_incidents'] = [dict(name='Recovered API outage', status='monitoring',
            affected_components=[dict(component_id=api, status='full_outage')], component_impacts=[
                dict(component_id=api, status='full_outage', start_at=provider_status.stamp(NOW - 120),
                     end_at=provider_status.stamp(NOW - 60)),
                dict(component_id=api, status='partial_outage', start_at=provider_status.stamp(NOW - 60),
                     end_at=STAMP)])]
        row = collector.normalize('openai', payload, NOW)
        self.assertEqual(row['indicator'], 'operational')
        self.assertIsNone(row['incident'])

    def test_openai_incident_without_impact_history_uses_affected_components(self):
        payload = sample('openai')
        api = payload['summary']['structure']['items'][0]['group']['components'][0]['component_id']
        payload['summary']['ongoing_incidents'] = [dict(name='API outage', status='investigating',
            affected_components=[dict(component_id=api, status='full_outage')], component_impacts=[])]
        row = collector.normalize('openai', payload, NOW)
        self.assertEqual(row['indicator'], 'critical')
        self.assertEqual(row['incident'], 'API outage')

    def test_google_filters_resolved_and_unrelated_products(self):
        payload = sample('google')
        payload.append(dict(end=None, affected_products=[dict(title='Google Compute Engine')],
                            status_impact='SERVICE_OUTAGE', external_desc='Unrelated outage'))
        row = collector.normalize('google', payload, NOW)
        self.assertEqual(row['indicator'], 'operational')
        self.assertIn('Gemini/Vertex AI', row['description'])
        self.assertIsNone(row['incident'])

    def test_google_active_incidents_impact_and_most_relevant_title(self):
        for impact, expected in [('SERVICE_INFORMATION', 'minor'), ('SERVICE_DISRUPTION', 'major'),
                                 ('SERVICE_OUTAGE', 'critical'), ('NEW_STATUS', 'unknown')]:
            with self.subTest(impact=impact):
                payload = [dict(end=None, affected_products=[dict(title='Vertex Gemini API')],
                                status_impact=impact, external_desc='Gemini errors')]
                row = collector.normalize('google', payload, NOW)
                self.assertEqual(row['indicator'], expected)
                self.assertEqual(row['incident'], 'Gemini errors')

    def test_xai_component_fixtures_reach_both_dashboard_accounts(self):
        for fixture, expected in [('xai', 'operational'), ('xai-degraded', 'major'),
                                  ('xai-outage', 'critical'), ('xai-maintenance', 'maintenance'),
                                  ('xai-unknown', 'unknown'), ('xai-malformed', 'unknown')]:
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as directory:
                payload = sample(fixture)
                data = collector.collect(load=lambda _: payload, clock=lambda: NOW)
                path = Path(directory) / 'status.json'
                collector.write_snapshot(path, data)
                read = provider_status.read_snapshot(path, NOW)
                for provider in ('xai', 'xai-api-key'):
                    self.assertEqual(data['provider_status'][provider]['indicator'], expected)
                    self.assertEqual(read[provider]['indicator'], expected)
                    self.assertEqual(read[provider]['url'], 'https://status.x.ai/')
                    self.assertEqual(read[provider]['checked_at'], STAMP)
                self.assertEqual(data['provider_status']['xai'], data['provider_status']['xai-api-key'])

    def test_xai_status_vocabulary_on_product_and_component(self):
        for raw, expected in [('available', 'operational'), ('info', 'minor'), ('disruption', 'major'),
                              ('outage', 'critical'), ('maintenance', 'maintenance'),
                              ('no_data', 'unknown'), ('empty', 'unknown'), ('new-state', 'unknown'),
                              (None, 'unknown'), ([], 'unknown')]:
            for level in ('product', 'component'):
                with self.subTest(raw=raw, level=level):
                    payload = sample('xai')
                    product = payload['products'][1]
                    target = product if level == 'product' else product['components'][0]
                    target['status'] = raw
                    row = collector.normalize('xai', payload, NOW)
                    self.assertEqual(row['indicator'], expected)
                    self.assertIsNone(row['incident'])

    def test_xai_uses_worst_api_status_and_ignores_consumer_and_overall_outages(self):
        payload = sample('xai')
        payload['overall']['status'] = 'outage'
        payload['products'][0]['status'] = 'outage'
        payload['products'][0]['components'][0]['status'] = 'outage'
        self.assertEqual(collector.normalize('xai', payload, NOW)['indicator'], 'operational')
        api = payload['products'][1]
        api['status'] = 'disruption'
        api['components'][0]['status'] = 'maintenance'
        api['components'][1]['status'] = 'outage'
        self.assertEqual(collector.normalize('xai', payload, NOW)['indicator'], 'critical')

    def test_xai_requires_fresh_dated_api_product_and_components(self):
        invalid = []
        for updated in (None, 'bad', '2026-10-31T12:00:00', provider_status.stamp(NOW + 1),
                        provider_status.stamp(NOW - provider_status.MAX_AGE - 1)):
            payload = sample('xai')
            payload['updated_at'] = updated
            invalid.append(payload)
        for products in ([], [sample('xai')['products'][0]], [None], {},
                         [sample('xai')['products'][1]] * 2):
            payload = sample('xai')
            payload['products'] = products
            invalid.append(payload)
        for components in (None, [], {}, [None]):
            payload = sample('xai')
            payload['products'][1]['components'] = components
            invalid.append(payload)
        payload = sample('xai')
        del payload['products'][1]['components']
        invalid.append(payload)
        for payload in invalid:
            with self.subTest(payload=payload):
                data = collector.collect(load=lambda _: payload, clock=lambda: NOW)['provider_status']
                self.assertEqual(data['xai']['indicator'], 'unknown')
                self.assertEqual(data['xai']['checked_at'], STAMP)
        payload = sample('xai')
        payload['updated_at'] = provider_status.stamp(NOW - provider_status.MAX_AGE)
        self.assertEqual(collector.normalize('xai', payload, NOW)['indicator'], 'operational')

    def test_malformed_feeds_never_turn_green(self):
        for payload in (None, [], {}, {'status': {'indicator': 'new-state'}}, {'status': 'bad'},
                        {'status': {'indicator': 'none'}, 'components': [None]}):
            for source in collector.FEEDS:
                if payload == [] and source == 'google':
                    continue  # Google's empty incident list is a valid healthy feed.
                with self.subTest(source=source, payload=payload):
                    data = collector.collect(load=lambda _: copy.deepcopy(payload), clock=lambda: NOW)
                    provider = next(p for p, s in provider_status.PROVIDERS.items() if s == source)
                    self.assertEqual(data['provider_status'][provider]['indicator'], 'unknown')

    def test_source_failure_isolated_and_all_mappings_present(self):
        calls = []
        def load(url):
            source = next(s for s, value in collector.FEEDS.items() if value == url)
            calls.append(source)
            if source == 'anthropic':
                raise TimeoutError()
            return sample(source)
        data = collector.collect(load=load, clock=lambda: NOW)['provider_status']
        self.assertCountEqual(calls, collector.FEEDS)
        self.assertEqual(set(data), set(provider_status.PROVIDERS))
        self.assertEqual(data['codex'], data['codex-api-key'])
        self.assertEqual(data['kimi'], data['kimi-ai'])
        self.assertEqual(data['claude']['indicator'], 'unknown')
        self.assertEqual(data['devin']['indicator'], 'operational')
        self.assertEqual(data['gemini-api-key']['indicator'], 'operational')
        self.assertEqual(data['antigravity']['indicator'], 'unknown')
        self.assertEqual(data['xai']['indicator'], 'operational')
        self.assertEqual(data['xai'], data['xai-api-key'])
        self.assertEqual(data['codex']['checked_at'], STAMP)
        self.assertNotIn('openai-compatibility', data)
        self.assertNotIn('meta', data)

    def test_missing_corrupt_and_oversized_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            for content in (None, b'invalid', b'\xff', b'[]', b'null', b'{"provider_status":[]}', b'x' * (provider_status.MAX_BYTES + 1)):
                with self.subTest(content=None if content is None else content[:30]):
                    if content is not None:
                        path.write_bytes(content)
                    data = provider_status.read_snapshot(path, NOW)
                    self.assertEqual(set(data), set(provider_status.PROVIDERS))
                    self.assertTrue(all(row['indicator'] == 'unknown' for row in data.values()))
                    self.assertEqual(data['codex']['url'], 'https://status.openai.com/')

    def test_stale_missing_invalid_and_future_timestamps_per_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            data = collector.collect(load=lambda _: sample('anthropic'), clock=lambda: NOW)
            for checked in (None, '', 'not-a-date', '2026-10-31T12:00:00', provider_status.stamp(NOW + 60),
                            provider_status.stamp(NOW - provider_status.MAX_AGE - 1)):
                with self.subTest(checked=checked):
                    data['provider_status']['claude']['checked_at'] = checked
                    path.write_text(json.dumps(data))
                    row = provider_status.read_snapshot(path, NOW)['claude']
                    self.assertEqual(row['indicator'], 'unknown')
                    self.assertEqual(row['description'], 'Status unavailable')
                    self.assertEqual(row['url'], provider_status.SOURCES['anthropic'][1])
                    self.assertNotIn('incident', row)
            data['provider_status']['claude']['checked_at'] = provider_status.stamp(NOW - provider_status.MAX_AGE)
            path.write_text(json.dumps(data))
            self.assertEqual(provider_status.read_snapshot(path, NOW)['claude']['indicator'], 'operational')

    def test_reader_revalidates_rows_and_uses_canonical_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            data = {'provider_status': {'codex': dict(indicator='minor', description='API degraded', checked_at=STAMP,
                                                     incident='API errors', url='https://example.com/', source='other')}}
            path.write_text(json.dumps(data))
            result = provider_status.read_snapshot(path, NOW)
            self.assertEqual(result['codex']['incident'], 'API errors')
            self.assertEqual(result['codex']['source'], 'OpenAI')
            self.assertEqual(result['codex']['url'], 'https://status.openai.com/')
            self.assertEqual(result['claude']['indicator'], 'unknown')
            for update in ({'indicator': []}, {'description': None}, {'incident': {}}, {'indicator': 'new-state'}):
                with self.subTest(update=update):
                    changed = copy.deepcopy(data)
                    changed['provider_status']['codex'].update(update)
                    path.write_text(json.dumps(changed))
                    self.assertEqual(provider_status.read_snapshot(path, NOW)['codex']['indicator'], 'unknown')

    def test_atomic_public_snapshot_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'provider-status/status.json'
            # systemd supplies this directory via RuntimeDirectoryMode=0755;
            # the collector itself runs with UMask=0077.
            path.parent.mkdir()
            path.parent.chmod(0o755)
            self.addCleanup(os.umask, os.umask(0o077))
            data = collector.collect(load=lambda url: sample(next(s for s, u in collector.FEEDS.items() if u == url)),
                                     clock=lambda: NOW)
            collector.write_snapshot(path, data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(provider_status.read_snapshot(path, NOW)['codex']['indicator'], 'operational')
            old = path.read_bytes()
            with self.assertRaises(TypeError):
                collector.write_snapshot(path, {'bad': object()})
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual([p.name for p in path.parent.iterdir()], ['status.json'])

    def test_fetch_rejects_unlisted_and_non_https_urls(self):
        for url in ('http://status.openai.com/api/v2/summary.json', 'https://example.com/', 'http://127.0.0.1/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                collector.fetch(url)

    def test_fetch_bounded_no_proxy_and_no_redirect(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"status":{"indicator":"none"}}'
        opener = Mock()
        opener.open.return_value = response
        with patch.object(collector, 'build_opener', return_value=opener) as build:
            self.assertEqual(collector.fetch(collector.FEEDS['anthropic'])['status']['indicator'], 'none')
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], collector.NoRedirects)
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 8)
            response.read.assert_called_once_with(2 * 1024 * 1024 + 1)
            response.read.return_value = b'x' * (2 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                collector.fetch(collector.FEEDS['anthropic'])
        with self.assertRaises(ValueError):
            collector.NoRedirects().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_landlock_denies_binds_and_all_tcp_connect_ports_except_https(self):
        libc = Mock()
        operations = []
        def syscall(number, *args):
            if number == 444 and args[-1] == 1:
                return 4
            if number == 444:
                rules = args[0]._obj
                operations.append(('rules', rules.handled_access_fs, rules.handled_access_net))
                return 123
            if number == 445:
                rule = args[2]._obj
                operations.append(('port', args[0], args[1], rule.allowed_access, rule.port))
            elif number == 446:
                operations.append(('restrict', *args))
            return 0
        libc.syscall.side_effect = syscall
        libc.prctl.return_value = 0
        with patch.object(collector.ctypes, 'CDLL', return_value=libc), patch.object(collector.os, 'close') as close:
            collector.restrict_https()
        self.assertEqual(operations, [('rules', 0, 3), ('port', 123, 2, 2, 443), ('restrict', 123, 0)])
        libc.prctl.assert_called_once_with(38, 1, 0, 0, 0)
        close.assert_called_once_with(123)

    def test_landlock_fails_closed_when_unsupported(self):
        for abi in (-1, 3):
            libc = Mock()
            libc.syscall.return_value = abi
            with self.subTest(abi=abi), patch.object(collector.ctypes, 'CDLL', return_value=libc), \
                    self.assertRaises((OSError, RuntimeError)):
                collector.restrict_https()
            libc.prctl.assert_not_called()


if __name__ == '__main__':
    unittest.main()
