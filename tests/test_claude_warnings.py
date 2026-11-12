import copy
import io
import http.client
import os
import socket
import ssl
import subprocess
import threading
import time
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import claude_warnings as warnings
import operations
import server
import usage_adapters as usage

NOW = 1793389860


def fixture(now=NOW):
    rows = []
    for index, email in enumerate(('claude.primary.account@example.test', 'claude.secondary.account@example.test')):
        key, identity = f'claude-{index}.json', f'claude-{index}'
        observed = dict(provider='claude', key=key, auth_index=identity, email=email,
                        status='ok', checked_at=usage.stamp(now), windows=[
            dict(kind=kind, label=label, seconds=seconds, remaining=left, used=100-left,
                 resets_at=now+offset, measured_at=usage.stamp(now))
            for kind, label, seconds, left, offset in (
                ('five_hour', '5-hour limit', 18000, 63-index*9, 11340-index*4260),
                ('seven_day', 'Weekly · all models', 604800, 29+index*64, 273600-index*222720),
                ('weekly_model', 'Weekly · Fable', 604800, 87+index*10, 273600-index*222720))])
        observed['windows'][2]['model'] = 'Fable'
        rows.append(dict(key=key, auth_index=identity, email=email, status='active',
                         runtime_available=True, routing_enabled=True, routing_id=str(index+1)*64,
                         _gateway_usage=observed))
    return dict(online=True, checked_at=usage.stamp(now), accounts=[], claude_accounts=rows,
                pool=dict(ready=1))


def window(gateway, account=0, kind='five_hour'):
    return next(w for w in gateway['claude_accounts'][account]['_gateway_usage']['windows'] if w['kind'] == kind)


def remaining(gateway, value, account=0, kind='five_hour'):
    window(gateway, account, kind).update(remaining=value, used=100-value)


class ClaudeWarningsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / 'notices.json'
        self.store = operations.NoticeStore(self.path, self.directory)
        self.gateway = fixture()
        self.now = NOW

    def project(self):
        return self.store.claude_view(self.gateway, now=self.now)

    def update(self):
        # Exercise the same projection contract as GatewayState.snapshot -> NoticeStore.update.
        self.gateway['claude_warnings'] = self.project()
        self.store.update(self.gateway, self.now)
        return self.project()

    def items(self, kind=None):
        return [r for r in self.store.view()['items'] if not kind or r['kind'] == kind]

    def legacy_duplicates(self, dismissed=False):
        remaining(self.gateway, 8)
        first = self.update()['low'][0]
        key = warnings.LOW_PREFIX + first['id']
        duplicate = copy.deepcopy(self.store.rows[key])
        duplicate.update(id='f'*24, created_at=self.now+272, dismissed=dismissed,
                         banner_dismissed=dismissed)
        duplicate['warning'].update(id=duplicate['id'], resets_at=first['resets_at']-0.306306)
        self.store.rows[warnings.LOW_PREFIX + duplicate['id']] = duplicate
        self.store.save()
        self.store = operations.NoticeStore(self.path, self.directory)
        return first

    def test_legacy_duplicate_period_migrates_without_new_notification(self):
        first = self.legacy_duplicates(dismissed=True)
        window(self.gateway)['resets_at'] += 0.3
        state = self.update()
        self.assertEqual(len(state['low']), 1)
        self.assertEqual(state['low'][0]['id'], first['id'])
        self.assertTrue(state['low'][0]['dismissed'])
        notices = [r for k, r in self.store.rows.items() if k.startswith(warnings.LOW_PREFIX)]
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]['dismissed'])
        self.assertEqual(notices[0]['created_at'], self.now)
        self.assertEqual(self.items('claude_low'), [])
        self.store = operations.NoticeStore(self.path, self.directory)
        self.assertEqual(self.update()['low'][0]['id'], first['id'])
        self.assertEqual(self.items('claude_low'), [])

    def test_duplicate_rows_dismiss_through_endpoint_after_reading_jitter(self):
        first = self.legacy_duplicates()
        shown = self.project()['low']
        window(self.gateway)['resets_at'] += 0.3
        self.gateway['claude_warnings'] = self.project()
        self.assertEqual(self.post(dict(rows=[r['id'] for r in shown])), 200)
        self.store = operations.NoticeStore(self.path, self.directory)
        rows = self.project()['low']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], first['id'])
        self.assertTrue(rows[0]['dismissed'])

    def test_dismissal_rolls_back_non_oserror_and_endpoint_reports_failure(self):
        remaining(self.gateway, 8)
        row = self.update()['low'][0]
        before = copy.deepcopy(self.store.rows)
        for error in (ValueError('serialization failed'), RuntimeError('save failed')):
            with self.subTest(error=type(error)), patch.object(self.store, 'save', side_effect=error):
                self.assertEqual(self.post(dict(rows=[row['id']])), 503)
                self.assertEqual(self.store.rows, before)
        self.assertFalse(self.project()['low'][0]['dismissed'])

    def test_period_tolerance_keeps_dismissal_without_merging_other_models_or_periods(self):
        remaining(self.gateway, 8, 0, 'weekly_model')
        first = self.update()['low'][0]
        self.store.dismiss_claude([first['id']], self.gateway, self.now)
        model = window(self.gateway, 0, 'weekly_model')
        model.update(model='fable', resets_at=first['resets_at']+300)
        same = self.update()['low']
        self.assertEqual(len(same), 1)
        self.assertEqual(same[0]['id'], first['id'])
        self.assertTrue(same[0]['dismissed'])
        model['resets_at'] += 1
        self.assertEqual(len(self.update()['low']), 2)
        model.update(model='Other', label='Weekly · Other', resets_at=first['resets_at'])
        self.assertEqual(len(self.update()['low']), 3)

    def test_legacy_duplicate_alias_can_dismiss_after_migration(self):
        self.legacy_duplicates()
        self.update()
        self.gateway['claude_warnings'] = self.project()
        self.assertEqual(self.post(dict(rows=['f'*24])), 200)
        self.assertTrue(self.project()['low'][0]['dismissed'])

    def test_all_out_episode_and_notice_keys_survive_reset_jitter_and_restart(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        self.update()
        episode = self.store.rows[warnings.OUT_KEY]['id']
        keys = set(self.store.rows)
        for delta in (0.3, -0.6, 299):
            for account in (0, 1):
                window(self.gateway, account)['resets_at'] += delta
            self.store = operations.NoticeStore(self.path, self.directory)
            self.update()
            self.assertEqual(self.store.rows[warnings.OUT_KEY]['id'], episode)
            self.assertEqual(set(self.store.rows), keys)
            self.assertEqual(len(self.items('claude_all_out')), 1)
            self.assertEqual(self.items('claude_ready'), [])

    def test_device_proxy_dismisses_legacy_duplicates_and_blocks_other_posts(self):
        self.legacy_duplicates()
        self.gateway['claude_warnings'] = self.project()
        upstream = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        upstream.state = SimpleNamespace(notices=self.store, snapshot=lambda: self.gateway)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        for base in range(19600, 20600, 10):
            sockets = []
            try:
                for port in range(base, base+10):
                    sock = socket.socket()
                    sockets.append(sock)
                    sock.bind(('127.0.0.1', port))
                break
            except OSError:
                pass
            finally:
                for sock in sockets:
                    sock.close()
        else:
            self.fail('No isolated proxy port range available')
        config = self.directory / 'previews.json'
        config.write_text(json.dumps(dict(bind='127.0.0.1', hostname='localhost', dns='localhost',
                                         base_port=base, devices=[], previews=[])))
        cert, key = self.directory / 'cert.pem', self.directory / 'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=localhost','-keyout',str(key),'-out',str(cert)],
                       check=True,capture_output=True,timeout=10)
        proxy = subprocess.Popen(['node', str(Path(__file__).resolve().parents[1] / 'scripts/router-preview.mjs')],
            env={**os.environ, 'ROUTER_PREVIEW_CONFIG': str(config), 'ROUTER_PREVIEW_CERT':str(cert), 'ROUTER_PREVIEW_KEY':str(key),
                 'ROUTER_PREVIEW_DASHBOARD_PORT': str(upstream.server_port)},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(('127.0.0.1', base), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            def request(path='/api/claude-warnings/dismiss', method='POST', payload=None, headers=None):
                con = http.client.HTTPSConnection('127.0.0.1', base+9, timeout=3, context=ssl._create_unverified_context())
                try:
                    con.request(method, path, json.dumps(payload or dict(rows=['f'*24])), headers={
                        'Content-Type': 'application/json', 'X-Router-Request': '1',
                        'Origin': f'https://127.0.0.1:{base+9}', **(headers or {})})
                    response = con.getresponse()
                    return response.status, response.read()
                finally:
                    con.close()
            before = self.path.read_bytes()
            for path in ('/api/notifications/dismiss', '/api/not-allowlisted',
                         '/api/claude-warnings/dismiss/extra', '/api/claude-warnings/dismiss/'):
                self.assertEqual(request(path)[0], 403)
            for method in ('GET', 'PATCH', 'DELETE'):
                self.assertEqual(request(method=method)[0], 403)
            self.assertEqual(request(headers={'Origin': 'https://evil.example.test'})[0], 403)
            self.assertEqual(request(headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
            self.assertEqual(request(payload=dict(rows=['f'*24], extra=True))[0], 400)
            self.assertEqual(request(payload=dict(rows=['invalid']))[0], 400)
            self.assertEqual(self.path.read_bytes(), before)
            # Approved device transport strips forged read-only metadata; this is harmless UI state.
            self.assertEqual(request(headers={'X-Router-Read-Only': '1'})[0], 200)
            self.store = operations.NoticeStore(self.path, self.directory)
            self.assertEqual(len(self.project()['low']), 1)
            self.assertTrue(self.project()['low'][0]['dismissed'])
        finally:
            proxy.terminate()
            proxy.wait(timeout=5)
            proxy.stderr.close()

    def test_legacy_delivered_ids_are_not_rearmed_before_or_after_migration(self):
        first = self.legacy_duplicates()
        delivered = {r['id'] for r in self.store.rows.values()}
        self.assertEqual([r['id'] for r in self.items('claude_low')], [first['id']])
        before = self.path.read_bytes()
        self.project()
        self.assertEqual(self.path.read_bytes(), before)
        window(self.gateway)['resets_at'] += .3
        self.update()
        self.assertEqual(len(self.items('claude_low')), 1)
        self.assertFalse({r['id'] for r in self.items('claude_low')} - delivered)
        self.assertEqual(len([k for k in self.store.rows if k.startswith(warnings.LOW_PREFIX)]), 1)

    def test_malformed_legacy_aliases_fail_closed(self):
        self.legacy_duplicates()
        data = json.loads(self.path.read_text())
        notice = next(iter(data['rows'].values()))
        for aliases in ('bad', [None], ['not-an-id']):
            notice['aliases'] = aliases
            self.path.write_text(json.dumps(data))
            store = operations.NoticeStore(self.path, self.directory)
            self.assertTrue(store.error)
            self.assertFalse(store.view()['available'])

    def test_no_warning_at_ten_percent_and_healthy_baseline(self):
        self.assertEqual(self.project()['low'], [])
        remaining(self.gateway, 10)
        self.assertEqual(self.project()['low'], [])
        self.assertIsNone(self.project()['out'])

    def test_each_account_and_limit_is_a_separate_row_in_card_order(self):
        for account in (0, 1):
            for kind in ('five_hour', 'seven_day', 'weekly_model'):
                remaining(self.gateway, 8, account, kind)
        rows = self.update()['low']
        self.assertEqual(len(rows), 6)
        self.assertEqual([r['kind'] for r in rows], ['five_hour', 'seven_day', 'weekly_model'] * 2)
        self.assertEqual(len({r['id'] for r in rows}), 6)
        self.assertEqual(len(self.items('claude_low')), 6)
        for notice in self.items('claude_low'):
            self.assertEqual(notice['urgency'], 'normal')
            self.assertRegex(notice['message'], r'\[[^\]]+\]$')

    def test_one_out_keeps_amber_and_model_exhaustion_never_starts_all_out(self):
        remaining(self.gateway, 0)
        state = self.update()
        self.assertIsNone(state['out'])
        self.assertEqual(state['low'][0]['remaining'], 0)
        self.assertIn('limit reached · back in', self.items('claude_low')[0]['message'])
        remaining(self.gateway, 0, 1, 'weekly_model')
        self.assertIsNone(self.update()['out'])

    def test_low_clears_at_reset_off_or_removal(self):
        for change in ('reset', 'off', 'remove'):
            with self.subTest(change=change):
                self.gateway = fixture()
                self.store = operations.NoticeStore(self.directory / (change + '.json'), self.directory)
                self.now = NOW
                remaining(self.gateway, 8)
                self.update()
                if change == 'reset':
                    self.now = window(self.gateway)['resets_at']
                elif change == 'off':
                    self.gateway['claude_accounts'][0]['routing_enabled'] = False
                else:
                    self.gateway['claude_accounts'].pop(0)
                self.assertEqual(self.update()['low'], [])
                self.assertEqual(self.items('claude_low'), [])

    def test_stale_failed_invalid_or_newer_positive_readings_never_clear_low(self):
        remaining(self.gateway, 8)
        row = self.update()['low'][0]
        for change in ('positive', 'invalid', 'failed', 'missing', 'offline'):
            with self.subTest(change=change):
                if change == 'positive':
                    remaining(self.gateway, 80)
                elif change == 'invalid':
                    window(self.gateway)['invalid_reset'] = True
                elif change == 'failed':
                    self.gateway['claude_accounts'][0]['_gateway_usage'].update(status='error', error='http_429')
                elif change == 'missing':
                    self.gateway['claude_accounts'][0]['_gateway_usage']['windows'] = []
                else:
                    self.gateway.update(online=False, claude_accounts=[])
                self.assertEqual(self.update()['low'][0]['id'], row['id'])
        self.now = row['resets_at']
        self.assertEqual(self.project()['low'], [])

    def test_invalid_future_missing_measurement_and_reset_do_not_create_low(self):
        for change in ({'invalid_reset': True}, {'resets_at': NOW-1}, {'resets_at': None},
                       {'measured_at': usage.stamp(NOW+1)}, {'measured_at': None},
                       {'resets_at': NOW+9*86400}, {'remaining': True}, {'remaining': float('nan')}):
            with self.subTest(change=change):
                gateway = fixture()
                remaining(gateway, 8)
                window(gateway).update(change)
                self.assertEqual(self.store.claude_view(gateway, now=NOW)['low'], [])

    def test_last_known_reading_with_future_reset_counts_even_after_poll_failure(self):
        remaining(self.gateway, 8)
        self.gateway['claude_accounts'][0]['_gateway_usage'].update(status='error', error='http_429')
        self.assertEqual(len(self.update()['low']), 1)

    def test_all_out_earliest_return_uses_later_limit_per_account(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 0, 'seven_day')
        remaining(self.gateway, 0, 1)
        out = self.update()['out']
        self.assertEqual(out['resets_at'], window(self.gateway, 1)['resets_at'])
        self.assertEqual(out['rows'][0]['email'], self.gateway['claude_accounts'][1]['email'])
        self.assertEqual(out['rows'][1]['resets_at'], window(self.gateway, 0, 'seven_day')['resets_at'])
        self.assertEqual(out['rows'][1]['limit'], 'Weekly · all models')
        self.assertTrue(out['codex_ready'])

    def test_secondary_low_note_only_when_it_survives_return(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        remaining(self.gateway, 2, 1, 'seven_day')
        out = self.update()['out']
        self.assertEqual(out['rows'][0]['note'], 'Weekly 2% left')
        window(self.gateway, 1, 'seven_day')['resets_at'] = NOW+60
        self.assertIsNone(self.project()['out']['rows'][0]['note'])

    def test_signed_out_unavailable_and_routing_off_rows(self):
        self.gateway['claude_accounts'][0]['auth_state'] = 'sign_in_required'
        self.gateway['claude_accounts'][1]['runtime_available'] = False
        out = self.update()['out']
        self.assertEqual({r['limit'] for r in out['rows']}, {'Signed out', 'Unavailable'})
        self.assertIsNone(out['resets_at'])
        self.gateway['claude_accounts'][1]['routing_enabled'] = False
        out = self.update()['out']
        self.assertEqual(out['rows'][-1]['limit'], 'Routing off')
        self.assertEqual(out['rows'][-1]['remaining'], 93)
        self.gateway['claude_accounts'][0]['routing_enabled'] = False
        self.assertIsNone(self.update()['out'])
        self.assertEqual(self.items('claude_ready'), [])

    def test_usage_cache_401_starts_all_out_until_successful_observation(self):
        account = self.gateway['claude_accounts'][0]
        file = dict(type='claude', name=account['key'], auth_index=account['auth_index'],
                    email=account['email'])
        cache = usage.UsageCache(clock=lambda: 0, wall_clock=lambda: self.now, jitter=lambda: 0)
        call = Mock(return_value=dict(status_code=401))
        account['_gateway_usage'] = cache.fetch(file, call)
        remaining(self.gateway, 0, 1)
        state = self.update()
        self.assertIsNotNone(state['out'])
        self.assertEqual(next(r for r in state['out']['rows'] if r['email'] == account['email'])['limit'],
                         'Signed out')
        self.assertEqual(len(self.items('claude_all_out')), 1)
        self.assertEqual(self.items('claude_ready'), [])
        self.now += 1
        call.return_value = dict(status_code=200, body={
            'five_hour': dict(utilization=20, resets_at=usage.stamp(self.now+18000)),
            'seven_day': dict(utilization=30, resets_at=usage.stamp(self.now+604800))})
        account['_gateway_usage'] = cache.fetch(file, call, force=True)
        self.assertEqual(account['_gateway_usage']['status'], 'ok')
        self.assertIsNone(self.update()['out'])
        self.assertEqual(len(self.items('claude_ready')), 1)
        self.update()
        self.assertEqual(len(self.items('claude_ready')), 1)
        # A later transient failure must not resurrect the old sign-in marker.
        self.now += 1
        call.return_value = dict(status_code=503)
        account['_gateway_usage'] = cache.fetch(file, call, force=True)
        self.assertNotEqual(account['_gateway_usage'].get('auth_state'), 'sign_in_required')
        self.assertIsNone(self.update()['out'])

    def test_unknown_routing_preserves_all_out_episode_until_confirmed_recovery(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        self.update()
        episode = self.items('claude_all_out')[0]['id']
        for routing in ((None, None), (False, None)):
            for account, enabled in zip(self.gateway['claude_accounts'], routing):
                account['routing_enabled'] = enabled
            self.assertIsNotNone(self.update()['out'])
            self.assertEqual(self.items('claude_all_out')[0]['id'], episode)
            self.assertEqual(self.items('claude_ready'), [])
        self.gateway['claude_accounts'][1]['routing_enabled'] = True
        remaining(self.gateway, 20, 1)
        self.assertIsNone(self.update()['out'])
        self.assertEqual(len(self.items('claude_ready')), 1)

    def test_signed_out_watch_status_precedes_old_exhausted_reading(self):
        for index in (0, 1):
            remaining(self.gateway, 0, index)
            self.gateway['claude_accounts'][index]['_gateway_usage']['status'] = 'missing_token'
        out = self.update()['out']
        self.assertTrue(all(r['limit'] == 'Signed out' for r in out['rows']))
        self.assertIsNone(out['resets_at'])

    def test_codex_no_ready_account_changes_action(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        self.gateway['pool']['ready'] = 0
        self.assertFalse(self.update()['out']['codex_ready'])
        self.assertIn('Codex has no ready account either', self.items('claude_all_out')[0]['message'])

    def test_stale_and_offline_cannot_start_episode(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        for changes in ({'online': False}, {'checked_at': usage.stamp(NOW-181)}):
            self.assertIsNone(self.store.claude_view(self.gateway | changes, now=NOW)['out'])

    def test_all_out_survives_stale_offline_and_elapsed_resets_until_fresh_recovery(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        state = self.update()
        first = self.items('claude_all_out')[0]['id']
        self.gateway['online'] = False
        self.gateway['pool']['ready'] = None
        remaining(self.gateway, 90)
        self.assertTrue(self.update()['out']['codex_ready'])
        self.now = NOW+300000
        self.assertIsNotNone(self.update()['out'])
        self.assertEqual(self.items('claude_all_out')[0]['id'], first)
        self.assertEqual(warnings.duration(state['out']['resets_at'], self.now), 'Reset due · checking')
        self.gateway = fixture(self.now)
        self.assertIsNone(self.update()['out'])
        self.assertEqual(len(self.items('claude_ready')), 1)

    def test_stale_positive_usage_cannot_end_episode(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        self.update()
        remaining(self.gateway, 90)
        self.gateway['claude_accounts'][0]['_gateway_usage'].update(status='error', error='http_429')
        self.assertIsNotNone(self.update()['out'])
        self.assertEqual(self.items('claude_ready'), [])

    def test_dismissal_persists_across_restart_and_zero_without_suppressing_notice(self):
        remaining(self.gateway, 8)
        row = self.update()['low'][0]
        self.store.dismiss_claude([row['id']], self.gateway, self.now)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.store = operations.NoticeStore(self.path, self.directory)
        self.assertTrue(self.project()['low'][0]['dismissed'])
        notice = self.items('claude_low')[0]
        remaining(self.gateway, 0)
        self.assertTrue(self.update()['low'][0]['dismissed'])
        self.assertEqual(self.items('claude_low')[0]['id'], notice['id'])
        self.assertEqual(len(self.items('claude_low')), 1)

    def test_same_period_identity_survives_numeric_encoding_and_model_case_changes(self):
        remaining(self.gateway, 4, 0, 'weekly_model')
        row = self.update()['low'][0]
        self.store.dismiss_claude([row['id']], self.gateway, self.now)
        model = window(self.gateway, 0, 'weekly_model')
        model.update(resets_at=float(model['resets_at']), model='fable')
        state = self.update()
        self.assertEqual(len(state['low']), 1)
        self.assertTrue(state['low'][0]['dismissed'])
        self.assertEqual(state['low'][0]['id'], row['id'])
        self.assertEqual(len(self.items('claude_low')), 1)

    def test_dismissal_returns_only_new_account_limit_and_period_rows(self):
        remaining(self.gateway, 8)
        row = self.update()['low'][0]
        self.store.dismiss_claude([row['id']], self.gateway, self.now)
        remaining(self.gateway, 6, 1, 'seven_day')
        remaining(self.gateway, 4, 0, 'weekly_model')
        state = self.update()
        self.assertEqual(len([r for r in state['low'] if not r['dismissed']]), 2)
        old = window(self.gateway)
        old['resets_at'] += 18000
        state = self.update()
        self.assertEqual(len([r for r in state['low'] if not r['dismissed']]), 3)
        self.assertEqual(len(self.items('claude_low')), 4)

    def test_notice_once_per_period_including_routing_toggle_and_restarts(self):
        remaining(self.gateway, 8)
        self.update()
        identity = self.items('claude_low')[0]['id']
        for value in (6, 2, 0):
            remaining(self.gateway, value)
            self.update()
            self.assertEqual([r['id'] for r in self.items('claude_low')], [identity])
        self.gateway['claude_accounts'][0]['routing_enabled'] = False
        self.update()
        self.store = operations.NoticeStore(self.path, self.directory)
        self.gateway['claude_accounts'][0]['routing_enabled'] = True
        self.update()
        self.assertEqual([r['id'] for r in self.items('claude_low')], [identity])

    def test_all_out_suppresses_coincident_low_and_recovery_fires_once_per_episode(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        self.update()
        self.assertEqual(self.items('claude_low'), [])
        self.assertEqual(len(self.items('claude_all_out')), 1)
        first = self.items('claude_all_out')[0]['id']
        self.assertEqual(self.items('claude_all_out')[0]['urgency'], 'critical')
        self.store = operations.NoticeStore(self.path, self.directory)
        self.now += 1
        self.update()
        self.assertEqual(self.items('claude_all_out')[0]['id'], first)
        remaining(self.gateway, 20)
        self.update()
        self.assertEqual(len(self.items('claude_ready')), 1)
        self.assertEqual(self.items('claude_all_out'), [])
        self.update()
        self.assertEqual(len(self.items('claude_ready')), 1)
        remaining(self.gateway, 0)
        self.now += 1
        self.update()
        self.assertNotEqual(self.items('claude_all_out')[0]['id'], first)
        remaining(self.gateway, 20)
        self.update()
        self.assertEqual(len([r for r in self.store.rows.values() if r['kind'] == 'claude_ready']), 2)

    def test_dismissed_rows_return_after_red_only_if_new(self):
        remaining(self.gateway, 8)
        state = self.update()
        self.store.dismiss_claude([state['low'][0]['id']], self.gateway, self.now)
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        self.update()
        remaining(self.gateway, 30, 1)
        state = self.update()
        self.assertIsNone(state['out'])
        self.assertTrue(next(r for r in state['low'] if r['account_id'] == warnings.account_id(self.gateway['claude_accounts'][0]))['dismissed'])

    def test_write_failure_does_not_apply_dismissal(self):
        remaining(self.gateway, 8)
        row = self.update()['low'][0]
        before = self.path.read_bytes()
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.dismiss_claude([row['id']], self.gateway, self.now)
        self.assertFalse(self.project()['low'][0]['dismissed'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_save_is_private_and_leaves_no_temp_file(self):
        remaining(self.gateway, 8)
        self.update()
        self.path.chmod(0o644)
        remaining(self.gateway, 8, 1)
        self.update()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.directory.glob('.notices-*')))

    def test_corrupt_structured_history_fails_closed_without_rewriting_it(self):
        remaining(self.gateway, 8)
        self.update()
        data = json.loads(self.path.read_text())
        key = next(k for k in data['rows'] if k.startswith(warnings.LOW_PREFIX))
        del data['rows'][key]['warning']['resets_at']
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        store = operations.NoticeStore(self.path, self.directory)
        self.assertTrue(store.error)
        self.assertFalse(store.view()['available'])
        store.update(self.gateway, self.now)
        self.assertEqual(self.path.read_bytes(), before)

    def test_projection_is_read_only_and_cannot_mutate_history(self):
        remaining(self.gateway, 8)
        self.assertFalse(self.path.exists())
        self.project()
        self.assertFalse(self.path.exists())
        self.update()
        before = self.path.read_bytes()
        state = self.project()
        state['low'][0]['email'] = 'changed'
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotEqual(self.project()['low'][0]['email'], 'changed')

    def post(self, payload=None, headers=None, raw=None):
        raw = raw if raw is not None else json.dumps(payload).encode()
        handler = object.__new__(server.Handler)
        handler.path = '/api/claude-warnings/dismiss'
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
            'Content-Type': 'application/json', 'Content-Length': str(len(raw))} | (headers or {})
        handler.rfile, handler.connection = io.BytesIO(raw), Mock()
        handler.server = SimpleNamespace(state=SimpleNamespace(notices=self.store, snapshot=lambda: self.gateway))
        handler.send = Mock()
        handler.do_POST()
        return handler.send.call_args.args[0]

    def test_endpoint_protections_and_strict_validation(self):
        remaining(self.gateway, 8)
        row = self.update()['low'][0]
        payload = dict(rows=[row['id']])
        before = self.path.read_bytes()
        for header, code in (({'Origin': 'https://evil.example.test'}, 403),
                ({'Host': 'evil.example.test'}, 421), ({'X-Router-Read-Only': '1'}, 403),
                ({'X-Router-Request': '0'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
                ({'Content-Type': 'text/plain'}, 400), ({'Content-Length': '8193'}, 400),
                ({'Content-Length': 'bad'}, 400)):
            self.assertEqual(self.post(payload, header), code)
        for value in ([], None, {}, {'rows': []}, {'rows': [row['id']], 'extra': 1},
                      {'rows': [row['id'], row['id']]}, {'rows': ['bad']}, {'rows': [True]},
                      {'rows': 'bad'}, {'rows': ['f'*24]}, {'rows': ['a'*24]*101}):
            self.assertEqual(self.post(value), 400)
        self.assertEqual(self.post(raw=b'{'), 400)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.post(payload), 200)
        self.assertTrue(self.project()['low'][0]['dismissed'])

    def test_endpoint_cannot_dismiss_red_and_reports_persistence_failure(self):
        remaining(self.gateway, 0)
        remaining(self.gateway, 0, 1)
        state = self.update()
        self.assertEqual(self.post(dict(rows=[state['low'][0]['id']])), 400)
        remaining(self.gateway, 30, 1)
        state = self.update()
        with patch.object(self.store, 'save', side_effect=OSError()):
            self.assertEqual(self.post(dict(rows=[state['low'][0]['id']])), 503)
        self.store.error = True
        self.assertEqual(self.post(dict(rows=[state['low'][0]['id']])), 503)


if __name__ == '__main__':
    unittest.main()
