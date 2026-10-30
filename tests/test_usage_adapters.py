"""Synthetic provider fixtures; no real accounts, auth files or transports."""
import copy
import datetime as dt
from email.utils import format_datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import providers
import routing_policy
import server
import t3_usage
import usage_adapters as usage

NOW = 1800000000


def claude_body(fable=True):
    # Representative keys/types from the gateway shape check; all values invented.
    return {'five_hour': {'utilization': 12.5, 'resets_at': usage.stamp(NOW + 12000)},
            'seven_day': {'utilization': 34, 'resets_at': usage.stamp(NOW + 300000)},
            'extra_usage': {'is_enabled': True, 'used_credits': 123, 'monthly_limit': 2000,
                            'currency': 'USD', 'utilization': 6.15},
            'limits': [{'kind': 'weekly_scoped', 'percent': 56,
                        'resets_at': usage.stamp(NOW + 300000),
                        'scope': {'model': {'display_name': 'Fable'}}}] if fable else [],
            'secret': 'must-not-escape'}


def file(provider='claude', suffix='a'):
    return dict(type=provider, name=provider + '-' + suffix + '.json',
                auth_index=provider + '-' + suffix, email=provider + suffix + '@example.test',
                status='active', disabled=False)


def observed(fable=True, at=NOW):
    cache = usage.UsageCache(clock=lambda: 0, wall_clock=lambda: at, jitter=lambda: 0)
    return cache.fetch(file(), lambda *a: dict(status_code=200, body=json.dumps(claude_body(fable))))


def runtime(observation=None):
    row = dict(file(), key=file()['name'], routing_id='binding-a', routing_enabled=True,
               runtime_available=True)
    if observation is not None:
        row['_gateway_usage'] = copy.deepcopy(observation)
    return row


def watch(at=NOW - 100):
    return dict(id='claude-b', provider='claude', email=file()['email'], status='ok',
                error=None, independent_windows=True, checked_at=usage.stamp(at),
                attempted_at=usage.stamp(at + 1), plan='Max (20x)', resets={'available': 2},
                windows=[dict(w, measured_at=usage.stamp(at)) for w in usage.normalize_claude(claude_body())['windows']])


