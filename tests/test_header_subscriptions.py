"""Synthetic traffic/profile contracts; no live credentials or provider requests."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import quota_headers
import server
import subscriptions
import usage_adapters as usage

NOW = 1800000000


def file(provider='claude', index='a'):
    return dict(type=provider, name=provider + index + '.json', auth_index=index,
                email=index + '@example.test', disabled=False, status='active')


def traffic(at=NOW, used=.25):
    return dict(observed_at=usage.stamp(at), signals={
        'Anthropic-Ratelimit-Unified-5h-Utilization': str(used),
        'Anthropic-Ratelimit-Unified-5h-Reset': str(NOW + 3000),
        'Anthropic-Ratelimit-Unified-7d-Utilization': '.1',
        'Anthropic-Ratelimit-Unified-7d-Reset': str(NOW + 500000)})


def codex_traffic(at=NOW, slot='Primary', minutes=10080, used=87):
    return dict(observed_at=usage.stamp(at), signals={
        'X-Codex-' + slot + '-Used-Percent': str(used),
        'X-Codex-' + slot + '-Window-Minutes': str(minutes),
        'X-Codex-' + slot + '-Reset-At': str(NOW + 500000 if minutes == 10080 else NOW + 3000)})


def profile(plan='pro', status='active', email='a@example.test'):
    return dict(account=dict(email=email, has_claude_pro=plan == 'pro', has_claude_max=plan == 'max'), organization=dict(
        rate_limit_tier='default_claude_max_5x' if plan == 'max' else 'default_claude_ai',
        organization_type='claude_pro' if plan == 'pro' else 'claude_max',
        subscription_status=status, billing_type='stripe_subscription'),
        subscription_created_at='2020-01-01T00:00:00Z',
        payment_auth_hosted_invoice_url='https://untrusted.example.test/invoice')


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.cache = usage.UsageCache(clock=lambda: self.now - NOW,
            wall_clock=lambda: self.now, jitter=lambda: 0, daily_jitter=lambda: 0)
        self.row = file()
        self.body = dict(five_hour=dict(utilization=60, resets_at=usage.stamp(NOW + 3000)),
            seven_day=dict(utilization=20, resets_at=usage.stamp(NOW + 500000)),
            seven_day_overage_included=dict(utilization=90, resets_at=usage.stamp(NOW + 500000)),
            extra_usage=dict(is_enabled=True, used_credits=1200))
        self.call = Mock(return_value=dict(status_code=200, body=self.body))

    def test_newer_traffic_headers_clear_a_stale_sign_in_marker(self):
        from unittest.mock import Mock
        failed = self.cache.fetch(self.row, Mock(return_value=dict(status_code=401)))
        self.assertEqual(failed['auth_state'], 'sign_in_required')
        self.row['quota'] = traffic(self.now - 30)
        self.assertEqual(self.cache.fetch(self.row, self.call)['auth_state'], 'sign_in_required')
        self.now += 60
        self.row['quota'] = traffic(self.now)
        view = self.cache.fetch(self.row, self.call)
        self.assertEqual(view['status'], 'ok')
        self.assertNotIn('auth_state', view)

    def test_confirmed_sign_in_clears_a_stale_sign_in_marker_and_makes_a_read_due(self):
        from unittest.mock import Mock
        self.cache.fetch(self.row, Mock(return_value=dict(status_code=401)))
        self.assertEqual(self.cache.fetch(self.row, self.call)['auth_state'], 'sign_in_required')
        self.call.assert_not_called()  # still backing off after the failed read
        self.cache.signed_in(dict(self.row, email='other@example.test'))
        self.assertEqual(self.cache.cached(self.row)['auth_state'], 'sign_in_required')
        self.cache.signed_in(self.row)
        self.assertNotIn('auth_state', self.cache.cached(self.row))
        self.assertEqual(self.cache.fetch(self.row, self.call)['status'], 'ok')

    def test_weekly_only_headers_replace_old_five_hour_and_feed_routing_and_t3(self):
        self.cache.fetch(self.row, self.call)
        self.now += 60
        self.row['quota'] = traffic(self.now)
        signals = self.row['quota']['signals']
        for key in list(signals):
            if '-5h-' in key:
                del signals[key]
        view = self.cache.fetch(self.row, self.call)
        self.assertEqual([w['kind'] for w in view['windows']], ['seven_day', 'weekly_model'])
        self.call.assert_called_once()
        runtime = server.claude_routing_projection([self.row], usage.stamp(self.now))
        usage.bind_claude_usage(runtime, [view])
        overview = dict(gateway=dict(online=True, checked_at=usage.stamp(self.now),
            inventory=[self.row], claude_accounts=runtime), watch_accounts=[])
        import routing_policy
        candidates, _ = routing_policy.eligible_accounts(overview, self.now, provider='claude', inventory=[self.row])
        self.assertEqual(candidates[self.row['email']]['remaining'], 90)
        import t3_usage
        body = t3_usage.claude_body(view, self.now)
        self.assertNotIn('five_hour', body)
        self.assertEqual(body['seven_day']['utilization'], 10)
        for key, value in [('Utilization', 'bad'), ('Status', 'rejected'), ('Reset', None)]:
            signals['Anthropic-Ratelimit-Unified-5h-' + key] = value
            self.assertIsNone(quota_headers.payload(self.row, self.now))
            del signals['Anthropic-Ratelimit-Unified-5h-' + key]

    def test_usage_optional_five_hour_requires_valid_weekly_and_rejects_malformed(self):
        weekly = dict(utilization=10, resets_at=usage.stamp(NOW + 500000))
        for payload in ({'seven_day': weekly}, {'five_hour': None, 'seven_day': weekly}):
            value = usage.normalize_claude(payload)
            self.assertEqual([w['kind'] for w in value['windows']], ['seven_day'])
        for bad in ({}, [], '', False, {'utilization': None}, {'utilization': True},
                    {'utilization': -1}, {'utilization': float('nan')}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                usage.normalize_claude(dict(five_hour=bad, seven_day=weekly))
        for payload in ({}, {'five_hour': weekly}, {'seven_day': None}):
            with self.assertRaises(ValueError):
                usage.normalize_claude(payload)
        invalid = usage.normalize_claude(dict(five_hour=dict(utilization=0, resets_at='bad'), seven_day=weekly))
        self.assertTrue(invalid['windows'][0]['invalid_reset'])

    def test_endpoint_primary_readings_live_until_reset_across_consumers(self):
        import routing_policy
        import t3_usage
        from availability import classify
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider):
                row = file(provider)
                if provider == 'codex':
                    self.call.return_value['body'] = dict(rate_limit={slot: dict(
                        used_percent=20, reset_at=NOW + delay, limit_window_seconds=seconds)
                        for slot, delay, seconds in (('primary_window', 3000, 18000),
                            ('secondary_window', 500000, 604800))})
                    view = server.fetch_account(row, call=self.call, cache=self.cache)
                    view.update(access_identity=view['_t3_usage']['identity'], disabled=False)
                    observed = dict(online=True, checked_at=usage.stamp(NOW + 2000), accounts=[view])
                    self.assertEqual(classify(row, view, NOW + 2000)['state'], 'ready')
                    self.assertIsNotNone(t3_usage.accounts(observed, NOW + 2000)[0][1])
                    routing_policy.eligible_accounts(dict(gateway=observed), NOW + 2000, inventory=[row])
                    self.assertEqual(classify(row, view, NOW + 3000)['state'], 'unknown')
                else:
                    view = self.cache.fetch(row, self.call)
                    self.assertTrue(usage.observation_fresh(view, NOW + 2000))
                    self.assertIsNotNone(t3_usage.claude_body(view, NOW + 2000))
                    runtime = server.claude_routing_projection([row], usage.stamp(NOW + 2000))
                    usage.bind_claude_usage(runtime, [view])
                    routing_policy.eligible_accounts(dict(gateway=dict(online=True,
                        checked_at=usage.stamp(NOW + 2000), claude_accounts=runtime), watch_accounts=[]),
                        NOW + 2000, provider='claude', inventory=[row])
                    self.assertIsNone(t3_usage.claude_body(view, NOW + 3000))
                for changes in ({'measured_at': usage.stamp(NOW + 2001)},
                                {'resets_at': NOW + 2000}, {'resets_at': None}):
                    bad = dict(view['windows'][0], **changes)
                    self.assertFalse(usage.window_fresh(bad, usage.stamp(NOW), NOW + 2000, primary=True))

    def test_newer_invalid_endpoint_window_does_not_shadow_valid_headers(self):
        self.cache.fetch(self.row, self.call)
        self.now += 60
        self.row['quota'] = traffic(self.now, .1)
        self.cache.fetch(self.row, self.call)
        self.now += 60
        self.call.return_value['body']['five_hour'] = dict(utilization=0, resets_at=usage.stamp(self.now + 1))
        self.call.return_value['body']['seven_day']['utilization'] = 30
        view = self.cache.fetch(self.row, self.call, manual=True)
        self.assertEqual(view['windows'][0]['used'], 0)
        self.now += 2
        view = self.cache.fetch(self.row, self.call)
        self.assertEqual(view['windows'][0]['used'], 10)
        self.assertEqual(view['windows'][0]['measured_at'], usage.stamp(NOW + 60))
        self.assertEqual(view['windows'][1]['used'], 30)
        self.assertEqual(view['windows'][1]['measured_at'], usage.stamp(NOW + 120))
        self.assertEqual(self.call.call_count, 2)
        # Older weekly-only headers cannot erase a newer reported 5h window.
        for key in list(self.row['quota']['signals']):
            if '-5h-' in key:
                del self.row['quota']['signals'][key]
        self.now += 60
        self.call.return_value['body']['five_hour']['resets_at'] = usage.stamp(self.now + 600)
        self.call.return_value['body']['seven_day']['resets_at'] = usage.stamp(self.now)
        view = self.cache.fetch(self.row, self.call, manual=True)
        self.assertEqual({w['kind'] for w in view['windows']}, {'five_hour', 'seven_day', 'weekly_model'})
        self.assertEqual(next(w for w in view['windows'] if w['kind'] == 'five_hour')['measured_at'], usage.stamp(self.now))

    def test_headers_update_without_polling_or_freshening_optional_observations(self):
        first = self.cache.fetch(self.row, self.call)
        self.now += 60
        self.row['quota'] = traffic(self.now, .1)
        view = self.cache.fetch(self.row, self.call)
        self.call.assert_called_once()
        self.assertEqual(view['windows'][0]['used'], 10)
        self.assertEqual(view['usage_source'], 'headers')
        self.assertEqual(view['checked_at'], usage.stamp(self.now))
        self.assertEqual(view['windows'][-1]['measured_at'], first['checked_at'])
        self.assertEqual(view['extra_usage_checked_at'], first['checked_at'])
        self.now = NOW + 3059
        self.cache.fetch(self.row, self.call)
        self.assertEqual(self.call.call_count, 1)

    def test_future_stale_partial_wrong_provider_and_bad_signals_are_not_promoted(self):
        for quota in (traffic(NOW + 1), dict(observed_at=usage.stamp(NOW), signals={}),
                      dict(traffic(), observed_at='bad')):
            self.assertIsNone(quota_headers.payload(dict(self.row, quota=quota), NOW))
        for field, value in (('5h-utilization', '1.1'), ('5h-reset', 'bad'),
                             ('7d-status', 'unexpected'), ('5h-utilization', 'nan')):
            quota = traffic()
            quota['signals']['Anthropic-Ratelimit-Unified-' + field] = value
            self.assertIsNone(quota_headers.payload(dict(self.row, quota=quota), NOW))
        self.assertIsNone(quota_headers.payload(dict(file('xai'), quota=traffic()), NOW))

    def test_expired_headers_never_become_zero_and_unused_zero_stays_null(self):
        row = dict(self.row, quota=traffic())
        self.assertIsNone(quota_headers.payload(row, NOW + 3001))
        row['quota'] = traffic(used=0)
        row['quota']['signals'].pop('Anthropic-Ratelimit-Unified-5h-Reset')
        body, _ = quota_headers.payload(row, NOW)
        self.assertEqual(body['five_hour'], dict(utilization=0, resets_at=None))

    def test_cached_watch_cannot_drop_malformed_five_hour_and_invent_weekly_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            for used in (None, 'bad', True):
                path.write_text(json.dumps(dict(status='ok', checked_at=usage.stamp(NOW),
                    windows=[dict(kind='five_hour', used=used), dict(kind='seven_day', used=10, resets_at=NOW + 500000)])))
                view = server.watch_account(path, 'claude', 'Claude', True, NOW)
                self.assertEqual([w['kind'] for w in view['windows']], ['five_hour', 'seven_day'])
                self.assertIsNone(view['windows'][0]['remaining'])
                import t3_usage
                self.assertIsNone(t3_usage.claude_body(view, NOW))

    def test_codex_header_slots_and_relative_reset_bind_t3_and_keep_access(self):
        row = file('codex')
        row['quota'] = dict(observed_at=usage.stamp(NOW), signals={
            'X-Codex-Plan-Type': 'pro', 'X-Codex-Primary-Used-Percent': '25',
            'X-Codex-Primary-Window-Minutes': '300', 'X-Codex-Primary-Reset-After-Seconds': '600',
            'X-Codex-Secondary-Used-Percent': '10', 'X-Codex-Secondary-Window-Minutes': '10080',
            'X-Codex-Secondary-Reset-At': str(NOW + 500000)})
        failing = Mock(return_value=dict(status_code=503))
        view = server.fetch_account(row, call=failing, cache=self.cache)
        self.assertEqual(view['windows'][0]['remaining'], 75)
        self.assertEqual(view['auth_state'], 'authenticated')
        from availability import classify
        self.assertEqual(classify(row, view, NOW)['state'], 'ready')
        self.assertEqual(view['_t3_usage']['body']['rate_limit']['primary_window']['reset_at'], NOW + 600)
        self.assertEqual(view['usage_source'], 'headers')
        from availability import classify
        self.assertEqual(classify(row, view, NOW + 601)['state'], 'unknown')

    def test_codex_single_slot_headers_accept_only_the_present_window(self):
        for slot in ('Primary', 'Secondary'):
            with self.subTest(slot=slot):
                row = dict(file('codex'), quota=codex_traffic(slot=slot))
                body, measured = quota_headers.payload(row, NOW)
                limits = body['rate_limit']
                key = slot.lower() + '_window'
                self.assertEqual(set(limits), {key, 'allowed', 'limit_reached'})
                self.assertEqual(limits[key], dict(used_percent=87, reset_at=NOW + 500000,
                    limit_window_seconds=604800))
                self.assertEqual(measured, usage.stamp(NOW))
                row['quota']['signals']['X-Codex-Limit-Reached'] = 'true'
                limits = quota_headers.payload(row, NOW)[0]['rate_limit']
                self.assertEqual(set(limits), {key, 'allowed', 'limit_reached'})
                self.assertEqual(limits[key]['used_percent'], 100)
                self.assertFalse(limits['allowed'])

    def test_codex_partial_malformed_or_duplicate_windows_reject_the_payload(self):
        for slot in ('Primary', 'Secondary'):
            for field, value in (('Used-Percent', None), ('Used-Percent', 'bad'),
                    ('Used-Percent', '-1'), ('Used-Percent', '101'), ('Used-Percent', True),
                    ('Used-Percent', 'nan'), ('Window-Minutes', '60'),
                    ('Reset-At', 'bad'), ('Reset-At', str(NOW)),
                    ('Reset-At', str(NOW + 8 * 86400 + 1)),
                    ('Reset-After-Seconds', 'bad'), ('Reset-After-Seconds', '-1'),
                    ('Status', 'bad\n'), ('Status', 'x' * 513)):
                with self.subTest(slot=slot, field=field, value=value):
                    quota = codex_traffic(slot=slot)
                    other = 'Secondary' if slot == 'Primary' else 'Primary'
                    quota['signals'].update(codex_traffic(slot=other, minutes=300)['signals'])
                    quota['signals']['X-Codex-' + slot + '-' + field] = value
                    self.assertIsNone(quota_headers.payload(dict(file('codex'), quota=quota), NOW))
            for value in (None, True, 'bad', 'bad\n'):
                quota = codex_traffic(slot=slot)
                other = 'Secondary' if slot == 'Primary' else 'Primary'
                quota['signals']['X-Codex-' + other + '-Used-Percent'] = value
                self.assertIsNone(quota_headers.payload(dict(file('codex'), quota=quota), NOW))
        quota = codex_traffic()
        quota['signals'].update(codex_traffic(slot='Secondary')['signals'])
        self.assertIsNone(quota_headers.payload(dict(file('codex'), quota=quota), NOW))
        for signals in ({}, {'X-Codex-Limit-Reached': 'true'}, {'X-Codex-Plan-Type': 'pro'}):
            self.assertIsNone(quota_headers.payload(dict(file('codex'), quota=dict(
                observed_at=usage.stamp(NOW), signals=signals)), NOW))

    def test_codex_present_slots_require_usage_duration_and_reset(self):
        for field in ('Used-Percent', 'Window-Minutes', 'Reset-At'):
            quota = codex_traffic()
            del quota['signals']['X-Codex-Primary-' + field]
            self.assertIsNone(quota_headers.payload(dict(file('codex'), quota=quota), NOW))
        for slot in ('Primary', 'Secondary'):
            for used in (0, 100):
                quota = codex_traffic(slot=slot, minutes=300, used=used)
                del quota['signals']['X-Codex-' + slot + '-Reset-At']
                quota['signals']['X-Codex-' + slot + '-Reset-After-Seconds'] = '600'
                limits = quota_headers.payload(dict(file('codex'), quota=quota), NOW)[0]['rate_limit']
                self.assertEqual(limits[slot.lower() + '_window'], dict(used_percent=used,
                    limit_window_seconds=18000, reset_at=NOW + 600))

    def test_codex_primary_only_weekly_headers_refresh_active_weekly_view(self):
        row = file('codex')
        read = Mock(return_value=dict(status_code=200, body=dict(rate_limit=dict(
            primary_window=dict(used_percent=84, reset_at=NOW + 500000, limit_window_seconds=604800)))))
        server.fetch_account(row, call=read, cache=self.cache)
        self.now += 60
        row['quota'] = codex_traffic(self.now)
        view = server.fetch_account(row, call=read, cache=self.cache)
        read.assert_called_once()
        self.assertEqual(len(view['windows']), 1)
        self.assertEqual(view['windows'][0]['used'], 87)
        self.assertEqual(view['windows'][0]['measured_at'], usage.stamp(self.now))
        self.assertEqual(view['quota_checked_at'], usage.stamp(self.now))
        self.assertEqual(view['usage_source'], 'headers')
        self.assertEqual(set(view['_t3_usage']['body']['rate_limit']), {'primary_window'})
        self.assertEqual(view['_t3_usage']['body']['rate_limit']['primary_window']['used_percent'], 87)

    def test_codex_secondary_only_flows_through_normalizer_and_t3(self):
        import t3_usage
        row = dict(file('codex'), quota=codex_traffic(slot='Secondary'))
        view = server.fetch_account(row, call=Mock(return_value=dict(status_code=503)), cache=self.cache)
        self.assertEqual([w['slot'] for w in view['windows']], ['secondary_window'])
        self.assertEqual(view['windows'][0]['used'], 87)
        self.assertEqual(set(view['_t3_usage']['body']['rate_limit']), {'secondary_window'})
        view['access_identity'] = view['_t3_usage']['identity']
        body = t3_usage.accounts(dict(online=True, checked_at=usage.stamp(NOW), accounts=[view]), NOW)[0][1]
        self.assertEqual(set(body['rate_limit']), {'secondary_window'})

    def test_codex_newer_weekly_endpoint_cannot_be_replaced_by_older_headers_in_another_slot(self):
        row = file('codex')
        read = Mock(return_value=dict(status_code=200, body=dict(rate_limit=dict(
            secondary_window=dict(used_percent=90, reset_at=NOW + 500000, limit_window_seconds=604800)))))
        server.fetch_account(row, call=read, cache=self.cache)
        self.now += 60
        row['quota'] = codex_traffic(NOW - 60)
        view = server.fetch_account(row, call=read, cache=self.cache)
        self.assertEqual(len(view['windows']), 1)
        self.assertEqual(view['windows'][0]['used'], 90)
        self.assertEqual(view['windows'][0]['measured_at'], usage.stamp(NOW))
        self.assertEqual([w['used_percent'] for w in view['_t3_usage']['body']['rate_limit'].values()], [90])

    def test_codex_newer_weekly_endpoint_does_not_keep_an_older_omitted_five_hour(self):
        self.now += 120
        row = file('codex')
        read = Mock(return_value=dict(status_code=200, body=dict(rate_limit=dict(
            primary_window=dict(used_percent=25, reset_at=NOW + 3000, limit_window_seconds=18000),
            secondary_window=dict(used_percent=90, reset_at=NOW + 500000, limit_window_seconds=604800)))))
        server.fetch_account(row, call=read, cache=self.cache)
        self.cache.entries[('codex', 'a')]['view']['windows'][0]['measured_at'] = usage.stamp(NOW - 60)
        row['quota'] = codex_traffic()
        view = server.fetch_account(row, call=read, cache=self.cache)
        self.assertEqual(len(view['windows']), 1)
        self.assertEqual(view['windows'][0]['used'], 90)
        self.assertEqual(view['windows'][0]['measured_at'], usage.stamp(self.now))
        projection = view['_t3_usage']['body']['rate_limit']
        self.assertEqual(set(projection), {'primary_window'})
        self.assertEqual(projection['primary_window']['used_percent'], 90)

    def test_codex_omitted_five_hour_uses_claude_carry_over_freshness_rules(self):
        # Slots are positional: primary may change from five-hour to weekly.
        for weekly_slot in ('primary_window', 'secondary_window'):
            five_slot = 'secondary_window' if weekly_slot == 'primary_window' else 'primary_window'
            for measured, reset, keep in ((NOW - 60, NOW + 3000, False),
                    (NOW, NOW + 3000, False), (NOW + 60, NOW + 3000, True),
                    (NOW + 60, NOW + 60, False), (NOW + 121, NOW + 3000, False)):
                with self.subTest(weekly_slot=weekly_slot, measured=measured, reset=reset):
                    self.now = NOW + 120
                    cache = usage.UsageCache(clock=lambda: 0, wall_clock=lambda: self.now,
                        jitter=lambda: 0, daily_jitter=lambda: 0)
                    row = file('codex')
                    read = Mock(return_value=dict(status_code=200, body=dict(rate_limit={
                        weekly_slot: dict(used_percent=84, reset_at=NOW + 500000, limit_window_seconds=604800),
                        five_slot: dict(used_percent=25, reset_at=reset, limit_window_seconds=18000)})))
                    server.fetch_account(row, call=read, cache=cache)
                    cached = cache.entries[('codex', 'a')]['view']
                    for window in cached['windows']:
                        window['measured_at'] = usage.stamp(measured if window['seconds'] == 18000 else NOW - 60)
                    row['quota'] = codex_traffic()
                    view = server.fetch_account(row, call=read, cache=cache)
                    by_seconds = {w['seconds']: w for w in view['windows']}
                    self.assertEqual(by_seconds[604800]['used'], 87)
                    self.assertEqual(by_seconds[604800]['measured_at'], usage.stamp(NOW))
                    self.assertEqual(18000 in by_seconds, keep)
                    if keep:
                        self.assertEqual(by_seconds[18000]['used'], 25)
                        self.assertEqual(by_seconds[18000]['measured_at'], usage.stamp(measured))
                    projection = view['_t3_usage']['body']['rate_limit']
                    self.assertEqual(len(projection), 2 if keep else 1)
                    self.assertEqual({w['limit_window_seconds'] for w in projection.values()}, set(by_seconds))
                    self.assertEqual(projection['primary_window']['used_percent'], 87)

    def test_newer_headers_survive_an_inflight_endpoint_response_and_alias(self):
        started, release = threading.Event(), threading.Event()
        errors = []
        def slow(*args):
            started.set()
            if not release.wait(3):
                raise AssertionError('fixture did not release')
            return dict(status_code=200, body=self.body)
        def run():
            try:
                self.cache.fetch(self.row, slow)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            self.now += 1
            row = dict(self.row, quota=traffic(self.now, .05))
            self.assertEqual(self.cache.fetch(row, self.call)['windows'][0]['used'], 5)
            alias = dict(row, name='other.json', email='other@example.test')
            self.assertEqual(self.cache.fetch(alias, self.call)['windows'], [])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.cache.cached(self.row)['windows'][0]['used'], 5)
        self.assertIsNone(self.cache.cached(alias))
        self.call.assert_not_called()

    def test_manual_retry_after_survives_positive_headers(self):
        self.cache.fetch(self.row, Mock(return_value=dict(status_code=429, header={'Retry-After': '7200'})))
        self.now += 60
        self.row['quota'] = traffic(self.now, .1)
        self.assertEqual(self.cache.fetch(self.row, self.call)['windows'][0]['used'], 10)
        with self.assertRaises(usage.RefreshError) as error:
            self.cache.fetch(self.row, self.call, manual=True)
        self.assertEqual(error.exception.retry_at, usage.stamp(NOW + 7200))
        self.call.assert_not_called()

    def test_headers_live_until_exact_reset_not_fixed_max_age(self):
        row = dict(self.row, quota=traffic(NOW - 7200))
        self.assertIsNotNone(quota_headers.payload(row, NOW + 2999))
        self.assertIsNone(quota_headers.payload(row, NOW + 3000))
        self.assertIsNone(quota_headers.payload(dict(row, quota=traffic(NOW + 1)), NOW))

    def test_optional_windows_and_spend_survive_an_endpoint_omission(self):
        first = self.cache.fetch(self.row, self.call)
        self.now += 60
        self.call.return_value['body'] = {k: v for k, v in self.body.items()
                                          if k in ('five_hour', 'seven_day')}
        view = self.cache.fetch(self.row, self.call, manual=True)
        self.assertEqual(view['windows'][-1], first['windows'][-1])
        self.assertEqual(view['extra_usage'], first['extra_usage'])
        self.assertEqual(view['extra_usage_checked_at'], first['extra_usage_checked_at'])

    def test_routing_accepts_old_model_reading_with_reset_and_valid_headers(self):
        self.cache.fetch(self.row, self.call)
        self.now += 7200
        self.row['quota'] = traffic(self.now)
        self.row['quota']['signals']['Anthropic-Ratelimit-Unified-5h-Reset'] = str(NOW + 18000)
        self.cache.fetch(self.row, self.call)
        runtime = server.claude_routing_projection([self.row], usage.stamp(self.now))
        usage.bind_claude_usage(runtime, [self.cache.cached(self.row)])
        import routing_policy
        overview = dict(gateway=dict(online=True, checked_at=usage.stamp(self.now),
            inventory=[self.row], claude_accounts=runtime), watch_accounts=[])
        # Keep the gateway poll current while the same header measurement ages.
        overview['gateway']['checked_at'] = usage.stamp(self.now + 1800)
        candidates, _ = routing_policy.eligible_accounts(overview, self.now + 1800,
            provider='claude', inventory=[self.row])
        self.assertEqual(set(candidates), {self.row['email']})
        self.assertEqual(self.cache.cached(self.row)['windows'][-1]['measured_at'], usage.stamp(NOW))


class ClaudeWarningTests(unittest.TestCase):
    def setUp(self):
        import routing_policy
        self.policy = routing_policy
        self.now = NOW - 60
        self.rows = [file(index=index) for index in ('a', 'b')]
        self.cache = usage.UsageCache(clock=lambda: self.now - NOW + 60,
            wall_clock=lambda: self.now, jitter=lambda: 0, daily_jitter=lambda: 0)
        self.call = Mock(return_value=dict(status_code=200, body=dict(
            five_hour=dict(utilization=25, resets_at=usage.stamp(NOW + 3000)),
            seven_day=dict(utilization=10, resets_at=usage.stamp(NOW + 500000)))))
        for row in self.rows:
            self.cache.fetch(row, self.call)
        self.now = NOW
        for i, row in enumerate(self.rows):
            row['quota'] = traffic()
            signals = row['quota']['signals']
            signals['Anthropic-Ratelimit-Unified-7d-Utilization'] = str(.4 - i * .2)
            signals['Anthropic-Ratelimit-Unified-7d-Reset'] = str(NOW + 100000 + i * 50000)
            for slot in ('5h', '7d'):
                signals['Anthropic-Ratelimit-Unified-' + slot + '-Status'] = 'allowed'

    def overview(self):
        views = [self.cache.fetch(row, self.call) for row in self.rows]
        runtime = server.claude_routing_projection(self.rows, usage.stamp(self.now))
        usage.bind_claude_usage(runtime, views)
        return dict(gateway=dict(online=True, checked_at=usage.stamp(self.now),
            inventory=self.rows, claude_accounts=runtime), watch_accounts=[])

    def warn(self, index, slot='5h', status='allowed_warning'):
        quota = self.rows[index]['quota']
        quota['observed_at'] = usage.stamp(self.now)
        quota['signals']['Anthropic-Ratelimit-Unified-' + slot + '-Status'] = status

    def choose(self, data, mode, current=None):
        return self.policy.choose(data, self.now, mode, current=current, provider='claude')

    def test_primary_header_status_is_retained_but_endpoint_status_is_not(self):
        for status in ('allowed', 'allowed_warning', 'rejected'):
            self.now += 60
            self.warn(0, status=status)
            view = self.cache.fetch(self.rows[0], self.call)
            window = view['windows'][0]
            self.assertEqual(window['status'], status)
            self.assertEqual(window['usage_source'], 'headers')
            self.assertEqual(window['measured_at'], usage.stamp(self.now))
            if status == 'rejected':
                self.assertEqual(window['remaining'], 0)
        body = copy.deepcopy(self.call.return_value['body'])
        body['five_hour']['status'] = 'allowed_warning'
        self.assertTrue(all('status' not in w for w in usage.normalize_claude(body)['windows']))

    def test_warned_best_account_is_demoted_in_both_modes_for_either_primary_window(self):
        for mode, best in (('earliest-weekly-reset', 0), ('highest-remaining-quota', 1)):
            for slot in ('5h', '7d'):
                for current in (None, self.rows[best]['email']):
                    with self.subTest(mode=mode, slot=slot, current=current):
                        self.now += 60
                        for index in (0, 1):
                            for window in ('5h', '7d'):
                                self.warn(index, window, 'allowed')
                        self.warn(best, slot)
                        plan = self.choose(self.overview(), mode, current)
                        self.assertEqual(plan['preferred_email'], self.rows[1 - best]['email'])
                        self.assertEqual(plan['priorities'][self.rows[best]['email']], 0)
                        self.assertEqual(plan['reason'],
                            'Moving new sessions off ' + self.rows[best]['email'] + ': near its limit')
        self.assertEqual(self.call.call_count, 2)

    def test_all_warned_preserves_existing_ranking_and_stability(self):
        baseline = self.overview()
        self.now += 60
        for index in (0, 1):
            self.warn(index)
        warned = self.overview()
        for mode in ('earliest-weekly-reset', 'highest-remaining-quota'):
            for current in (None, *[r['email'] for r in self.rows]):
                with self.subTest(mode=mode, current=current):
                    self.assertEqual(self.choose(warned, mode, current), self.choose(baseline, mode, current))

    def test_warning_bypasses_both_stability_margins(self):
        signals = self.rows[1]['quota']['signals']
        signals['Anthropic-Ratelimit-Unified-7d-Utilization'] = '.39'
        signals['Anthropic-Ratelimit-Unified-7d-Reset'] = str(NOW + 99400)
        baseline = self.overview()
        current = self.rows[0]['email']
        self.now += 60
        self.warn(0)
        warned = self.overview()
        for mode in ('earliest-weekly-reset', 'highest-remaining-quota'):
            with self.subTest(mode=mode):
                self.assertEqual(self.choose(baseline, mode, current)['preferred_email'], current)
                plan = self.choose(warned, mode, current)
                self.assertEqual(plan['preferred_email'], self.rows[1]['email'])
                self.assertIn('near its limit', plan['reason'])
        self.now += 60
        self.warn(1)
        all_warned = self.overview()
        for mode in ('earliest-weekly-reset', 'highest-remaining-quota'):
            self.assertEqual(self.choose(all_warned, mode, current), self.choose(baseline, mode, current))

    def test_newer_allowed_reading_clears_warning_but_older_one_does_not(self):
        self.warn(0)
        warned = self.overview()
        self.assertEqual(self.choose(warned, 'earliest-weekly-reset')['preferred_email'], self.rows[1]['email'])
        self.rows[0]['quota']['observed_at'] = usage.stamp(NOW - 1)
        self.rows[0]['quota']['signals']['Anthropic-Ratelimit-Unified-5h-Status'] = 'allowed'
        self.assertEqual(self.choose(self.overview(), 'earliest-weekly-reset')['preferred_email'], self.rows[1]['email'])
        self.now += 60
        self.warn(0, status='allowed')
        cleared = self.overview()
        view = cleared['gateway']['claude_accounts'][0]['_gateway_usage']
        self.assertEqual(usage.claude_near_limit_windows(view, self.now), [])
        self.assertEqual(self.choose(cleared, 'earliest-weekly-reset')['preferred_email'], self.rows[0]['email'])

    def test_warning_expires_at_its_window_reset_without_inventing_allowance(self):
        for slot, kind, reset in (('5h', 'five_hour', NOW + 3000), ('7d', 'seven_day', NOW + 100000)):
            with self.subTest(slot=slot):
                self.now += 60
                self.warn(0, '5h', 'allowed')
                self.warn(0, slot)
                data = self.overview()
                view = data['gateway']['claude_accounts'][0]['_gateway_usage']
                self.assertEqual(usage.claude_near_limit_windows(view, reset - 1), [kind])
                self.assertEqual(usage.claude_near_limit_windows(view, reset), [])
                data['gateway']['checked_at'] = usage.stamp(reset)
                with self.assertRaises(self.policy.DataHold):
                    self.policy.choose(data, reset, provider='claude')

    def test_newer_endpoint_reading_clears_warning_and_endpoint_only_ranking_is_unchanged(self):
        self.warn(0)
        data = self.overview()
        self.assertEqual(self.choose(data, 'earliest-weekly-reset')['preferred_email'], self.rows[1]['email'])
        self.now += 60
        # Even an endpoint payload's status field must not create header evidence.
        self.call.return_value['body']['five_hour']['status'] = 'allowed_warning'
        view = self.cache.fetch(self.rows[0], self.call, manual=True)
        self.assertTrue(all('status' not in w for w in view['windows']))
        self.assertEqual(usage.claude_near_limit_windows(view, self.now), [])
        self.assertEqual(self.choose(self.overview(), 'earliest-weekly-reset')['preferred_email'], self.rows[1]['email'])
        # Restore the earlier weekly reset through a newer endpoint observation.
        self.now += 60
        self.call.return_value['body']['seven_day']['resets_at'] = usage.stamp(NOW + 90000)
        self.cache.fetch(self.rows[0], self.call, manual=True)
        self.assertEqual(self.choose(self.overview(), 'earliest-weekly-reset')['preferred_email'], self.rows[0]['email'])

    def test_endpoint_only_accounts_keep_existing_ranking(self):
        self.now += 60
        for i, row in enumerate(self.rows):
            row.pop('quota')
            body = self.call.return_value['body']
            body['five_hour']['status'] = 'allowed_warning'
            body['seven_day'].update(utilization=40 - i * 20,
                resets_at=usage.stamp(NOW + 100000 + i * 50000))
            self.cache.fetch(row, self.call, manual=True)
        data = self.overview()
        for mode, best in (('earliest-weekly-reset', 0), ('highest-remaining-quota', 1)):
            plan = self.choose(data, mode)
            self.assertEqual(plan['preferred_email'], self.rows[best]['email'])
            self.assertIsNone(plan['reason'])
        for account in data['gateway']['claude_accounts']:
            view = account['_gateway_usage']
            self.assertTrue(all('status' not in w for w in view['windows']))
            self.assertEqual(usage.claude_near_limit_windows(view, self.now), [])

    def test_dashboard_projection_exposes_current_warning_windows(self):
        self.warn(0)
        self.warn(0, '7d')
        data = self.overview()
        state = server.GatewayState(Mock(), Mock())
        self.addCleanup(state.signins.close)
        state.data = dict(data['gateway'], accounts=[])
        with patch.object(server, 'watch_accounts', return_value=[]), \
                patch.object(server, 'billing_records', return_value={}), \
                patch.object(server, 'preference_status', return_value=None), \
                patch.object(self.policy, 'routing_linked', return_value=True), \
                patch.object(server.time, 'time', return_value=self.now):
            projected = state.snapshot()['claude_accounts']
            self.assertIs(projected[0]['near_limit'], True)
            self.assertEqual(projected[0]['near_limit_windows'], ['five_hour', 'seven_day'])
            self.assertIs(projected[1]['near_limit'], False)
            self.assertEqual(projected[1]['near_limit_windows'], [])
            with patch.object(server.time, 'time', return_value=NOW + 3000):
                self.assertEqual(state.snapshot()['claude_accounts'][0]['near_limit_windows'], ['seven_day'])
            with patch.object(server.time, 'time', return_value=NOW + 100000):
                cleared = state.snapshot()['claude_accounts'][0]
                self.assertIs(cleared['near_limit'], False)
                self.assertEqual(cleared['near_limit_windows'], [])
        self.assertNotIn('near_limit', state.data['claude_accounts'][0])


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.profiles = subscriptions.Profiles(clock=lambda: self.now - NOW,
            wall_clock=lambda: self.now, jitter=lambda: 0, daily_jitter=lambda: 0)
        self.usage = Mock()
        self.usage.cached.return_value = {}
        self.usage.event_due.return_value = False
        self.call = Mock(return_value=dict(status_code=200, body=profile()))

    def test_plan_tiers_flags_legacy_names_and_rename_equivalence(self):
        for tier, ultra, expected in [('default_claude_max_5x', False, 'Max'),
                ('default_claude_max_20x', False, 'Ultra'), ('new_claude_ultra_verified', False, 'Ultra'),
                ('ULTRA_rollout', False, 'Ultra'), ('unknown', True, 'Ultra')]:
            raw = profile()
            raw['organization']['rate_limit_tier'] = tier
            raw['account']['has_claude_ultra'] = ultra
            self.assertEqual(subscriptions.normalize(raw, file())['plan'], expected)
        for old, new, tier in [('Max (5x)', 'Max', 'default_claude_max_5x'),
                               ('Max (20x)', 'Ultra', 'default_claude_max_20x')]:
            self.call.return_value['body'] = profile('max')
            self.profiles.fetch(file(), self.call, self.usage)
            entry = self.profiles.cache.entries[('claude', 'a')]
            entry['view'].update(plan=old, plan_change=dict(previous=old, current=new, checked_at=usage.stamp(self.now)))
            self.assertEqual(self.profiles.view(file())['plan'], new)
            self.assertIsNone(self.profiles.view(file())['plan_change'])
            self.now += 60
            self.call.return_value['body']['organization']['rate_limit_tier'] = tier
            self.profiles.fetch(file(), self.call, self.usage, manual=True)
            self.assertEqual(self.profiles.view(file())['plan'], new)
            self.assertIsNone(self.profiles.view(file())['plan_change'])

    def test_active_and_future_entitlements_prevent_false_ended_alarm(self):
        for status in ('active', 'trialing', None, 'unknown'):
            for flag in (None, 'has_claude_ultra', 'has_claude_future'):
                raw = profile(status=status)
                raw['organization']['rate_limit_tier'] = 'future_ultra'
                raw['account'].update(has_claude_pro=False, has_claude_max=False)
                if flag:
                    raw['account'][flag] = True
                value = subscriptions.normalize(raw, file())
                ok = status in ('active', 'trialing') or flag is not None
                self.assertEqual(value['state'], 'ok' if ok else 'ended')
                self.assertEqual(value['entitled'], ok)
                self.assertEqual(value['plan'], 'Ultra')
        for status in ('canceled', 'cancelled', 'expired', 'incomplete_expired'):
            raw['organization']['subscription_status'] = status
            self.assertEqual(subscriptions.normalize(raw, file())['state'], 'ended')

    def test_profile_daily_budget_and_manual_active_plan_change(self):
        row = file()
        self.profiles.fetch(row, self.call, self.usage)
        self.assertIsNone(self.profiles.view(row)['plan_change'])
        self.usage.due_now.assert_called_once_with(row)
        self.now += 86399
        self.profiles.fetch(row, self.call, self.usage)
        self.call.assert_called_once()
        self.now += 1
        self.call.return_value['body'] = profile('max')
        self.profiles.fetch(row, self.call, self.usage)
        view = self.profiles.view(row)
        self.assertEqual((view['plan'], view['subscription_status']), ('Max', 'active'))
        self.assertEqual(view['plan_change']['previous'], 'Pro')
        self.assertEqual(self.usage.due_now.call_count, 2)
        self.profiles.fetch(row, self.call, self.usage)
        self.assertEqual(self.usage.due_now.call_count, 2)
        self.assertNotIn('invoice', json.dumps(view))
        self.assertNotIn('subscription_created_at', json.dumps(view))

    def test_manual_profile_and_usage_rate_limits_are_independent(self):
        row = file()
        self.profiles.fetch(row, self.call, self.usage, manual=True)
        self.call.return_value = dict(status_code=429, header={'Retry-After': '7200'})
        self.now += 60
        self.profiles.fetch(row, self.call, self.usage, manual=True)
        self.assertFalse(self.profiles.view(row)['current'])
        self.now += 3600
        self.profiles.fetch(row, self.call, self.usage, manual=True)
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.profiles.view(row)['plan'], 'Pro')

    def test_inventory_identity_unverified_status_and_restart_are_conservative(self):
        row = file()
        self.call.return_value['body'] = profile(email='different@example.test')
        self.profiles.fetch(row, self.call, self.usage)
        self.assertIsNone(self.profiles.view(row)['plan'])
        self.assertFalse(self.profiles.view(row)['current'])
        self.assertIsNone(subscriptions.normalize(profile(status='unexpected'), row)['subscription_status'])
        self.assertIsNone(subscriptions.Profiles().view(row)['plan_change'])
        alias = dict(row, name='replacement.json', email='other@example.test')
        self.assertIsNone(self.profiles.view(alias)['plan'])
        self.usage.due_now.assert_not_called()

    def test_manual_refresh_queries_profile_even_when_usage_is_cooled(self):
        state = server.GatewayState(resets=Mock(), notices=Mock(), fast_modes=Mock())
        state.profiles = self.profiles
        self.addCleanup(state.signins.close)
        state.usage_cache = Mock()
        state.usage_cache.cached.return_value = {}
        state.usage_cache.available.return_value = {'available_at': usage.stamp(NOW + 7200)}
        state.usage_cache.event_due.return_value = False
        state.usage_cache.fetch.side_effect = usage.RefreshError('rate_limited', 429)
        def call(path, body=None, **kwargs):
            return {'files': [file()]} if path == '/auth-files' else self.call(path, body)
        with patch.object(server, 'api', side_effect=call), patch.object(server, 'watch_accounts', return_value=[]):
            result = state.refresh_usage('claude', file()['name'])
        self.assertTrue(result['profile_refreshed'])
        self.assertFalse(result['usage_refreshed'])
        self.assertEqual(self.profiles.view(file())['subscription_status'], 'active')
        self.assertTrue(self.call.call_args.args[1]['url'].endswith('/profile'))

    def test_watch_usage_retry_after_does_not_hold_the_profile(self):
        # The watch's 429 deadline is for another endpoint and token.
        state = server.GatewayState(resets=Mock(), notices=Mock(), fast_modes=Mock())
        state.profiles = self.profiles
        self.addCleanup(state.signins.close)
        state.usage_cache = Mock()
        state.usage_cache.cached.return_value = {}
        state.usage_cache.available.return_value = {'available_at': usage.stamp(NOW + 7200)}
        state.usage_cache.event_due.return_value = False
        state.usage_cache.fetch.side_effect = usage.RefreshError('rate_limited', 429)
        def call(path, body=None, **kwargs):
            return {'files': [file()]} if path == '/auth-files' else self.call(path, body)
        watch = {'provider_retry_at': usage.stamp(NOW + 7200)}
        with patch.object(server, 'api', side_effect=call), patch.object(server, 'watch_accounts', return_value=[]), \
                patch.object(server.usage_adapters, 'matched_claude_watch',
                             side_effect=lambda f, rows, watched: watch if rows else None):
            result = state.refresh_usage('claude', file()['name'])
        self.assertTrue(result['profile_refreshed'])
        self.assertTrue(self.call.call_args.args[1]['url'].endswith('/profile'))

    def test_current_profile_plan_overrides_old_watch_plan_even_when_ended(self):
        import providers
        row = file()
        for plan in ('Max (5x)', None):
            runtime = dict(row, key=row['name'], subscription=dict(current=True, plan=plan),
                _gateway_usage=dict(provider='claude', key=row['name'], auth_index=row['auth_index'],
                    email=row['email'], status='ok', checked_at=usage.stamp(NOW), windows=[], plan='Pro'))
            groups = providers.groups(dict(inventory=[row], claude_accounts=[runtime]), [])
            self.assertEqual(groups[0]['accounts'][0]['plan'], subscriptions.plan_name(plan))

    def test_each_subscription_state_flags_and_profile_age(self):
        for status, state in [('active', 'ok'), ('trialing', 'ok'), ('canceled', 'ended'),
                              ('expired', 'ended'), ('incomplete_expired', 'ended'),
                              ('past_due', 'payment_due'), ('unpaid', 'payment_due'),
                              ('incomplete', 'payment_due'), ('unknown', 'ok')]:
            self.assertEqual(subscriptions.normalize(profile(status=status), file())['state'], state)
        raw = profile()
        raw['account'].update(has_claude_pro=False, has_claude_max=False)
        self.assertEqual(subscriptions.normalize(raw, file())['state'], 'ok')
        raw['account'].pop('has_claude_pro');raw['account'].pop('has_claude_max')
        self.assertEqual(subscriptions.normalize(raw, file())['state'], 'ok')
        self.profiles.fetch(file(), self.call, self.usage)
        self.now += 90001
        self.assertFalse(self.profiles.view(file())['current'])
        self.assertEqual(self.profiles.view(file())['checked_at'], usage.stamp(NOW))

    def test_plan_change_expires_at_24_hours_without_fabricating_initial_change(self):
        self.profiles.fetch(file(), self.call, self.usage)
        self.assertIsNone(self.profiles.view(file())['plan_change'])
        self.now += 60
        self.call.return_value['body'] = profile('max')
        self.profiles.fetch(file(), self.call, self.usage, manual=True)
        self.now += 86399
        self.assertEqual(self.profiles.view(file())['plan_change']['previous'], 'Pro')
        self.now += 1
        self.assertIsNone(self.profiles.view(file())['plan_change'])


class ProFallbackTests(unittest.TestCase):
    def test_pro_organization_without_entitlement_flags_keeps_plan_and_entitlement(self):
        raw = profile()
        raw['account'] = {'email': 'a@example.test'}
        for status in ('active', None, 'paused', 'expired'):
            raw['organization']['subscription_status'] = status
            value = subscriptions.normalize(raw, file())
            self.assertEqual(value['plan'], 'Pro')
            self.assertEqual(value['entitled'], status in ('active', None))
            self.assertEqual(value['state'], 'ended' if status in ('paused', 'expired') else 'ok')


class EventTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.cache = usage.UsageCache(clock=lambda: self.now - NOW, wall_clock=lambda: self.now,
                                      jitter=lambda: 0, daily_jitter=lambda: 123)
        self.body = dict(five_hour=dict(utilization=20, resets_at=usage.stamp(NOW + 6000)),
                         seven_day=dict(utilization=10, resets_at=usage.stamp(NOW + 500000)))
        self.call = Mock(return_value=dict(status_code=200, body=self.body))

    def read(self, row, **kwargs):
        if row['type'] == 'codex':
            return server.fetch_account(row, call=self.call, cache=self.cache, **kwargs)
        return self.cache.fetch(row, self.call, **kwargs)

    def codex(self):
        self.call.return_value['body'] = dict(rate_limit={slot: dict(used_percent=20,
            reset_at=NOW + delay, limit_window_seconds=seconds) for slot, delay, seconds in (
            ('primary_window', 6000, 18000), ('secondary_window', 500000, 604800))})

    def test_each_cooldown_signal_is_one_event_per_episode_for_both_providers(self):
        for provider in ('claude', 'codex'):
            for change in (dict(status='error'), dict(unavailable=True),
                           dict(next_retry_after=usage.stamp(NOW + 3000))):
                with self.subTest(provider=provider, change=change):
                    self.setUp()
                    if provider == 'codex': self.codex()
                    row = file(provider)
                    self.read(row)
                    self.now += 60
                    cooled = dict(row, **change)
                    self.read(cooled)
                    self.assertEqual(self.call.call_count, 2)
                    self.now += 60
                    self.read(dict(cooled, status='error', unavailable=True))
                    self.assertEqual(self.call.call_count, 2)
                    self.read(row)
                    self.now += 60
                    self.read(cooled)
                    self.assertEqual(self.call.call_count, 3)

    def test_watch_deferral_still_ends_episode_and_allows_the_next_edge(self):
        row = dict(file(), unavailable=True)
        self.cache.fetch(row, self.call)
        self.now += 60
        row['unavailable'] = False
        watch = dict(id='claude', email=row['email'], independent_windows=False,
                     attempted_at=usage.stamp(self.now), checked_at=usage.stamp(self.now))
        self.cache.fetch(row, self.call, runtime_rows=[row], watched=[watch])
        self.assertIsNone(self.cache.entries[('claude', 'a')]['episode'])
        self.now += 60
        row['unavailable'] = True
        self.cache.fetch(row, self.call, runtime_rows=[row], watched=[watch])
        self.assertEqual(self.call.call_count, 2)

    def test_reset_edge_checks_once_and_retains_provider_backoff(self):
        row = dict(file(), unavailable=True)
        self.cache.fetch(row, self.call)
        self.now += 1
        self.call.return_value = dict(status_code=429, header={'Retry-After': '7200'})
        self.cache.fetch(row, self.call, manual=True)
        self.now = NOW + 6000
        self.cache.fetch(row, self.call)
        self.assertEqual(self.call.call_count, 2)
        self.assertIn(('claude', 'a'), self.cache.pending)
        self.now = NOW + 7201
        self.call.return_value = dict(status_code=200, body=self.body)
        self.cache.fetch(row, self.call)
        self.assertEqual(self.call.call_count, 3)
        self.now += 60
        self.cache.fetch(row, self.call)
        self.assertEqual(self.call.call_count, 3)

    def test_next_retry_edge_triggers_once_without_a_window(self):
        row = dict(file(), unavailable=True, next_retry_after=usage.stamp(NOW + 120))
        self.cache.fetch(row, self.call)
        self.now += 119
        self.cache.fetch(row, self.call)
        self.call.assert_called_once()
        self.now += 1
        self.cache.fetch(row, self.call)
        self.assertEqual(self.call.call_count, 2)
        self.now += 60
        self.cache.fetch(row, self.call)
        self.assertEqual(self.call.call_count, 2)

    def test_daily_jitter_per_account_and_no_hourly_polling(self):
        jitters = iter((123, 456, 789))
        self.cache.daily_jitter = lambda: next(jitters)
        self.body['five_hour']['resets_at'] = usage.stamp(NOW + 90000)
        self.cache.fetch(file(), self.call)
        self.cache.fetch(file(index='b'), self.call)
        self.assertEqual(self.cache.entries[('claude', 'a')]['due'], 86523)
        self.assertEqual(self.cache.entries[('claude', 'b')]['due'], 86856)
        for delta in (1800, 3600, 7200, 86400, 86522):
            self.now = NOW + delta
            self.cache.fetch(file(), self.call)
            self.cache.fetch(file(index='b'), self.call)
        self.assertEqual(self.call.call_count, 2)
        self.now = NOW + 86523
        self.cache.fetch(file(), self.call)
        self.assertEqual(self.call.call_count, 3)

    def codex_body(self, weekly_reset):
        return dict(rate_limit={slot: dict(used_percent=used, reset_at=reset, limit_window_seconds=seconds)
            for slot, used, reset, seconds in (('primary_window', 20, NOW + 18000, 18000),
                                               ('secondary_window', 30, weekly_reset, 604800))})

    def test_idle_ready_codex_reads_once_just_after_an_unexhausted_weekly_reset(self):
        from availability import classify
        row = file('codex')
        self.call.return_value['body'] = self.codex_body(NOW + 3000)
        self.assertEqual(classify(row, self.read(row), NOW)['state'], 'ready')
        self.now = NOW + 3059
        self.read(row)
        self.call.assert_called_once()
        self.call.return_value['body'] = self.codex_body(NOW + 3000 + 604800)
        self.now = NOW + 3060
        self.assertEqual(classify(row, self.read(row), self.now)['state'], 'ready')
        self.assertEqual(self.call.call_count, 2)
        for delta in (3120, 7200, 17000):
            self.now = NOW + delta
            self.read(row)
        self.assertEqual(self.call.call_count, 2)

    def test_idle_claude_post_reset_read_does_not_force_a_profile_read(self):
        row = file()
        self.read(row)
        self.now = NOW + 6060
        self.assertFalse(self.cache.event_due(row))
        self.read(row)
        self.assertEqual(self.call.call_count, 2)
        self.now += 60
        self.read(row)
        self.assertEqual(self.call.call_count, 2)

    def test_model_reset_reads_once_despite_new_primary_headers_and_long_schedule(self):
        row = file()
        self.body['seven_day_overage_included'] = dict(utilization=90, resets_at=usage.stamp(NOW + 120))
        self.read(row)
        self.now = NOW + 179
        row['quota'] = traffic(self.now)
        self.read(row)
        self.call.assert_called_once()
        self.assertFalse(self.cache.event_due(row))  # Does not force a subscription/profile read.
        self.now = NOW + 180
        self.read(row)
        self.assertEqual(self.call.call_count, 2)
        # Even an endpoint that keeps returning the elapsed model reset must not spin.
        for delta in (181, 240, 360):
            self.now = NOW + delta
            self.read(row)
        self.assertEqual(self.call.call_count, 2)

    def test_model_reset_read_failure_preserves_provider_retry_after(self):
        row = file()
        self.body['seven_day_overage_included'] = dict(utilization=90, resets_at=usage.stamp(NOW + 120))
        self.read(row)
        self.now = NOW + 180
        self.call.return_value = dict(status_code=429, header={'Retry-After': '7200'})
        self.read(row)
        self.assertEqual(self.call.call_count, 2)
        self.now += 7199
        self.read(row)
        self.assertEqual(self.call.call_count, 2)
        self.now += 1
        self.call.return_value = dict(status_code=200, body=self.body)
        self.read(row)
        self.assertEqual(self.call.call_count, 3)

    def test_newer_header_reading_with_a_later_reset_needs_no_post_reset_read(self):
        row = file()
        self.read(row)
        self.now = NOW + 5000
        row['quota'] = dict(observed_at=usage.stamp(self.now), signals={
            'Anthropic-Ratelimit-Unified-5h-Utilization': '.3',
            'Anthropic-Ratelimit-Unified-5h-Reset': str(self.now + 18000),
            'Anthropic-Ratelimit-Unified-7d-Utilization': '.1',
            'Anthropic-Ratelimit-Unified-7d-Reset': str(NOW + 500000)})
        self.assertEqual(self.read(row)['usage_source'], 'headers')
        for delta in (6060, 7200):
            self.now = NOW + delta
            self.read(row)
        self.call.assert_called_once()

    def test_endpoint_returning_the_passed_reset_does_not_repeat_reads(self):
        row = file('codex')
        self.call.return_value['body'] = self.codex_body(NOW + 3000)
        self.read(row)
        for delta in (3060, 3120, 3600, 17000):
            self.now = NOW + delta
            self.read(row)
        self.assertEqual(self.call.call_count, 2)

    def test_post_reset_read_keeps_a_failure_backoff(self):
        row = file()
        self.read(row)
        self.call.return_value = dict(status_code=500)
        for minute in range(1, 7):
            self.now = NOW + 100 * minute
            self.read(row, manual=True)
        # Six failures back off 300 * 2**5 s, past the kept 5-hour reset at +6000.
        self.assertEqual(self.call.call_count, 7)
        self.now = NOW + 6060
        self.read(row)
        self.assertEqual(self.call.call_count, 7)

    def test_post_reset_read_waits_for_provider_retry_after(self):
        row = file()
        self.read(row)
        self.now = NOW + 100
        self.call.return_value = dict(status_code=429, header={'Retry-After': '7200'})
        self.read(row, manual=True)
        self.call.return_value = dict(status_code=200, body=dict(self.body, five_hour=dict(
            utilization=0, resets_at=usage.stamp(NOW + 7300 + 18000))))
        for delta in (6060, 7299):
            self.now = NOW + delta
            self.read(row)
        self.assertEqual(self.call.call_count, 2)
        self.now = NOW + 7300
        self.read(row)
        self.assertEqual(self.call.call_count, 3)
        self.now += 60
        self.read(row)
        self.assertEqual(self.call.call_count, 3)

    def test_manual_floor_and_scheduled_429_hour_survive_event(self):
        self.call.return_value = dict(status_code=429)
        self.cache.fetch(file(), self.call, manual=True)
        self.now += 30
        with self.assertRaises(usage.RefreshError):
            self.cache.fetch(file(), self.call, manual=True)
        self.cache.fetch(dict(file(), unavailable=True), self.call)
        self.assertEqual(self.call.call_count, 1)
        self.now = NOW + 3599
        self.cache.fetch(dict(file(), unavailable=True), self.call)
        self.assertEqual(self.call.call_count, 1)
        self.now += 1
        self.cache.fetch(dict(file(), unavailable=True), self.call)
        self.assertEqual(self.call.call_count, 2)

    def test_profile_events_follow_same_episode_reset_and_daily_rules(self):
        profiles = subscriptions.Profiles(clock=lambda: self.now - NOW, wall_clock=lambda: self.now,
            jitter=lambda: 0, daily_jitter=lambda: 123)
        profile_call = Mock(return_value=dict(status_code=200, body=profile()))
        row = file()
        self.cache.fetch(row, self.call)
        profiles.fetch(row, profile_call, self.cache)
        self.cache.fetch(row, self.call)
        self.now += 1800
        profiles.fetch(row, profile_call, self.cache)
        profile_call.assert_called_once()
        row['unavailable'] = True
        profiles.fetch(row, profile_call, self.cache)
        self.cache.fetch(row, self.call)
        profiles.fetch(row, profile_call, self.cache)
        self.assertEqual(profile_call.call_count, 2)
        self.now = NOW + 6000
        profiles.fetch(row, profile_call, self.cache)
        self.cache.fetch(row, self.call)
        profiles.fetch(row, profile_call, self.cache)
        self.assertEqual(profile_call.call_count, 3)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.cache = usage.UsageCache(clock=lambda: self.now - NOW, wall_clock=lambda: self.now,
                                      jitter=lambda: 0, daily_jitter=lambda: 0)
        self.row = dict(file(), unavailable=True)
        self.read = Mock(return_value=dict(status_code=200, body=dict(
            five_hour=dict(utilization=20, resets_at=usage.stamp(NOW + 6000)),
            seven_day=dict(utilization=10, resets_at=usage.stamp(NOW + 500000)))))
        self.cache.fetch(self.row, self.read)
        self.now += 1
        self.row['quota'] = traffic(self.now)
        self.cache.fetch(self.row, self.read)
        self.profile = dict(current=True, state='ok', subscription_status='active', entitled=True)
        self.reset = Mock(return_value={'status': 'ok'})

    def test_all_guards_and_positive_recovery(self):
        key = ('claude', 'a')
        original = copy.deepcopy(self.cache.entries[key]['view'])
        cases = [lambda: self.row.update(disabled=True),
                 lambda: self.row.update(status='disabled'),
                 lambda: self.row.update(unavailable=False, status='active'),
                 lambda: self.profile.update(current=False),
                 lambda: self.profile.update(state='unknown'),
                 lambda: self.profile.update(subscription_status='past_due'),
                 lambda: self.profile.update(entitled=False, subscription_status=None),
                 lambda: self.cache.entries[key]['view'].update(status='error'),
                 lambda: self.cache.entries[key]['view']['windows'].pop(),
                 lambda: self.cache.entries[key]['view']['windows'][0].update(remaining=0),
                 lambda: self.cache.entries[key]['view']['windows'][0].update(remaining=None),
                 lambda: self.cache.entries[key]['view']['windows'][0].update(status='rejected'),
                 lambda: self.cache.entries[key]['view']['windows'][0].update(measured_at=usage.stamp(NOW)),
                 lambda: self.cache.entries[key]['view']['windows'][0].update(measured_at=usage.stamp(self.now + 1)),
                 lambda: self.cache.entries[key]['view']['windows'][0].update(resets_at=self.now),
                 lambda: self.cache.entries[key]['view']['windows'].append(dict(kind='weekly_model',
                     remaining=0, resets_at=NOW + 6000, measured_at=usage.stamp(NOW - 7200))),
                 lambda: self.cache.entries[key]['view']['windows'].append(dict(kind='weekly_model',
                     remaining=0, resets_at=None, measured_at=usage.stamp(NOW)))]
        for change in cases:
            with self.subTest(guard=cases.index(change)):
                self.row = dict(file(), unavailable=True, quota=traffic(self.now))
                self.profile = dict(current=True, state='ok', subscription_status='active', entitled=True)
                self.cache.entries[key]['view'] = copy.deepcopy(original)
                change()
                self.cache.recover(self.row, self.profile, self.reset)
                self.reset.assert_not_called()
        self.cache.entries[key]['view'] = copy.deepcopy(original)
        result = self.cache.recover(self.row, self.profile, self.reset)
        self.assertEqual(result['outcome'], 'cleared')
        self.reset.assert_called_once_with('/reset-quota', {'auth_index': 'a'},
            episode=self.cache.entries[('claude', 'a')]['episode'],
            episode_started_at=self.cache.entries[('claude', 'a')]['episode']['started_at'])
        for _ in range(3): self.cache.recover(self.row, self.profile, self.reset)
        self.reset.assert_called_once()
        self.assertEqual(self.cache.recovery_view(self.row)['outcome'], 'cleared')

    def test_weekly_only_recovery_uses_current_entitlement_and_all_reported_windows(self):
        view = self.cache.entries[('claude', 'a')]['view']
        view['windows'] = [w for w in view['windows'] if w['kind'] == 'seven_day']
        self.profile.update(subscription_status=None, entitled=True)
        weekly = view['windows'][0]
        for changes in ({'remaining': 0}, {'remaining': None}, {'invalid_reset': True},
                        {'resets_at': self.now}, {'measured_at': usage.stamp(NOW)}):
            original = dict(weekly)
            weekly.update(changes)
            self.assertIsNone(self.cache.recover(self.row, self.profile, self.reset))
            weekly.clear(); weekly.update(original)
        view['windows'].append(dict(kind='five_hour', remaining=None, resets_at=None))
        self.assertIsNone(self.cache.recover(self.row, self.profile, self.reset))
        view['windows'].pop()
        self.assertEqual(self.cache.recover(self.row, self.profile, self.reset)['outcome'], 'cleared')
        self.reset.assert_called_once()

    def test_failed_attempt_never_retries_but_new_episode_can_recover(self):
        self.reset.side_effect = OSError('never publish')
        result = self.cache.recover(self.row, self.profile, self.reset)
        self.assertEqual(result['outcome'], 'failed')
        self.cache.recover(self.row, self.profile, self.reset)
        self.reset.assert_called_once()
        self.row['unavailable'] = False
        self.cache.fetch(self.row, self.read)
        self.now += 60
        self.row['unavailable'] = True
        self.cache.fetch(self.row, self.read)
        self.assertIsNone(self.cache.recover(self.row, self.profile, self.reset))
        self.now += 1
        self.row['quota'] = traffic(self.now)
        self.cache.fetch(self.row, self.read)
        self.reset.side_effect = None
        self.assertEqual(self.cache.recover(self.row, self.profile, self.reset)['outcome'], 'cleared')
        self.assertEqual(self.reset.call_count, 2)
        self.assertNotIn('never publish', json.dumps(self.cache.recovery_view(self.row)))

    def test_known_watch_model_blocks_and_expired_model_does_not(self):
        model = dict(kind='weekly_model', remaining=0, resets_at=self.now + 10, measured_at=usage.stamp(NOW))
        self.cache.recover(self.row, self.profile, self.reset, extra_windows=[model])
        self.reset.assert_not_called()
        model['resets_at'] = self.now - 1
        self.assertEqual(self.cache.recover(self.row, self.profile, self.reset, extra_windows=[model])['outcome'], 'cleared')

    def test_codex_requires_both_slots_with_post_episode_allowance(self):
        row = dict(file('codex'), unavailable=True)
        body = dict(rate_limit={slot: dict(used_percent=10, reset_at=NOW + delay,
            limit_window_seconds=seconds) for slot, delay, seconds in (
            ('primary_window', 6000, 18000), ('secondary_window', 500000, 604800))})
        body['rate_limit']['allowed'] = True
        read = Mock(return_value=dict(status_code=200, body=body))
        server.fetch_account(row, call=read, cache=self.cache)
        self.assertIsNone(self.cache.recover(row, {}, self.reset))
        self.now += 60
        server.fetch_account(row, call=read, cache=self.cache, manual=True)
        self.assertEqual(self.cache.recover(row, {}, self.reset)['outcome'], 'cleared')
        self.reset.assert_called_once_with('/reset-quota', {'auth_index': 'a'},
            episode=self.cache.entries[('codex', 'a')]['episode'],
            episode_started_at=self.cache.entries[('codex', 'a')]['episode']['started_at'])

    def test_codex_single_window_headers_do_not_relax_cooldown_recovery(self):
        row = dict(file('codex'), unavailable=True)
        read = Mock(return_value=dict(status_code=503))
        server.fetch_account(row, call=read, cache=self.cache)
        self.now += 60
        row['quota'] = codex_traffic(self.now)
        view = server.fetch_account(row, call=read, cache=self.cache)
        self.assertEqual(view['windows'][0]['used'], 87)
        self.assertEqual(len(view['windows']), 1)
        self.assertIsNone(self.cache.recover(row, {}, self.reset))
        self.reset.assert_not_called()

    def test_codex_rejection_below_100_never_proves_recovery_allowance(self):
        for source in ('endpoint', 'headers'):
            for flags in ({'allowed': False}, {'limit_reached': True},
                          {'allowed': True, 'limit_reached': True}, {}):
                if source == 'headers' and flags != {'limit_reached': True}:
                    continue
                with self.subTest(source=source, flags=flags):
                    self.now = NOW
                    cache = usage.UsageCache(clock=lambda: self.now - NOW,
                        wall_clock=lambda: self.now, jitter=lambda: 0)
                    row = dict(file('codex'), unavailable=True)
                    body = dict(rate_limit=dict(flags, **{slot: dict(used_percent=99.9,
                        reset_at=NOW + delay, limit_window_seconds=seconds)
                        for slot, delay, seconds in (('primary_window', 6000, 18000),
                            ('secondary_window', 500000, 604800))}))
                    read = Mock(return_value=dict(status_code=200, body=body))
                    server.fetch_account(row, call=read, cache=cache)
                    self.now += 60
                    if source == 'headers':
                        row['quota'] = dict(observed_at=usage.stamp(self.now), signals={
                            'X-Codex-Limit-Reached': 'true',
                            **{'X-Codex-' + slot + '-' + field: str(value)
                               for slot, delay, minutes in (('Primary', 6000, 300),
                                   ('Secondary', 500000, 10080))
                               for field, value in (('Used-Percent', 99.9),
                                   ('Window-Minutes', minutes), ('Reset-At', NOW + delay))}})
                    server.fetch_account(row, call=read, cache=cache, manual=source == 'endpoint')
                    reset = Mock(return_value={'status': 'ok'})
                    self.assertIsNone(cache.recover(row, {}, reset))
                    reset.assert_not_called()
                    # A later affirmative reading can still recover this episode.
                    self.now += 60
                    row.pop('quota', None)
                    body['rate_limit'].update(allowed=True, limit_reached=False)
                    server.fetch_account(row, call=read, cache=cache, manual=True)
                    self.assertEqual(cache.recover(row, {}, reset)['outcome'], 'cleared')
                    reset.assert_called_once()

    def test_codex_scoped_exhaustion_survives_primary_headers_until_reset(self):
        row = dict(file('codex'), unavailable=True)
        body = dict(rate_limit=dict(allowed=True, **{slot: dict(used_percent=10,
            reset_at=NOW + delay, limit_window_seconds=seconds)
            for slot, delay, seconds in (('primary_window', 6000, 18000),
                ('secondary_window', 500000, 604800))}), additional_rate_limits=[dict(
            limit_name='Scoped model', rate_limit=dict(allowed=False, limit_reached=True,
                primary_window=dict(used_percent=99.9, reset_at=NOW + 180,
                    limit_window_seconds=18000)))])
        read = Mock(return_value=dict(status_code=200, body={'rate_limit': body['rate_limit']}))
        server.fetch_account(row, call=read, cache=self.cache)
        # First scoped report races a newer primary header observation.
        read.return_value['body'] = body
        self.now += 60
        def headers_during_read(*args):
            self.now += 1
            row['quota'] = dict(observed_at=usage.stamp(self.now), signals={
                'X-Codex-' + slot + '-' + field: str(value)
                for slot, delay, minutes in (('Primary', 6000, 300), ('Secondary', 500000, 10080))
                for field, value in (('Used-Percent', 10), ('Window-Minutes', minutes),
                    ('Reset-At', NOW + delay))})
            server.fetch_account(row, call=read, cache=self.cache)
            return read.return_value
        read.side_effect = headers_during_read
        server.fetch_account(row, call=read, cache=self.cache, manual=True)
        read.side_effect = None
        self.assertIsNone(self.cache.recover(row, {}, self.reset))
        self.reset.assert_not_called()
        body.pop('additional_rate_limits')
        self.now += 60
        server.fetch_account(row, call=read, cache=self.cache, manual=True)
        self.assertIsNone(self.cache.recover(row, {}, self.reset))
        row['quota'] = dict(observed_at=usage.stamp(self.now + 1), signals={
            **{'X-Codex-' + slot + '-' + field: str(value)
               for slot, delay, minutes in (('Primary', 6000, 300), ('Secondary', 500000, 10080))
               for field, value in (('Used-Percent', 10), ('Window-Minutes', minutes),
                   ('Reset-At', NOW + delay))}})
        self.now += 1
        view = server.fetch_account(row, call=read, cache=self.cache)
        self.assertEqual(len(view['windows']), 2)  # Scoped evidence is not a new UI window.
        self.assertIsNone(self.cache.recover(row, {}, self.reset))
        self.reset.assert_not_called()
        self.now = NOW + 180
        self.assertEqual(self.cache.recover(row, {}, self.reset)['outcome'], 'cleared')
        self.reset.assert_called_once()

    def test_rebinding_starts_a_new_episode_without_old_recovery_or_pending_event(self):
        self.cache.recover(self.row, self.profile, self.reset)
        key = ('claude', 'a')
        entry = self.cache.entries[key]
        old_episode = entry['episode']
        self.cache.due_now(self.row)
        entry.update(provider_due=7200, manual_due=60)
        alias = dict(file(), name='replacement.json', email='replacement@example.test')
        self.cache.fetch(alias, self.read)
        self.assertNotIn(key, self.cache.pending)
        self.assertIsNone(entry.get('episode'))
        self.assertIsNone(entry.get('last_recovery'))
        self.assertEqual((entry['provider_due'], entry['manual_due']), (7200, 60))
        self.now += 1
        alias['unavailable'] = True
        self.cache.fetch(alias, self.read)
        self.assertIsNot(entry['episode'], old_episode)
        self.assertEqual(entry['episode']['started_at'], self.now)
        self.assertIsNone(entry['episode']['recovery'])

    def test_superseded_reserved_episode_cannot_reset_after_new_episode(self):
        state = server.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        state.usage_cache = self.cache
        entered, release = threading.Event(), threading.Event()
        results = []
        call = Mock(side_effect=lambda path, body=None: {'files': [self.row]} if path == '/auth-files' else {'status': 'ok'})
        def reset(path, body, **reservation):
            entered.set()
            self.assertTrue(release.wait(3))
            return state.reset_cooldown(self.row, call, body['auth_index'], **reservation)
        worker = threading.Thread(target=lambda: results.append(self.cache.recover(self.row, self.profile, reset)))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.row['unavailable'] = False
            self.cache.fetch(self.row, self.read)
            self.now += 60
            self.row['unavailable'] = True
            self.cache.fetch(self.row, self.read)
            self.now += 1
            self.row['quota'] = traffic(self.now)
            self.cache.fetch(self.row, self.read)
            second = self.cache.recover(self.row, self.profile,
                lambda path, body, **reserved: state.reset_cooldown(self.row, call, body['auth_index'], **reserved))
            self.assertEqual(second['outcome'], 'cleared')
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['outcome'], 'superseded')
        self.assertEqual(sum(c.args[0] == '/reset-quota' for c in call.call_args_list), 1)
        self.assertEqual(self.cache.recovery_view(self.row)['outcome'], 'cleared')

    def test_rebinding_cannot_inherit_recovery_or_reading(self):
        self.cache.recover(self.row, self.profile, self.reset)
        alias = dict(self.row, name='replacement.json', email='replacement@example.test')
        self.assertIsNone(self.cache.recover(alias, self.profile, self.reset))
        self.assertIsNone(self.cache.recovery_view(alias))
        self.reset.assert_called_once()

    def test_server_rechecks_binding_disabled_and_live_cooldown_before_reset(self):
        state = server.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        state.usage_cache = self.cache
        episode = self.cache.entries[('claude', 'a')]['episode']
        reserved = dict(episode=episode, episode_started_at=episode['started_at'])
        for change in (dict(disabled=True), dict(name='replacement.json'),
                       dict(unavailable=False, status='active')):
            call = Mock(return_value={'files': [dict(self.row, **change)]})
            with patch.object(server.time, 'time', return_value=self.now):
                self.assertEqual(state.reset_cooldown(self.row, call, 'a', **reserved)['status'], 'account_changed')
            call.assert_called_once_with('/auth-files')
        call = Mock(return_value={'files': [self.row, dict(self.row, name='alias.json')]})
        with patch.object(server.time, 'time', return_value=self.now):
            self.assertEqual(state.reset_cooldown(self.row, call, 'a', **reserved)['status'], 'account_changed')
        call.assert_called_once_with('/auth-files')
        call = Mock(side_effect=[{'files': [self.row]}, {'status': 'ok'}])
        with patch.object(server.time, 'time', return_value=self.now):
            self.assertEqual(state.reset_cooldown(self.row, call, 'a', **reserved), {'status': 'ok'})
        self.assertEqual(call.call_args.args, ('/reset-quota', {'auth_index': 'a'}))


class BillingTests(unittest.TestCase):
    def post_billing(self, path, rows, value, headers=None):
        raw = json.dumps(value).encode()
        handler = object.__new__(server.Handler)
        handler.path = '/api/accounts/billing'
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
            'Content-Type': 'application/json', 'Content-Length': str(len(raw))} | (headers or {})
        handler.rfile, handler.connection = io.BytesIO(raw), Mock()
        handler.server = SimpleNamespace(state=SimpleNamespace(lifecycle_lock=threading.RLock()))
        handler.send = Mock()
        with patch.object(server, 'BILLING', path), patch.object(server, 'api', return_value={'files': rows}):
            handler.do_POST()
        status, body = handler.send.call_args.args
        return status, json.loads(body)

    def test_xai_partial_updates_clear_fields_and_keep_the_other(self):
        row = file('xai')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            identity = dict(key=row['name'], auth_index=row['auth_index'])
            for changes, expected in (
                    ({'renewal_date': '2026-11-01'}, {'renewal_date': '2026-11-01'}),
                    ({'plan': ' SuperGrok '}, {'renewal_date': '2026-11-01', 'plan': 'SuperGrok'}),
                    ({'renewal_date': None}, {'plan': 'SuperGrok'}),
                    ({'renewal_date': '2026-12-01'}, {'renewal_date': '2026-12-01', 'plan': 'SuperGrok'}),
                    ({'plan': None}, {'renewal_date': '2026-12-01'}),
                    ({'renewal_date': '2027-01-01', 'plan': 'Premium'},
                     {'renewal_date': '2027-01-01', 'plan': 'Premium'}),
                    ({'renewal_date': None, 'plan': None}, {}),
                    ({'plan': 'SuperGrok'}, {'plan': 'SuperGrok'})):
                with self.subTest(changes=changes):
                    self.assertEqual(self.post_billing(path, [row], identity | changes), (200, {'ok': True}))
                    records = json.loads(path.read_text())['accounts']
                    self.assertEqual(set(records), {'xai:a'})
                    view = subscriptions.billing_view(row, records)
                    self.assertEqual({k: v for k, v in view.items() if k in ('plan', 'renewal_date')}, expected)
                    if 'renewal_date' in expected:
                        self.assertTrue(view['confirmed'])
                        self.assertEqual(view['source'], 'user_confirmed')
                        self.assertIsNotNone(usage.epoch(view['verified_at']))
                    else:
                        self.assertEqual(view, expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(path.parent.glob('.billing-*')))

    def test_billing_endpoint_rejects_invalid_fields_and_non_subscription_rows(self):
        row = file('xai')
        identity = dict(key=row['name'], auth_index=row['auth_index'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            for value in ({}, [], None, identity, identity | {'unknown': 'value'},
                          identity | {'plan': 'Pro', 'unknown': None},
                          identity | {'key': None, 'plan': 'Pro'},
                          identity | {'auth_index': 1, 'plan': 'Pro'}):
                with self.subTest(value=value):
                    self.assertEqual(self.post_billing(path, [row], value)[0], 400)
            for plan in ('', '   ', 'x' * 41, '\nPro', 'Pro\t', 'Pro\x00', 'Pro\x7f', 1, True, [], {}):
                with self.subTest(plan=plan):
                    self.assertEqual(self.post_billing(path, [row], identity | {'plan': plan})[0], 400)
            for date in ('bad', '', '1999-12-31', '2101-01-01', True, 123, [], {}):
                with self.subTest(date=date):
                    self.assertEqual(self.post_billing(path, [row], identity | {'renewal_date': date})[0], 400)
            for rejected in (dict(row, kind='api_key'), dict(row, account_type='api-key'),
                             dict(row, label='xai-apikey'), file('xai-api-key')):
                value = dict(key=rejected['name'], auth_index=rejected['auth_index'], plan='Pro')
                with self.subTest(row=rejected):
                    self.assertEqual(self.post_billing(path, [rejected], value),
                                     (409, {'error': 'account_changed'}))
            for rows in ([], [dict(row, auth_index='replacement')], [row, row]):
                self.assertEqual(self.post_billing(path, rows, identity | {'plan': 'Pro'}),
                                 (409, {'error': 'account_changed'}))
            self.assertEqual(self.post_billing(path, [row], identity | {'plan': 'Pro'},
                                              {'Content-Type': 'text/plain'})[0], 400)
            self.assertEqual(self.post_billing(path, [row], identity | {'plan': 'Pro'},
                                              {'Content-Length': '2049'})[0], 400)
            self.assertFalse(path.exists())

    def test_old_claude_record_and_identity_checks_apply_to_both_fields(self):
        row = file()
        old = dict(renewal_date='2026-11-01', source='user_confirmed', verified_at=usage.stamp(NOW),
                   name=row['name'], email=row['email'])
        records = {'claude:a': old}
        self.assertEqual(subscriptions.billing_view(row, records), dict(renewal_date='2026-11-01',
            confirmed=True, source='user_confirmed', verified_at=usage.stamp(NOW)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            path.write_text(json.dumps({'accounts': records}))
            subscriptions.save_billing(path, row, plan='Personal')
            records = json.loads(path.read_text())['accounts']
            self.assertEqual(subscriptions.billing_view(row, records)['renewal_date'], '2026-11-01')
            self.assertEqual(subscriptions.billing_view(row, records)['plan'], 'Personal')
            for changes in ({'email': 'other@example.test'}, {'name': 'replacement.json'}, {'type': 'xai'}):
                self.assertEqual(subscriptions.billing_view(dict(row, **changes), records), {})
            replacement = dict(row, email='other@example.test')
            subscriptions.save_billing(path, replacement, plan='Replacement')
            records = json.loads(path.read_text())['accounts']
            self.assertEqual(subscriptions.billing_view(replacement, records), {'plan': 'Replacement'})
        for changes in ({'source': 'provider'}, {'verified_at': 'bad'}):
            self.assertEqual(subscriptions.billing_view(row, {'claude:a': old | changes | {'plan': 'Pro'}}), {})

    def test_provider_keys_are_independent_and_invalid_updates_are_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            for provider, plan in (('claude', 'Pro'), ('xai', 'SuperGrok')):
                subscriptions.save_billing(path, file(provider), renewal_date='2026-11-01', plan=plan)
            records = json.loads(path.read_text())['accounts']
            self.assertEqual(set(records), {'claude:a', 'xai:a'})
            self.assertEqual(subscriptions.billing_view(file(), records)['plan'], 'Pro')
            self.assertEqual(subscriptions.billing_view(file('xai'), records)['plan'], 'SuperGrok')
            before = path.read_bytes()
            identity = dict(key=file('xai')['name'], auth_index='a')
            for changes in ({'renewal_date': '2026-12-01', 'plan': ''},
                            {'renewal_date': 'bad', 'plan': 'New plan'}):
                self.assertEqual(self.post_billing(path, [file('xai')], identity | changes)[0], 400)
                self.assertEqual(path.read_bytes(), before)

    def test_provider_projection_plan_precedence_sources_and_editability(self):
        import providers
        for provider in ('claude', 'xai', 'kimi', 'custom-provider'):
            row = file(provider)
            records = {provider + ':a': dict(name=row['name'], email=row['email'], plan='Personal',
                renewal_date='2026-11-01', source='user_confirmed', verified_at=usage.stamp(NOW))}
            for fetched in (None, '', 'Pro'):
                with self.subTest(provider=provider, fetched=fetched):
                    runtime = dict(key=row['name'], auth_index='a', provider=provider, plan=fetched,
                                   subscription=dict(current=True, plan=fetched))
                    gateway = dict(inventory=[row], claude_accounts=[runtime], usage_accounts=[runtime])
                    public = providers.groups(gateway, [], billing=records)[0]['accounts'][0]
                    self.assertEqual(public['plan'], fetched or 'Personal')
                    self.assertEqual(public['billing'], dict(renewal_date='2026-11-01', confirmed=True,
                        source='user_confirmed', verified_at=usage.stamp(NOW), plan='Personal'))
                    self.assertEqual(public['billing_editable'], dict(renewal=True, plan=not bool(fetched)))
                    if fetched:
                        self.assertNotIn('plan_source', public)
                    else:
                        self.assertEqual(public['plan_source'], 'user')
            public = providers.groups(dict(inventory=[row]), [], billing=records)[0]['accounts'][0]
            self.assertEqual(public['plan'], 'Personal')
            public = providers.groups(dict(inventory=[row]), [], billing={})[0]['accounts'][0]
            self.assertEqual(public['billing'], {})
            self.assertEqual(public['billing_editable'], dict(renewal=True, plan=True))
            self.assertNotIn('plan_source', public)
        row = dict(file('xai'), plan='Provider plan')
        public = providers.groups(dict(inventory=[row], usage_accounts=[dict(provider='xai',
            key=row['name'], auth_index='a', plan=None)]), [], billing=records)[0]['accounts'][0]
        self.assertEqual(public['plan'], 'Provider plan')
        self.assertEqual(public['billing_editable'], dict(renewal=True, plan=False))
        public = providers.groups(dict(inventory=[file('codex')]), [], billing={})[0]['accounts'][0]
        self.assertEqual(public['billing_editable'], dict(renewal=True, plan=False))
        self.assertNotIn('plan_source', public)
        self.assertEqual(providers.groups(dict(inventory=[dict(file('xai'), kind='api_key')]), [], billing={}), [])

    def test_codex_date_only_endpoint_save_clear_and_write_guards(self):
        row = file('codex')
        identity = dict(key=row['name'], auth_index=row['auth_index'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            for changes in ({'plan': 'Pro'}, {'plan': None},
                            {'renewal_date': '2026-12-01', 'plan': 'Pro'}):
                self.assertEqual(self.post_billing(path, [row], identity | changes),
                                 (400, {'error': 'invalid_request'}))
                self.assertFalse(path.exists())
            value = identity | {'renewal_date': '2026-12-01'}
            for headers in ({'Origin': 'https://evil.example.test'}, {'X-Router-Read-Only': '1'},
                            {'X-Router-Request': '0'}):
                self.assertEqual(self.post_billing(path, [row], value, headers)[0], 403)
            self.assertEqual(self.post_billing(path, [dict(row, auth_index='other')], value)[0], 409)
            self.assertEqual(self.post_billing(path, [row], identity | {'renewal_date': 'bad'})[0], 400)
            self.assertFalse(path.exists())
            self.assertEqual(self.post_billing(path, [row], value), (200, {'ok': True}))
            records = json.loads(path.read_text())['accounts']
            self.assertEqual(set(records), {'codex:a'})
            self.assertTrue(subscriptions.billing_view(row, records)['confirmed'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.post_billing(path, [row], identity | {'renewal_date': None}),
                             (200, {'ok': True}))
            self.assertEqual(subscriptions.billing_view(row, json.loads(path.read_text())['accounts']), {})

    def test_atomic_private_persistence_survives_reload_but_not_rebinding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            path.write_text(json.dumps({'accounts': {'codex@example.test': {'keep': True}}}))
            workers = [threading.Thread(target=subscriptions.save_billing,
                args=(path, file(index=index), '2026-11-01')) for index in ('a', 'b')]
            for worker in workers: worker.start()
            for worker in workers: worker.join(3)
            records = json.loads(path.read_text())['accounts']
            self.assertEqual(len(records), 3)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(subscriptions.billing_view(file(), records)['confirmed'])
            self.assertEqual(subscriptions.billing_view(dict(file(), email='other@example.test'), records), {})
            self.assertEqual(subscriptions.billing_view(file(index='c'), records), {})
            before = path.read_bytes()
            for date in ('2026-02-30', '20261101', '2026-11-01T12:00:00Z', '2101-01-01'):
                with self.assertRaises(ValueError):
                    subscriptions.save_billing(path, file(), date)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(path.parent.glob('.billing-*')))

    def test_http_write_requires_origin_write_mode_and_current_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            def post(headers=None, **changes):
                value = dict(key=file()['name'], auth_index='a', renewal_date='2026-11-01') | changes
                raw = json.dumps(value).encode()
                handler = object.__new__(server.Handler)
                handler.path = '/api/accounts/billing'
                handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                    'Content-Type': 'application/json', 'Content-Length': str(len(raw))} | (headers or {})
                handler.rfile, handler.connection = io.BytesIO(raw), Mock()
                handler.server = SimpleNamespace(state=SimpleNamespace(lifecycle_lock=threading.RLock()))
                handler.send = Mock()
                handler.do_POST()
                return handler.send.call_args.args[0]
            with patch.object(server, 'BILLING', path), patch.object(server, 'api', return_value={'files': [file()]}):
                for headers in ({'Origin': 'https://evil.example.test'}, {'X-Router-Read-Only': '1'},
                                {'X-Router-Request': '0'}):
                    self.assertEqual(post(headers), 403)
                self.assertEqual(post(auth_index='wrong'), 409)
                self.assertEqual(post(renewal_date='bad'), 400)
                self.assertFalse(path.exists())
                self.assertEqual(post(), 200)


if __name__ == '__main__':
    unittest.main()
