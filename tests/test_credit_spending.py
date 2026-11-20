"""Credit guard decisions, crash recovery, consumers, scheduling, and HTTP contract. No live I/O."""
import copy
import http.client
import itertools
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import account_access as access
import availability
import fast_mode
import operations
import providers
import reset_credits
import routing_policy as policy
import server
import t3_usage
import usage_adapters as usage
from request_deadline import RequestDeadline

NOW = 1800000000


def record(suffix='a', disabled=False):
    return dict(type='codex', name=suffix+'.json', auth_index=suffix, email=suffix+'@example.test',
                disabled=disabled, status='active', priority=0,
                id_token={'plan_type':'pro', 'chatgpt_account_id':'workspace-'+suffix})


MARKER = dict(since=NOW-10, windows=['weekly'], resets_at=NOW+600,
              access_identity=access.identity(record()))

def observed(row, spent=(), stale=False, now=NOW):
    a = dict(id=row['auth_index'], key=row['name'], email=row['email'], active=not row['disabled'],
             disabled=row['disabled'], auth_state='authenticated', quota_error='unavailable' if stale else None,
             quota_checked_at=usage.stamp(now), quota_attempted_at=usage.stamp(now),
             windows=[dict(slot=slot, seconds=seconds, label=label, used=100 if name in spent else 20,
                           remaining=0 if name in spent else 80, resets_at=now+delay)
                      for slot, seconds, label, name, delay in (
                          ('primary_window',604800,'Weekly','weekly',600),
                          ('secondary_window',18000,'5-hour','five_hour',1200))])
    access.AccountAccess().project(row, a)
    a['availability'] = availability.classify(row, a, now)
    return a


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = access.CreditSpending(self.root/'controls.json')
        self.row = record()
        self.calls = []
        self.patcher = patch.object(policy, 'STATE', self.root/'routing')
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def call(self, path, body=None, method=None, **kw):
        self.calls.append((path, body, method))
        if path == '/auth-files':
            return {'files':[copy.deepcopy(self.row)]}
        if path == '/auth-files/status':
            self.assertEqual(method, 'PATCH')
            self.assertTrue(self.guard.marked(self.row))  # Both PATCH directions retain the marker.
            self.assertEqual(body['auth_index'], self.row['auth_index'])
            self.row['disabled'] = body['disabled']
            return {'status':'ok'}
        self.fail('Unexpected transport')

    def run_guard(self, a=None):
        a = observed(self.row, ('weekly',)) if a is None else a
        with patch.object(access.time, 'time', return_value=NOW):
            return self.guard.reconcile(self.row, a, self.call, RequestDeadline()), a

    def test_decision_matrix(self):
        cases = 0
        for spend, fresh, spent, disabled, marked in itertools.product(
                (True, False), (True, False), ((), ('weekly',), ('five_hour',), ('weekly','five_hour')),
                (True, False), (True, False)):
            with self.subTest(spend=spend, fresh=fresh, spent=spent, disabled=disabled, marked=marked):
                r = record(disabled=disabled); a = observed(r, spent, not fresh)
                action, marker = access.credit_spending_decision(spend, MARKER if marked else None, r, a, NOW)
                expected = ('resume' if marked and (spend or fresh and not spent) else
                            'pause' if not spend and fresh and spent and not disabled else 'keep')
                self.assertEqual(action, expected)
                if action == 'pause':
                    self.assertEqual(set(marker['windows']), set(spent))
                    self.assertEqual(marker['resets_at'], NOW+(1200 if 'five_hour' in spent else 600))
                cases += 1
        self.assertEqual(cases, 64)

    def test_missing_stale_rejected_reset_and_zero_credits(self):
        for a in ({}, dict(observed(self.row, ('weekly',)), quota_checked_at=None),
                  dict(observed(self.row, ('weekly',)), auth_state='sign_in_required')):
            self.assertEqual(access.credit_spending_decision(False, None, self.row, a, NOW)[0], 'keep')
        unknown = dict(self.row); unknown.pop('disabled')
        self.assertEqual(access.credit_spending_decision(False, None, unknown,
                         observed(self.row, ('weekly',)), NOW)[0], 'keep')
        a = observed(self.row)
        a['usage_credits'] = {'balance':0}
        a['windows'][0]['status'] = 'rejected'
        self.assertEqual(access.credit_spending_decision(False, None, self.row, a, NOW)[0], 'pause')
        a['windows'][0]['resets_at'] = NOW
        self.assertEqual(access.credit_spending_decision(False, MARKER, record(disabled=True), a, NOW)[0], 'keep')
        a = observed(self.row, ('weekly',)); a['usage_credits'] = None
        self.assertEqual(access.credit_spending_decision(False, None, self.row, a, NOW)[0], 'pause')
        for a in (dict(observed(self.row), windows=[]), dict(observed(self.row), quota_error='last known')):
            self.assertEqual(access.credit_spending_decision(False, MARKER, self.row, a, NOW)[0], 'keep')

    def test_private_atomic_store_default_refresh_signin_and_prune(self):
        self.assertEqual(self.guard.view(self.row), {'enabled':True, 'paused':None})
        self.guard.update(self.row['email'], spend_credits=False, paused=MARKER)
        self.assertEqual(stat.S_IMODE(self.guard.path.stat().st_mode), 0o600)
        fresh = access.CreditSpending(self.guard.path)
        r = dict(self.row, auth_index='new', name='new.json', id_token={'plan_type':'plus'})
        self.assertFalse(fresh.view(r)['enabled'])
        fresh.prune([])
        self.assertIsNone(fresh.view(r)['paused'])
        self.assertFalse(fresh.view(r)['enabled'])
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_broken_store_defaults_on_and_rate_limits_safe_log(self):
        self.guard.path.write_text('{broken')
        with self.assertLogs(access.__name__, 'WARNING') as logs:
            broken = access.CreditSpending(self.guard.path)
            broken.warn(); broken.warn()
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(self.row['email'], logs.output[0])
        self.assertTrue(broken.view(self.row)['enabled'])
        for value in ({'accounts':[]}, {'accounts':{'a':{'spend_credits':'false'}}},
                      {'accounts':{'a':{'spend_credits':False,'paused':{}}}}):
            self.guard.path.write_text(json.dumps(value))
            with self.assertLogs(access.__name__, 'WARNING'):
                self.assertTrue(access.CreditSpending(self.guard.path).view(self.row)['enabled'])

    def test_verified_pause_resume_fresh_redemption_and_turn_on(self):
        self.guard.update(self.row['email'], spend_credits=False)
        _, a = self.run_guard()
        self.assertTrue(self.row['disabled']); self.assertFalse(a['active'])
        self.assertEqual(a['access_state'], 'paused'); self.assertTrue(a['routing_enabled'])
        self.assertEqual(a['availability']['state'], 'paused')
        self.assertEqual(len([c for c in self.calls if c[0]=='/auth-files/status']), 1)
        self.run_guard(observed(self.row, (), stale=True))
        self.assertTrue(self.row['disabled'])
        self.run_guard(observed(self.row))  # The same fresh reading used after redemption.
        self.assertFalse(self.row['disabled']); self.assertFalse(self.guard.marked(self.row))
        self.run_guard(); self.assertTrue(self.row['disabled'])
        self.guard.update(self.row['email'], spend_credits=True)
        self.run_guard({})  # Turning on needs no reading.
        self.assertFalse(self.row['disabled']); self.assertFalse(self.guard.marked(self.row))

    def test_partial_positive_read_cannot_resume_both_exhausted_windows(self):
        self.guard.update(self.row['email'], spend_credits=False)
        self.run_guard(observed(self.row, ('weekly', 'five_hour')))
        self.assertEqual(self.guard.view(self.row)['paused']['windows'], ['weekly', 'five_hour'])
        self.calls.clear()
        partial = observed(self.row)
        partial['windows'] = partial['windows'][:1]  # New weekly-only response header.
        self.run_guard(partial)
        self.assertTrue(self.row['disabled'])
        self.assertTrue(self.guard.marked(self.row))
        self.assertFalse(any(c[0] == '/auth-files/status' for c in self.calls))
        self.run_guard(observed(self.row))  # Full post-redemption read includes both windows.
        self.assertFalse(self.row['disabled'])
        self.assertFalse(self.guard.marked(self.row))

    def test_replacement_discards_pause_without_touching_gateway_and_keeps_preference(self):
        self.guard.update(self.row['email'], spend_credits=False)
        self.run_guard(observed(self.row, ('weekly', 'five_hour')))
        self.row.update(name='replacement.json', auth_index='replacement', disabled=True)
        self.assertFalse(self.guard.marked(self.row))
        self.calls.clear()
        self.run_guard(observed(self.row))
        self.assertTrue(self.row['disabled'])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.guard.state['accounts'][self.row['email']]['paused'])
        saved = access.CreditSpending(self.guard.path)
        self.assertFalse(saved.view(self.row)['enabled'])
        self.assertFalse(saved.marked(self.row))

    def test_marker_write_failure_never_patches_gateway(self):
        self.guard.update(self.row['email'], spend_credits=False)
        with patch.object(self.guard, 'save', side_effect=OSError('disk')), self.assertRaises(OSError):
            self.run_guard()
        self.assertFalse(self.guard.marked(self.row))
        self.assertFalse(self.row['disabled'])
        self.assertFalse(any(c[0] == '/auth-files/status' for c in self.calls))

    def test_marker_first_patch_failure_restart_and_readback_failure(self):
        self.guard.update(self.row['email'], spend_credits=False)
        real = self.call
        def failure(path, body=None, method=None, **kw):
            if path == '/auth-files/status':
                self.assertIsNotNone(access.CreditSpending(self.guard.path).view(self.row)['paused'])
                raise OSError('synthetic')
            return real(path, body, method, **kw)
        with self.assertRaises(OSError), patch.object(access.time, 'time', return_value=NOW):
            self.guard.reconcile(self.row, observed(self.row, ('weekly',)), failure, RequestDeadline())
        self.assertFalse(self.row['disabled']); self.assertTrue(self.guard.marked(self.row))
        a=observed(self.row); self.guard.project(self.row,a)
        self.assertIsNone(a['credit_spending']['paused'])  # Never claim an unverified disable succeeded.
        self.guard = access.CreditSpending(self.guard.path)
        self.run_guard(); self.assertTrue(self.row['disabled'])
        self.guard.update(self.row['email'], spend_credits=True)
        def unconfirmed(path, body=None, method=None, **kw):
            if path == '/auth-files/status':
                return {}  # PATCH acknowledged but does not take effect.
            return real(path, body, method, **kw)
        with self.assertRaisesRegex(ValueError, 'readback'):
            self.guard.reconcile(self.row, {}, unconfirmed, RequestDeadline())
        self.assertTrue(self.guard.marked(self.row))
        self.run_guard({}); self.assertFalse(self.row['disabled'])

    def test_patch_success_marker_clear_failure_restart(self):
        self.guard.update(self.row['email'], spend_credits=False, paused=MARKER)
        self.row['disabled'] = True
        self.guard.update(self.row['email'], spend_credits=True)
        with patch.object(self.guard, 'save', side_effect=OSError('disk')), self.assertRaises(OSError):
            self.run_guard({})
        self.assertFalse(self.row['disabled']); self.assertTrue(self.guard.marked(self.row))
        self.guard = access.CreditSpending(self.guard.path)
        self.run_guard({})
        self.assertFalse(self.guard.marked(self.row))

    def test_external_enable_repauses_or_clears_and_identity_replacement_refused(self):
        self.guard.update(self.row['email'], spend_credits=False, paused=MARKER)
        self.run_guard(); self.assertTrue(self.row['disabled'])
        self.row['disabled'] = False
        self.run_guard(observed(self.row)); self.assertFalse(self.guard.marked(self.row))
        self.guard.update(self.row['email'], paused=MARKER)
        a=observed(self.row, ('weekly',)); old=copy.deepcopy(self.row)
        self.row['auth_index'] = 'replacement'
        with self.assertRaises(server.signin.AccessError), patch.object(access.time, 'time', return_value=NOW):
            self.guard.reconcile(old, a, self.call, RequestDeadline())
        self.assertFalse(self.row['disabled'])


