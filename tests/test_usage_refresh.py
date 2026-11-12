"""Synthetic usage budgets and HTTP contracts; all transports are mocked."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import providers
import server
import usage_adapters as usage
import subscriptions
import claude_warnings

spec = importlib.util.spec_from_file_location('refresh_collector', ROOT / 'scripts/collect-claude-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
NOW = 1800000000


def account(provider='claude', suffix='a'):
    return dict(type=provider, name=provider + '-' + suffix + '.json', auth_index=provider + '-' + suffix,
                email=suffix + '@example.test', status='active', disabled=False)


def claude_body(used=15, reset=NOW + 600, weekly_used=25, weekly_reset=NOW + 3000):
    return dict(five_hour=dict(utilization=used, resets_at=usage.stamp(reset)),
                seven_day=dict(utilization=weekly_used, resets_at=usage.stamp(weekly_reset)))


def codex_body(used=100, reset=NOW + 600, secondary_used=20, secondary_reset=NOW + 9000):
    return dict(plan_type='plus', rate_limit=dict(
        primary_window=dict(used_percent=used, reset_at=reset, limit_window_seconds=18000),
        secondary_window=dict(used_percent=secondary_used, reset_at=secondary_reset, limit_window_seconds=604800)))


class GatewayBudgetTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0
        self.cache = usage.UsageCache(clock=lambda: self.elapsed, wall_clock=lambda: NOW + self.elapsed,
                                      jitter=lambda: 0, daily_jitter=lambda: 0)
        self.call = Mock(return_value=dict(status_code=200, body=claude_body(100)))

    def test_failures_back_off_to_daily_for_both_gateway_providers(self):
        for provider in ('claude', 'codex'):
            cache = usage.UsageCache(clock=lambda: self.elapsed, wall_clock=lambda: NOW + self.elapsed,
                                     jitter=lambda: 0, daily_jitter=lambda: 0)
            failed = Mock(return_value=dict(status_code=503))
            for attempt in range(11):
                before = self.elapsed
                cache.fetch(account(provider), failed)
                due = cache.entries[(provider, provider + '-a')]['due']
                self.assertEqual(due - before, min(86400, 300 * 2 ** attempt))
                self.elapsed = due
            self.assertEqual(failed.call_count, 11)

    def test_latest_exhausted_primary_reset_without_hourly_cap(self):
        for body, delay in ((claude_body(100), 660),
                            (claude_body(100, reset=NOW + 10), 70),
                            (claude_body(100, weekly_used=100), 3060),
                            (claude_body(100, weekly_used=100, weekly_reset=NOW + 9000), 9060)):
            cache = usage.UsageCache(clock=lambda: self.elapsed, wall_clock=lambda: NOW + self.elapsed,
                                     jitter=lambda: 30)
            self.call.return_value['body'] = body
            cache.fetch(account(), self.call)
            self.assertEqual(cache.entries[('claude', 'claude-a')]['due'], delay)
        self.assertIsNone(usage.exhausted_delay([
            dict(kind='weekly_model', used=100, resets_at=NOW + 9000),
            dict(kind='five_hour', used=100, resets_at=None),
            dict(kind='seven_day', used=100, resets_at=NOW)], NOW))

    def test_scheduled_read_waits_until_blocked_reset(self):
        self.call.return_value['body'] = claude_body(100, reset=NOW + 9000, weekly_reset=NOW + 90000)
        self.cache.fetch(account(), self.call)
        self.elapsed = 3599
        self.cache.fetch(account(), self.call)
        self.call.assert_called_once()
        self.elapsed = 9060
        self.call.return_value['body'] = claude_body(10)
        result = self.cache.fetch(account(), self.call)
        self.assertEqual(result['windows'][0]['used'], 10)
        self.assertEqual(self.cache.entries[('claude', 'claude-a')]['due'], 95460)

    def test_xai_credit_exhaustion_with_period_end_uses_same_rule(self):
        self.call.return_value['body'] = dict(config=dict(creditUsagePercent=100,
            currentPeriod=dict(type='weekly', end=usage.stamp(NOW + 900))))
        self.cache.fetch(account('xai'), self.call)
        self.elapsed = 959
        self.cache.fetch(account('xai'), self.call)
        self.call.assert_called_once()
        self.elapsed = 960
        self.cache.fetch(account('xai'), self.call)
        self.assertEqual(self.call.call_count, 2)

    def test_codex_windows_share_budget_and_resume_normal_polling(self):
        row = account('codex')
        self.call.return_value['body'] = codex_body(reset=NOW + 900, secondary_used=100, secondary_reset=NOW + 900)
        original = server.fetch_account(row, call=self.call, cache=self.cache)
        self.assertEqual(self.cache.entries[('codex', 'codex-a')]['due'], 960)
        self.elapsed = 959
        self.assertEqual(server.fetch_account(row, call=self.call, cache=self.cache)['windows'], original['windows'])
        self.call.assert_called_once()
        self.elapsed = 960
        self.call.return_value['body'] = codex_body(used=10)
        server.fetch_account(row, call=self.call, cache=self.cache)
        self.assertEqual(self.cache.entries[('codex', 'codex-a')]['due'], 87360)

    def test_aged_exhausted_gateway_observations_keep_original_time_until_due_plus_grace(self):
        for provider in ('codex', 'claude', 'xai'):
            self.elapsed = 0
            row = account(provider)
            body = (codex_body(reset=NOW + 9000) if provider == 'codex' else
                    claude_body(100, reset=NOW + 9000) if provider == 'claude' else
                    dict(config=dict(creditUsagePercent=100, currentPeriod=dict(
                        type='weekly', end=usage.stamp(NOW + 9000)))))
            call = Mock(return_value=dict(status_code=200, body=body))
            view = (server.fetch_account(row, call=call, cache=self.cache) if provider == 'codex'
                    else self.cache.fetch(row, call))
            grace = 60 if provider == 'codex' else 300
            self.assertEqual(view['fresh_until'], usage.stamp(NOW + 9060 + grace))
            self.elapsed = 1800
            cached = self.cache.cached(row)
            self.assertEqual(cached['checked_at'], usage.stamp(NOW))
            self.assertTrue(usage.fresh(cached['checked_at'], NOW + 1800, cached['fresh_until']))
            self.assertFalse(usage.fresh(cached['checked_at'], NOW + 9060 + grace + 1, cached['fresh_until']))
            if provider == 'codex':
                from availability import classify
                self.assertEqual(classify(row, view, NOW + 1800)['state'], 'quota_exhausted')
                self.assertEqual(classify(row, view, NOW + 9060 + grace + 1)['state'], 'unknown')
            call.assert_called_once()

    def test_codex_idle_direct_reads_are_daily_with_jitter(self):
        self.cache.jitter = lambda: 30
        self.call.return_value['body'] = codex_body(used=10, reset=NOW + 90000, secondary_reset=NOW + 90000)
        server.fetch_account(account('codex'), call=self.call, cache=self.cache)
        self.elapsed = 60
        server.fetch_account(account('codex'), call=self.call, cache=self.cache)
        self.assertEqual(self.call.call_count, 1)
        self.elapsed = 3600
        server.fetch_account(account('codex'), call=self.call, cache=self.cache)
        self.assertEqual(self.call.call_count, 1)
        self.elapsed = 86400
        server.fetch_account(account('codex'), call=self.call, cache=self.cache)
        self.assertEqual(self.call.call_count, 2)

    def test_exhausted_last_success_failure_keeps_reset_deadline(self):
        self.cache.jitter = lambda: 30
        self.call.return_value['body'] = claude_body(100, reset=NOW + 90000)
        self.cache.fetch(account(), self.call)
        self.call.return_value = dict(status_code=503, header={})
        for i in range(5):
            self.elapsed = (i + 1) * 60
            self.cache.fetch(account(), self.call, manual=True)
            self.assertEqual(self.cache.entries[('claude', 'claude-a')]['due'], 90060)

    def test_manual_bypasses_exhaustion_and_normal_interval(self):
        self.cache.fetch(account(), self.call)
        self.call.return_value['body'] = claude_body(10)
        self.elapsed = 1
        result = self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(result['windows'][0]['used'], 10)
        with self.assertRaises(usage.RefreshError) as caught:
            self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(caught.exception.payload(), dict(error='rate_limited', retry_at=usage.stamp(NOW + 61)))
        self.elapsed = 61
        self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(self.call.call_count, 3)

    def test_manual_can_bypass_local_429_hour_but_not_retry_after(self):
        self.call.return_value = dict(status_code=429, header={'Retry-After': '120'})
        self.cache.fetch(account(), self.call)
        self.elapsed = 119
        with self.assertRaises(usage.RefreshError) as caught:
            self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(caught.exception.retry_at, usage.stamp(NOW + 120))
        self.elapsed = 120
        self.call.return_value = dict(status_code=200, body=claude_body())
        self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.cache.available(account())['available_at'], usage.stamp(NOW + 180))

    def test_manual_429_sets_another_scheduled_hour_and_manual_floor(self):
        self.call.return_value = dict(status_code=429, header={})
        self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(self.cache.entries[('claude', 'claude-a')]['due'], 3600)
        self.assertEqual(self.cache.available(account())['available_at'], usage.stamp(NOW + 60))
        self.elapsed = 60
        self.cache.fetch(account(), self.call)
        self.call.assert_called_once()
        self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.cache.entries[('claude', 'claude-a')]['due'], 3660)

    def test_long_retry_after_cannot_be_shortened_by_reset_or_exhaustion(self):
        self.cache.fetch(account(), self.call)
        self.elapsed = 660
        self.call.return_value = dict(status_code=429, header={'Retry-After': '7200'})
        self.cache.fetch(account(), self.call)
        self.cache.due_now(account())
        self.cache.fetch(account(), self.call, force=True)
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.cache.available(account())['available_at'], usage.stamp(NOW + 7860))
        self.elapsed = 7859
        self.cache.fetch(account(), self.call)
        self.assertEqual(self.call.call_count, 2)

    def test_outer_429_retry_after_is_enforced_and_not_published(self):
        self.call.side_effect = HTTPError('https://example.test', 429, 'private', {'Retry-After': '7200'}, None)
        self.cache.fetch(account(), self.call)
        with self.assertRaises(usage.RefreshError) as caught:
            self.cache.fetch(account(), self.call, manual=True)
        self.assertEqual(caught.exception.retry_at, usage.stamp(NOW + 7200))
        self.assertNotIn('private', json.dumps(self.cache.cached(account())))
        self.call.side_effect.close()

    def test_inflight_credential_is_busy_and_other_credentials_are_independent(self):
        started, finish = threading.Event(), threading.Event()
        def call(*args):
            started.set()
            if not finish.wait(5):
                raise AssertionError('Test transport did not finish')
            return dict(status_code=200, body=claude_body())
        worker = threading.Thread(target=lambda: self.cache.fetch(account(), call))
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(usage.RefreshError) as caught:
                self.cache.fetch(dict(account(), name='alias.json'), call, manual=True)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, 'busy'))
            self.cache.fetch(account(suffix='b'), self.call, manual=True)
            self.cache.due_now(account())
        finally:
            finish.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        # A redemption concurrent with a read invalidates that attempt's schedule.
        self.assertEqual(self.cache.entries[('claude', 'claude-a')]['due'], 0)

    def test_redemption_during_inflight_429_preserves_the_new_hour_wait(self):
        def call(*args):
            self.cache.due_now(account())
            return dict(status_code=429, header={})
        self.cache.fetch(account(), call, manual=True)
        self.assertEqual(self.cache.entries[('claude', 'claude-a')]['due'], 3600)


class RefreshHTTPTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 0
        self.state = server.GatewayState(Mock(), Mock(), Mock())
        # Billing has its own background budget; this fixture counts usage/profile requests only.
        self.state.codex_billing.refresh = Mock()
        self.addCleanup(self.state.signins.close)
        self.state.access = Mock()
        self.state.access.reconcile.side_effect = lambda rows, *a, **kw: rows
        self.state.fast_modes.views.return_value = {}
        self.state.resets.view.return_value = {}
        self.state.notices.view.return_value = []
        self.state.notices.claude_view.side_effect = lambda gateway, watched: claude_warnings.project(
            gateway, watched, {}, NOW + self.elapsed)
        self.state.usage_cache = usage.UsageCache(clock=lambda: self.elapsed,
            wall_clock=lambda: NOW + self.elapsed, jitter=lambda: 0)
        self.rows = [account(p) for p in ('codex', 'claude', 'xai', 'kimi')]
        self.state.profiles = subscriptions.Profiles(clock=lambda: self.elapsed,
            wall_clock=lambda: NOW + self.elapsed, jitter=lambda: 0)
        self.state.data.update(inventory=providers.inventory_view(self.rows), online=True)
        self.response = dict(status_code=200, body=claude_body())
        self.calls = []
        self.profile_calls = []
        def call(path, body=None, **kwargs):
            if path == '/auth-files':
                return dict(files=copy.deepcopy(self.rows))
            if path == '/api-call':
                if body['url'].endswith('/profile'):
                    self.profile_calls.append(body)
                    return dict(status_code=503)
                self.calls.append(body)
                if body['auth_index'].startswith('codex') and self.response.get('status_code') == 200:
                    return dict(status_code=200, body=codex_body())
                if body['auth_index'].startswith('xai') and self.response.get('status_code') == 200:
                    return dict(status_code=200, body=dict(config=dict(monthlyLimit=100, used=10)))
                return copy.deepcopy(self.response)
            raise AssertionError('Unexpected mocked gateway route')
        for obj, name, value in ((server, 'api', call), (server, 'watch_accounts', lambda: []),
                                  (server, 'billing_records', lambda: {}),
                                  (server, 'preference_status', lambda *a, **kw: {}),
                                  (server.routing_policy, 'routing_linked', lambda: False),
                                  (server, 'usage_summary', lambda *a: {}),
                                  (server, 'health', lambda *a: {})):
            change = patch.object(obj, name, value)
            change.start()
            self.addCleanup(change.stop)

    def post(self, provider='claude', key=None, headers=None, data=None):
        handler = object.__new__(server.Handler)
        handler.path = '/api/accounts/usage/refresh'
        raw = json.dumps(data if data is not None else dict(provider=provider, key=key or account(provider)['name'])).encode()
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
            'Content-Type': 'application/json', 'Content-Length': str(len(raw)), **(headers or {})}
        handler.rfile, handler.connection = io.BytesIO(raw), Mock()
        handler.server = SimpleNamespace(state=self.state)
        handler.send = Mock()
        handler.do_POST()
        status, body = handler.send.call_args.args
        return status, json.loads(body)

    def overview(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/overview'
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1'}
        handler.server = SimpleNamespace(state=self.state)
        handler.send = Mock()
        handler.do_GET()
        status, raw = handler.send.call_args.args
        self.assertEqual(status, 200)
        return {group['id']: group['accounts'][0] for group in json.loads(raw)['providers']}

    def test_read_completes_and_next_overview_contains_new_reading_for_all_readers(self):
        for provider in ('codex', 'claude', 'xai'):
            with self.subTest(provider=provider):
                self.assertEqual(self.post(provider), (200, {'ok': True}))
                card = self.overview()[provider]
                self.assertTrue(card['windows'])
                self.assertEqual(card['usage_checked_at'], usage.stamp(NOW))
                self.assertEqual(card['usage_refresh'], dict(supported=True, available_at=usage.stamp(NOW + 60), **({'usage_available_at': usage.stamp(NOW + 60)} if provider == 'claude' else {})))
        self.assertEqual(len(self.calls), 3)
        self.state.codex_billing.refresh.assert_called_once()
        self.assertEqual(self.overview()['kimi']['usage_refresh'], dict(supported=False, available_at=None))

    def test_provider_error_still_returns_200_and_is_published(self):
        self.response = dict(status_code=503, body='private response')
        for provider in ('codex', 'claude', 'xai'):
            self.assertEqual(self.post(provider), (200, {'ok': True}))
            card = self.overview()[provider]
            self.assertEqual(card.get('quota_error') if provider == 'codex' else
                             card.get('status') if provider == 'claude' else card.get('usage_status'),
                             'Quota refresh unavailable' if provider == 'codex' else 'error')
            self.assertNotIn('private response', json.dumps(card))

    def test_failed_profile_can_retry_before_usage_provider_deadline(self):
        self.response = dict(status_code=429, header={'Retry-After': '7200'})
        self.assertEqual(self.post()[0], 200)
        self.elapsed = 60
        self.assertEqual(self.post(), (429, dict(error='rate_limited', retry_at=usage.stamp(NOW + 120))))
        info = self.overview()['claude']['usage_refresh']
        self.assertEqual(info['available_at'], usage.stamp(NOW + 120))
        self.assertEqual(info['usage_available_at'], usage.stamp(NOW + 7200))
        self.elapsed = 120
        self.post()
        self.assertEqual(len(self.profile_calls), 3)
        self.assertEqual(len(self.calls), 1)

    def test_floor_429_and_available_at_count_down_then_clear(self):
        self.assertEqual(self.post()[0], 200)
        self.elapsed = 59
        self.assertEqual(self.post(), (429, dict(error='rate_limited', retry_at=usage.stamp(NOW + 60))))
        self.elapsed = 60
        self.assertIsNone(self.overview()['claude']['usage_refresh']['available_at'])
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(len(self.calls), 2)

    def test_retry_after_429_wins_floor_and_local_hour_can_be_bypassed(self):
        self.response = dict(status_code=429, header={'Retry-After': '120'})
        self.assertEqual(self.post()[0], 200)
        self.elapsed = 60
        self.assertEqual(self.post(), (429, dict(error='rate_limited', retry_at=usage.stamp(NOW + 120))))
        self.assertEqual(self.overview()['claude']['usage_refresh']['usage_available_at'], usage.stamp(NOW + 120))
        self.elapsed = 120
        self.response = dict(status_code=200, body=claude_body())
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(len(self.calls), 2)

    def test_busy_unknown_and_unsupported(self):
        self.state.usage_cache.fetch(account(), lambda *a: self.response)
        self.state.usage_cache.entries[('claude', 'claude-a')]['inflight'] = True
        self.assertEqual(self.post(), (409, {'error': 'busy'}))
        self.assertEqual(self.post(key='missing.json'), (404, {'error': 'unknown_account'}))
        self.assertEqual(self.post('kimi'), (400, {'error': 'unsupported'}))
        self.assertEqual(self.calls, [])

    def test_codex_provider_retry_after_uses_the_shared_manual_budget(self):
        self.response = dict(status_code=429, header={'Retry-After': '120'})
        self.assertEqual(self.post('codex')[0], 200)
        self.elapsed = 60
        self.assertEqual(self.post('codex'), (429, dict(error='rate_limited', retry_at=usage.stamp(NOW + 120))))
        self.elapsed = 120
        self.response = dict(status_code=200)
        self.assertEqual(self.post('codex')[0], 200)
        self.assertEqual(self.post('codex')[0], 429)

    def test_csrf_host_body_checks_and_read_only_tailscale_transport(self):
        for headers, expected in (({'X-Router-Request': '0'}, 403),
            ({'Origin': 'https://evil.test'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
            ({'Host': 'evil.test'}, 421), ({'Content-Type': 'text/plain'}, 400),
            ({'Content-Length': '2049'}, 400)):
            self.assertEqual(self.post(headers=headers)[0], expected)
        for data in ({}, {'provider': 'claude', 'key': 12}, {'provider': 'claude', 'key': ''},
                     {'provider': 'claude', 'key': 'a', 'extra': True}, []):
            self.assertEqual(self.post(data=data)[0], 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.post(headers={'X-Router-Read-Only': '1', 'X-Router-Transport': 'tailscale'}),
                         (200, {'ok': True}))

    def test_watch_owned_a_manual_gateway_read_wins_and_survives_scheduled_poll(self):
        watch = dict(id='claude', provider='claude', email=account()['email'], independent_windows=False,
            status='ok', checked_at=usage.stamp(NOW - 1), attempted_at=usage.stamp(NOW),
            windows=usage.normalize_claude(claude_body(100))['windows'])
        with patch.object(server, 'watch_accounts', return_value=[watch]), \
                patch.object(usage.time, 'time', side_effect=lambda: NOW + self.elapsed):
            self.state.data['claude_accounts'] = server.claude_routing_projection(self.rows, usage.stamp(NOW))
            self.assertEqual(self.post(), (200, {'ok': True}))
            card = self.overview()['claude']
            self.assertEqual((card['usage_source'], card['windows'][0]['used']), ('gateway', 15))
            polled = self.state.usage_cache.fetch(account(), Mock(side_effect=AssertionError('Watch owns polling')),
                runtime_rows=self.state.data['claude_accounts'], watched=[watch])
            self.assertEqual(polled['checked_at'], usage.stamp(NOW))
            self.assertEqual(len(self.calls), 1)
            self.elapsed = 1
            watch.update(checked_at=usage.stamp(NOW + 1))
            self.assertEqual(providers.groups(self.state.snapshot(), [watch])[1]['accounts'][0]['usage_source'], 'watch')

    def test_watch_retry_after_blocks_manual_read_and_is_exposed_in_overview(self):
        watch = dict(id='claude', provider='claude', email=account()['email'], independent_windows=False,
                     attempted_at=usage.stamp(NOW), provider_retry_at=usage.stamp(NOW + 7200))
        self.state.data['claude_accounts'] = server.claude_routing_projection(self.rows, usage.stamp(NOW))
        with patch.object(server, 'watch_accounts', return_value=[watch]):
            # The watch deadline holds usage only; the profile keeps its own 60 s manual floor.
            self.assertEqual(self.post(), (429, dict(error='rate_limited', retry_at=usage.stamp(NOW + 60))))
            self.assertEqual(len(self.profile_calls), 1)
            self.assertEqual(self.overview()['claude']['usage_refresh']['usage_available_at'], usage.stamp(NOW + 7200))
            self.assertIsNone(self.overview()['codex']['usage_refresh']['available_at'])
            self.assertIsNone(self.overview()['xai']['usage_refresh']['available_at'])
        self.assertEqual(self.calls, [])

    def test_codex_redemption_makes_exhausted_quota_due_now(self):
        self.state.refresh_usage('codex', account('codex')['name'])
        self.elapsed = 1
        with patch.object(server.routing_policy, 'reconcile', return_value=dict(status='disabled')):
            result = self.state.after_reset('codex-a')
        self.assertEqual(result['quota_refresh'], 'ok')
        self.assertEqual(len(self.calls), 2)
        self.state.codex_billing.refresh.assert_called_once()
        self.assertEqual(self.state.data['accounts'][0]['quota_checked_at'], usage.stamp(NOW + 1))

    def test_confirmed_codex_redemption_with_failed_gateway_recovery_is_still_due(self):
        self.state.refresh_usage('codex', account('codex')['name'])
        self.assertEqual(self.state.usage_cache.entries[('codex', 'codex-a')]['due'], 660)
        handler = object.__new__(server.Handler)
        handler.path = '/api/resets/consume'
        body = json.dumps(dict(account_id='codex-a', credit_id='one', confirmed=True)).encode()
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                          'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        handler.rfile, handler.connection, handler.send = io.BytesIO(body), Mock(), Mock()
        handler.server = SimpleNamespace(state=self.state)
        self.state.resets.consume.return_value = dict(outcome='reset', steps=dict(gateway_reset='failed'))
        handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0], 200)
        self.assertEqual(self.state.usage_cache.entries[('codex', 'codex-a')]['due'], 0)

    def test_scheduled_poll_cannot_overwrite_manual_publication(self):
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider):
                self.rows = [account(provider)]
                at_config, finish = threading.Event(), threading.Event()
                original = server.api
                def call(path, *args, **kwargs):
                    if path == '/config':
                        at_config.set()
                        if not finish.wait(5):
                            raise AssertionError('Poll was not released')
                        return {'routing': {}}
                    return original(path, *args, **kwargs)
                with patch.object(server, 'api', side_effect=call), \
                        patch.object(server, 'move_available', return_value=False), \
                        patch.object(server.routing_policy, 'reconcile'):
                    worker = threading.Thread(target=self.state._fetch)
                    worker.start()
                    try:
                        self.assertTrue(at_config.wait(5))
                        self.elapsed += 1
                        self.assertEqual(self.post(provider), (200, {'ok': True}))
                    finally:
                        finish.set()
                        worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(self.overview()[provider]['usage_checked_at'], usage.stamp(NOW + self.elapsed))

    def test_claude_redemption_only_schedules_next_poll_and_honours_retry_after(self):
        watch = dict(id='claude', provider='claude', email=account()['email'], independent_windows=False,
                     attempted_at=usage.stamp(NOW), next_attempt_at=usage.stamp(NOW + 3600))
        self.response = dict(status_code=200, body=claude_body(100, reset=NOW + 9000))
        with patch.object(server, 'watch_accounts', return_value=[watch]):
            self.state.refresh_usage('claude', account()['name'])
            self.elapsed = 1
            with patch.object(server, 'api', side_effect=AssertionError('Redemption must not wait for gateway')):
                self.state.after_claude_reset()
            self.assertEqual(len(self.calls), 1)
            self.state.usage_cache.fetch(account(), server.api, runtime_rows=self.rows, watched=[watch])
            self.assertEqual(len(self.calls), 2)
            self.response = dict(status_code=503, header={'Retry-After': '7200'})
            self.state.after_claude_reset()
            self.state.usage_cache.fetch(account(), server.api, runtime_rows=self.rows, watched=[watch])
            self.assertEqual(len(self.calls), 3)
            self.state.after_claude_reset()
            self.state.usage_cache.fetch(account(), server.api, runtime_rows=self.rows, watched=[watch])
            self.assertEqual(len(self.calls), 3)

    def test_watch_503_retry_after_survives_reload_and_blocks_only_matching_manual_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token, snapshot = root / 'token', root / 'claude-b.json'
            token.write_text('synthetic-token')
            with patch.dict(collector.os.environ, {'CLAUDE_B_EMAIL': account()['email']}), \
                    patch.object(collector, 'fetch_usage', side_effect=HTTPError(
                        'https://example.test', 503, '', {'Retry-After': '7200'}, None)), \
                    patch.object(collector, 'probe_usage', return_value=usage.normalize_claude(
                        claude_body(100, reset=NOW + 9000))['windows']):
                collector.collect_b(token, snapshot, NOW)
            watch = server.watch_account(snapshot, 'claude-b', 'Claude B', False, NOW)
            self.state.data['claude_accounts'] = server.claude_routing_projection(self.rows, usage.stamp(NOW))
            with patch.object(server, 'watch_accounts', return_value=[watch]):
                self.assertEqual(self.post(), (429, dict(error='rate_limited', retry_at=usage.stamp(NOW + 60))))
                self.assertEqual(len(self.profile_calls), 1)
                self.assertEqual(self.overview()['claude']['usage_refresh']['usage_available_at'], usage.stamp(NOW + 7200))
                self.assertIsNone(self.overview()['codex']['usage_refresh']['available_at'])
                self.assertIsNone(self.overview()['xai']['usage_refresh']['available_at'])
            self.assertEqual(self.calls, [])

    def test_claude_successful_reset_route_schedules_once(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/claude/resets/consume'
        body = json.dumps(dict(grant_id='one', confirmed=True)).encode()
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                          'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        handler.rfile, handler.connection, handler.send = io.BytesIO(body), Mock(), Mock()
        handler.server = SimpleNamespace(state=Mock())
        with patch.object(server, 'claude_request', return_value=dict(outcome='reset')):
            handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0], 200)
        handler.server.state.after_claude_reset.assert_called_once_with()


class WatchSchedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.credentials = self.root / 'credentials.json'
        self.credentials.write_text(json.dumps(dict(claudeAiOauth=dict(
            accessToken='synthetic-token', expiresAt=(NOW + 90000) * 1000))))
        self.snapshot = self.root / 'claude.json'
        self.token = self.root / 'b-token'
        self.token.write_text('synthetic-token')

    def test_a_exhaustion_latest_reset_and_no_requests_before_due(self):
        for reset, weekly, expected in ((600, 900, 960), (600, 9000, 9060)):
            self.snapshot.unlink(missing_ok=True)
            with patch.object(collector, 'fetch_profile', return_value={}), patch.object(collector, 'fetch_usage',
                    return_value=claude_body(100, NOW + reset, 100, NOW + weekly)) as fetch:
                result = collector.collect(self.credentials, self.snapshot, NOW)
                self.assertEqual(result['next_attempt_at'], usage.stamp(NOW + expected))
                collector.collect(self.credentials, self.snapshot, NOW + expected - 1)
                fetch.assert_called_once()
                result = collector.collect(self.credentials, self.snapshot, NOW + expected)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(result['attempted_at'], usage.stamp(NOW + expected))

    def test_a_failed_reads_back_off_to_daily(self):
        self.credentials.write_text(json.dumps(dict(claudeAiOauth=dict(
            accessToken='synthetic-token', expiresAt=(NOW + 1000000) * 1000))))
        at = NOW
        with patch.object(collector, 'fetch_profile', return_value={}), patch.object(collector, 'fetch_usage',
                side_effect=URLError('synthetic')) as fetch:
            for attempt in range(11):
                result = collector.collect(self.credentials, self.snapshot, at)
                delay = min(86400, 300 * 2 ** attempt)
                self.assertEqual(result['next_attempt_at'], usage.stamp(at + delay))
                collector.collect(self.credentials, self.snapshot, at + delay - 1)
                self.assertEqual(fetch.call_count, attempt + 1)
                at += delay

    def test_a_retry_after_for_any_http_status_is_durable(self):
        for code in (429, 503, 403):
            self.snapshot.unlink(missing_ok=True)
            with patch.object(collector, 'fetch_profile', return_value={}), patch.object(collector, 'fetch_usage',
                    return_value=claude_body(100)):
                collector.collect(self.credentials, self.snapshot, NOW)
            with patch.object(collector, 'fetch_usage', side_effect=HTTPError(
                    'https://example.test', code, '', {'Retry-After': '7200'}, None)) as fetch:
                result = collector.collect(self.credentials, self.snapshot, NOW + 660)
                self.assertEqual(result['next_attempt_at'], usage.stamp(NOW + 7860))
                self.assertEqual(collector.previous_snapshot(self.snapshot)['provider_retry_at'], usage.stamp(NOW + 7860))
                collector.collect(self.credentials, self.snapshot, NOW + 4260)
                fetch.assert_called_once()

    def test_b_endpoint_exhaustion_defers_cli_and_endpoint_until_last_reset(self):
        with patch.object(collector, 'fetch_usage', return_value=claude_body(100, weekly_used=100)) as endpoint, \
                patch.object(collector, 'probe_usage', side_effect=AssertionError('Exhausted CLI must not run')):
            result = collector.collect_b(self.token, self.snapshot, NOW)
            for field in ('endpoint_next_attempt_at', 'cli_next_attempt_at'):
                self.assertEqual(result[field], usage.stamp(NOW + 3060))
            collector.collect_b(self.token, self.snapshot, NOW + 3059)
            endpoint.assert_called_once()
            collector.collect_b(self.token, self.snapshot, NOW + 3060)
            self.assertEqual(endpoint.call_count, 2)

    def test_b_cli_exhaustion_waits_for_reset_and_preserves_any_http_retry_after(self):
        windows = usage.normalize_claude(claude_body(100, NOW + 9000))['windows']
        for code in (429, 503, 403):
            self.snapshot.unlink(missing_ok=True)
            with patch.object(collector, 'fetch_usage', side_effect=HTTPError(
                    'https://example.test', code, '', {'Retry-After': '7200'}, None)) as endpoint, \
                    patch.object(collector, 'probe_usage', return_value=windows) as cli:
                result = collector.collect_b(self.token, self.snapshot, NOW)
                self.assertEqual(result['cli_next_attempt_at'], usage.stamp(NOW + 9060))
                self.assertEqual(result['endpoint_next_attempt_at'], usage.stamp(NOW + 9060))
                self.assertEqual(collector.b_snapshot(self.snapshot)['provider_retry_at'], usage.stamp(NOW + 7200))
                collector.collect_b(self.token, self.snapshot, NOW + 9059)
                cli.assert_called_once()
                endpoint.assert_called_once()
                collector.collect_b(self.token, self.snapshot, NOW + 9060)
                self.assertEqual(endpoint.call_count, 2)
                self.assertEqual(cli.call_count, 2)

    def test_aged_exhausted_watch_snapshots_are_fresh_until_scheduled_read_plus_grace(self):
        for independent in (False, True):
            self.snapshot.unlink(missing_ok=True)
            with patch.object(collector, 'fetch_usage', return_value=claude_body(100, NOW + 9000)), \
                    patch.object(collector, 'fetch_profile', return_value={}):
                result = (collector.collect_b(self.token, self.snapshot, NOW) if independent else
                          collector.collect(self.credentials, self.snapshot, NOW))
            deadline = 9360
            self.assertEqual(result['fresh_until'], usage.stamp(NOW + deadline))
            self.assertEqual(result['checked_at'], usage.stamp(NOW))
            for elapsed, status in ((1800, 'ok'), (2999, 'ok'), (3000, 'stale')):
                view = server.watch_account(self.snapshot, 'claude-b' if independent else 'claude',
                    'Claude', not independent, NOW + elapsed)
                self.assertEqual(view['status'], status)
                self.assertEqual(view['checked_at'], usage.stamp(NOW))
                self.assertEqual(view['fresh_until'], usage.stamp(NOW + deadline))

    def test_b_failed_fable_probe_with_exhausted_primary_avoids_haiku_request(self):
        windows = usage.normalize_claude(claude_body(100))['windows']
        with patch.object(collector, 'fetch_usage', side_effect=URLError('synthetic')), \
                patch.object(collector, 'probe_usage', side_effect=collector.UsageResponseError(
                    'cli_unavailable', windows=windows)) as cli:
            result = collector.collect_b(self.token, self.snapshot, NOW)
            cli.assert_called_once()
            self.assertEqual(result['cli_next_attempt_at'], usage.stamp(NOW + 660))


if __name__ == '__main__':
    unittest.main()