class NormalizationTests(unittest.TestCase):
    def test_claude_main_models_and_extra_are_allowlisted(self):
        result = usage.normalize_claude(json.dumps(claude_body()))
        main, weekly, model = result['windows']
        self.assertEqual((main['kind'], main['seconds'], main['used_pct'], main['remaining']),
                         ('five_hour', 18000, 12.5, 87.5))
        self.assertEqual((weekly['kind'], weekly['seconds']), ('seven_day', 604800))
        self.assertEqual((model['kind'], model['model'], model['used']), ('weekly_model', 'Fable', 56))
        self.assertEqual(model['resets_at'], NOW + 300000)
        self.assertEqual(result['extra_usage'], dict(enabled=True, used=1.23, limit=20,
                                                    currency='USD', utilization=6.15))
        self.assertNotIn('must-not-escape', json.dumps(result))

    def test_claude_legacy_models_deduplicate_fable_and_bad_values_fail_soft(self):
        body = claude_body()
        body.update(seven_day_overage_included={'utilization': 90, 'resets_at': None},
                    seven_day_sonnet={'utilization': 101, 'resets_at': 'bad'},
                    seven_day_opus=None)
        result = usage.normalize_claude(body)
        self.assertEqual(len([w for w in result['windows'] if w.get('model') == 'Fable']), 1)
        sonnet = result['windows'][-1]
        self.assertEqual((sonnet['model'], sonnet['used'], sonnet['invalid_reset']), ('Sonnet', 100, True))
        for invalid in ({}, [], {'error': 'private'}, {**body, 'five_hour': {'utilization': float('nan')}},
                        {**body, 'seven_day': {'utilization': True}}):
            with self.subTest(invalid=type(invalid)):
                with self.assertRaises(ValueError):
                    usage.normalize_claude(invalid)

    def test_xai_mc_weekly_and_monthly_shapes(self):
        for config in ({'creditUsagePercent': '23.5', 'currentPeriod': {'type': 'weekly', 'end': usage.stamp(NOW + 1000)}},
                       {'credit_usage_percent': 23.5, 'current_period': {'type': 'weekly', 'end': usage.stamp(NOW + 1000)}}):
            row, = usage.normalize_xai({'config': config})['windows']
            self.assertEqual((row['label'], row['used_pct'], row['resets_at']), ('Weekly', 23.5, NOW + 1000))
        row, = usage.normalize_xai({'config': {'monthlyLimit': {'val': '2000'}, 'used': {'val': '500'},
                                              'billingPeriodEnd': usage.stamp(NOW + 2000)}})['windows']
        self.assertEqual((row['label'], row['used_pct'], row['remaining']), ('Monthly', 25, 75))
        self.assertEqual(usage.normalize_xai({'config': {'monthly_limit': 2000, 'used': 500}})['windows'][0]['used'], 25)

    def test_xai_unknown_weekly_percent_cannot_borrow_monthly_usage(self):
        for config in ({}, {'creditUsagePercent': 'nan'}, {'currentPeriod': {'type': 'weekly'},
                       'monthlyLimit': 1000, 'used': 300}, {'monthlyLimit': 0, 'used': 20}):
            self.assertEqual(usage.normalize_xai({'config': config})['windows'], [])

    def test_xai_request_is_billing_get_with_mc_headers_and_optional_user(self):
        row = file('xai')
        row['metadata'] = {'oauth': {'sub': 'user-fixture'}}
        request = usage.request(row)
        self.assertEqual(request['method'], 'GET')
        self.assertEqual(request['url'], usage.XAI_URL)
        self.assertEqual(request['header']['x-userid'], 'user-fixture')
        self.assertEqual(request['header']['Authorization'], 'Bearer $TOKEN$')
        self.assertEqual(request['header']['x-xai-token-auth'], 'xai-grok-cli')
        self.assertEqual(request['header']['x-grok-client-version'], '1.0.46')
        self.assertNotIn('data', request)
        self.assertNotIn('x-userid', usage.request(dict(file('xai'), id='inventory-file-id'))['header'])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = [0]
        self.wall = [NOW]
        self.cache = usage.UsageCache(clock=lambda: self.clock[0], wall_clock=lambda: self.wall[0], jitter=lambda: 17)
        self.call = Mock(return_value=dict(status_code=200, body=claude_body()))

    def advance(self, value):
        self.clock[0] += value
        self.wall[0] += value

    def test_cache_jitter_is_per_credential_and_refresh_cannot_bypass(self):
        first = self.cache.fetch(file(), self.call)
        for amount in (0, 30, 60, 210, 16):
            self.advance(amount)
            self.assertEqual(self.cache.fetch(file(), self.call), first)
        self.assertEqual(self.call.call_count, 1)
        self.advance(1)
        second = self.cache.fetch(file(), self.call)
        self.assertNotEqual(first['checked_at'], second['checked_at'])
        self.cache.fetch(file(suffix='b'), self.call)
        self.assertEqual(self.call.call_count, 3)
        # A changed runtime binding cannot inherit the removed account's readings.
        self.cache.fetch(dict(file(), auth_index='replacement'), self.call)
        self.assertEqual(self.call.call_count, 4)

    def test_retry_after_seconds_and_exponential_backoff_keep_original_timestamp(self):
        original = self.cache.fetch(file(), self.call)
        self.advance(317)
        self.call.return_value = dict(status_code=429, header={'Retry-After': ['1000']}, body='private')
        failed = self.cache.fetch(file(), self.call)
        self.assertEqual(failed['checked_at'], original['checked_at'])
        self.assertEqual(failed['windows'], original['windows'])
        self.assertEqual(failed['status'], 'error')
        self.assertNotIn('private', json.dumps(failed))
        self.advance(1016)
        self.cache.fetch(file(), self.call)
        self.assertEqual(self.call.call_count, 2)
        self.advance(1)
        self.call.return_value = dict(status_code=503, header={}, body='private')
        self.cache.fetch(file(), self.call)
        self.advance(616)
        self.cache.fetch(file(), self.call)
        self.assertEqual(self.call.call_count, 3)
        self.advance(1)
        self.call.return_value = dict(status_code=200, body=claude_body())
        self.cache.fetch(file(), self.call)
        self.advance(317)
        self.cache.fetch(file(), self.call)
        self.assertEqual(self.call.call_count, 5)

    def test_aliases_share_budget_without_borrowing_another_identitys_usage(self):
        self.cache.fetch(file(), self.call)
        alias = dict(file(), name='alias.json', email='other@example.test')
        for _ in range(10):
            self.assertEqual(self.cache.fetch(alias, self.call)['windows'], [])
        self.assertEqual(self.call.call_count, 1)
        self.advance(317)
        result = self.cache.fetch(alias, self.call)
        self.assertEqual(result['key'], 'alias.json')
        self.assertEqual(self.cache.fetch(file(), self.call)['windows'], [])
        self.assertEqual(self.call.call_count, 2)

    def test_retry_after_http_date_and_outer_management_error(self):
        later = format_datetime(dt.datetime.fromtimestamp(NOW + 900, dt.timezone.utc), usegmt=True)
        self.call.side_effect = HTTPError('http://loopback/', 503, 'not forwarded', {'retry-after': later}, None)
        self.cache.fetch(file(), self.call)
        self.advance(916)
        self.cache.fetch(file(), self.call)
        self.assertEqual(self.call.call_count, 1)
        self.advance(1)
        self.cache.fetch(file(), self.call)
        self.assertEqual(self.call.call_count, 2)
        for raw in ('invalid', '-5', [], None, 'NaN'):
            self.assertEqual(usage.retry_after({'Retry-After': raw}, NOW), 0)

    def test_single_inflight_and_different_accounts_are_independent(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            return dict(status_code=200, body=claude_body())
        call = Mock(side_effect=blocked)
        worker = threading.Thread(target=self.cache.fetch, args=(file(), call))
        worker.start()
        self.addCleanup(lambda: worker.join(6))
        try:
            self.assertTrue(entered.wait(2))
            for _ in range(10):
                self.assertEqual(self.cache.fetch(file(), call)['status'], 'unavailable')
                self.assertEqual(self.cache.fetch(dict(file(), name='alias.json'), call)['windows'], [])
            self.cache.fetch(file(suffix='b'), self.call)
            self.assertEqual(self.call.call_count, 1)
            self.assertEqual(call.call_count, 1)
        finally:
            release.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.cache.fetch(file(), call)['status'], 'ok')
        self.assertEqual(call.call_count, 1)

    def test_xai_no_windows_errors_and_no_chat_fallback(self):
        self.call.return_value = dict(status_code=200, body={'config': {}})
        first = self.cache.fetch(file('xai'), self.call)
        self.assertEqual(first['windows'], [])
        self.assertIsNone(first['usage_checked_at'])
        for _ in range(10):
            self.cache.fetch(file('xai'), self.call)
        self.assertEqual(self.call.call_count, 1)
        self.assertEqual(self.call.call_args.args[1]['url'], usage.XAI_URL)
        self.advance(317)
        self.call.return_value = dict(status_code=200, body={'config': {'creditUsagePercent': 20}})
        self.assertEqual(self.cache.fetch(file('xai'), self.call)['windows'][0]['used_pct'], 20)


