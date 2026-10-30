"""Mixed gateway providers must never enter Codex controls or activity."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import account_access
import routing_policy
import server
import signin
from codex_fixture import use_codex_pair


def setUpModule():
    use_codex_pair()

spec = importlib.util.spec_from_file_location('mixed_provider_collector', ROOT / 'scripts/collect-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class ClaudeGatewayCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = [dict(type='codex', name=f'codex-{i}.json', auth_index=f'codex-{i}',
                           email=email, priority=0, disabled=False,
                           id_token={'plan_type': 'pro', 'chatgpt_account_id': f'workspace-{i}'})
                      for i, email in enumerate(routing_policy.EMAILS)]
        # Reusing an email must not make Claude part of a Codex identity or policy.
        self.claude = dict(type='claude', name='claude-a.json', auth_index='claude-a',
                           email=routing_policy.EMAILS[0], priority=100, disabled=False,
                           id_token={'plan_type': 'free'})
        self.rows = self.codex + [self.claude]
        self.calls = []

    def call(self, path, body=None, method=None):
        self.calls.append((path, body, method))
        if path == '/auth-files':
            return {'files': copy.deepcopy(self.rows)}
        if path == '/config':
            return {'routing': {'strategy': 'round-robin', 'session-affinity': True}}
        if path == '/api-call':
            self.assertIn(body['auth_index'], {r['auth_index'] for r in self.codex})
            return {'status_code': 200, 'body': {'plan_type': 'pro', 'rate_limit': {
                'primary_window': {'used_percent': 10, 'limit_window_seconds': 604800}}}}
        if path == '/auth-files/fields':
            self.assertEqual(method, 'PATCH')
            next(r for r in self.codex if r['name'] == body['name'])['priority'] = body['priority']
            return {'status': 'ok'}
        self.fail('Unexpected management call: ' + path)

    def test_poll_excludes_claude_from_accounts_quota_resets_and_health(self):
        state = server.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        with patch.object(server, 'api', side_effect=self.call), \
                patch.object(routing_policy, 'reconcile'):
            state._fetch()
        self.assertTrue(state.data['online'])
        self.assertEqual({a['id'] for a in state.data['accounts']}, {'codex-0', 'codex-1'})
        self.assertEqual({c.args[0]['type'] for c in state.resets.fetch.call_args_list}, {'codex'})
        self.assertFalse(any(method == 'PATCH' for _, _, method in self.calls))
        health = server.health(state.data, directory=self.root)
        self.assertEqual(len(health['accounts']), 2)
        self.assertEqual(self.claude['priority'], 100)

    def test_timer_and_ui_routing_leave_claude_priority_untouched(self):
        original = copy.deepcopy(self.claude)
        self.codex[0]['priority'] = 100
        result = routing_policy.reconcile({}, {'mode': 'round-robin'}, self.call, self.root)
        self.assertEqual(result['status'], 'active')
        routing_policy.reconcile({}, call=self.call, state_dir=self.root)
        self.assertEqual(self.claude, original)
        self.assertEqual([r['priority'] for r in self.codex], [0, 0])

    def test_claude_timer_and_ui_routing_leave_codex_priorities_untouched(self):
        self.rows.append(dict(self.claude, name='claude-b.json', auth_index='claude-b',
                              email=routing_policy.CLAUDE_EMAILS[1], priority=0))
        original = copy.deepcopy(self.codex)
        def call(path, body=None, method=None):
            if method == 'PATCH':
                self.assertEqual(path, '/auth-files/fields')
                self.assertTrue(body['name'].startswith('claude-'))
                next(r for r in self.rows if r['name'] == body['name'])['priority'] = body['priority']
                return {'status': 'ok'}
            return self.call(path, body, method)
        result = routing_policy.reconcile({}, {'mode': 'round-robin'}, call,
                                          self.root/'claude', provider='claude')
        self.assertEqual(result['status'], 'active')
        routing_policy.reconcile({}, call=call, state_dir=self.root/'claude', provider='claude')
        self.assertEqual(self.codex, original)
        self.assertEqual([r['priority'] for r in self.rows if r['type'] == 'claude'], [0, 0])

    def test_signin_cannot_resolve_claude_even_with_matching_identity(self):
        manager = signin.SigninManager(self.call, threading.RLock(), Mock())
        self.addCleanup(manager.close)
        with self.assertRaises(signin.AccessError):
            manager.resolve(self.claude['auth_index'], account_access.identity(self.claude))
        self.assertFalse(any(method == 'PATCH' for _, _, method in self.calls))

    def test_access_poll_filters_non_codex_and_never_refreshes_its_plan(self):
        access = account_access.AccountAccess()
        call = Mock()
        self.assertEqual(access.reconcile(self.rows, call), self.codex)
        access.maybe_refresh(self.claude, 'pro', call)
        call.assert_not_called()

    def test_access_status_refuses_claude_before_sending_a_write(self):
        call = Mock()
        with self.assertRaises(ValueError):
            account_access.set_disabled_verified(self.claude, True, call)
        call.assert_not_called()

    def test_collector_preserves_provider_and_codex_views_exclude_other_providers(self):
        db, key = self.root / 'usage.sqlite3', self.root / 'key'
        key.write_text('synthetic-client-key')
        events = [dict(provider=provider, model=model, auth_index=account,
                       timestamp=at, tokens={'total_tokens': tokens})
                  for provider, model, account, at, tokens in [
                      ('codex', 'gpt-6', 'codex-0', '2026-10-15T13:00:00Z', 10),
                      ('claude', 'claude-opus-4-8', 'claude-a', '2026-10-15T14:00:00Z', 900),
                      ('gemini', 'gemini', 'other', '2026-10-01T00:00:00Z', 800)]]
        with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())), \
                contextlib.redirect_stdout(io.StringIO()):
            collector.collect(db, key)
        with contextlib.closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute('SELECT provider FROM usage ORDER BY id').fetchall(),
                             [('codex',), ('claude',), ('gemini',)])
            con.execute("UPDATE usage SET collected_at='2099-01-01' WHERE provider='claude'")
            con.commit()
        report = server.usage_summary('1970-01-01T00:00:00Z', db)
        self.assertTrue(report['available'])
        self.assertEqual(report['since'], '2026-10-15T13:00:00Z')
        self.assertNotEqual(report['last_collected_at'], '2099-01-01')
        self.assertEqual([r['auth_index'] for r in report['accounts']], ['codex-0'])
        self.assertEqual([r['model'] for r in report['models']], ['gpt-6'])
        # Recent requests show both gateways, newest first; other providers stay out.
        self.assertEqual([(r['provider'], r['auth_index']) for r in report['recent']],
                         [('claude', 'claude-a'), ('codex', 'codex-0')])
        self.assertEqual([r['auth_index'] for r in report['hours']], ['codex-0'])
        self.assertEqual(report['accounts'][0]['total_tokens'], 10)

    def test_usage_retains_legacy_rows_without_provider(self):
        db = self.root / 'usage.sqlite3'
        with contextlib.closing(collector.connect(db)) as con:
            for provider in (None, '', 'codex', 'claude'):
                con.execute('INSERT INTO usage (collected_at,timestamp,provider,auth_index) VALUES (?,?,?,?)',
                            ('2026-10-15', '2026-10-15T00:00:00Z', provider, str(provider)))
            con.commit()
        report = server.usage_summary('1970-01-01T00:00:00Z', db)
        self.assertEqual(sum(r['requests'] for r in report['accounts']), 3)
