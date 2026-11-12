"""Codex billing fixtures only: no gateway, provider calls, credentials or runtime files."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import providers
import server
import subscriptions
import usage_adapters as usage

NOW = usage.epoch('2026-11-09T12:00:00Z')


def response(**changes):
    entitlement = dict(has_active_subscription=True, renews_at='2026-12-01T22:27:13+00:00',
                       cancels_at=None, expires_at='2026-12-02T04:27:13+00:00', billing_period='monthly')
    entitlement.update(changes)
    account = dict(account=dict(plan_type='promax'), entitlement=entitlement,
                   subscription_id='PRIVATE-IGNORE', last_active_subscription=dict(will_renew=False))
    return dict(status_code=200, body=dict(account_ordering=['other'], accounts={
        'workspace-a': account, 'default': dict(entitlement=dict(entitlement, renews_at='2026-12-03T12:00:00Z'))}))


class CodexBillingTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'codex-billing.json'
        self.row = dict(type='codex', auth_index='a', name='codex-a.json', email='a@example.test',
                        id_token=dict(plan_type='promax', chatgpt_account_id='workspace-a',
                                      chatgpt_subscription_active_until='2026-11-17T12:00:00Z'))
        self.billing = self.new_cache()
        self.call = Mock(return_value=response())

    def new_cache(self):
        return subscriptions.CodexBilling(self.path, zone=dt.timezone.utc,
            clock=lambda: self.now - NOW, wall_clock=lambda: self.now)

    def refresh(self, plan='promax', account_id='workspace-a', row=None, billing=None):
        # Run the reserved background job inline; all scheduling still goes through refresh().
        with patch.object(threading.Thread, 'start', lambda worker: worker.run()):
            (billing or self.billing).refresh(row or self.row, account_id, plan, self.call)

    def snapshot(self, records=None):
        state = server.GatewayState(Mock(), Mock(), Mock(), codex_billing=self.billing)
        state.resets.view.return_value = {}
        self.addCleanup(state.signins.close)
        account = server.fetch_account(self.row, call=Mock(return_value=dict(status_code=200, body={})))
        account.pop('_fast_file', None)
        state.data.update(accounts=[account], inventory=providers.inventory_view([self.row]))
        with patch.object(server, 'billing_records', return_value=records or {}), \
                patch.object(server, 'preference_status', return_value=None), \
                patch.object(server.routing_policy, 'routing_linked', return_value=False):
            gateway = state.snapshot()
        return providers.groups(gateway, [], billing=records or {})[0]['accounts'][0]

    def test_live_wins_over_legacy_token_and_manual_date_and_payload_is_minimal(self):
        subscriptions.save_billing(self.path.with_name('manual.json'), self.row, '2026-11-20')
        records = json.loads(self.path.with_name('manual.json').read_text())['accounts']
        records[self.row['email']] = dict(renewal_date='2026-11-16', source='user_confirmed',
                                         verified_at='2026-10-17T12:00:00Z')
        self.refresh()
        public = self.snapshot(records)
        self.assertEqual(public['billing'], dict(renewal_date='2026-12-01', confirmed=True,
            source='chatgpt_billing', verified_at=usage.stamp(NOW)))
        self.assertEqual(public['billing_editable'], dict(renewal=False, plan=False))
        self.assertNotIn('PRIVATE-IGNORE', json.dumps(public['billing']))
        self.assertNotIn('PRIVATE-IGNORE', self.path.read_text())
        self.assertNotIn('workspace-a', json.dumps(public['billing']))
        path, body = self.call.call_args.args
        self.assertEqual(path, '/api-call')
        self.assertEqual(body, dict(auth_index='a', method='GET', url=subscriptions.CHATGPT_BILLING,
                                   header=subscriptions.codex_headers('workspace-a')))

    def test_entitlement_estimates_prefer_cancellation_and_default_selection(self):
        for changes, date, source in (
                ({'cancels_at': '2026-11-30T12:00:00Z'}, '2026-11-30', 'account_metadata'),
                ({'renews_at': None}, '2026-12-02', 'account_metadata'),
                ({'has_active_subscription': False}, '2026-12-02', 'account_metadata'),
                ({'renews_at': 'bad', 'cancels_at': 'bad'}, '2026-12-02', 'account_metadata'),
                ({'has_active_subscription': False, 'renews_at': None, 'expires_at': None}, None, None)):
            with self.subTest(changes=changes):
                self.call.return_value = response(**changes)
                self.refresh()
                view = self.billing.view(self.row)
                if date:
                    self.assertEqual(view['renewal_date'], date)
                    self.assertFalse(view['confirmed'])
                    self.assertEqual(view['source'], source)
                    self.assertFalse(self.snapshot()['billing_editable']['renewal'])
                else:
                    self.assertEqual(view, {})
                    self.assertTrue(self.snapshot()['billing_editable']['renewal'])
                self.now += 21600
        self.call.return_value = response()
        for account_id in (None, 'absent'):
            self.refresh(account_id=account_id)
            self.assertEqual(self.billing.view(self.row)['renewal_date'], '2026-12-03')
            headers = self.call.call_args.args[1]['header']
            self.assertEqual(headers.get('Chatgpt-Account-Id'), account_id)
            self.now += 21600

    def test_null_claim_no_live_date_manual_confirmation_then_clear_and_legacy(self):
        self.row['id_token']['chatgpt_subscription_active_until'] = None
        self.call.return_value = response(renews_at=None, expires_at=None, has_active_subscription=False)
        self.refresh()
        path = self.path.with_name('manual.json')
        subscriptions.save_billing(path, self.row, '2026-12-01')
        records = json.loads(path.read_text())['accounts']
        public = self.snapshot(records)
        self.assertEqual(public['billing']['renewal_date'], '2026-12-01')
        self.assertEqual(public['billing']['source'], 'user_confirmed')
        self.assertTrue(public['billing']['confirmed'])
        self.assertEqual(public['billing_editable'], dict(renewal=True, plan=False))
        # A hand-set date also takes precedence over a differing token estimate.
        self.row['id_token']['chatgpt_subscription_active_until'] = '2026-12-02T12:00:00Z'
        self.assertEqual(self.snapshot(records)['billing']['renewal_date'], '2026-12-01')
        subscriptions.save_billing(path, self.row, None)
        records = json.loads(path.read_text())['accounts']
        self.assertEqual(self.snapshot(records)['billing'], server.billing_status(
            self.row['email'], '2026-12-02T12:00:00Z', records))
        self.row['id_token']['chatgpt_subscription_active_until'] = None
        records[self.row['email']] = dict(renewal_date='2026-11-16', source='user_confirmed',
                                         verified_at='2026-10-17T12:00:00Z')
        self.assertEqual(self.snapshot(records)['billing']['renewal_date'], '2026-11-16')

    def test_persistence_restart_identity_binding_and_failed_lookup_keep_last_known(self):
        self.refresh()
        saved = json.loads(self.path.read_text())['accounts']['codex:a']
        self.assertEqual(set(saved), {'renewal_date', 'source', 'verified_at', 'name', 'email', 'plan_type'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.path.parent.glob('.billing-*')))
        restarted = self.new_cache()
        self.assertEqual(restarted.view(self.row), self.billing.view(self.row))
        self.refresh(billing=restarted)
        self.assertEqual(self.call.call_count, 1, 'restart respects the persisted successful lookup time')
        for changes in ({'email': 'other@example.test'}, {'name': 'replacement.json'}, {'auth_index': 'b'}):
            self.assertEqual(restarted.view(dict(self.row, **changes)), {})
        self.now += 21600
        self.call.return_value = dict(status_code=401)
        self.refresh(billing=restarted)
        self.assertEqual(restarted.view(self.row)['verified_at'], usage.stamp(NOW))
        self.assertEqual(json.loads(self.path.read_text())['accounts']['codex:a'], saved)
        self.assertEqual(self.new_cache().view(self.row), restarted.view(self.row))

    def test_success_without_date_removes_old_live_date_and_persists_lookup(self):
        self.refresh()
        self.now += 21600
        self.call.return_value = response(renews_at=None, expires_at=None)
        self.refresh()
        self.assertEqual(self.billing.view(self.row), {})
        self.assertEqual(self.new_cache().view(self.row), {})
        self.refresh(billing=self.new_cache())
        self.assertEqual(self.call.call_count, 2)

    def test_cadence_plan_change_and_six_hour_refresh(self):
        self.refresh()
        self.refresh()
        self.now += 1799
        self.refresh(plan='pro')
        self.assertEqual(self.call.call_count, 1)
        self.now += 1
        self.refresh(plan='pro')
        self.assertEqual(self.call.call_count, 2)
        self.now += 21599
        self.refresh(plan='pro')
        self.assertEqual(self.call.call_count, 2)
        self.now += 1
        self.refresh(plan='pro')
        self.assertEqual(self.call.call_count, 3)

    def test_passed_renewal_rechecks_at_thirty_minute_floor(self):
        self.call.return_value = response(renews_at='2026-11-09T11:00:00Z')
        self.now = usage.epoch('2026-11-09T23:50:00Z')
        self.refresh()
        self.now += 1200
        self.refresh()
        self.assertEqual(self.call.call_count, 1)
        self.now += 600
        self.refresh()
        self.assertEqual(self.call.call_count, 2)

    def test_failures_retry_after_thirty_minutes_and_429_after_one_hour(self):
        self.call.return_value = dict(status_code=500)
        self.refresh()
        self.now += 1799
        self.refresh(plan='pro')
        self.assertEqual(self.call.call_count, 1)
        self.now += 1
        self.call.return_value = dict(status_code=429)
        self.refresh(plan='pro')
        self.assertEqual(self.call.call_count, 2)
        self.now += 3599
        self.refresh(plan='free')
        self.assertEqual(self.call.call_count, 2)
        self.now += 1
        self.call.return_value = response()
        self.refresh(plan='free')
        self.assertEqual(self.call.call_count, 3)

    def test_longer_retry_after_from_upstream_and_management_is_respected(self):
        for outer in (False, True):
            with self.subTest(outer=outer):
                self.billing = subscriptions.CodexBilling(zone=dt.timezone.utc,
                    clock=lambda: self.now - NOW, wall_clock=lambda: self.now)
                self.call.reset_mock()
                self.call.side_effect = HTTPError('http://fixture.test', 429, 'limited',
                    {'Retry-After': '7200'}, None) if outer else None
                if outer:
                    self.addCleanup(self.call.side_effect.close)
                self.call.return_value = dict(status_code=429, header={'Retry-After': ['7200']})
                self.refresh()
                self.now += 7199
                self.refresh(plan='pro')
                self.assertEqual(self.call.call_count, 1)
                self.now += 1
                self.call.side_effect = None
                self.call.return_value = response()
                self.refresh(plan='pro')
                self.assertEqual(self.call.call_count, 2)

    def test_blocked_failed_billing_lookup_does_not_delay_or_poison_usage(self):
        entered, release = threading.Event(), threading.Event()
        workers = []
        real_thread = threading.Thread
        def start_worker(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker
        def slow_billing(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return dict(status_code=401)
        try:
            with patch.object(threading, 'Thread', side_effect=start_worker):
                self.billing.refresh(self.row, 'workspace-a', 'promax', slow_billing)
                self.assertTrue(entered.wait(2))
                self.billing.refresh(self.row, 'workspace-a', 'pro', slow_billing)
                self.assertEqual(len(workers), 1, 'one in-flight billing read per auth index')
                quota = Mock(return_value=dict(status_code=200, body=dict(plan_type='promax',
                    rate_limit=dict(primary_window=dict(used_percent=20, limit_window_seconds=18000,
                                                       reset_at=self.now + 3000)))))
                account = server.fetch_account(self.row, call=quota)
                self.assertEqual(account['auth_state'], 'authenticated')
                self.assertIsNone(account['quota_error'])
                self.assertEqual(account['windows'][0]['remaining'], 80)
                self.assertFalse(release.is_set(), 'quota finished while billing was still blocked')
        finally:
            release.set()
            for worker in workers:
                worker.join(3)
                self.assertFalse(worker.is_alive())
        self.assertEqual(account['auth_state'], 'authenticated')
        self.assertEqual(account['windows'][0]['remaining'], 80)
        self.assertEqual(self.billing.view(self.row), {})

    def test_invalid_response_retains_last_known_and_corrupt_storage_fails_closed(self):
        self.refresh()
        old = self.billing.view(self.row)
        for raw in ({}, {'accounts': []}, {'accounts': {'default': {}}}, 'bad json'):
            self.now += 21600
            self.call.return_value = dict(status_code=200, body=raw)
            self.refresh()
            self.assertEqual(self.billing.view(self.row), old)
        for raw in ('bad json', '[]', '{"accounts":[]}', '{"accounts":{"codex:a":[]}}'):
            self.path.write_text(raw)
            self.assertEqual(self.new_cache().view(self.row), {})

    def test_timestamp_local_calendar_date_near_midnight_and_dst(self):
        for stamp, zone, expected in (
                ('2026-12-02T01:00:00Z', 'America/Santiago', '2026-12-01'),
                ('2026-12-02T01:00:00Z', 'America/New_York', '2026-12-01'),
                ('2026-03-09T03:30:00Z', 'America/New_York', '2026-03-08'),
                ('2026-12-02T01:00:00Z', 'UTC', '2026-12-02')):
            with self.subTest(stamp=stamp, zone=zone):
                self.assertEqual(subscriptions.local_billing_date(stamp, ZoneInfo(zone)), expected)
        for value in (None, 'bad', '2026-12-02', '2026-12-02T01:00:00', 1800000000):
            self.assertIsNone(subscriptions.local_billing_date(value, dt.timezone.utc))


if __name__ == '__main__':
    unittest.main()
