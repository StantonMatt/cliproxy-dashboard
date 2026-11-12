"""Speed levels (normal / fast / ultrafast), level history and the model catalog."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import fast_mode as fast
import codex_catalog as catalog_module
import server as app

spec = importlib.util.spec_from_file_location('speed_usage_collector', ROOT / 'scripts/collect-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

INSERT = '''INSERT INTO usage (collected_at,timestamp,auth_index,model,service_tier,response_service_tier)
    VALUES (?,?,?,?,?,?)'''


def post(gateway, data, headers=None):
    handler = object.__new__(app.Handler)
    handler.path = '/api/fast-mode'
    content = json.dumps(data).encode()
    handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                       'Content-Type': 'application/json', 'Content-Length': str(len(content)),
                       **(headers or {})}
    handler.rfile = io.BytesIO(content)
    handler.connection = Mock()
    handler.server = Mock()
    handler.server.state = gateway
    handler.send = Mock()
    handler.do_POST()
    return handler.send.call_args.args[0], json.loads(handler.send.call_args.args[1])


class SpeedLevelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.now = 1790985600
        self.manager = fast.FastModeManager(self.base / 'controls.json', self.base / 'policy.json',
                                            self.base / 'status.json')
        self.sol = {'known': True, 'status': 'ok', 'fetched_at': fast.stamp(self.now),
                    'ultrafast_models': [{'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol'},
                                         {'id': 'gpt-6-astra', 'name': 'GPT-6-Astra'}]}
        self.accounts = [fast.fast_account('index1', 'one@example.test', 'codex-one.json', self.sol),
                         fast.fast_account('index2', 'two@example.test', 'codex-two.json',
                                           {'known': True, 'status': 'ok', 'ultrafast_models': []})]
        self.db = self.base / 'usage.sqlite3'
        self.status(2)

    def status(self, schema):
        value = {'plugin': 'fast-mode-policy', 'pid': 321, 'policy_path': str(self.manager.policy_path)}
        if schema is not None:
            value['policy_schema'] = schema
        self.manager.status_path.write_text(json.dumps(value))

    def at(self, offset, level, account='index1', accounts=None):
        with patch.object(fast, 'enforcement', return_value='active'), \
                patch.object(fast.time, 'time', return_value=self.now + offset):
            return self.manager.set_level(account, level, accounts or self.accounts)

    def test_v1_state_migrates_to_levels_and_old_history_infers_as_before(self):
        self.manager.path.write_text(json.dumps({'accounts': {
            'one@example.test': {'allow_fast': False, 'changed_at': fast.stamp(self.now),
                                 'history': [{'changed_at': fast.stamp(self.now - 10), 'allow_fast': False},
                                             {'changed_at': fast.stamp(self.now - 5), 'allow_fast': True},
                                             {'changed_at': fast.stamp(self.now), 'allow_fast': False}]},
            'two@example.test': {'allow_fast': True, 'changed_at': None}}}))
        self.manager.reconcile(self.accounts)
        self.assertEqual(self.manager.setting('one@example.test'),
                         {'allowed': False, 'level': 'normal', 'changed_at': fast.stamp(self.now)})
        self.assertEqual(self.manager.setting('two@example.test')['level'], 'fast')
        self.assertEqual(self.manager.setting('new@example.test')['level'], 'fast')
        for offset, level in ((-11, 'fast'), (-10, 'normal'), (-6, 'normal'), (-5, 'fast'), (0, 'normal'), (9, 'normal')):
            with self.subTest(offset=offset):
                self.assertEqual(self.manager.level_at('index1', fast.stamp(self.now + offset)), (level, []))
        for requested in ('normal', 'priority', 'ultrafast', 'unknown', None):
            for offset in (-11, -10, -5):
                with self.subTest(requested=requested, offset=offset):
                    allowed = self.manager.allowed_at('index1', fast.stamp(self.now + offset))
                    old = fast.forwarded_tier(requested, allowed, False)
                    view = self.manager.request_view('index1', fast.stamp(self.now + offset), requested, 'gpt-6.1-sol')
                    self.assertEqual(view['forwarded_service_tier'], old)
                    self.assertEqual(view['fast_mode_allowed'], allowed)
        policy = json.loads(self.manager.policy_path.read_text())
        self.assertEqual(policy['accounts']['index1'], {'level': 'normal', 'ultrafast_models': ['gpt-6-astra', 'gpt-6.1-sol'],
                                                        'allow_fast': False, 'supports_ultrafast': False})
        # Saving keeps allow_fast on the setting and every history entry, so an older dashboard can still load it.
        self.at(20, 'fast')
        saved = json.loads(self.manager.path.read_text())['accounts']['one@example.test']
        self.assertEqual((saved['level'], saved['allow_fast']), ('fast', True))
        self.assertTrue(all(type(h['allow_fast']) is bool for h in saved['history']))

    def test_policy_mirrors_each_level_for_old_libraries(self):
        for level, allow, supports in (('normal', False, False), ('fast', True, False), ('ultrafast', True, True)):
            with self.subTest(level=level):
                self.at(0, level)
                policy = json.loads(self.manager.policy_path.read_text())
                self.assertEqual(policy['version'], 2)
                entry = policy['accounts']['index1']
                self.assertEqual(entry, {'level': level, 'ultrafast_models': ['gpt-6-astra', 'gpt-6.1-sol'],
                                         'allow_fast': allow, 'supports_ultrafast': supports})
                self.assertEqual(policy['files']['codex-one.json'], entry)
        self.assertEqual(json.loads(self.manager.policy_path.read_text())['accounts']['index2'],
                         {'level': 'fast', 'ultrafast_models': [], 'allow_fast': True, 'supports_ultrafast': False})

    def test_legacy_ultrafast_inference_survives_migration_and_new_level_changes(self):
        self.manager.path.write_text(json.dumps({'accounts': {'one@example.test': {
            'allow_fast': True, 'changed_at': fast.stamp(self.now - 5),
            'history': [{'changed_at': fast.stamp(self.now - 10), 'allow_fast': False},
                        {'changed_at': fast.stamp(self.now - 5), 'allow_fast': True}]}}}))
        self.accounts[0]['supports_ultrafast'] = True
        with patch.object(fast.time, 'time', return_value=self.now + 6):
            self.manager.reconcile(self.accounts)
        self.at(10, 'normal')
        self.at(20, 'fast')
        for offset, expected in ((-11, 'ultrafast'), (-10, 'normal'), (-5, 'ultrafast'),
                                 (5, 'ultrafast'), (10, 'normal'), (20, 'priority')):
            with self.subTest(offset=offset):
                view = self.manager.request_view('index1', fast.stamp(self.now + offset),
                                                 'ultrafast', 'model-not-in-catalog')
                self.assertEqual(view['forwarded_service_tier'], expected)
        self.assertEqual(self.manager.request_view('index1', fast.stamp(self.now + 5),
                                                   'priority', 'gpt-6.1-sol')['forwarded_service_tier'], 'priority')
        self.assertFalse(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['supports_ultrafast'])
        gateway = app.GatewayState(Mock(), Mock(), self.manager)
        self.addCleanup(gateway.signins.close)
        gateway.data['accounts'] = [{'id': 'index1', 'plan': 'Pro (25x)'}]
        files = [dict(type='codex', auth_index='index1', email='one@example.test', name='codex-one.json')]
        with gateway.lock:
            self.manager.reconcile(gateway.fast_accounts(files))
        self.assertEqual(self.manager.request_view('index1', fast.stamp(self.now + 5),
                                                   'ultrafast')['forwarded_service_tier'], 'ultrafast')
        self.assertFalse(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['supports_ultrafast'])

    def test_migrated_on_account_switches_inference_at_policy_publication(self):
        self.manager.path.write_text(json.dumps({'accounts': {'one@example.test': {
            'allow_fast': True, 'changed_at': fast.stamp(self.now - 5)}}}))
        self.accounts[0]['supports_ultrafast'] = True
        self.status(1)
        with patch.object(fast.time, 'time', return_value=self.now):
            self.manager.reconcile(self.accounts)
        # The boundary follows publication even if the new plugin loads later.
        self.status(2)
        for offset, expected in ((-6, 'ultrafast'), (-1, 'ultrafast'), (0, 'priority'), (1, 'priority')):
            for model in ('model-not-in-catalog', 'gpt-6.1-sol'):
                with self.subTest(offset=offset, model=model):
                    view = self.manager.request_view('index1', fast.stamp(self.now + offset), 'ultrafast', model)
                    self.assertEqual(view['forwarded_service_tier'], expected)
        history = self.manager.history('one@example.test')
        self.assertEqual(history, [
            {'changed_at': fast.stamp(self.now - 5), 'allow_fast': True},
            {'changed_at': fast.stamp(self.now), 'allow_fast': True, 'level': 'fast',
             'ultrafast_models': ['gpt-6-astra', 'gpt-6.1-sol']}])
        self.assertEqual(self.manager.setting('one@example.test')['changed_at'], fast.stamp(self.now - 5))
        rebuilt = fast.FastModeManager(self.manager.path, self.manager.policy_path, self.manager.status_path)
        with patch.object(fast.time, 'time', return_value=self.now + 10):
            rebuilt.reconcile(self.accounts)
        self.assertEqual(rebuilt.history('one@example.test'), history)

    def test_migration_boundary_requires_policy_publication_and_retries_controls(self):
        original_state = {'accounts': {'one@example.test': {
            'allow_fast': True, 'changed_at': fast.stamp(self.now - 5),
            'history': [{'changed_at': fast.stamp(self.now - 5), 'allow_fast': True}]}}}
        self.manager.path.write_text(json.dumps(original_state))
        self.accounts[0]['supports_ultrafast'] = True
        original_write = fast.write_json

        def fail_policy(path, value, mode):
            if path == self.manager.policy_path:
                raise OSError('fixture policy failure')
            original_write(path, value, mode)

        with patch.object(fast, 'write_json', side_effect=fail_policy), \
                patch.object(fast.time, 'time', return_value=self.now - 1), self.assertRaises(OSError):
            self.manager.reconcile(self.accounts)
        self.assertEqual(self.manager.state, original_state)
        self.assertEqual(json.loads(self.manager.path.read_text()), original_state)

        def fail_controls(path, value, mode):
            if path == self.manager.path:
                raise OSError('fixture controls failure')
            original_write(path, value, mode)

        with patch.object(fast, 'write_json', side_effect=fail_controls), \
                patch.object(fast.time, 'time', return_value=self.now), self.assertRaises(OSError):
            self.manager.reconcile(self.accounts)
        self.assertEqual(self.manager.state, original_state)
        policy = json.loads(self.manager.policy_path.read_text())
        self.assertEqual(policy['updated_at'], fast.stamp(self.now))
        rebuilt = fast.FastModeManager(self.manager.path, self.manager.policy_path, self.manager.status_path)
        with patch.object(fast.time, 'time', return_value=self.now + 10):
            rebuilt.reconcile(self.accounts)
        self.assertEqual(json.loads(self.manager.policy_path.read_text()), policy)
        self.assertEqual(rebuilt.history('one@example.test')[-1]['changed_at'], fast.stamp(self.now))
        for offset, expected in ((-1, 'ultrafast'), (1, 'priority')):
            self.assertEqual(rebuilt.request_view('index1', fast.stamp(self.now + offset), 'ultrafast',
                                                 'model-not-in-catalog')['forwarded_service_tier'], expected)

    def test_catalog_history_write_failure_is_retried_before_policy_publication(self):
        self.at(0, 'ultrafast')
        previous_controls = self.manager.path.read_text()
        previous_policy = self.manager.policy_path.read_text()
        changed = [dict(self.accounts[0], ultrafast_models=['gpt-6.1-sol']), self.accounts[1]]
        original_write = fast.write_json

        def fail_controls(path, value, mode):
            if path == self.manager.path:
                raise OSError('fixture controls failure')
            original_write(path, value, mode)

        with patch.object(fast, 'write_json', side_effect=fail_controls), self.assertRaises(OSError):
            self.manager.reconcile(changed)
        self.assertEqual(self.manager.path.read_text(), previous_controls)
        self.assertEqual(self.manager.policy_path.read_text(), previous_policy)
        self.manager.reconcile(changed)
        durable = json.loads(self.manager.path.read_text())['accounts']['one@example.test']['history']
        self.assertEqual(durable, self.manager.history('one@example.test'))
        self.assertEqual(durable[-1]['ultrafast_models'], ['gpt-6.1-sol'])
        self.assertEqual(len(durable), 2)
        self.assertEqual(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['ultrafast_models'],
                         ['gpt-6.1-sol'])

    def test_post_captures_catalog_after_concurrent_reconciliation(self):
        self.at(0, 'ultrafast')
        catalog = catalog_module.CodexCatalog(self.base / 'catalog.json')
        catalog.entries = {'one@example.test': {'models': [
            {'id': 'model-a', 'service_tiers': ['ultrafast']}]}}
        gateway = app.GatewayState(Mock(), Mock(), self.manager, catalog=catalog)
        self.addCleanup(gateway.signins.close)
        files = [dict(auth_index=a['id'], type='codex', email=a['email'], name=a['name']) for a in self.accounts]

        def publish_newer_catalog():
            catalog.entries['one@example.test']['models'] = [{'id': 'model-b', 'service_tiers': ['ultrafast']}]
            newer = [fast.fast_account(f['auth_index'], f['email'], f['name'], catalog.view(f['email'])) for f in files]
            self.manager.reconcile(newer)

        class InterleavedLock:
            """Let another request finish immediately before this request acquires its lock."""
            def __init__(self):
                self.inner = threading.RLock()
                self.pending = True

            def __enter__(self):
                self.inner.acquire()
                if self.pending:
                    self.pending = False
                    publish_newer_catalog()
                return self

            def __exit__(self, *args):
                self.inner.release()

        # Gate either lock so this regression also covers the manager's old entry point.
        gateway.lock = self.manager.lock = InterleavedLock()
        with patch.object(app, 'api', return_value={'files': files}), \
                patch.object(fast, 'enforcement', return_value='active'):
            gateway.set_fast_mode('index1', level='ultrafast')
        self.assertEqual(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['ultrafast_models'],
                         ['model-b'])
        self.assertEqual(self.manager.history('one@example.test')[-1]['ultrafast_models'], ['model-b'])

    def test_ultrafast_requires_models_and_live_schema2_plugin(self):
        pending = [fast.fast_account('index1', 'one@example.test', 'codex-one.json', None)]
        for accounts, schema, live, code in ((pending, 2, 'active', 'catalog_pending'),
                                             (self.accounts, 1, 'active', 'plugin_outdated'),
                                             (self.accounts, None, 'active', 'plugin_outdated'),
                                             (self.accounts, 2, 'unavailable', 'plugin_not_enforced'),
                                             (self.accounts, 2, 'unknown', 'plugin_not_enforced'),
                                             (self.accounts, 2, 'active', None)):
            with self.subTest(code=code, schema=schema, live=live):
                self.status(schema)
                with patch.object(fast, 'enforcement', return_value=live), \
                        patch.object(fast.time, 'time', return_value=self.now):
                    if code:
                        with self.assertRaises(fast.LevelUnavailable) as caught:
                            self.manager.set_level('index1', 'ultrafast', accounts)
                        self.assertEqual(caught.exception.code, code)
                        self.assertIn('Ultrafast', str(caught.exception))
                        self.assertEqual(self.manager.setting('one@example.test')['level'], 'fast')
                        self.assertFalse(self.manager.path.exists())
                    else:
                        self.assertEqual(self.manager.set_level('index1', 'ultrafast', accounts)['level'], 'ultrafast')
        with patch.object(fast, 'enforcement', return_value='active'), self.assertRaises(fast.LevelUnavailable) as caught:
            self.manager.set_level('index2', 'ultrafast', self.accounts)
        self.assertEqual(caught.exception.code, 'no_models')
        with self.assertRaises(ValueError):
            self.manager.set_level('index1', 'turbo', self.accounts)
        with self.assertRaises(ValueError):
            self.manager.set_level('missing', 'fast', self.accounts)

    def test_schema1_requests_keep_or_select_levels(self):
        self.at(0, 'ultrafast')
        with patch.object(fast, 'enforcement', return_value='active'), patch.object(fast.time, 'time', return_value=self.now + 5):
            self.assertEqual(self.manager.set_allowed('index1', True, self.accounts)['level'], 'ultrafast')
            self.assertEqual(self.manager.set_allowed('index1', False, self.accounts)['level'], 'normal')
            self.assertEqual(self.manager.set_allowed('index1', True, self.accounts)['level'], 'fast')
        # A saved ultrafast level stays selected (a no-op save) even after a plugin rollback.
        self.at(10, 'ultrafast')
        self.status(1)
        result = self.at(20, 'ultrafast')
        self.assertEqual((result['level'], result['changed_at']), ('ultrafast', fast.stamp(self.now + 10)))

    def test_views_report_levels_models_plugin_and_catalog(self):
        with patch.object(fast, 'enforcement', return_value='active'):
            views = self.manager.views(self.accounts, self.db)
        self.assertEqual(views['index1'], {
            'allowed': True, 'level': 'fast', 'changed_at': None, 'enforcement': 'active',
            'levels': ['normal', 'fast', 'ultrafast'], 'ultrafast_available': True,
            'ultrafast_unavailable_reason': None,
            'ultrafast_models': [{'id': 'gpt-6-astra', 'name': 'GPT-6-Astra'}, {'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol'}],
            'plugin_policy_schema': 2, 'plugin_supports_levels': True,
            'catalog': {'status': 'ok', 'fetched_at': fast.stamp(self.now), 'attempted_at': None}})
        self.assertEqual(views['index2']['levels'], ['normal', 'fast'])
        self.assertEqual(views['index2']['ultrafast_unavailable_reason'], 'no_models')
        self.status(None)
        with patch.object(fast, 'enforcement', return_value='active'):
            view = self.manager.views(self.accounts, self.db)['index1']
        self.assertEqual((view['plugin_policy_schema'], view['plugin_supports_levels'], view['ultrafast_unavailable_reason']),
                         (1, False, 'plugin_outdated'))
        self.manager.status_path.unlink()
        with patch.object(fast, 'enforcement', return_value='unavailable'):
            view = self.manager.views(self.accounts, self.db)['index1']
        self.assertEqual((view['plugin_policy_schema'], view['ultrafast_unavailable_reason']), (None, 'plugin_not_enforced'))
        self.assertEqual(set(fast.UNKNOWN_VIEW), set(view))

    def test_forwarded_at_level_matrix(self):
        models = ['gpt-6.1-sol']
        for level in ('normal', 'fast', 'ultrafast', None):
            for requested in ('normal', 'priority', 'ultrafast', 'unknown', None, 'flex'):
                for model in ('gpt-6.1-sol', 'codex/GPT-6.1-Sol(high)', 'gpt-6.1-terra', None, ''):
                    with self.subTest(level=level, requested=requested, model=model):
                        allowed = model not in (None, '', 'gpt-6.1-terra')
                        if level is None:
                            expected = fast.forwarded_tier(requested, None)
                        elif requested not in ('normal', 'priority', 'ultrafast'):
                            expected = 'unknown'
                        elif level == 'normal' or requested == 'normal':
                            expected = 'normal'
                        elif level == 'fast':
                            expected = 'priority'
                        else:
                            expected = 'ultrafast' if allowed else 'priority'
                        self.assertEqual(fast.forwarded_at_level(requested, level, model, models), expected)

    def test_request_inference_uses_level_and_allowlist_at_request_time(self):
        con = collector.connect(self.db)
        self.addCleanup(con.close)
        self.at(0, 'ultrafast')
        self.at(20, 'fast')
        self.at(40, 'ultrafast')
        # The catalog drops gpt-6-astra while ultrafast is selected.
        changed = [fast.fast_account('index1', 'one@example.test', 'codex-one.json',
                                     dict(self.sol, ultrafast_models=[{'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol'}])),
                   self.accounts[1]]
        with patch.object(fast.time, 'time', return_value=self.now + 60):
            self.manager.reconcile(changed)
            self.manager.reconcile(changed)
        history = self.manager.history('one@example.test')
        self.assertEqual(len(history), 4)
        self.assertEqual([(h['level'], h['ultrafast_models']) for h in history[-2:]],
                         [('ultrafast', ['gpt-6-astra', 'gpt-6.1-sol']), ('ultrafast', ['gpt-6.1-sol'])])
        self.assertEqual(history[-1]['reason'], 'catalog')
        self.assertEqual(self.manager.setting('one@example.test')['changed_at'], fast.stamp(self.now + 40))
        self.assertEqual(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['ultrafast_models'],
                         ['gpt-6.1-sol'])
        # An unknown catalog never rewrites the allowlist history.
        unknown = [fast.fast_account('index1', 'one@example.test', 'codex-one.json', None), self.accounts[1]]
        with patch.object(fast.time, 'time', return_value=self.now + 70):
            self.manager.reconcile(unknown)
        self.assertEqual(len(self.manager.history('one@example.test')), 4)
        self.manager.reconcile(changed)
        cases = [(-5, 'priority', 'gpt-6.1-sol', 'priority'),
                 (5, 'priority', 'gpt-6.1-sol(high)', 'ultrafast'),
                 (6, 'ultrafast', 'gpt-6.1-terra', 'priority'),
                 (7, 'priority', 'gpt-6-astra', 'ultrafast'),
                 (8, 'normal', 'gpt-6.1-sol', 'normal'),
                 (25, 'priority', 'gpt-6.1-sol', 'priority'),
                 (26, 'ultrafast', 'gpt-6.1-sol', 'priority'),
                 (45, 'priority', 'gpt-6-astra', 'ultrafast'),
                 (65, 'priority', 'gpt-6-astra', 'priority'),
                 (66, 'ultrafast', 'gpt-6.1-sol', 'ultrafast'),
                 (67, None, 'gpt-6.1-sol', 'unknown')]
        for offset, asked, model, _ in cases:
            con.execute(INSERT, (fast.stamp(self.now), fast.stamp(self.now + offset), 'index1', model, asked, 'default'))
        con.commit()
        report = app.usage_summary('1970-01-01T00:00:00Z', self.db, self.manager)
        rows = list(reversed(report['recent']))
        self.assertEqual([r['forwarded_service_tier'] for r in rows], [c[3] for c in cases])
        self.assertEqual([r['fast_mode_level'] for r in rows],
                         ['fast'] + ['ultrafast'] * 4 + ['fast'] * 2 + ['ultrafast'] * 4)
        self.assertTrue(all(r['fast_mode_allowed'] is True for r in rows))

    def test_level_post_validation_and_errors(self):
        catalog = catalog_module.CodexCatalog(self.base / 'catalog.json', clock=lambda: self.now)
        catalog.entries = {'one@example.test': {'fetched_at': fast.stamp(self.now), 'error': None, 'models': [
            {'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol', 'service_tiers': ['priority', 'ultrafast']}]}}
        gateway = app.GatewayState(Mock(), Mock(), self.manager, catalog=catalog)
        self.addCleanup(gateway.signins.close)
        files = [dict(auth_index=a['id'], type='codex', email=a['email'], name=a['name']) for a in self.accounts]
        with patch.object(app, 'api', return_value={'files': files}) as api, \
                patch.object(fast, 'enforcement', return_value='active'):
            for invalid in ({'account_id': 'index1', 'level': 'turbo'}, {'account_id': 'index1', 'level': None},
                            {'account_id': 'index1', 'level': 'fast', 'allowed': True}, {'account_id': 'index1'},
                            {'account': 'index1', 'level': 'fast'}, {'account_id': 'index1', 'level': 'FAST'}):
                self.assertEqual(post(gateway, invalid)[0], 400)
            for headers, code in (({'Host': 'evil.test'}, 421), ({'X-Router-Read-Only': '1'}, 403),
                                  ({'Origin': 'http://evil.test'}, 403), ({'X-Router-Request': '0'}, 403)):
                self.assertEqual(post(gateway, {'account_id': 'index1', 'level': 'fast'}, headers)[0], code)
            api.assert_not_called()
            status, value = post(gateway, {'account_id': 'index1', 'level': 'ultrafast'})
            self.assertEqual(status, 200)
            self.assertEqual((value['level'], value['allowed'], value['ultrafast_models']),
                             ('ultrafast', True, [{'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol'}]))
            self.assertEqual(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['level'], 'ultrafast')
            status, value = post(gateway, {'account_id': 'index2', 'level': 'ultrafast'})
            self.assertEqual((status, value['code']), (409, 'catalog_pending'))
            self.assertIn('model list', value['error'])
            self.status(1)
            status, value = post(gateway, {'account_id': 'index1', 'level': 'normal'})
            self.assertEqual((status, value['level'], value['allowed']), (200, 'normal', False))
            status, value = post(gateway, {'account_id': 'index1', 'level': 'ultrafast'})
            self.assertEqual((status, value['code']), (409, 'plugin_outdated'))
            status, value = post(gateway, {'account_id': 'index1', 'allowed': True})
            self.assertEqual((status, value['level']), (200, 'fast'))
            self.assertEqual(post(gateway, {'account_id': 'missing', 'level': 'fast'})[0], 400)
            with patch.object(fast, 'write_json', side_effect=OSError('secret fixture')):
                status, value = post(gateway, {'account_id': 'index1', 'level': 'normal'})
                self.assertEqual(status, 503)
                self.assertNotIn('secret', json.dumps(value))

    def test_plugin_schema_reads_only_a_fast_mode_status(self):
        for raw, expected in (('{"plugin":"fast-mode-policy","policy_schema":2}', 2),
                              ('{"plugin":"fast-mode-policy"}', 1),
                              ('{"plugin":"fast-mode-policy","policy_schema":"2"}', 1),
                              ('{"plugin":"fast-mode-policy","policy_schema":true}', 1),
                              ('{"plugin":"other","policy_schema":2}', None), ('[]', None), ('{broken', None)):
            with self.subTest(raw=raw):
                self.manager.status_path.write_text(raw)
                self.assertEqual(fast.plugin_schema(self.manager.status_path), expected)
        self.manager.status_path.unlink()
        self.assertIsNone(fast.plugin_schema(self.manager.status_path))

    def test_invalid_level_state_is_rejected(self):
        changed = fast.stamp(self.now)
        for state in ({'accounts': {'one@example.test': {'level': 'turbo'}}},
                      {'accounts': {'one@example.test': {'history': [{'changed_at': changed, 'level': 'turbo', 'allow_fast': True}]}}},
                      {'accounts': {'one@example.test': {'history': [{'changed_at': changed, 'level': 'fast'}]}}},
                      {'accounts': {'one@example.test': {'history': [{'changed_at': changed, 'level': 'fast',
                                                                      'allow_fast': True, 'ultrafast_models': [1]}]}}}):
            with self.subTest(state=state):
                self.manager.state = None
                self.manager.path.write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    self.manager.set_level('index1', 'normal', self.accounts)


class CatalogTests(unittest.TestCase):
    BODY = {'models': [
        {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1-Sol', 'base_instructions': 'long private text',
         'service_tiers': [{'id': 'priority', 'name': 'Fast'}, {'id': 'ultrafast', 'name': 'Ultrafast'}]},
        {'slug': 'GPT-6-Astra', 'service_tiers': [{'id': ' UltraFast '}]},
        {'slug': 'gpt-6.1-terra', 'display_name': 'GPT-6.1 Terra', 'service_tiers': [{'id': 'priority'}]},
        {'slug': '', 'service_tiers': [{'id': 'ultrafast'}]}, 'junk', {'slug': 'no-tiers'}]}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'catalog.json'
        self.now = 1790985600
        self.clock = [self.now]
        self.catalog = catalog_module.CodexCatalog(self.path, clock=lambda: self.clock[0])
        self.file = {'auth_index': 'index1', 'email': 'one@example.test', 'name': 'codex-one.json', 'type': 'codex'}
        self.calls = []
        self.response = {'status_code': 200, 'body': json.dumps(self.BODY)}

    def call(self, path, body=None, method=None, timeout=None):
        self.calls.append((path, body, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def test_parse_keeps_only_public_fields_and_derives_ultrafast(self):
        models = catalog_module.parse(json.dumps(self.BODY))
        self.assertEqual(models, [
            {'id': 'gpt-6-astra', 'service_tiers': ['ultrafast']},
            {'id': 'gpt-6.1-sol', 'service_tiers': ['priority', 'ultrafast'], 'name': 'GPT-6.1-Sol'},
            {'id': 'gpt-6.1-terra', 'service_tiers': ['priority'], 'name': 'GPT-6.1 Terra'},
            {'id': 'no-tiers', 'service_tiers': []}])
        for raw in ('{broken', '[]', '{"models": {}}', '{}'):
            with self.assertRaises(ValueError):
                catalog_module.parse(raw)

    def test_request_uses_gateway_token_placeholder_and_account_header(self):
        body = catalog_module.request_body(self.file, 'acct-1')
        self.assertEqual((body['auth_index'], body['method']), ('index1', 'GET'))
        self.assertEqual(body['url'], 'https://chatgpt.com/backend-api/codex/models?client_version=0.166.0')
        self.assertEqual(body['header']['Authorization'], 'Bearer $TOKEN$')
        self.assertEqual(body['header']['ChatGPT-Account-Id'], 'acct-1')
        self.assertEqual(body['header']['originator'], 'codex_cli_rs')
        self.assertNotIn('ChatGPT-Account-Id', catalog_module.request_body(self.file, None)['header'])

    def test_refresh_cadence_failure_keeps_last_good_and_persists(self):
        self.assertEqual(self.catalog.view('one@example.test')['status'], 'pending')
        self.assertTrue(self.catalog.refresh(self.file, 'acct', 'promax', self.call))
        self.assertEqual(self.calls[0][0], '/api-call')
        self.assertEqual(self.calls[0][2], catalog_module.TIMEOUT)
        view = self.catalog.view('one@example.test')
        self.assertEqual(view['ultrafast_models'], [{'id': 'gpt-6-astra', 'name': 'gpt-6-astra'},
                                                    {'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol'}])
        self.assertEqual((view['status'], view['known'], view['fetched_at']), ('ok', True, fast.stamp(self.now)))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertNotIn('long private text', self.path.read_text())
        # Fresh and unchanged: no further reads until six hours pass or the plan changes.
        self.clock[0] += 3600
        self.assertFalse(self.catalog.refresh(self.file, 'acct', 'promax', self.call))
        self.assertTrue(self.catalog.due('one@example.test', 'pro'))
        self.assertFalse(self.catalog.due('one@example.test', None))
        self.clock[0] = self.now + catalog_module.MAX_AGE
        self.assertTrue(self.catalog.due('one@example.test', 'promax'))
        # A failed read keeps the last good list and backs off.
        for failure in ({'status_code': 401, 'body': ''}, {'status_code': 200, 'body': '{broken'}, OSError('down')):
            with self.subTest(failure=failure):
                self.response = failure
                self.assertFalse(self.catalog.refresh(self.file, 'acct', 'promax', self.call))
                view = self.catalog.view('one@example.test')
                self.assertEqual((view['status'], len(view['ultrafast_models']), view['fetched_at']),
                                 ('error', 2, fast.stamp(self.now)))
                self.assertFalse(self.catalog.due('one@example.test', 'pro'))
                self.clock[0] += catalog_module.RETRY_AFTER
        self.assertEqual(json.loads(self.path.read_text())['accounts']['one@example.test']['error'], 'unavailable')
        # A restart keeps the persisted list and reads again once.
        restarted = catalog_module.CodexCatalog(self.path, clock=lambda: self.clock[0])
        self.assertEqual(len(restarted.ultrafast_models('one@example.test')), 2)
        self.response = {'status_code': 200, 'body': json.dumps({'models': []})}
        self.assertTrue(restarted.refresh(self.file, 'acct', 'promax', self.call))
        self.assertEqual(restarted.view('one@example.test')['ultrafast_models'], [])
        self.assertEqual(restarted.view('one@example.test')['status'], 'ok')

    def test_startup_reads_once_even_when_persisted_list_is_fresh(self):
        self.catalog.refresh(self.file, 'acct', 'promax', self.call)
        restarted = catalog_module.CodexCatalog(self.path, clock=lambda: self.clock[0] + 60)
        self.assertTrue(restarted.due('one@example.test', 'promax'))
        self.assertTrue(restarted.refresh(self.file, 'acct', 'promax', self.call))
        self.assertFalse(restarted.due('one@example.test', 'promax'))
        self.assertEqual(len(self.calls), 2)

    def test_corrupt_persisted_catalog_is_ignored_and_bad_files_are_skipped(self):
        self.path.write_text('{broken')
        self.assertEqual(self.catalog.view('one@example.test')['status'], 'pending')
        self.path.write_text(json.dumps({'accounts': {'one@example.test': {'models': 'bad'}}}))
        restarted = catalog_module.CodexCatalog(self.path)
        self.assertEqual(restarted.view('one@example.test')['known'], False)
        self.assertFalse(self.catalog.refresh(dict(self.file, email=None), 'acct', 'promax', self.call))
        self.assertEqual(self.calls, [])

    def test_gateway_refresh_passes_plan_and_account_id_and_snapshot_uses_catalog(self):
        directory = Path(self.path.parent)
        manager = fast.FastModeManager(directory / 'controls.json', directory / 'policy.json', directory / 'status.json')
        state = app.GatewayState(Mock(), Mock(), manager)
        self.addCleanup(state.signins.close)
        self.assertEqual(state.catalog.path, directory / 'codex-model-catalog.json')
        state.catalog = self.catalog
        with patch.object(app, 'codex_account_id', return_value='acct-9'):
            state.refresh_catalogs([self.file], {'index1': 'promax'}, self.call)
        self.assertEqual(self.calls[0][1]['header']['ChatGPT-Account-Id'], 'acct-9')
        self.assertEqual(self.catalog.entries['one@example.test']['plan_type'], 'promax')
        state.refresh_catalogs([self.file], {}, Mock(side_effect=AssertionError('not due')))

    def test_background_catalog_refresh_reconciles_policy_and_history_without_overview(self):
        manager = fast.FastModeManager(self.path.parent / 'controls.json', self.path.parent / 'policy.json',
                                       self.path.parent / 'status.json')
        manager.status_path.write_text('{"plugin":"fast-mode-policy","policy_schema":2}')
        state = app.GatewayState(Mock(), Mock(), manager, catalog=self.catalog)
        self.addCleanup(state.signins.close)
        self.catalog.refresh(self.file, 'acct', 'promax', self.call)
        accounts = [fast.fast_account('index1', self.file['email'], self.file['name'], self.catalog.view(self.file['email']))]
        with patch.object(fast, 'enforcement', return_value='active'), \
                patch.object(fast.time, 'time', return_value=self.now):
            manager.set_level('index1', 'ultrafast', accounts)
        self.clock[0] += catalog_module.MAX_AGE
        self.response = {'status_code': 200, 'body': '{"models":[]}'}
        with patch.object(app, 'codex_account_id', return_value='acct'), \
                patch.object(fast.time, 'time', return_value=self.clock[0]):
            state.refresh_catalogs([self.file], {'index1': 'promax'}, self.call)
        policy = json.loads(manager.policy_path.read_text())['accounts']['index1']
        self.assertEqual(policy['ultrafast_models'], [])
        self.assertEqual(manager.history(self.file['email'])[-1]['ultrafast_models'], [])
        self.assertEqual(manager.request_view('index1', fast.stamp(self.clock[0] + 1),
                                             'priority', 'gpt-6.1-sol')['forwarded_service_tier'], 'priority')
        self.assertEqual(manager.request_view('index1', fast.stamp(self.now + 1),
                                             'priority', 'gpt-6.1-sol')['forwarded_service_tier'], 'ultrafast')


if __name__ == '__main__':
    unittest.main()