class ConsumerTests(unittest.TestCase):
    setUp = GuardTests.setUp
    call = GuardTests.call
    # Reuse the scratch setup, not the decision tests.
    def test_projection_preserves_classified_availability_without_pause(self):
        a = {'availability': {'state': 'ready'}, 'active': True}
        with patch.object(availability, 'classify', side_effect=AssertionError('already classified')):
            self.guard.project(self.row, a)
        self.assertEqual(a['availability'], {'state': 'ready'})
        self.assertTrue(a['active'])

    def test_fetch_classifies_once_with_saved_spending_choice(self):
        self.guard.update(self.row['email'], spend_credits=False)
        cache = Mock()
        cache.fetch.return_value = dict(status='ok', auth_state='authenticated',
            checked_at=usage.stamp(NOW), windows=observed(self.row, ('weekly',))['windows'],
            usage_credits={'balance': 10})
        with patch.object(access.time, 'time', return_value=NOW), patch.object(
                server, 'classify', wraps=availability.classify) as classify:
            a = server.fetch_account(self.row, cache=cache, credit_spending=self.guard)
        self.assertEqual(classify.call_count, 1)
        self.assertFalse(a['credit_spending']['enabled'])
        self.assertEqual(a['availability']['state'], 'quota_exhausted')

    def test_consumers(self):
        self.row['disabled'] = True
        self.guard.update(self.row['email'], spend_credits=False, paused=MARKER)
        a=observed(self.row, ('weekly',)); self.guard.project(self.row,a)
        g=dict(online=True,checked_at=usage.stamp(NOW),accounts=[a],inventory=providers.inventory_view([self.row]))
        self.assertEqual(operations.account_health(a,g,NOW), 'paused')
        self.assertTrue(operations.can_resume(a)); self.assertFalse(operations.can_serve(a,g,NOW))
        required=dict(a,auth_state='sign_in_required')
        self.assertEqual(operations.account_health(required,g,NOW),'sign_in_required')
        self.assertFalse(operations.can_resume(required))
        a['resets']=dict(available=1,eligible=True,credits=[dict(id='credit',expires_at=NOW+60)])
        notices=operations.exhaustion_candidates({a['email']:a},g,NOW)
        self.assertTrue(notices)
        self.assertTrue(any((v.get('action') or {}).get('type')=='redeem_reset' for v in notices.values()))
        self.assertEqual(policy.ineligible_reason(a),'disabled')
        candidates, excluded=policy.eligible_accounts({'gateway':g},NOW,inventory=[self.row])
        self.assertEqual(candidates,{})
        self.assertEqual(excluded,{a['email']:'disabled'})
        neutral=policy.choose({},NOW,inventory=[self.row])
        self.assertEqual(neutral['priorities'],{a['email']:0})
        self.assertEqual(availability.pool_status(g,NOW)['ready'],0)
        # A stale paused account never creates DataHold for another ready account.
        b=record('b'); ba=observed(b); a['quota_error']='stale'; a['quota_checked_at']=None
        g.update(accounts=[a,ba], inventory=providers.inventory_view([self.row,b]))
        candidates,_=policy.eligible_accounts({'gateway':g},NOW,inventory=[self.row,b])
        self.assertEqual(set(candidates),{b['email']})
        resets=reset_credits.ResetManager(self.call,lambda _: {},self.root/'resets.json')
        resets.is_paused=self.guard.marked
        self.assertEqual(resets.account('a')['auth_index'],'a')
        self.guard.update(self.row['email'],paused=None)
        with self.assertRaises(ValueError):resets.account('a')

    def test_provider_projection_preserves_verified_flag_newer_than_inventory(self):
        a=observed(self.row,('weekly',))
        self.guard.update(self.row['email'],spend_credits=False,paused=MARKER)
        paused_record=dict(self.row,disabled=True)
        a.update(disabled=True,active=False)
        self.guard.project(paused_record,a)
        g=dict(accounts=[a],inventory=providers.inventory_view([self.row]))
        groups=providers.groups(g,[])
        self.assertTrue(groups[0]['accounts'][0]['disabled'])
        self.assertTrue(groups[0]['accounts'][0]['routing_enabled'])
        self.assertEqual(groups[0]['accounts'][0]['availability']['state'],'paused')

    def test_disabled_poll_after_reset_headers_and_daily_read(self):
        elapsed=[0]
        cache=usage.UsageCache(clock=lambda:elapsed[0],wall_clock=lambda:NOW+elapsed[0],jitter=lambda:0,daily_jitter=lambda:0)
        row=record(disabled=True)
        call=Mock(return_value={'status_code':200,'body':{}})
        spent=observed(row,('weekly',))
        normalize=lambda _:dict(windows=copy.deepcopy(spent['windows']),auth_state='authenticated')
        cache.fetch(row,call,request_body={},normalize=normalize)
        self.assertEqual(call.call_count,1)
        # Polls still read a DISABLED credential; first attempt at reset+60 seconds.
        elapsed[0]=600
        cache.fetch(row,call,request_body={},normalize=normalize);self.assertEqual(call.call_count,1)
        elapsed[0]=660
        cache.fetch(row,call,request_body={},normalize=normalize);self.assertEqual(call.call_count,2)
        self.assertGreaterEqual(cache.entries[('codex','a')]['due'],elapsed[0]+86400)


class APIAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.row=record();self.calls=[]
        self.guard=access.CreditSpending(root/'credits.json')
        self.state=server.GatewayState(resets=reset_credits.ResetManager(lambda *a: {},lambda _: {},root/'resets.json'),
              notices=server.NoticeStore(root/'notices.json'),
              fast_modes=fast_mode.FastModeManager(root/'fast.json',root/'policy.json',root/'status.json'),credit_spending=self.guard)
        self.state.data.update(online=True,checked_at=usage.stamp(NOW),accounts=[observed(self.row,('weekly',))])
        self.api=patch.object(server,'api',side_effect=self.call);self.api.start();self.addCleanup(self.api.stop)
        self.clock=patch.object(access.time,'time',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)
        self.routing=patch.object(policy,'STATE',root/'routing');self.routing.start();self.addCleanup(self.routing.stop)
        self.state.signin_changed=Mock()
        self.identity=access.identity(self.row)
        class Handler(server.Handler):
            def log_message(self,*a): pass
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),Handler);self.http.state=self.state
        self.thread=threading.Thread(target=self.http.serve_forever);self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.http.shutdown();self.http.server_close();self.thread.join(2);self.state.signins.close()

    def call(self,path,body=None,method=None,**kw):
        self.calls.append((path,body,method))
        if path=='/auth-files': return {'files':[copy.deepcopy(self.row)]}
        if path=='/auth-files/status':self.row['disabled']=body['disabled'];return {'status':'ok'}
        raise AssertionError('No provider requests allowed')

    def post(self,data=None,headers=None,raw=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=3)
        h={'X-Router-Request':'1','Content-Type':'application/json','Host':'localhost'}
        h.update(headers or {})
        body=json.dumps(data if data is not None else dict(account_id='a',identity=self.identity,spend=False)) if raw is None else raw
        connection.request('POST','/api/accounts/credit-spending',body,h)
        response=connection.getresponse();value=json.loads(response.read());connection.close()
        return response.status,value

    def test_api_saved_immediate_pause_and_resume(self):
        code,value=self.post();self.assertEqual(code,200);self.assertFalse(value['enabled']);self.assertIsNotNone(value['paused'])
        self.assertTrue(self.row['disabled']);self.assertTrue(self.state.data['accounts'][0]['routing_enabled'])
        code,value=self.post(dict(account_id='a',identity=self.identity,spend=True))
        self.assertEqual((code,value),(200,dict(enabled=True,paused=None)));self.assertFalse(self.row['disabled'])
        self.assertFalse(any(c[0]=='/api-call' for c in self.calls))

    def test_api_validation_origin_readonly_identity(self):
        for data in ({},[],dict(account_id='a',identity=self.identity,spend=0),
                     dict(account_id='a',identity=self.identity,spend=False,extra=1),
                     dict(account_id='a',identity='invalid',spend=False)):
            self.assertEqual(self.post(data)[0],400)
        self.assertEqual(self.post(raw='x'*2049)[0],400)
        self.assertEqual(self.post(headers={'Content-Type':'text/plain'})[0],400)
        for headers in ({'X-Router-Request':''},{'Origin':'http://elsewhere.test'},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.post(headers=headers)[0],403)
        self.assertEqual(self.post(headers={'X-Router-Read-Only':'1'})[0],403)
        code,value=self.post(dict(account_id='a',identity='0'*64,spend=False))
        self.assertEqual((code,value),(409,dict(error='account_changed')))
        self.assertTrue(self.guard.view(self.row)['enabled'])

    def test_api_patch_failure_restores_setting_retains_recovery_marker(self):
        original = self.call
        def failure(path, body=None, method=None, **kw):
            if path == '/auth-files/status':raise OSError('synthetic')
            return original(path,body,method,**kw)
        with patch.object(server,'api',side_effect=failure):
            self.assertEqual(self.post()[0],503)
        self.assertTrue(self.guard.view(self.row)['enabled'])
        self.assertTrue(self.guard.marked(self.row))
        self.state.apply_credit_spending(self.row,observed(self.row))
        self.assertFalse(self.guard.marked(self.row))
        self.assertFalse(self.row['disabled'])

    def test_last_ready_and_disconnect_checks_ignore_spendable_balance(self):
        other=record('b')
        a=observed(self.row)
        b=observed(other,('weekly',));b['usage_credits']={'balance':10}
        b['availability']=availability.classify(other,b,NOW)
        self.assertEqual(b['availability']['state'],'quota_exhausted')
        self.guard.update(other['email'],spend_credits=False)
        def call(path,body=None,method=None,**kw):
            if path=='/auth-files':return {'files':[copy.deepcopy(self.row),copy.deepcopy(other)]}
            return self.call(path,body,method,**kw)
        def fetch(file, credit_spending=None, **kw):
            account = copy.deepcopy(a if file['auth_index']=='a' else b)
            account['credit_spending'] = credit_spending.view(file)
            account['availability'] = availability.classify(file, account, NOW)
            return account
        with patch.object(server,'api',side_effect=call), patch.object(server,'fetch_account',side_effect=fetch):
            with self.assertRaisesRegex(server.signin.AccessError,'last_ready'):
                self.state.set_routing_enabled('a',self.identity,False)
            self.assertFalse(self.state.other_ready_accounts([self.row,other],'codex',self.row['name'],call,
                                                             credit_spending=self.guard))
        self.assertFalse(self.row['disabled'])

    def test_routing_on_reconciles_cached_allowance_before_returning(self):
        self.guard.update(self.row['email'], spend_credits=False)
        self.row['disabled'] = True
        self.state.data['accounts'] = [observed(self.row, ('weekly',))]
        deadline = RequestDeadline()
        with patch.object(server.threading, 'Thread'), patch.object(
                self.guard, 'reconcile', wraps=self.guard.reconcile) as reconcile:
            self.assertEqual(self.state.set_routing_enabled('a', self.identity, True,
                             deadline=deadline), {'enabled': True})
        self.assertTrue(self.row['disabled'])
        self.assertTrue(self.guard.marked(self.row))
        a = self.state.data['accounts'][0]
        self.assertEqual(a['availability']['state'], 'paused')
        self.assertTrue(a['routing_enabled'])
        self.assertIs(reconcile.call_args.args[-1], deadline)
        self.assertEqual([body['disabled'] for path, body, _ in self.calls
                          if path == '/auth-files/status'], [False, True])
        for stale in (dict(quota_error='unavailable'), dict(quota_checked_at=None),
                      dict(windows=[dict(w, resets_at=NOW-1) for w in observed(self.row, ('weekly',))['windows']])):
            with self.subTest(stale=stale):
                self.guard.update(self.row['email'], paused=None)
                self.row['disabled'] = True
                self.state.data['accounts'] = [dict(observed(self.row, ('weekly',)), **stale)]
                self.calls.clear()
                with patch.object(server.threading, 'Thread'):
                    self.state.set_routing_enabled('a', self.identity, True)
                self.assertFalse(self.row['disabled'])
                self.assertFalse(self.guard.marked(self.row))
                self.assertEqual([body['disabled'] for path, body, _ in self.calls
                                  if path == '/auth-files/status'], [False])

    def test_redemption_full_usage_refresh_resumes_all_paused_windows(self):
        self.state.usage_cache = usage.UsageCache(wall_clock=lambda: NOW)
        self.state.data['accounts'] = [observed(self.row, ('weekly', 'five_hour'))]
        self.post()
        self.assertTrue(self.row['disabled'])
        def provider(path, body=None, method=None, **kw):
            if path == '/api-call':
                self.assertTrue(body['url'].endswith('/usage'))
                return {'status_code': 200, 'body': {'plan_type': 'pro', 'rate_limit': {
                    'primary_window': {'limit_window_seconds': 604800, 'used_percent': 10, 'reset_at': NOW+600},
                    'secondary_window': {'limit_window_seconds': 18000, 'used_percent': 20, 'reset_at': NOW+1200}}}}
            return self.call(path, body, method, **kw)
        with patch.object(server, 'api', side_effect=provider), \
                patch.object(server, 'account_metadata', return_value={}), \
                patch.object(policy, 'reconcile', return_value={'status': 'active'}):
            steps = self.state.after_reset('a')
        self.assertEqual(steps['quota_refresh'], 'ok')
        self.assertEqual(len(self.state.data['accounts'][0]['windows']), 2)
        self.assertFalse(self.row['disabled'])
        self.assertFalse(self.guard.marked(self.row))

    def test_user_off_persists_marker_removal_before_patch_and_crash(self):
        self.guard.update(self.row['email'], spend_credits=False, paused=MARKER)
        def crash_after_patch(path, body=None, method=None, **kw):
            if path == '/auth-files/status':
                self.row['disabled'] = body['disabled']
                self.assertFalse(access.CreditSpending(self.guard.path).marked(self.row))
                raise OSError('crash after PATCH')
            return self.call(path, body, method, **kw)
        with patch.object(server, 'api', side_effect=crash_after_patch), self.assertRaises(OSError):
            self.state.set_routing_enabled('a', self.identity, False, confirmed=True)
        restarted = access.CreditSpending(self.guard.path)
        self.assertFalse(restarted.marked(self.row))
        restarted.reconcile(self.row, observed(self.row), self.call, RequestDeadline())
        self.assertTrue(self.row['disabled'])

    def test_user_routing_off_drops_marker_no_confirmation_no_reenable(self):
        self.post();self.assertTrue(self.row['disabled'])
        self.state.set_routing_enabled('a',self.identity,False)
        self.assertFalse(self.guard.marked(self.row));self.assertTrue(self.row['disabled'])
        self.assertEqual(self.state.signins.routing_generations['a'],1)
        self.state.apply_credit_spending(self.row,observed(self.row))
        self.assertTrue(self.row['disabled'])

    def test_pause_does_not_suppress_signin_and_signin_fresh_usage_can_resume(self):
        self.post()
        self.assertEqual(self.state.signins.routing_generations,{})
        # Automatic pause never invokes suppress_enable/routing_set. A successful
        # sign-in can enable, then its existing post-signin refresh re-evaluates.
        session=dict(account_id='a',enable=True,routing_generation=0,stop=threading.Event())
        self.state.signins.sessions['a']=session
        self.assertTrue(self.state.signins.can_enable(session))
        self.state.apply_credit_spending(self.row,observed(self.row))
        self.assertFalse(self.row['disabled']);self.assertFalse(self.guard.marked(self.row))


if __name__=='__main__':unittest.main()
