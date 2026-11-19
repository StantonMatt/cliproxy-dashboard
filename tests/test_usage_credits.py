"""Purchasable Codex credits stay in the dashboard's existing usage reading."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import server
import usage_adapters as usage

NOW = 1800000000


class UsageCreditsTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0
        self.row = dict(type='codex', name='codex-fixture.json', auth_index='fixture',
                        email='fixture@example.test', status='active', disabled=False,
                        id_token={'plan_type': 'pro'})
        self.cache = usage.UsageCache(clock=lambda: self.elapsed, wall_clock=lambda: NOW + self.elapsed,
                                      jitter=lambda: 0, daily_jitter=lambda: 0)
        self.body = dict(plan_type='pro', credits=dict(has_credits=True, unlimited=False,
                         balance='60247.8287130000', approx_cloud_messages=[1, 2]),
                         rate_limit=dict(allowed=True, primary_window=dict(used_percent=40,
                            reset_at=NOW + 9000, limit_window_seconds=18000)))
        self.call = Mock(return_value=dict(status_code=200, body=self.body))

    def fetch(self):
        return server.fetch_account(self.row, call=self.call, cache=self.cache)

    def test_balance_matrix_and_minimal_allowlist(self):
        for raw, expected in [('60247.8287130000', 60247.828713), (60247.828713, 60247.828713),
                              (42, 42.0), ('0', 0.0), (None, None), ('garbage', None),
                              (True, None), ([], None), ({}, None), (-1, None), ('-0.01', None),
                              ('NaN', None), (float('nan'), None), ('Infinity', None),
                              (float('inf'), None), (10 ** 400, None), ('1e300', None),
                              (1e12 + 1, None), (1e12, 1e12)]:
            with self.subTest(raw=raw):
                self.body['credits']['balance'] = raw
                self.assertEqual(server.usage_credits(self.body),
                    dict(balance=expected, unlimited=False, limit_reached=False))
        json.dumps(server.usage_credits(self.body), allow_nan=False)

    def test_missing_malformed_and_unqualified_credits(self):
        for body in (None, [], 3, 'bad', {}, {'credits': None}, {'credits': []}, {'credits': True},
                     {'credits': 'bad'}, {'credits': {}}, {'credits': {'has_credits': False, 'balance': 10}},
                     {'credits': {'has_credits': 'true', 'balance': 10}},
                     {'credits': {'has_credits': 1, 'unlimited': 1, 'balance': 10}}):
            with self.subTest(body=body):
                self.assertIsNone(server.usage_credits(body))

    def test_unlimited_and_strict_limit_flags(self):
        self.assertEqual(server.usage_credits({'credits': {'unlimited': True}}),
                         dict(balance=None, unlimited=True, limit_reached=False))
        for spend in (None, [], 'bad', {'reached': 'true'}, {'reached': 1}):
            self.body['spend_control'] = spend
            self.assertFalse(server.usage_credits(self.body)['limit_reached'])
        for credits, spend in (({'overage_limit_reached': True}, {}), ({}, {'reached': True})):
            body = dict(credits=dict(has_credits=True, **credits), spend_control=spend)
            self.assertTrue(server.usage_credits(body)['limit_reached'])
        self.body['credits'].update(unlimited='true', overage_limit_reached='true')
        self.assertFalse(server.usage_credits(self.body)['unlimited'])
        self.assertFalse(server.usage_credits(self.body)['limit_reached'])

    def test_public_reading_is_cached_across_failure_and_cleared_by_success(self):
        first = self.fetch()
        self.assertEqual(first['usage_credits'], dict(balance=60247.828713, unlimited=False, limit_reached=False))
        self.assertIsNone(first['quota_error'])
        self.elapsed = 10
        self.assertEqual(self.fetch()['usage_credits'], first['usage_credits'])
        self.call.assert_called_once()
        self.elapsed = 70
        self.cache.due_now(self.row)
        self.call.return_value = dict(status_code=503)
        failed = self.fetch()
        self.assertEqual(failed['usage_credits'], first['usage_credits'])
        for key in ('windows', 'quota_checked_at', 'fresh_until'):
            self.assertEqual(failed[key], first[key])
        self.assertNotEqual(failed['quota_attempted_at'], first['quota_attempted_at'])
        self.assertEqual(failed['quota_error'], 'Quota refresh unavailable')
        self.elapsed = 400
        self.body.pop('credits')
        self.call.return_value = dict(status_code=200, body=self.body)
        self.cache.due_now(self.row)
        self.assertIsNone(self.fetch()['usage_credits'])
        self.assertEqual(self.call.call_count, 3)

    def traffic(self):
        return dict(observed_at=usage.stamp(NOW + self.elapsed), signals={
            'X-Codex-Primary-Used-Percent': '50', 'X-Codex-Primary-Window-Minutes': '300',
            'X-Codex-Primary-Reset-At': str(NOW + 9000)})

    def test_passive_headers_preserve_balance_without_extra_poll(self):
        first = self.fetch()
        self.elapsed = 60
        self.row['quota'] = self.traffic()
        observed = self.fetch()
        self.assertEqual(observed['usage_credits'], first['usage_credits'])
        self.assertEqual(observed['windows'][0]['used'], 50)
        self.assertEqual(observed['usage_source'], 'headers')
        self.assertEqual(self.cache.cached(self.row)['usage_credits_checked_at'], first['quota_checked_at'])
        self.call.assert_called_once()

    def test_endpoint_balance_survives_concurrent_newer_header_reading(self):
        self.fetch()
        self.elapsed = 70
        self.cache.due_now(self.row)
        def concurrent(*args):
            self.elapsed += 1
            self.row['quota'] = self.traffic()
            self.fetch()  # Same in-flight request, so only passive headers can advance.
            return dict(status_code=200, body=dict(self.body, credits=dict(has_credits=True, balance='31499.588')))
        self.call.side_effect = concurrent
        observed = self.fetch()
        self.assertEqual(observed['usage_credits']['balance'], 31499.588)
        self.assertEqual(observed['windows'][0]['used'], 50)
        self.assertEqual(self.call.call_count, 2)

    def test_public_snapshot_and_overview_projection_keep_credits_out_of_hub_and_notices(self):
        account = self.fetch()
        state = object.__new__(server.GatewayState)
        state.lock, state.running = threading.Lock(), False
        state.data = dict(online=True, checked_at=usage.stamp(NOW), accounts=[account], claude_accounts=[])
        state.models_cache = server.providers.ModelsCache()
        state.profiles = Mock()
        state.usage_cache = self.cache
        state.fast_modes = Mock()
        state.fast_modes.views.return_value = {}
        state.catalog = Mock()
        state.catalog.view.return_value = {}
        state.codex_billing = Mock()
        state.codex_billing.view.return_value = {}
        state.resets, state.signins = Mock(), Mock()
        state.resets.view.return_value = {}
        state.signins.view.return_value = {}
        state.notices = Mock()
        with patch.object(server, 'watch_accounts', return_value=[]), \
                patch.object(server, 'billing_records', return_value={}), \
                patch.object(server, 'billing_status', return_value={}), \
                patch.object(server, 'pool_status', return_value={}), \
                patch.object(server, 'preference_status', return_value=None), \
                patch.object(server.routing_policy, 'routing_linked', return_value=False):
            snapshot = state.snapshot()
            self.assertEqual(snapshot['accounts'][0]['usage_credits'], account['usage_credits'])
            groups = server.providers.groups(dict(snapshot, inventory=[self.row]), [], {}, {})
            self.assertEqual(groups[0]['accounts'][0]['usage_credits'], account['usage_credits'])
            hub = state.t3_usage_cache(NOW)
            plain_body = {k: v for k, v in self.body.items() if k != 'credits'}
            self.assertEqual(account['_t3_usage'], server.t3_usage.codex_projection(
                plain_body, server.account_access.identity(self.row)))
            account.pop('usage_credits')
            self.assertEqual(hub, state.t3_usage_cache(NOW))
            self.assertNotIn('credits', json.dumps(hub))
        # Compare actual durable notice projections with and without the new field.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notices = server.NoticeStore(root / 'notices.json', root)
            account.update(plan_type='free', access_identity='fixture-identity')
            notices.update(state.data, NOW)
            before = copy.deepcopy(notices.view())
            self.assertTrue(before['items'])
            account['usage_credits'] = server.usage_credits(self.body)
            notices.update(state.data, NOW)
            self.assertEqual(notices.view(), before)
            self.assertNotIn('usage_credits', (root / 'notices.json').read_text())


if __name__ == '__main__':
    unittest.main()
