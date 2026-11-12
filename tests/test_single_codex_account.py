"""Inventory membership, provider isolation and legacy email-keyed routing state."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import providers
import routing_policy as policy
import server
from codex_fixture import CODEX_PAIR

A, B = CODEX_PAIR
C = 'third@example.test'
NOW = 1700000000


def inventory(codex=1, claude=2, xai=False):
    rows = [dict(type=p, name=f'{p}-{i}.json', auth_index=f'{p}-{i}', email=email,
                 priority=0, disabled=False, status='active')
            for p, emails in (('codex', (A, B)[:codex]), ('claude', (A, B, C)[:claude]))
            for i, email in enumerate(emails)]
    if xai:
        rows.append(dict(type='xai', name='xai-service.json', auth_index='xai-0', status='active', disabled=False))
    return rows


def observed(rows, watches=2):
    stamp = policy.stamp(NOW)
    codex = [dict(id=r['auth_index'], key=r['name'], email=r['email'], active=True,
                  quota_checked_at=stamp, windows=[dict(seconds=604800, remaining=50, resets_at=NOW+100000)])
             for r in rows if r['type']=='codex']
    watched = [dict(id='watch-'+str(i), email=r['email'], status='ok', checked_at=stamp,
                    windows=[dict(kind=kind, remaining=60-i*10, resets_at=NOW+offset+i*10000)
                             for kind, offset in (('five_hour', 10000), ('seven_day', 100000))])
               for i, r in enumerate(r for r in rows if r['type']=='claude') if i<watches]
    return dict(gateway=dict(online=True, checked_at=stamp, inventory=providers.inventory_view(rows),
                             accounts=codex, claude_accounts=server.claude_routing_projection(rows, stamp)),
                watch_accounts=watched)


class SingleCodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = inventory()
        self.writes = []

    def call(self, path, body=None, method=None):
        if path=='/auth-files':
            return {'files':copy.deepcopy(self.rows)}
        if path=='/config':
            return {'routing':{'strategy':'round-robin'}}
        self.assertEqual((path,method),('/auth-files/fields','PATCH'))
        self.writes.append(copy.deepcopy(body))
        next(r for r in self.rows if r['name']==body['name']).update(priority=body['priority'])
        return {'status':'ok'}

    def reconcile(self, selected=None, provider='codex', data=None):
        with patch.object(policy.time,'time',return_value=NOW):
            return policy.reconcile(observed(self.rows) if data is None else data, selected,
                                    self.call, self.root/provider, provider)

    def test_single_account_all_modes_neutral_and_other_providers_untouched(self):
        self.rows[0]['priority']=100
        original=copy.deepcopy(self.rows[1:])
        for mode in policy.MODES:
            result=self.reconcile(dict(mode=mode,preferred_email=A),data={})
            self.assertEqual(result['status'],'active')
            self.assertEqual(result['intended_priorities'],{A:0})
            self.assertIsNone(result['preferred_email'])
        self.assertEqual(self.writes,[dict(name='codex-0.json',priority=0)])
        self.assertEqual(self.rows[1:],original)

    def test_disabled_single_account_needs_no_quota_and_is_never_enabled(self):
        self.rows[0].update(disabled=True,priority=100)
        self.assertEqual(self.reconcile({'mode':'highest-remaining-quota'},data={})['status'],'active')
        self.assertTrue(self.rows[0]['disabled'])

    def test_zero_inventory_is_idle_and_renders_no_providers(self):
        self.rows=[]
        for provider in policy.PROVIDERS:
            result=self.reconcile(provider=provider)
            self.assertEqual(result['status'],'idle')
            self.assertEqual(result['intended_priorities'],{})
        self.assertEqual(providers.groups(observed([])['gateway'],[]),[])
        self.assertFalse(self.writes)

    def test_unavailable_inventory_is_never_treated_as_empty(self):
        for value in ({}, {'files':None}):
            with self.subTest(value=value), self.assertRaises(policy.Hold):
                policy.reconcile({},call=lambda *args:value,state_dir=self.root)
        with self.assertRaises(OSError):
            policy.reconcile({},call=lambda *args:(_ for _ in ()).throw(OSError()),state_dir=self.root)
        self.assertFalse((self.root/'status.json').exists())

    def test_inventory_is_sorted_by_filename_and_includes_disabled_accounts(self):
        self.rows=inventory(codex=2)
        self.rows[1]['disabled']=True
        self.assertEqual(policy.identities('codex',list(reversed(self.rows))),(A,B))
        self.assertEqual(list(policy.records(inventory=self.rows)),[A,B])

    def test_replaced_codex_auth_index_discards_cached_quota(self):
        self.rows=inventory(codex=2)
        data=observed(self.rows)
        data['gateway']['accounts'][0]['windows'][0]['remaining']=90
        self.assertEqual(self.reconcile({'mode':'highest-remaining-quota'},data=data)['preferred_email'],A)
        self.rows[0]['auth_index']='replacement-codex-0'
        candidates,excluded=policy.eligible_accounts(data,NOW,inventory=self.rows)
        self.assertEqual(set(candidates),{B})
        self.assertEqual(excluded,{A:'no_data'})
        for mode in ('earliest-weekly-reset','highest-remaining-quota','preferred-account'):
            with self.subTest(mode=mode):
                result=self.reconcile(dict(mode=mode,preferred_email=A),data=data)
                self.assertEqual(result['preferred_email'],B)
                self.assertEqual(result['intended_priorities'],{A:0,B:100})
        fresh=observed(self.rows)
        fresh['gateway']['accounts'][0]['windows'][0]['remaining']=90
        self.assertEqual(self.reconcile({'mode':'highest-remaining-quota'},data=fresh)['preferred_email'],A)

    def test_duplicate_email_across_providers_has_distinct_filename_keys(self):
        data=observed(self.rows)
        groups=providers.groups(data['gateway'],data['watch_accounts'])
        self.assertEqual([p['id'] for p in groups],['codex','claude'])
        # Default display order follows labels/emails, not auth filenames.
        codex=next(a for a in groups[0]['accounts'] if a['key']=='codex-0.json')
        claude=next(a for a in groups[1]['accounts'] if a['key']=='claude-0.json')
        self.assertEqual(codex['email'],A)
        self.assertEqual(claude['email'],A)
        self.assertNotEqual(codex['key'],claude['key'])
        self.assertFalse(groups[0]['routable'])
        self.assertTrue(groups[1]['routable'])

    def test_email_less_xai_and_unknown_providers_have_no_controls(self):
        self.rows=inventory(xai=True)
        self.rows += [dict(type='kimi',name='kimi.json',status='error'),dict(type='antigravity',name='anti.json',disabled=True),
                      dict(type='mystery',name='mystery.json')]
        data=observed(self.rows)
        groups=providers.groups(data['gateway'],data['watch_accounts'])
        self.assertEqual([g['id'] for g in groups],['codex','claude','antigravity','kimi','mystery','xai'])
        for p in groups[2:]:
            self.assertEqual(p['features'],[])
            self.assertFalse(p['routable'])
            self.assertEqual(p['accent'],{'xai':'xai','kimi':'kimi','antigravity':'antigravity'}.get(p['id'],'neutral'))
        self.assertEqual((groups[-1]['name'],groups[-2]['name']),('Grok','mystery'))
        xai=groups[-1]['accounts'][0]
        self.assertEqual((xai['key'],xai['label']),('xai-service.json','xai-service.json'))
        self.assertNotIn('email',xai)
        self.assertEqual(xai['availability']['state'],'ready')

    def test_three_claude_two_watches_rank_unobserved_last(self):
        self.rows=inventory(claude=3)
        data=observed(self.rows)
        candidates,excluded=policy.eligible_accounts(data,NOW,'claude',self.rows)
        self.assertEqual(set(candidates),{A,B})
        self.assertIn(C,excluded)
        for mode in ('earliest-weekly-reset','highest-remaining-quota','preferred-account'):
            result=self.reconcile(dict(mode=mode,preferred_email=C),provider='claude')
            self.assertEqual(result['status'],'active')
            self.assertIn(result['preferred_email'],(A,B))
            self.assertEqual(result['intended_priorities'][C],0)
        public=providers.groups(data['gateway'],data['watch_accounts'])[1]['accounts']
        self.assertEqual(len(public),3)
        self.assertEqual(public[2]['key'],'claude-2.json')
        self.assertNotIn('windows',public[2])
        self.assertFalse(public[2]['watch_observed'])
        self.assertEqual(public[2]['status'],'active')

    def test_tied_leaders_keep_other_accounts_demoted(self):
        for mode in ('earliest-weekly-reset', 'highest-remaining-quota'):
            for third in ('absent', 'lower-ranked', 'unobserved', 'disabled'):
                with self.subTest(mode=mode, third=third):
                    rows=inventory(claude=2 if third=='absent' else 3)
                    if third=='disabled':
                        rows[-1]['disabled']=True
                    data=observed(rows, watches=2 if third=='unobserved' else 3)
                    for watch in data['watch_accounts'][:2]:
                        for window in watch['windows']:
                            window.update(remaining=60, resets_at=NOW+100000)
                    plan=policy.choose(data,NOW,mode,provider='claude',inventory=rows)
                    self.assertIsNone(plan['preferred_email'])
                    self.assertEqual(plan['priorities'], {A:0,B:0} if third=='absent' else {A:100,B:100,C:0})

    def test_watch_matching_is_by_unique_email_and_not_watch_id(self):
        self.rows=inventory(claude=3)
        data=observed(self.rows)
        data['watch_accounts'].reverse()
        for w in data['watch_accounts']:w['id']='arbitrary-'+w['email']
        candidates,_=policy.eligible_accounts(data,NOW,'claude',self.rows)
        self.assertEqual(set(candidates),{A,B})
        data['watch_accounts'].append(copy.deepcopy(data['watch_accounts'][0]))
        candidates,excluded=policy.eligible_accounts(data,NOW,'claude',self.rows)
        self.assertEqual(set(candidates),{A})
        self.assertEqual(set(excluded),{B,C})

    def test_claude_inventory_accepts_new_members_removed_members_and_arbitrary_notes(self):
        self.rows=inventory(claude=3)
        for row in self.rows:
            row['note']='A label is not an identity'
        result=self.reconcile(dict(mode='round-robin'),provider='claude')
        self.assertEqual(result['intended_priorities'],{A:0,B:0,C:0})
        self.rows=[r for r in self.rows if r.get('email')!=B]
        self.assertEqual(self.reconcile(provider='claude')['intended_priorities'],{A:0,C:0})

    def test_no_email_or_duplicate_email_provider_cannot_route(self):
        for change in ('missing','duplicate'):
            rows=inventory(claude=3)
            rows[-1]['email']=None if change=='missing' else A
            with self.assertRaises(policy.Hold):policy.records(provider='claude',inventory=rows)
            data=observed(rows)
            self.assertFalse(providers.groups(data['gateway'],data['watch_accounts'])[1]['routable'])

    def test_retired_preference_defaults_for_each_provider(self):
        for provider in policy.PROVIDERS:
            directory=self.root/provider
            directory.mkdir()
            policy.write_json(directory/'selection.json',dict(mode='preferred-account',preferred_email='removed@example.test'))
            result=self.reconcile(provider=provider)
            self.assertEqual(result['policy'],policy.PROVIDERS[provider]['default'])
            self.assertEqual(result['status'],'active')
            with self.assertRaises(ValueError):
                policy.validate_selection(dict(mode='preferred-account',preferred_email='removed@example.test'),provider,self.rows)

    def test_filename_preference_api_preserves_email_keyed_state(self):
        result=self.reconcile(dict(mode='preferred-account',preferred_key='claude-1.json'),provider='claude')
        self.assertEqual(result['selected_email'],B)
        saved=json.loads((self.root/'claude/selection.json').read_text())
        self.assertEqual(saved,dict(mode='preferred-account',preferred_email=B))
        with self.assertRaises(ValueError):
            policy.validate_selection(dict(mode='preferred-account',preferred_key='codex-0.json'),'claude',self.rows)

    def test_membership_change_invalidates_pending_plan(self):
        self.rows=inventory(codex=2)
        self.reconcile(dict(mode='round-robin'))
        state=policy.read_status(self.root/'codex')
        state.update(pending_plan=True,intended_priorities={A:0,B:100},preferred_email=B,plan_status='active')
        policy.write_json(self.root/'codex/status.json',state)
        self.rows=[r for r in self.rows if r['type']!='codex' or r['email']!=B]
        self.writes.clear()
        result=self.reconcile(data={})
        self.assertEqual(result['intended_priorities'],{A:0})
        self.assertFalse(policy.read_status(self.root/'codex')['pending_plan'])
        self.assertFalse(self.writes)

    def test_rollback_restores_only_current_snapshot_and_email_state(self):
        self.rows[0]['priority']=100
        self.reconcile({'mode':'round-robin'})
        baseline=json.loads((self.root/'codex/baseline.json').read_text())
        self.assertEqual(baseline,{A:dict(name='codex-0.json',priority=100)})
        baseline[B]=dict(name='removed.json',priority=0)
        policy.write_json(self.root/'codex/baseline.json',baseline)
        untouched=copy.deepcopy(self.rows[1:])
        result=policy.rollback(self.call,self.root/'codex')
        self.assertEqual(result,dict(status='restored',policy='baseline'))
        self.assertEqual(self.rows[0]['priority'],100)
        self.assertEqual(self.rows[1:],untouched)


if __name__=='__main__':unittest.main()
