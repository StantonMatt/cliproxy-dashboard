import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
from reset_credits import ResetManager, epoch


class ResetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.used, self.applicable, self.expiry = 95, 1, time.time() + 50
        self.posts = []
        self.gateway_resets = []
        self.fail_request = False
        self.f = {'type':'codex','auth_index':'a','email':'a@example.test'}
        self.manager = ResetManager(self.api, lambda _: {'chatgpt_account_id':'test-account'}, self.path)

    def api(self, path, body=None):
        if path == '/auth-files':
            return {'files':[self.f]}
        if path == '/reset-quota':
            self.gateway_resets.append(body)
            return {'status':'ok', 'auth_index':body['auth_index']}
        self.assertEqual(path, '/api-call')
        self.assertEqual(body['auth_index'], 'a')
        if body['url'].endswith('/usage'):
            result = {'rate_limit_reset_credits':{'applicable_available_count':self.applicable},
                      'rate_limit':{'primary_window':{'used_percent':self.used,'limit_window_seconds':604800,'reset_at':time.time()+600}}}
        elif body['url'].endswith('/consume'):
            self.posts.append(json.loads(body['data']))
            if self.fail_request:
                raise RuntimeError('SECRET MUST NOT ESCAPE')
            result = {'code':'reset','windows_reset':1}
        else:
            result = {'available_count':2,'credits':[{'id':'credit-one','status':'available','is_supported_by_plan':True,
                       'reset_type':'codex_rate_limits','expires_at':self.expiry},
                      {'id':'credit-two','status':'available','is_supported_by_plan':True,
                       'reset_type':'codex_rate_limits','expires_at':time.time()+9000}]}
        return {'status_code':200,'body':json.dumps(result)}

    def test_auto_attempts_at_97_percent_remaining_without_applicability_gate(self):
        self.manager.policy('a', True)
        self.used, self.applicable = 3, 0
        self.manager.fetch(self.f)
        self.manager.tick()
        self.assertEqual([p['credit_id'] for p in self.posts], ['credit-one'])

    def test_guard_paused_manual_and_expiry_redemption_still_work(self):
        self.f['disabled'] = True
        self.manager.is_paused = lambda record: True
        self.assertEqual(self.manager.consume('a', 'credit-one')['outcome'], 'reset')
        self.manager.state['attempts'].clear()
        self.manager.policy('a', True)
        self.manager.fetch(self.f)
        self.manager.tick()
        self.assertEqual(len(self.posts), 2)
        self.manager.is_paused = lambda record: False
        with self.assertRaises(ValueError):
            self.manager.consume('a', 'credit-one')

    def test_account_validation(self):
        with self.assertRaises(ValueError): self.manager.consume('wrong-account','credit-one')
        self.assertEqual(self.posts, [])

    def test_quota_unavailable_accounts_can_redeem_but_auth_failures_cannot(self):
        for message in ('', json.dumps({'error':{'type':'usage_limit_reached'}})):
            with self.subTest(message=message):
                self.f.update(unavailable=True, status_message=message)
                self.assertIn(self.manager.consume('a','credit-one')['outcome'], ('reset','already_redeemed'))
        for change in ({'disabled':True}, {'status_message':'refresh_token_expired'},
                       {'status_message':'invalid_grant'}, {'status_message':'authentication_error'}):
            with self.subTest(change=change):
                self.f.update(disabled=False, status_message='')
                self.f.update(change)
                with self.assertRaises(ValueError):self.manager.consume('a','credit-one')
        self.assertEqual(len(self.posts), 1)

    def test_success_clears_only_redeemed_account_and_records_followup_steps(self):
        calls = []
        self.manager.after_reset = lambda account_id: calls.append(account_id) or {
            'quota_refresh':'ok', 'routing_reconcile':'skipped'}
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                self.manager.state['attempts'].clear()
                self.manager.policy('a', True)
                if automatic:
                    self.manager.fetch(self.f)
                    self.manager.tick()
                else:
                    self.manager.consume('a','credit-one')
                self.assertEqual(self.manager.view(self.f['email'])['last_event']['steps'], {
                    'provider':'reset', 'gateway_reset':'ok', 'quota_refresh':'ok', 'routing_reconcile':'skipped'})
        self.assertEqual(self.gateway_resets, [{'auth_index':'a'}, {'auth_index':'a'}])
        self.assertEqual(calls, ['a','a'])

    def test_failed_cooldown_clear_does_not_lose_confirmed_redemption(self):
        original = self.manager.api
        def fail_reset(path, body=None):
            if path == '/reset-quota':raise RuntimeError('SECRET')
            return original(path,body)
        self.manager.api = fail_reset
        result = self.manager.consume('a','credit-one')
        self.assertEqual(result['outcome'], 'reset')
        self.assertEqual(result['steps']['gateway_reset'], 'failed')
        self.assertNotIn('SECRET', json.dumps(self.manager.view(self.f['email'])))
        self.manager.api = original
        self.assertEqual(self.manager.consume('a','credit-one')['steps']['gateway_reset'], 'ok')
        self.assertEqual(len(self.posts), 1)

    def test_retry_failed_quota_refresh_does_not_clear_gateway_twice(self):
        self.manager.after_reset = lambda _: {'quota_refresh':'failed','routing_reconcile':'held'}
        self.manager.consume('a','credit-one')
        self.manager.after_reset = lambda _: {'quota_refresh':'ok','routing_reconcile':'skipped'}
        result = self.manager.consume('a','credit-one')
        self.assertEqual(result['outcome'], 'already_redeemed')
        self.assertEqual(result['steps']['quota_refresh'], 'ok')
        self.assertEqual(self.gateway_resets, [{'auth_index':'a'}])
        with patch.object(self.manager,'after_reset') as callback:
            self.manager.consume('a','credit-one')
            callback.assert_not_called()

    def test_tick_recovers_consumed_credit_after_restart_with_backoff_without_redeeming_again(self):
        now = 1800000000
        self.expiry = now + 50
        resets, followups = [], []
        failing = True
        def api(path, body=None):
            if path == '/reset-quota':
                resets.append(body['auth_index'])
                if failing:raise RuntimeError('PRIVATE_ERROR')
            response = self.api(path, body)
            if path == '/api-call' and body['url'].endswith('/rate-limit-reset-credits') and self.posts:
                response = {'status_code':200, 'body':{'available_count':0, 'credits':[]}}
            return response
        def after_reset(account_id):
            followups.append(account_id)
            return {'quota_refresh':'ok', 'routing_reconcile':'ok'}
        self.manager.api, self.manager.after_reset = api, after_reset
        with patch('reset_credits.time.time', return_value=now):
            self.manager.consume('a','credit-one')
            self.assertTrue(self.manager.view(self.f['email'])['recovery_pending'])
            self.assertEqual(self.manager.view(self.f['email'])['credits'], [])
            self.assertEqual(followups, [])  # Gateway reset must succeed first.
        restored = ResetManager(api, lambda _: {}, self.path, after_reset)
        with patch('reset_credits.time.time', return_value=now + 9):restored.tick()
        self.assertEqual(resets, ['a'])
        with patch('reset_credits.time.time', return_value=now + 10):restored.tick()
        self.assertEqual(resets, ['a','a'])
        failing = False
        with patch('reset_credits.time.time', return_value=now + 29):restored.tick()
        self.assertEqual(resets, ['a','a'])
        with patch('reset_credits.time.time', return_value=now + 30):
            restored.tick()
            self.assertFalse(restored.view(self.f['email'])['recovery_pending'])
            self.assertEqual(restored.view(self.f['email'])['last_event']['steps'], {
                'provider':'reset', 'gateway_reset':'ok', 'quota_refresh':'ok', 'routing_reconcile':'ok'})
        with patch('reset_credits.time.time', return_value=now + 300):restored.tick()
        self.assertEqual(resets, ['a','a','a'])
        self.assertEqual(followups, ['a'])
        self.assertEqual(len(self.posts), 1)

    def test_recovery_stops_at_original_weekly_reset(self):
        now = 1800000000
        self.expiry = now + 50
        def api(path, body=None):
            if path == '/reset-quota':
                self.gateway_resets.append(body)
                raise RuntimeError('unavailable')
            return self.api(path, body)
        self.manager.api = api
        with patch('reset_credits.time.time', return_value=now):self.manager.consume('a','credit-one')
        with patch('reset_credits.time.time', return_value=now + 599):self.manager.tick()
        self.assertEqual(len(self.gateway_resets), 2)
        for later in (600, 1000, 86400):
            with patch('reset_credits.time.time', return_value=now + later):
                self.manager.tick()
                self.assertFalse(self.manager.view(self.f['email'])['recovery_pending'])
        self.assertEqual(len(self.gateway_resets), 2)
        self.assertEqual(len(self.posts), 1)

    def test_tick_retries_quota_and_routing_failures_without_resetting_gateway_again(self):
        for failed in ({'quota_refresh':'failed','routing_reconcile':'held'},
                       {'quota_refresh':'ok','routing_reconcile':'failed'},
                       {'quota_refresh':'ok','routing_reconcile':'held'}):
            with self.subTest(steps=failed):
                self.manager.state['attempts'].clear()
                self.posts.clear();self.gateway_resets.clear()
                now = 1800000000
                self.expiry = now + 50
                self.manager.after_reset = lambda _: failed
                with patch('reset_credits.time.time', return_value=now):
                    self.manager.consume('a','credit-one')
                    self.assertTrue(self.manager.view(self.f['email'])['recovery_pending'])
                self.manager.after_reset = lambda _: {'quota_refresh':'ok','routing_reconcile':'ok'}
                with patch('reset_credits.time.time', return_value=now + 10):
                    self.manager.tick()
                    self.assertFalse(self.manager.view(self.f['email'])['recovery_pending'])
                self.assertEqual(len(self.gateway_resets), 1)
                self.assertEqual(len(self.posts), 1)

    def test_recovery_backoff_is_capped(self):
        attempt = {}
        with patch('reset_credits.time.time', return_value=1800000000):
            for index in range(12):
                self.manager.schedule_recovery(attempt)
                self.assertEqual(attempt['recovery_retry_at'] - 1800000000, min(300, 10 * 2 ** index))

    def test_provider_auth_failure_prevents_redemption_for_unavailable_account(self):
        self.f['unavailable'] = True
        original = self.manager.api
        def rejected(path, body=None):
            if path == '/api-call':return {'status_code':401,'body':{'error':'private error'}}
            return original(path,body)
        self.manager.api = rejected
        with self.assertRaises(RuntimeError):self.manager.consume('a','credit-one')
        self.assertFalse(self.posts)
        self.assertFalse(self.gateway_resets)

    def test_naive_expiry_is_unknown_and_not_automated(self):
        self.assertIsNone(epoch('2030-01-01T12:00:00'))
        self.assertEqual(epoch('2030-01-01T12:00:00Z'), epoch('2030-01-01T09:00:00-03:00'))
        self.expiry = '2030-01-01T12:00:00'
        self.manager.policy('a', True)
        self.manager.fetch(self.f)
        self.manager.tick()
        self.assertIsNone(next(c for c in self.manager.view(self.f['email'])['credits']
                               if c['id'] == 'credit-one')['expires_at'])
        self.assertFalse(self.posts)

    def test_lost_response_missing_credit_and_no_credit_keep_second_credit_blocked(self):
        self.fail_request = True
        with self.assertRaises(RuntimeError):self.manager.consume('a','credit-one')
        restored = ResetManager(self.api, lambda _: {'chatgpt_account_id':'test-account'}, self.path)
        self.assertEqual(restored.view(self.f['email'])['pending_credit_id'], 'credit-one')
        self.assertIsNone(restored.view('another@example.test')['pending_credit_id'])
        original = restored.upstream
        reads = []
        def response(file, path, body=None):
            reads.append(path)
            if path.endswith('/consume'):
                self.posts.append(body)
                return {'code':'no_credit'}
            result = original(file,path,body)
            if path == 'rate-limit-reset-credits':
                result['credits'] = [c for c in result['credits'] if c['id'] != 'credit-one']
                result['available_count'] = 1
            return result
        with patch.object(restored,'upstream',side_effect=response):
            self.assertEqual(restored.consume('a','credit-two'), {'outcome':'uncertain', 'credit_id':'credit-one'})
            self.assertEqual(restored.view(self.f['email'])['credits'][0]['id'], 'credit-two')
            self.assertEqual(restored.view(self.f['email'])['pending_credit_id'], 'credit-one')
            self.assertEqual(len(self.posts), 1)
            self.assertEqual(restored.consume('a','credit-one'), {'outcome':'uncertain', 'credit_id':'credit-one'})
            self.assertEqual(restored.view(self.f['email'])['pending_credit_id'], 'credit-one')
            self.assertEqual(restored.consume('a','credit-two')['outcome'], 'uncertain')
        self.assertEqual([p['credit_id'] for p in self.posts], ['credit-one','credit-one'])
        self.assertEqual(self.posts[0],self.posts[1])
        self.assertEqual(reads.count('usage'), 3)
        self.assertEqual(reads.count('rate-limit-reset-credits'), 4)  # Includes post-attempt balance.
        self.assertEqual(next(iter(restored.state['attempts'].values()))['outcome'], 'uncertain')

    def test_pending_allowance_recheck_failure_blocks_another_credit(self):
        self.fail_request = True
        with self.assertRaises(RuntimeError):self.manager.consume('a','credit-one')
        original = self.manager.upstream
        def response(file,path,body=None):
            if path == 'usage':raise RuntimeError('private error')
            return original(file,path,body)
        with patch.object(self.manager,'upstream',side_effect=response):
            with self.assertRaisesRegex(RuntimeError,'allowance recheck unavailable'):
                self.manager.consume('a','credit-two')
        self.assertEqual([p['credit_id'] for p in self.posts], ['credit-one'])

    def test_pending_missing_credit_can_be_confirmed_by_same_identity(self):
        self.fail_request = True
        with self.assertRaises(RuntimeError):self.manager.consume('a','credit-one')
        original = self.manager.upstream
        def response(file,path,body=None):
            if path.endswith('/consume'):
                self.posts.append(body)
                return {'code':'already_redeemed'}
            result = original(file,path,body)
            if path == 'rate-limit-reset-credits':result['credits'] = result['credits'][1:]
            return result
        with patch.object(self.manager,'upstream',side_effect=response):
            result = self.manager.consume('a','credit-one')
        self.assertEqual(result['outcome'], 'already_redeemed')
        self.assertIsNone(self.manager.view(self.f['email'])['pending_credit_id'])
        self.assertEqual(self.posts[0],self.posts[1])
        self.assertEqual(self.gateway_resets, [{'auth_index':'a'}])

    def test_manual_request_delegates_eligibility_to_provider(self):
        self.used, self.applicable = 13, 0
        self.assertEqual(self.manager.consume('a','credit-one')['outcome'],'reset')
        self.assertEqual(len(self.posts),1)

    def test_later_credit_rejected_server_side_even_with_manual_confirmation(self):
        self.assertEqual(self.manager.consume('a','credit-two')['outcome'],'earliest_required')
        self.assertFalse(self.posts)
        # Fresh provider ordering overrides an earlier browser snapshot.
        self.expiry=time.time()+10000
        self.assertEqual(self.manager.consume('a','credit-one')['outcome'],'earliest_required')
        self.assertFalse(self.posts)

    def test_manual_and_auto_share_one_durable_attempt(self):
        self.manager.policy('a', True)
        results = []
        threads = [threading.Thread(target=lambda auto=a: results.append(self.manager.consume('a','credit-one',auto))) for a in (False, True)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(self.posts),1)
        restored = ResetManager(self.api, lambda _: {'chatgpt_account_id':'test-account'}, self.path)
        self.assertEqual(restored.consume('a','credit-one')['outcome'],'already_redeemed')
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.gateway_resets, [{'auth_index':'a'}])

    def test_uncertain_request_reuses_id_after_restart(self):
        self.fail_request = True
        with self.assertRaisesRegex(RuntimeError,'uncertain') as e:self.manager.consume('a','credit-one')
        self.assertNotIn('SECRET', str(e.exception))
        self.fail_request = False
        restored = ResetManager(self.api, lambda _: {'chatgpt_account_id':'test-account'}, self.path)
        restored.consume('a','credit-one')
        self.assertEqual(self.posts[0],self.posts[1])
        self.assertEqual(self.posts[0]['credit_id'],'credit-one')

    def test_auto_only_expiring_credit_and_only_when_enabled(self):
        self.manager.fetch(self.f)
        self.manager.tick()
        self.assertFalse(self.posts)
        self.manager.policy('a', True)
        self.manager.tick()
        self.assertEqual([p['credit_id'] for p in self.posts], ['credit-one'])
        self.manager.tick()
        self.assertEqual(len(self.posts),1)

    def test_expiry_boundaries_unknown_and_expired_skip(self):
        self.manager.policy('a', True)
        for expiry in (None, time.time()-5, time.time()+301):
            self.expiry = expiry
            self.manager.fetch(self.f)
            self.manager.tick()
        self.assertFalse(self.posts)

    def test_auto_starts_at_five_minutes_with_fresh_deadline_check(self):
        now = 1800000000
        with patch('reset_credits.time.time', return_value=now):
            self.expiry = now + 301
            self.manager.policy('a', True)
            self.manager.fetch(self.f)
            self.manager.tick()
            self.assertEqual(self.manager.consume('a', 'credit-one', True)['outcome'], 'not_due')
            self.assertFalse(self.posts)
        with patch('reset_credits.time.time', return_value=now + 1):
            self.manager.tick()
            self.assertEqual(len(self.posts), 1)

    def test_automatic_failure_retries_same_credit_until_expiry(self):
        now = 1800000000
        with patch('reset_credits.time.time', return_value=now):
            self.expiry = now + 300
            self.fail_request = True
            self.manager.policy('a', True)
            self.manager.fetch(self.f)
            self.manager.tick()
        with patch('reset_credits.time.time', return_value=now + 10):
            self.manager.tick()
            self.assertEqual(len(self.posts), 2)
            self.assertEqual(self.posts[0], self.posts[1])
        with patch('reset_credits.time.time', return_value=now + 300):
            self.manager.tick()
            self.assertEqual(len(self.posts), 2)

    def test_failed_automatic_retry_stops_when_disabled_and_after_success(self):
        self.fail_request = True
        self.manager.policy('a', True)
        self.manager.fetch(self.f)
        self.manager.tick()
        self.assertEqual(self.manager.view(self.f['email'])['expiry_alert']['status'], 'failed')
        self.manager.policy('a', False)
        self.manager.tick()
        self.assertEqual(len(self.posts), 1)
        self.fail_request = False
        self.manager.policy('a', True)
        self.manager.tick()
        self.manager.tick()
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.posts[0], self.posts[1])
        self.assertEqual(self.manager.view(self.f['email'])['expiry_alert']['status'], 'resolved')

    def test_provider_nothing_to_reset_is_reported_without_selecting_another_credit(self):
        original = self.manager.upstream
        def response(file, path, body=None):
            if path.endswith('/consume'):
                self.posts.append(body)
                return {'code':'nothing_to_reset'}
            return original(file,path,body)
        self.manager.policy('a',True)
        with patch.object(self.manager,'upstream',side_effect=response):
            self.assertEqual(self.manager.consume('a','credit-one',automatic=True)['outcome'],'nothing_to_reset')
        self.assertEqual([p['credit_id'] for p in self.posts],['credit-one'])
        self.assertIn('No reset applied',self.manager.view(self.f['email'])['last_event']['message'])
        self.assertFalse(self.gateway_resets)

    def test_stale_read_only_and_malformed_state_fail_closed(self):
        self.manager.fetch(self.f)
        with patch('reset_credits.time.time',return_value=time.time()+200):
            self.assertFalse(self.manager.view(self.f['email'])['eligible'])
        self.path.write_text('bad')
        with self.assertRaises(RuntimeError):ResetManager(self.api,lambda _: {},self.path)

    def test_failed_persistence_prevents_spending(self):
        with patch.object(self.manager,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.manager.consume('a','credit-one')
        self.assertFalse(self.posts)


if __name__ == '__main__':unittest.main()
