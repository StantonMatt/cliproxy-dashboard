from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
import copy
import contextlib
import datetime as dt
import fcntl
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import routing_policy as policy
from signin_fixture import auth_file_row
from codex_fixture import CODEX_PAIR, use_codex_pair
A, B = CODEX_PAIR


def setUpModule():
    use_codex_pair()

class RoutingTests(unittest.TestCase):
    def test_free_eligibility_follows_preference_auth_and_quota(self):
        account = {'active': True, 'disabled': False, 'plan_type': 'free',
                   'auth_state': 'authenticated', 'availability': {'state': 'ready'}}
        self.assertIsNone(policy.ineligible_reason(account))
        for change, reason in (({'disabled': True}, 'disabled'),
                               ({'auth_state': 'sign_in_required'}, 'auth_required'),
                               ({'availability': {'state': 'quota_exhausted'}}, 'quota_exhausted')):
            self.assertEqual(policy.ineligible_reason({**account, **change}), reason)

    def test_revoked_or_unconfirmed_account_excluded_before_unknown_quota_validation(self):
        for change, expected in (({'auth_state': 'sign_in_required'}, 'auth_required'),
                                 ({'availability': {'state': 'auth_required'}}, 'auth_required'),
                                 ({'access_state': 'disable_unconfirmed'}, 'disable_unconfirmed')):
            data = self.quota_overview()
            account = data['gateway']['accounts'][0]
            account.update(change, active=True, quota_error='PRIVATE_TOKEN', windows=[], quota_checked_at=None)
            candidates, excluded = policy.eligible_accounts(data, 1700000000)
            self.assertEqual(set(candidates), {B})
            self.assertEqual(excluded, {A: expected})
            self.assertEqual(policy.selected_plan({'mode': 'preferred-account', 'preferred_email': A},
                                                data, 1700000000)['preferred_email'], B)
        account['access_state'] = 'enabled'
        with self.assertRaises(policy.DataHold):
            policy.eligible_accounts(data, 1700000000)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.rows=[{'type':'codex','email':e,'name':e,'priority':100 if e==A else 0,
                    'unavailable':e==B,'private':'NEVER_SAVE'} for e in (A,B)]
        self.writes=[]
    def api(self,path,body=None,method=None):
        if path=='/config': return {'routing':{'strategy':'round-robin','session-affinity':True}}
        if path=='/auth-files':return {'files':[auth_file_row(row,row['name'],row['email']) for row in self.rows]}
        self.assertEqual(path,'/auth-files/fields');self.assertEqual(method,'PATCH')
        self.assertEqual(set(body),{'name','priority'})
        self.writes.append(body.copy())
        next(r for r in self.rows if r['name']==body['name'])['priority']=body['priority']
        return {'status':'ok'}
    def test_round_robin_neutralizes_priorities_and_survives_timer(self):
        result=policy.reconcile({}, {'mode':'round-robin'},self.api,self.root)
        self.assertEqual(result['status'],'active')
        self.assertEqual([r['priority'] for r in self.rows],[0,0])
        self.assertTrue(self.rows[1]['unavailable'])
        n=len(self.writes)
        policy.reconcile({},call=self.api,state_dir=self.root)
        self.assertEqual(len(self.writes),n)
        self.assertNotIn('NEVER_SAVE',(self.root/'baseline.json').read_text())
    def test_manual_preference_keeps_failover_and_cooldown_fields(self):
        with patch.object(policy.time,'time',return_value=1700000000):
            policy.reconcile(self.quota_overview(), {'mode':'preferred-account','preferred_email':B},self.api,self.root)
        self.assertEqual([r['priority'] for r in self.rows],[0,100])
        self.assertTrue(self.rows[1]['unavailable'])
        with patch.object(policy.time,'time',return_value=1700000000):
            policy.reconcile(self.quota_overview(),call=self.api,state_dir=self.root)
        self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY)['preferred_email'],B)
    def test_invalid_choice_never_writes(self):
        for choice in ({'mode':'bad'},{'mode':'preferred-account','preferred_email':'unknown'},[],None):
            with self.assertRaises(ValueError):policy.validate_selection(choice, inventory=CODEX_INVENTORY)
        self.assertEqual(self.writes,[])
    def test_partial_failure_persists_intent_then_next_tick_finishes(self):
        def fail(path,body=None,method=None):
            if method=='PATCH' and body['name']==A:raise OSError('private')
            return self.api(path,body,method)
        with patch.object(policy.time,'time',return_value=1700000000),self.assertRaises(OSError):
            policy.reconcile(self.quota_overview(), {'mode':'preferred-account','preferred_email':B},fail,self.root)
        self.assertEqual(json.loads((self.root/'status.json').read_text())['status'],'held')
        policy.reconcile({},call=self.api,state_dir=self.root)
        self.assertEqual([r['priority'] for r in self.rows],[0,100])
    def test_lock_excludes_overlapping_timer(self):
        with (self.root/'controller.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(policy.Hold):
                policy.reconcile({}, {'mode':'round-robin'},self.api,self.root)
        self.assertEqual(self.writes,[])
    def test_cli_rollback_restores_recorded_baseline(self):
        baseline={A:{'name':A,'priority':None},B:{'name':B,'priority':100}}
        policy.write_json(self.root/'baseline.json',baseline)
        with patch.object(policy,'STATE',self.root),patch.object(policy,'request',self.api), \
                patch.object(sys,'argv',['routing-policy','--rollback']),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(policy.main(),0)
        self.assertEqual([r['priority'] for r in self.rows],[None,100])
        self.assertEqual(json.loads(output.getvalue()),{'status':'restored','policy':'baseline'})
        self.assertEqual(json.loads((self.root/'status.json').read_text())['status'],'disabled')
        self.assertTrue(self.rows[1]['unavailable'])
    def test_cli_rollback_without_baseline_uses_neutral_priorities(self):
        with patch.object(policy,'STATE',self.root),patch.object(policy,'request',self.api), \
                patch.object(sys,'argv',['routing-policy','--rollback']),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(policy.main(),0)
        self.assertEqual([r['priority'] for r in self.rows],[0,0])
        self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY)['mode'],'round-robin')
        self.assertEqual(json.loads(output.getvalue()),{'status':'restored','policy':'round-robin'})
        self.assertEqual(json.loads((self.root/'status.json').read_text())['status'],'disabled')
        self.assertFalse((self.root/'baseline.json').exists())
    def test_rollback_does_not_fall_back_for_invalid_or_changed_baseline(self):
        for content in ('invalid','null',json.dumps({e:{'name':'changed','priority':None} for e in (A,B)})):
            (self.root/'baseline.json').write_text(content)
            with self.subTest(content=content),patch.object(policy,'STATE',self.root), \
                    patch.object(policy,'request',self.api),patch.object(sys,'argv',['routing-policy','--rollback']), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(policy.main(),1)
            self.assertEqual(self.writes,[])
    def test_rollback_shares_timer_lock(self):
        with (self.root/'controller.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(policy.Hold):
                policy.rollback(self.api,self.root)
        self.assertEqual(self.writes,[])
    def test_script_rollback_exits_nonzero_on_hold_or_failure(self):
        for failure in (BlockingIOError, OSError):
            with self.subTest(failure=failure),patch.object(sys,'argv',['routing-policy','--rollback']), \
                    patch.object(Path,'mkdir'),patch.object(Path,'open',mock_open()), \
                    patch.object(policy.fcntl,'flock',side_effect=failure), \
                    patch.object(policy.time,'monotonic',side_effect=range(1000)), \
                    patch.object(policy.time,'sleep'), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(policy.__file__,run_name='__main__')
            self.assertEqual(exited.exception.code,1)
            self.assertEqual(json.loads(output.getvalue())['status'],'held')
    def test_rollback_only_disables_status_after_confirming_priorities(self):
        status={'status':'active','policy':'preferred-account','preferred_email':A}
        policy.write_json(self.root/'status.json',status)
        def unconfirmed(path,body=None,method=None):
            if method=='PATCH':return {'status':'ok'}
            return self.api(path,body,method)
        with self.assertRaises(policy.Hold):policy.rollback(unconfirmed,self.root)
        self.assertEqual(json.loads((self.root/'status.json').read_text()),status)
        self.assertTrue(policy.suspended(self.root))
    def test_equal_resets_use_equal_priorities(self):
        now=1700000000
        overview={'gateway':{'online':True,'checked_at':policy.stamp(now),'accounts':[
            {'email':e,'active':True,'quota_checked_at':policy.stamp(now),
             'windows':[{'seconds':604800,'remaining':50,'resets_at':now+10000}]} for e in (A,B)]}}
        result=policy.selected_plan({'mode':'earliest-weekly-reset'},overview,now)
        self.assertEqual(result['priorities'],{A:0,B:0})
        overview['gateway']['accounts'][0]['windows'][0]['resets_at']=now+5000
        self.assertEqual(policy.selected_plan({'mode':'earliest-weekly-reset'},overview,now)['preferred_email'],A)
    def test_changed_strategy_is_not_overwritten(self):
        def changed(path,*args):
            if path=='/config':return {'routing':{'strategy':'fill-first'}}
            return self.api(path,*args)
        with self.assertRaises(policy.Hold):policy.reconcile({}, {'mode':'round-robin'},changed,self.root)
        self.assertFalse((self.root/'selection.json').exists())

    def quota_overview(self, a=92, b=86):
        now=1700000000
        return {'gateway':{'online':True,'checked_at':policy.stamp(now),
            'inventory':[dict(type='codex',name=e,email=e,auth_index=e) for e in (A,B)],'accounts':[
            {'id':e,'email':e,'active':True,'quota_checked_at':policy.stamp(now),
             'windows':[{'seconds':604800,'remaining':remaining,'resets_at':now+reset}]}
            for e,remaining,reset in ((A,a,20000),(B,b,10000))]}}

    def test_aged_exhausted_quota_deferral_preserves_routing_until_expiry(self):
        now = 1700000000
        data = self.quota_overview(a=0)
        first = data['gateway']['accounts'][0]
        first.update(quota_checked_at=policy.stamp(now - 1800), fresh_until=policy.stamp(now + 60))
        data['gateway'] = policy.observations(data['gateway'])['gateway']
        candidates, excluded = policy.eligible_accounts(data, now)
        self.assertEqual(set(candidates), {B})
        self.assertEqual(excluded, {A: 'no remaining allowance'})
        data['gateway']['checked_at'] = policy.stamp(now + 10000)
        with self.assertRaises(policy.DataHold):
            policy.eligible_accounts(data, now + 10000)

    def test_higher_quota_wins_regardless_of_reset_order_and_ties_are_neutral(self):
        selected={'mode':'highest-remaining-quota'}
        for a,b,winner in [(92,86,A),(40,90,B),(50,50,None),(100,100,None)]:
            with self.subTest(a=a,b=b):
                plan=policy.selected_plan(selected,self.quota_overview(a,b),1700000000)
                self.assertEqual(plan['preferred_email'],winner)
                self.assertEqual(plan['priorities'],{e:100 if e==winner else 0 for e in (A,B)})

    def test_higher_quota_skips_exhausted_or_inactive_accounts(self):
        for reason in ('weekly','short','inactive'):
            data=self.quota_overview();account=data['gateway']['accounts'][0]
            if reason=='weekly':account['windows'][0]['remaining']=0
            elif reason=='short':account['windows'].append({'seconds':18000,'remaining':0,'resets_at':1700010000})
            else:account['active']=False
            with self.subTest(reason=reason):
                self.assertEqual(policy.selected_plan({'mode':'highest-remaining-quota'},data,1700000000)['preferred_email'],B)
        with self.assertRaises(policy.Hold):
            policy.selected_plan({'mode':'highest-remaining-quota'},self.quota_overview(0,0),1700000000)

    def test_uncertain_quota_holds_priorities_and_saves_selection_and_status(self):
        for reason in ('stale','quota-error','missing-week','missing-reset','expired-reset','offline','pair',None,True,-1,101,float('nan')):
            data=self.quota_overview();account=data['gateway']['accounts'][0]
            if reason=='stale':account['quota_checked_at']=policy.stamp(1700000000+1)
            elif reason=='quota-error':account['quota_error']='Unavailable'
            elif reason=='missing-week':account['windows']=[]
            elif reason=='missing-reset':del account['windows'][0]['resets_at']
            elif reason=='expired-reset':account['windows'][0]['resets_at']=1699999999
            elif reason=='offline':data['gateway']['online']=False
            elif reason=='pair':data['gateway']['accounts'].pop()
            else:account['windows'][0]['remaining']=reason
            with self.subTest(reason=reason),patch.object(policy.time,'time',return_value=1700000000):
                result=policy.reconcile(data,{'mode':'highest-remaining-quota'},self.api,self.root)
            self.assertEqual(self.writes,[])
            self.assertEqual(result['status'],'held')
            self.assertTrue(result['hold_reason'])
            self.assertEqual(result['held_since'],policy.stamp(1700000000))
            self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY)['mode'],'highest-remaining-quota')
            status=json.loads((self.root/'status.json').read_text())
            self.assertEqual(status['hold_reason'],result['hold_reason'])

    def free_overview(self, pro=57, free=88, free_reset=23*86400):
        data=self.quota_overview(pro,0)
        data['gateway']['accounts'][1]['windows']=[{'label':'Usage window','seconds':2592000,
                                                   'used':100-free,'remaining':free,'resets_at':1700000000+free_reset}]
        return data

    def test_free_usage_window_account_is_secondary_to_weekly_account(self):
        for mode in ('earliest-weekly-reset','highest-remaining-quota'):
            for current in (None,A,B):
                with self.subTest(mode=mode,current=current):
                    plan=policy.selected_plan({'mode':mode},self.free_overview(),1700000000,current)
                    self.assertEqual(plan['preferred_email'],A)
                    self.assertEqual(plan['priorities'],{A:100,B:0})
                    self.assertEqual(plan['weekly_resets_at'],1700020000)
            self.rows[0]['priority']=0;self.rows[1]['priority']=100
            with self.subTest(mode=mode),patch.object(policy.time,'time',return_value=1700000000):
                result=policy.reconcile(self.free_overview(5,100),{'mode':mode},self.api,self.root)
            self.assertEqual(result['status'],'active')
            self.assertIsNone(result['hold_reason'])
            self.assertEqual([r['priority'] for r in self.rows],[100,0])

    def test_free_usage_window_account_takes_over_when_weekly_account_has_no_allowance(self):
        for mode in ('earliest-weekly-reset','highest-remaining-quota'):
            for reason in ('weekly','short','inactive'):
                data=self.free_overview();account=data['gateway']['accounts'][0]
                if reason=='weekly':account['windows'][0]['remaining']=0
                elif reason=='short':account['windows'].append({'seconds':18000,'remaining':0,'resets_at':1700010000})
                else:account['active']=False
                with self.subTest(mode=mode,reason=reason):
                    plan=policy.selected_plan({'mode':mode},data,1700000000,current=A)
                    self.assertEqual(plan['preferred_email'],B)
                    self.assertEqual(plan['priorities'],{A:0,B:100})
                    self.assertEqual(plan['weekly_resets_at'],1700000000+23*86400)
            with self.subTest(mode=mode),self.assertRaisesRegex(policy.Hold,'No account currently reports'):
                policy.selected_plan({'mode':mode},self.free_overview(0,0),1700000000)

    def test_free_usage_window_accepts_its_own_period_and_rejects_invalid_data(self):
        for reset in (25*86400,30*86400,31*86400):
            with self.subTest(reset=reset):
                candidates,_=policy.eligible_accounts(self.free_overview(0,88,reset),1700000000)
                self.assertEqual(candidates[B],{'remaining':88,'reset':1700000000+reset,'secondary':True})
        for reason in ('missing-reset','expired-reset','far-reset','missing-remaining',None,-1,101,float('nan'),'duplicate'):
            data=self.free_overview();window=data['gateway']['accounts'][1]['windows'][0]
            if reason=='missing-reset':del window['resets_at']
            elif reason=='expired-reset':window['resets_at']=1699999999
            elif reason=='far-reset':window['resets_at']=1700000000+31*86400+1
            elif reason=='missing-remaining':del window['remaining']
            elif reason=='duplicate':data['gateway']['accounts'][1]['windows'].append(dict(window))
            else:window['remaining']=reason
            for mode in ('earliest-weekly-reset','highest-remaining-quota'):
                with self.subTest(reason=reason,mode=mode),self.assertRaises(policy.DataHold):
                    policy.selected_plan({'mode':mode},data,1700000000)

    def unused_free_overview(self, pro=57, remaining=True):
        data=self.free_overview(pro)
        window=data['gateway']['accounts'][1]['windows'][0]
        window.update(used=0,remaining=100 if remaining else None,resets_at=None)
        return data

    def test_unused_free_usage_window_without_reset_starts_one_full_period(self):
        now=1700000000
        for mode in ('earliest-weekly-reset','highest-remaining-quota'):
            for remaining in (True,False):
                with self.subTest(mode=mode,remaining=remaining):
                    plan=policy.selected_plan({'mode':mode},self.unused_free_overview(remaining=remaining),now)
                    self.assertEqual(plan['priorities'],{A:100,B:0})
                    plan=policy.selected_plan({'mode':mode},self.unused_free_overview(0,remaining),now,current=A)
                    self.assertEqual(plan['preferred_email'],B)
                    self.assertEqual(plan['priorities'],{A:0,B:100})
                    self.assertEqual(plan['weekly_resets_at'],now+2592000)
            self.rows[0]['priority']=0;self.rows[1]['priority']=100
            data=self.unused_free_overview()
            with self.subTest(mode=mode),patch.object(policy.time,'time',return_value=now):
                result=policy.reconcile(data,{'mode':mode},self.api,self.root)
            self.assertEqual(result['status'],'active')
            self.assertIsNone(result['hold_reason'])
            self.assertEqual([r['priority'] for r in self.rows],[100,0])
            self.assertIsNone(data['gateway']['accounts'][1]['windows'][0]['resets_at'])
        for remaining,used in ((99,1),(None,1),(None,None)):
            data=self.unused_free_overview()
            data['gateway']['accounts'][1]['windows'][0].update(remaining=remaining,used=used)
            with self.subTest(remaining=remaining,used=used),self.assertRaisesRegex(policy.DataHold,'stale quota data|reset time is not reported'):
                policy.selected_plan({'mode':'earliest-weekly-reset'},data,now)

    def test_preferred_account_with_free_usage_window_sets_priorities(self):
        for data in (self.free_overview(),self.unused_free_overview()):
            for email in (A,B):
                with self.subTest(email=email,reset=data['gateway']['accounts'][1]['windows'][0]['resets_at']):
                    plan=policy.selected_plan({'mode':'preferred-account','preferred_email':email},data,1700000000)
                    self.assertEqual(plan['preferred_email'],email)
                    self.assertEqual(plan['priorities'],{e:100 if e==email else 0 for e in (A,B)})
                    self.assertIsNone(plan['reason'])
        with patch.object(policy.time,'time',return_value=1700000000):
            result=policy.reconcile(self.unused_free_overview(),{'mode':'preferred-account','preferred_email':B},
                                    self.api,self.root)
        self.assertEqual(result['status'],'active')
        self.assertEqual([r['priority'] for r in self.rows],[0,100])

    def test_weekly_account_with_longer_window_is_ranked_by_weekly_window(self):
        data=self.quota_overview(40,90)
        data['gateway']['accounts'][0]['windows'].append({'seconds':2592000,'remaining':99,'resets_at':1700000000+20*86400})
        candidates,_=policy.eligible_accounts(data,1700000000)
        self.assertEqual(candidates[A],{'remaining':40,'reset':1700020000,'secondary':False})
        self.assertEqual(policy.choose(data,1700000000,'highest-remaining-quota')['preferred_email'],B)
        self.assertEqual(policy.choose(data,1700000000,'earliest-weekly-reset')['preferred_email'],B)

    def test_free_account_with_exhausted_shorter_window_is_excluded(self):
        for window in ({'seconds':18000,'remaining':0,'resets_at':1700010000},{'label':'Other','remaining':0}):
            data=self.free_overview(0)
            data['gateway']['accounts'][1]['windows'].append(window)
            with self.subTest(window=window):
                candidates,excluded=policy.eligible_accounts(data,1700000000)
                self.assertEqual(candidates,{})
                self.assertEqual(excluded,{A:'no remaining allowance',B:'no remaining allowance'})

    def test_codex_full_unused_weekly_window_still_requires_reset(self):
        for mode in ('earliest-weekly-reset', 'highest-remaining-quota'):
            for missing in (False, True):
                data=self.quota_overview(100,90)
                window=data['gateway']['accounts'][0]['windows'][0]
                window.update(used=0,resets_at=None)
                if missing:window.pop('resets_at')
                with self.subTest(mode=mode,missing=missing),self.assertRaises(policy.DataHold):
                    policy.choose(data,1700000000,mode)

    def test_saved_quota_mode_rebalances_then_can_return_to_round_robin(self):
        with patch.object(policy.time,'time',return_value=1700000000):
            policy.reconcile(self.quota_overview(),{'mode':'highest-remaining-quota'},self.api,self.root)
            self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY)['mode'],'highest-remaining-quota')
            self.assertEqual([r['priority'] for r in self.rows],[100,0])
            policy.reconcile(self.quota_overview(80,86),call=self.api,state_dir=self.root)
            self.assertEqual([r['priority'] for r in self.rows],[0,100])
            writes=len(self.writes)
            policy.reconcile(self.quota_overview(80,86),call=self.api,state_dir=self.root)
            self.assertEqual(len(self.writes),writes)
            policy.reconcile(self.quota_overview(80,80),call=self.api,state_dir=self.root)
            self.assertEqual([r['priority'] for r in self.rows],[0,100])
            policy.reconcile({}, {'mode':'round-robin'},self.api,self.root)
            self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY)['mode'],'round-robin')
            self.assertTrue(self.rows[1]['unavailable'])

    def current_overview(self, now, a=92, b=86):
        data=self.quota_overview(a,b)
        data['gateway']['checked_at']=policy.stamp(now)
        for account in data['gateway']['accounts']:
            account['quota_checked_at']=policy.stamp(now)
        return data

    def test_shared_observations_keep_cached_quota_stale_and_detached(self):
        now=1700000000
        data=self.quota_overview(1,0)
        account=data['gateway']['accounts'][0]
        account['quota_checked_at']=policy.stamp(now+1)
        exhausted=data['gateway']['accounts'][1]
        exhausted['availability']={'state':'quota_exhausted','scope':'account'}
        overview=policy.observations(data['gateway'])
        # Later snapshot updates cannot change a controller input already built.
        account['quota_checked_at']=policy.stamp(now)
        exhausted['windows'][0]['remaining']=100
        exhausted['availability']['state']='ready'
        with patch.object(policy.time,'time',return_value=now):
            result=policy.reconcile(overview,{'mode':'preferred-account','preferred_email':B},self.api,self.root)
        self.assertEqual(result['status'],'held')
        self.assertEqual(result['hold_reason'],'stale quota data for owner@example.test')
        self.assertEqual(self.writes,[])
        self.assertEqual(overview['gateway']['accounts'][1]['windows'][0]['remaining'],0)
        self.assertEqual(overview['gateway']['accounts'][1]['availability']['state'],'quota_exhausted')

    def test_quota_holds_fall_back_after_ten_minutes_then_resume_saved_policy(self):
        start=1700000000
        for failure in ('stale','unknown'):
            with self.subTest(failure=failure),patch.object(policy.time,'time',return_value=start):
                policy.reconcile(self.quota_overview(),{'mode':'highest-remaining-quota'},self.api,self.root)
                saved=(self.root/'selection.json').read_text()
            count=len(self.writes)
            for elapsed in (0,599,600,601):
                now=start+elapsed
                data=self.current_overview(now)
                account=data['gateway']['accounts'][1 if failure=='stale' else 0]
                if failure=='stale':
                    account['quota_checked_at']=policy.stamp(now+1)
                    reason='stale quota data for alternate@example.test'
                else:
                    account.update(quota_error='PRIVATE_ERROR',auth_state='unknown')
                    reason='quota error for owner@example.test: quota_unavailable'
                with patch.object(policy.time,'time',return_value=now):
                    result=policy.reconcile(data,call=self.api,state_dir=self.root)
                self.assertEqual(result['status'],'held' if elapsed<600 else 'fallback')
                self.assertEqual(result['hold_reason'],reason)
                self.assertEqual(result['held_since'],policy.stamp(start))
                self.assertEqual(result['checked_at'],policy.stamp(now))
                self.assertEqual((self.root/'selection.json').read_text(),saved)
                self.assertNotIn('PRIVATE_ERROR',(self.root/'status.json').read_text())
                if elapsed<600:
                    self.assertEqual(len(self.writes),count)
                    self.assertEqual(result['intended_priorities'],{A:100,B:0})
                else:
                    self.assertEqual([r['priority'] for r in self.rows],[0,0])
                    self.assertEqual(result['intended_priorities'],{A:0,B:0})
            with patch.object(policy.time,'time',return_value=start+602):
                result=policy.reconcile(self.current_overview(start+602),call=self.api,state_dir=self.root)
            self.assertEqual(result['status'],'active')
            self.assertEqual(result['preferred_email'],A)
            self.assertIsNone(result['hold_reason'])
            self.assertIsNone(result['held_since'])
            self.assertEqual([r['priority'] for r in self.rows],[100,0])

    def test_stale_gateway_data_falls_back_and_keeps_the_first_hold_timestamp(self):
        start=1700000000
        with patch.object(policy.time,'time',return_value=start):
            policy.reconcile(self.quota_overview(),{'mode':'highest-remaining-quota'},self.api,self.root)
        for elapsed in (181,781):
            with patch.object(policy.time,'time',return_value=start+elapsed):
                result=policy.reconcile(self.quota_overview(),call=self.api,state_dir=self.root)
            self.assertEqual(result['held_since'],policy.stamp(start+181))
            self.assertEqual(result['status'],'held' if elapsed==181 else 'fallback')
        self.assertEqual([r['priority'] for r in self.rows],[0,0])

    def test_non_quota_hold_preserves_timestamp_without_applying_fallback(self):
        for now in (1700000000,1700000600):
            with patch.object(policy.time,'time',return_value=now):
                result=policy.reconcile({}, {'mode':'highest-remaining-quota'},self.api,self.root)
            self.assertEqual(result['status'],'held')
            self.assertEqual(result['held_since'],policy.stamp(1700000000))
        self.assertEqual(self.writes,[])

    def test_preferred_account_falls_back_and_returns_without_changing_selection(self):
        selected={'mode':'preferred-account','preferred_email':A}
        with patch.object(policy.time,'time',return_value=1700000000):
            policy.reconcile(self.quota_overview(),selected,self.api,self.root)
            result=policy.reconcile(self.quota_overview(0,86),call=self.api,state_dir=self.root)
            self.assertEqual(result['preferred_email'],B)
            self.assertEqual(result['selected_email'],A)
            self.assertIn('no remaining allowance',result['reason'])
            self.assertEqual(result['intended_priorities'],{A:0,B:100})
            self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY),selected)
            result=policy.reconcile(self.quota_overview(1,86),call=self.api,state_dir=self.root)
        self.assertEqual(result['preferred_email'],A)
        self.assertIsNone(result['reason'])
        self.assertEqual([r['priority'] for r in self.rows],[100,0])

    def test_preferred_cooldown_uses_other_account_then_returns_when_ready(self):
        selected={'mode':'preferred-account','preferred_email':B}
        with patch.object(policy.time,'time',return_value=1700000000):
            data=self.quota_overview()
            data['gateway']['accounts'][1].update(active=False,
                availability={'state':'quota_exhausted','scope':'account'})
            result=policy.reconcile(data,selected,self.api,self.root)
            self.assertEqual(result['preferred_email'],A)
            self.assertIn('quota_exhausted',result['reason'])
            self.assertEqual(result['selected_email'],B)
            result=policy.reconcile(self.quota_overview(),call=self.api,state_dir=self.root)
            self.assertEqual(result['preferred_email'],B)
            self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY),selected)

    def test_account_availability_prevents_promotion_in_all_selecting_modes(self):
        for mode in ('earliest-weekly-reset','highest-remaining-quota','preferred-account'):
            for state in ('quota_exhausted','rate_limited','provider_unavailable','auth_required','disabled'):
                data=self.quota_overview();account=data['gateway']['accounts'][1]
                account['windows'][0]['remaining']=99
                account['availability']={'state':state,'scope':'account'}
                selected={'mode':mode,'preferred_email':B}
                with self.subTest(mode=mode,state=state):
                    result=policy.selected_plan(selected,data,1700000000,current=B)
                    self.assertEqual(result['preferred_email'],A)
                    if mode=='preferred-account':self.assertIn(state,result['reason'])
        data=self.quota_overview();account=data['gateway']['accounts'][1]
        account['auth_state']='sign_in_required'
        self.assertEqual(policy.choose(data,1700000000)['preferred_email'],A)

    def test_model_scoped_cooldowns_remain_eligible_despite_inactive_snapshot(self):
        for mode in ('earliest-weekly-reset','highest-remaining-quota','preferred-account'):
            for state in ('rate_limited','provider_unavailable'):
                data=self.quota_overview();account=data['gateway']['accounts'][1]
                account.update(active=False,availability={'state':state,'scope':'model'})
                account['windows'][0]['remaining']=99
                with self.subTest(mode=mode,state=state):
                    result=policy.selected_plan({'mode':mode,'preferred_email':B},data,1700000000)
                    self.assertEqual(result['preferred_email'],B)

    def test_ready_availability_overrides_coarse_inactive_flag(self):
        data=self.quota_overview();account=data['gateway']['accounts'][1]
        account.update(active=False,availability={'state':'ready','scope':'account'})
        account['windows'][0]['remaining']=99
        for mode in ('earliest-weekly-reset','highest-remaining-quota','preferred-account'):
            with self.subTest(mode=mode):
                result=policy.selected_plan({'mode':mode,'preferred_email':B},data,1700000000)
                self.assertEqual(result['preferred_email'],B)

    def test_quota_margin_uses_current_gateway_winner_and_switches_at_five_points(self):
        self.rows[0]['priority']=0;self.rows[1]['priority']=100
        with patch.object(policy.time,'time',return_value=1700000000):
            for a,b,winner in ((5,4.9,B),(9.9,5,B),(10,5,A),(10,10,A),(0,1,B)):
                result=policy.reconcile(self.quota_overview(a,b),{'mode':'highest-remaining-quota'},self.api,self.root)
                self.assertEqual(result['preferred_email'],winner)
                self.assertEqual(result['intended_priorities'],{e:100 if e==winner else 0 for e in (A,B)})

    def test_half_applied_plan_is_persisted_and_repaired_before_stale_data_check(self):
        def fail(path,body=None,method=None):
            if method=='PATCH':
                status=json.loads((self.root/'status.json').read_text())
                self.assertEqual(status['intended_priorities'],{A:0,B:100})
                self.assertTrue(status['pending_plan'])
                if body['name']==A:raise OSError('PRIVATE_ERROR')
            return self.api(path,body,method)
        with patch.object(policy.time,'time',return_value=1700000000),self.assertRaises(OSError):
            policy.reconcile(self.quota_overview(20,90),{'mode':'highest-remaining-quota'},fail,self.root)
        self.assertEqual([r['priority'] for r in self.rows],[100,100])
        data=self.current_overview(1700000001)
        data['gateway']['accounts'][0]['quota_checked_at']=policy.stamp(1700000002)
        with patch.object(policy.time,'time',return_value=1700000001):
            result=policy.reconcile(data,call=self.api,state_dir=self.root)
        self.assertEqual([r['priority'] for r in self.rows],[0,100])
        self.assertEqual(result['intended_priorities'],{A:0,B:100})
        self.assertEqual(result['status'],'held')
        self.assertEqual(result['hold_reason'],'stale quota data for owner@example.test')
        status=json.loads((self.root/'status.json').read_text())
        self.assertFalse(status['pending_plan'])
        self.assertNotIn('PRIVATE_ERROR',json.dumps(status))

    def test_explicit_selection_supersedes_failing_pending_promotion(self):
        def fail(path,body=None,method=None):
            if method=='PATCH' and body=={'name':B,'priority':100}:
                raise OSError('PRIVATE_ERROR')
            return self.api(path,body,method)
        with patch.object(policy.time,'time',return_value=1700000000):
            with self.assertRaises(OSError):
                policy.reconcile(self.quota_overview(),{'mode':'preferred-account','preferred_email':B},fail,self.root)
            with self.assertRaises(OSError):
                policy.reconcile({},call=fail,state_dir=self.root)  # Timer keeps repairing the saved plan.
            result=policy.reconcile(self.quota_overview(),{'mode':'preferred-account','preferred_email':A},fail,self.root)
        self.assertEqual(result['status'],'active')
        self.assertEqual(result['preferred_email'],A)
        self.assertEqual([r['priority'] for r in self.rows],[100,0])
        self.assertFalse(json.loads((self.root/'status.json').read_text())['pending_plan'])
        self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY),{'mode':'preferred-account','preferred_email':A})

    def test_round_robin_selection_replaces_half_applied_plan(self):
        def fail(path,body=None,method=None):
            if method=='PATCH' and body=={'name':A,'priority':0}:
                raise OSError('unavailable')
            return self.api(path,body,method)
        with patch.object(policy.time,'time',return_value=1700000000):
            with self.assertRaises(OSError):
                policy.reconcile(self.quota_overview(),{'mode':'preferred-account','preferred_email':B},fail,self.root)
            self.assertEqual([r['priority'] for r in self.rows],[100,100])
            result=policy.reconcile({},{'mode':'round-robin'},self.api,self.root)
        self.assertEqual(result['status'],'active')
        self.assertEqual([r['priority'] for r in self.rows],[0,0])

    def test_new_selection_with_stale_data_cancels_obsolete_pending_plan(self):
        policy.write_json(self.root/'status.json',{
            'policy':'preferred-account','selected_email':B,'preferred_email':B,
            'pending_plan':True,'intended_priorities':{A:0,B:100},
            'held_since':policy.stamp(1699999000)})
        with patch.object(policy.time,'time',return_value=1700000000):
            result=policy.reconcile({}, {'mode':'preferred-account','preferred_email':A},self.api,self.root)
            self.assertEqual(result['status'],'held')
            self.assertEqual(result['held_since'],policy.stamp(1700000000))
            self.assertFalse(json.loads((self.root/'status.json').read_text())['pending_plan'])
            self.assertEqual(self.writes,[])
            result=policy.reconcile(self.quota_overview(),call=self.api,state_dir=self.root)
        self.assertEqual(result['preferred_email'],A)
        self.assertEqual(self.writes,[])

    def test_rollback_suspends_saved_selection_until_dashboard_saves_a_new_one(self):
        selected={'mode':'preferred-account','preferred_email':B}
        with patch.object(policy.time,'time',return_value=1700000000):
            policy.reconcile(self.quota_overview(),selected,self.api,self.root)
            policy.rollback(self.api,self.root)
        self.assertEqual(policy.selection(self.root, inventory=CODEX_INVENTORY),selected)
        self.assertTrue(policy.suspended(self.root))
        def no_gateway(*args):self.fail('Suspended reconciliation called the gateway')
        for now in (1700000001,1700003600):
            with patch.object(policy.time,'time',return_value=now):
                result=policy.reconcile({},call=no_gateway,state_dir=self.root)
            self.assertEqual(result['status'],'disabled')
            self.assertTrue(result['suspended'])
            self.assertIsNone(result['held_since'])
            self.assertEqual([r['priority'] for r in self.rows],[100,0])
        with patch.object(policy.time,'time',return_value=1700003601):
            result=policy.reconcile(self.current_overview(1700003601),selected,self.api,self.root)
        self.assertEqual(result['status'],'active')
        self.assertFalse(result['suspended'])
        self.assertFalse(policy.suspended(self.root))
        self.assertEqual([r['priority'] for r in self.rows],[0,100])

    def test_failed_rollback_suspends_pending_repairs_and_fallback(self):
        policy.write_json(self.root/'status.json',{
            'status':'held','pending_plan':True,'intended_priorities':{A:0,B:100},
            'held_since':policy.stamp(1699999000)})
        def fail(path,body=None,method=None):
            if method=='PATCH':raise OSError('unavailable')
            return self.api(path,body,method)
        with self.assertRaises(OSError):policy.rollback(fail,self.root)
        def no_gateway(*args):self.fail('Rollback suspension called the gateway')
        with patch.object(policy.time,'time',return_value=1700000000):
            result=policy.reconcile(self.quota_overview(),call=no_gateway,state_dir=self.root)
        self.assertEqual(result['status'],'disabled')
        self.assertIsNone(result['hold_reason'])
        self.assertEqual(self.writes,[])

    def test_suspended_timer_skips_fetch_and_reports_disabled(self):
        with patch.object(policy,'selection',return_value={'mode':'preferred-account','preferred_email':B}), \
                patch.object(policy,'suspended',return_value=True), \
                patch.object(policy,'urlopen') as fetch, \
                patch.object(policy,'reconcile',return_value={'status':'disabled'}) as reconcile, \
                patch.object(sys,'argv',['routing-policy','--apply']),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(policy.main(),0)
        fetch.assert_not_called()
        reconcile.assert_called_once_with({})
        self.assertEqual(json.loads(output.getvalue())['status'],'disabled')

    def test_timer_fetch_failure_still_reconciles_to_repair_pending_intent(self):
        with patch.object(policy,'selection',return_value={'mode':'highest-remaining-quota'}), \
                patch.object(policy,'suspended',return_value=False), \
                patch.object(policy,'urlopen',side_effect=OSError('private')), \
                patch.object(policy,'reconcile',return_value={'status':'held'}) as reconcile, \
                patch.object(sys,'argv',['routing-policy','--apply']),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(policy.main(),0)
        reconcile.assert_called_once_with({}, inventory=CODEX_INVENTORY)

    def test_preferred_mode_timer_fetches_overview(self):
        data=self.quota_overview()
        with patch.object(policy,'selection',return_value={'mode':'preferred-account','preferred_email':B}), \
                patch.object(policy,'suspended',return_value=False), \
                patch.object(policy,'urlopen',return_value=io.BytesIO(json.dumps(data).encode())) as fetch, \
                patch.object(policy,'reconcile',return_value={'status':'active'}) as reconcile, \
                patch.object(sys,'argv',['routing-policy','--apply']),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(policy.main(),0)
        self.assertEqual(fetch.call_args.args[0].full_url,'http://127.0.0.1:8320/api/overview')
        reconcile.assert_called_once_with(data, inventory=CODEX_INVENTORY)

    def test_cli_and_timer_wrapper_accept_expected_controller_states(self):
        wrapper=Path(__file__).resolve().parents[1]/'scripts/prefer-earliest-reset.py'
        for entrypoint in (Path(policy.__file__),wrapper):
            for status in ('held','fallback','disabled','active'):
                result={'status':status,'hold_reason':'Quota data stale' if status=='held' else None}
                with self.subTest(entrypoint=entrypoint.name,status=status), \
                        patch.object(policy,'selection',return_value={'mode':'round-robin'}), \
                        patch.object(policy,'reconcile',return_value=result), \
                        patch.object(sys,'argv',[str(entrypoint),'--apply']), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit) as exited:
                        if entrypoint == Path(policy.__file__):
                            raise SystemExit(policy.main())
                        runpy.run_path(str(entrypoint),run_name='__main__')
                self.assertEqual(exited.exception.code,0)
                self.assertEqual(json.loads(output.getvalue()),result)

    def test_timer_wrapper_real_failures_exit_nonzero_without_private_details(self):
        wrapper=Path(__file__).resolve().parents[1]/'scripts/prefer-earliest-reset.py'
        for failure in (OSError('PRIVATE_ERROR'),policy.Hold('Gateway strategy changed')):
            with self.subTest(failure=type(failure)), \
                    patch.object(policy,'selection',return_value={'mode':'round-robin'}), \
                    patch.object(policy,'reconcile',side_effect=failure), \
                    patch.object(sys,'argv',[str(wrapper),'--apply']), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(str(wrapper),run_name='__main__')
            self.assertEqual(exited.exception.code,1)
            self.assertNotIn('PRIVATE_ERROR',output.getvalue())

    def test_quota_mode_timer_fetches_overview(self):
        data=self.quota_overview()
        with patch.object(policy,'selection',return_value={'mode':'highest-remaining-quota'}), \
                patch.object(policy,'urlopen',return_value=io.BytesIO(json.dumps(data).encode())) as fetch, \
                patch.object(policy,'reconcile',return_value={'status':'active'}) as reconcile, \
                patch.object(sys,'argv',['routing-policy','--apply']),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(policy.main(),0)
        self.assertEqual(fetch.call_args.args[0].full_url,'http://127.0.0.1:8320/api/overview')
        reconcile.assert_called_once_with(data, inventory=CODEX_INVENTORY)