class SelectionTests(unittest.TestCase):
    def select(self, row=None, watched=None, now=NOW, rows=None):
        row = row if row is not None else runtime(observed(False))
        return usage.claude_observation(row, [row] if rows is None else rows,
                                        [watch()] if watched is None else watched, now)

    def test_gateway_primary_merges_only_extras_with_original_measurements(self):
        result = self.select()
        self.assertEqual(result['usage_source'], 'gateway')
        self.assertEqual(result['checked_at'], usage.stamp(NOW))
        self.assertEqual(result['watch_checked_at'], usage.stamp(NOW - 100))
        self.assertEqual(result['resets_checked_at'], usage.stamp(NOW - 100))
        self.assertEqual(result['plan'], 'Max (20x)')
        self.assertEqual(result['resets'], {'available': 2})
        fable = result['windows'][-1]
        self.assertEqual((fable['model'], fable['measured_at'], fable['usage_source']),
                         ('Fable', usage.stamp(NOW - 100), 'watch'))
        self.assertEqual(result['windows'][0]['usage_source'], 'gateway')
        self.assertEqual(result['id'], 'claude-b')

    def test_gateway_model_wins_and_old_watch_model_never_gets_freshened(self):
        result = self.select(runtime(observed()))
        self.assertEqual(result['windows'][-1]['usage_source'], 'gateway')
        for watched in (watch(NOW - 901), watch(NOW + 1)):
            result = self.select(watched=[watched])
            self.assertEqual(len(result['windows']), 2)
        expired = watch()
        expired['windows'][-1]['resets_at'] = NOW - 1
        self.assertEqual(len(self.select(watched=[expired])['windows']), 2)

    def test_failed_or_stale_gateway_falls_back_to_watch_without_changing_time(self):
        for gateway in (dict(observed(), status='error', error='usage_unavailable'), observed(at=NOW - 901)):
            result = self.select(runtime(gateway))
            self.assertEqual(result['usage_source'], 'watch')
            self.assertEqual(result['checked_at'], usage.stamp(NOW - 100))
            self.assertEqual(result['usage_checked_at'], result['checked_at'])
            self.assertEqual(result['gateway_checked_at'], gateway['checked_at'])
        result = self.select(runtime(observed(at=NOW - 901)), watched=[])
        self.assertEqual((result['usage_source'], result['status']), ('gateway', 'stale'))

    def test_unique_provider_email_only_and_casefold_match(self):
        row = runtime(observed(False))
        duplicate = dict(row, key='other.json', name='other.json')
        for watched, rows in (([watch(), watch()], [row]), ([watch()], [row, duplicate]),
                              ([dict(watch(), provider='codex')], [row]),
                              ([dict(watch(), email='other@example.test')], [row])):
            result = self.select(row, watched, rows=rows)
            self.assertNotIn('plan', result)
            self.assertNotIn('resets', result)
            self.assertEqual(len(result['windows']), 2)
        result = self.select(row, [dict(watch(), email=file()['email'].upper())])
        self.assertEqual(result['plan'], 'Max (20x)')

    def test_replacement_runtime_cannot_inherit_usage(self):
        self.assertEqual(self.select(dict(runtime(observed()), auth_index='replacement'), watched=[]), {})
        self.assertEqual(self.select(dict(runtime(observed()), email='new@example.test'), watched=[]), {})

    def test_provider_cards_show_gateway_usage_without_watch_and_xai_fields(self):
        row = runtime(observed())
        gateway = dict(inventory=[file(), file('xai')], claude_accounts=[row], usage_accounts=[
            dict(key=file('xai')['name'], provider='xai', auth_index=file('xai')['auth_index'],
                 windows=[dict(label='Weekly', used_pct=20, resets_at=NOW + 1000)],
                 usage_source='gateway', usage_checked_at=usage.stamp(NOW))])
        with patch.object(usage.time, 'time', return_value=NOW):
            groups = providers.groups(gateway, [])
        claude, xai = [group['accounts'][0] for group in groups]
        self.assertEqual((claude['usage_source'], claude['watch_observed'], claude['status']), ('gateway', True, 'ok'))
        self.assertNotIn('_gateway_usage', claude)
        self.assertEqual(xai['windows'][0]['used_pct'], 20)
        self.assertEqual(xai['usage_checked_at'], usage.stamp(NOW))

    def test_routing_and_t3_consume_the_same_gateway_view_without_watch(self):
        row = runtime(observed())
        gateway = dict(online=True, checked_at=usage.stamp(NOW), claude_accounts=[row])
        projected = routing_policy.claude_observations({'gateway': gateway, 'watch_accounts': []}, NOW)
        account, = projected['gateway']['accounts']
        self.assertEqual(account['quota_checked_at'], usage.stamp(NOW))
        self.assertTrue(account['active'])
        self.assertEqual(account['windows'][1]['seconds'], 604800)
        candidates, excluded = routing_policy.eligible_accounts({'gateway': gateway}, NOW, 'claude', [file()])
        self.assertEqual(candidates[file()['email']]['remaining'], 66)
        self.assertEqual(excluded, {})
        (public, body), = t3_usage.accounts(gateway, NOW)
        self.assertEqual(set(public), {'id', 'auth_index', 'provider', 'email', 'disabled'})
        self.assertEqual(body['five_hour']['utilization'], 12.5)
        self.assertEqual(body['limits'][0]['scope']['model']['display_name'], 'Fable')

    def test_t3_and_routing_fallback_share_original_watch_timestamp(self):
        row = runtime(dict(observed(), status='error', error='usage_unavailable'))
        gateway = dict(online=True, checked_at=usage.stamp(NOW), claude_accounts=[row], watch_accounts=[watch()])
        (_, body), = t3_usage.accounts(gateway, NOW)
        self.assertEqual(body['seven_day']['utilization'], 34)
        projected = routing_policy.claude_observations({'gateway': gateway, 'watch_accounts': [watch()]}, NOW)
        self.assertEqual(projected['gateway']['accounts'][0]['quota_checked_at'], usage.stamp(NOW - 100))


class WiringTests(unittest.TestCase):
    def test_reconcile_rebinds_gateway_usage_before_claude_quota_selection(self):
        for mode in ('highest-remaining-quota', 'earliest-weekly-reset'):
            for has_watch in (False, True):
                with self.subTest(mode=mode, watch=has_watch), tempfile.TemporaryDirectory() as directory:
                    rows = [dict(file(suffix=suffix), priority=100 if i == 0 else 0)
                            for i, suffix in enumerate(('a', 'b'))]
                    observations, watched = [], []
                    for i, row in enumerate(rows):
                        quota = dict(observed(), key=row['name'], id=row['name'],
                                     auth_index=row['auth_index'], email=row['email'])
                        quota['windows'][1].update(used=80 - i * 60, used_pct=80 - i * 60,
                                                   remaining=20 + i * 60, resets_at=NOW + 300000 - i * 100000)
                        observations.append(quota)
                        fallback = dict(watch(), id='watch-' + str(i), email=row['email'])
                        # Watch usage favors A; fresh gateway usage must select B.
                        fallback['windows'][1].update(used=10 + i * 80, used_pct=10 + i * 80,
                                                      remaining=90 - i * 80, resets_at=NOW + 100000 + i * 300000)
                        watched.append(fallback)
                    gateway = dict(online=True, checked_at=usage.stamp(NOW), usage_accounts=observations,
                                   claude_accounts=usage.bind_claude_usage(copy.deepcopy(rows), observations))
                    writes = []

                    def call(path, body=None, method=None):
                        if path == '/auth-files':
                            return {'files': copy.deepcopy(rows)}
                        if path == '/config':
                            return {'routing': {'strategy': 'round-robin'}}
                        self.assertEqual((path, method), ('/auth-files/fields', 'PATCH'))
                        writes.append(body.copy())
                        next(row for row in rows if row['name'] == body['name'])['priority'] = body['priority']
                        return {}

                    with patch.object(routing_policy.time, 'time', return_value=NOW):
                        result = routing_policy.reconcile(
                            {'gateway': gateway, 'watch_accounts': watched if has_watch else []},
                            {'mode': mode}, call, Path(directory), provider='claude')
                    self.assertEqual(result['status'], 'active')
                    self.assertEqual(result['preferred_email'], rows[1]['email'])
                    self.assertEqual(result['weekly_resets_at'], NOW + 200000)
                    self.assertEqual([row['priority'] for row in rows], [0, 100])
                    self.assertEqual(len(writes), 2)

    def test_routing_toggle_keeps_gateway_usage_and_needs_no_watch(self):
        rows = [file(suffix=suffix) for suffix in ('a', 'b')]
        observations = [dict(observed(), key=row['name'], id=row['name'], auth_index=row['auth_index'],
                             email=row['email']) for row in rows]
        state = server.GatewayState(resets=Mock(), notices=Mock(), fast_modes=Mock())
        state.data.update(usage_accounts=observations)
        def call(path, body=None, method=None, **kwargs):
            if path == '/auth-files':
                return {'files': copy.deepcopy(rows)}
            self.assertEqual((path, method), ('/auth-files/status', 'PATCH'))
            next(row for row in rows if row['name'] == body['name'])['disabled'] = body['disabled']
            return {}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(routing_policy, 'STATE', Path(directory)), \
                patch.object(server, 'api', side_effect=call), \
                patch.object(server, 'watch_accounts', return_value=[]), \
                patch.object(server.time, 'time', return_value=NOW):
            target = server.account_access.claude_routing_id(rows[0])
            self.assertEqual(state.set_claude_routing_enabled(target, False), {'enabled': False})
        self.assertTrue(rows[0]['disabled'])
        self.assertEqual(len(state.data['claude_accounts']), 2)
        self.assertTrue(all(row.get('_gateway_usage') for row in state.data['claude_accounts']))
        self.assertEqual(state.data['claude_accounts'][1]['_gateway_usage']['checked_at'], usage.stamp(NOW))

    def test_existing_gateway_fetch_hosts_cached_usage_without_extra_threads(self):
        rows = [file(), file('xai')]
        state = server.GatewayState(resets=Mock(), notices=Mock(), fast_modes=Mock())
        state.access.reconcile = Mock(return_value=[])
        calls = []
        def call(path, body=None, **kw):
            if path == '/auth-files':
                return {'files': rows}
            if path == '/config':
                return {'routing': {}}
            self.assertEqual(path, '/api-call')
            calls.append(body)
            return dict(status_code=200, body=claude_body() if body['url'] == usage.CLAUDE_URL else
                        {'config': {'creditUsagePercent': 20}})
        with patch.object(server, 'api', side_effect=call):
            state._fetch_locked()
            first = copy.deepcopy(state.data)
            state._fetch_locked()
        self.assertTrue(state.data['online'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(state.data['usage_accounts']), 2)
        self.assertEqual(state.data['claude_accounts'][0]['_gateway_usage']['checked_at'],
                         first['claude_accounts'][0]['_gateway_usage']['checked_at'])
        with patch.object(server, 'watch_account', return_value=None), patch.object(server, 'api') as network:
            cached = state.t3_usage_cache()
        network.assert_not_called()
        (_, body), = t3_usage.accounts(cached, usage.epoch(state.data['checked_at']))
        self.assertEqual(body['five_hour']['utilization'], 12.5)


if __name__ == '__main__':
    unittest.main()
