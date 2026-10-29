import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import fast_mode as fast
import server as app

spec = importlib.util.spec_from_file_location('fast_usage_collector', ROOT / 'scripts/collect-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class FastModeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.manager = fast.FastModeManager(self.base / 'controls.json', self.base / 'policy.json',
                                            self.base / 'status.json')
        self.accounts = [{'id': 'index1', 'email': 'one@example.test', 'name': 'codex-one.json',
                          'supports_ultrafast': False},
                         {'id': 'index2', 'email': 'two@example.test', 'name': 'codex-two.json',
                          'supports_ultrafast': True}]
        self.db = self.base / 'usage.sqlite3'
        self.now = 1790985600

    def connect(self):
        con = collector.connect(self.db)
        self.addCleanup(con.close)
        return con

    def row(self, con, offset=0, asked='normal', served='normal', account='index1'):
        con.execute('''INSERT INTO usage
            (collected_at,timestamp,auth_index,service_tier,response_service_tier)
            VALUES (?,?,?,?,?)''', (fast.stamp(self.now), fast.stamp(self.now + offset), account, asked, served))
        con.commit()

    def test_ui_states_and_save_refresh_races(self):
        result = subprocess.run(['node', str(ROOT / 'tests/test_fast_mode_ui.cjs')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_status_default_is_gateway_state_outside_auth(self):
        self.assertEqual(fast.STATUS, Path('/var/lib/cliproxyapi/fast-mode-plugin-status.json'))

    def test_policy_defaults_both_keys_modes_reconciliation_and_reload(self):
        self.manager.reconcile(self.accounts)
        policy = json.loads(self.manager.policy_path.read_text())
        self.assertEqual(policy['version'], 1)
        self.assertIsNotNone(fast.epoch(policy['updated_at']))
        for account in self.accounts:
            expected = {'allow_fast': True, 'supports_ultrafast': account['supports_ultrafast']}
            self.assertEqual(policy['accounts'][account['id']], expected)
            self.assertEqual(policy['files'][account['name']], expected)
        self.assertEqual(self.manager.policy_path.stat().st_mode & 0o777, 0o640)
        with patch.object(fast, 'enforcement', return_value='active'), patch.object(fast.time, 'time', return_value=self.now):
            result = self.manager.set_allowed('index1', False, self.accounts)
            self.assertEqual(result, {'allowed': False, 'changed_at': fast.stamp(self.now), 'enforcement': 'active'})
            self.assertEqual(self.manager.set_allowed('index1', False, self.accounts), result)
        self.assertEqual(self.manager.path.stat().st_mode & 0o777, 0o600)
        rebuilt = fast.FastModeManager(self.manager.path, self.manager.policy_path, self.manager.status_path)
        for contents in (None, '{}', '{broken', json.dumps(policy)):
            if contents is None:
                self.manager.policy_path.unlink()
            else:
                self.manager.policy_path.write_text(contents)
            rebuilt.reconcile(self.accounts)
            self.assertFalse(json.loads(rebuilt.policy_path.read_text())['accounts']['index1']['allow_fast'])
        with patch.object(fast, 'write_json') as write:
            rebuilt.reconcile(self.accounts)
            write.assert_not_called()
        changed = copy.deepcopy(self.accounts)
        changed[0].update(id='new-index', name='new-name.json')
        rebuilt.reconcile(changed)
        policy = json.loads(rebuilt.policy_path.read_text())
        self.assertFalse(policy['accounts']['new-index']['allow_fast'])
        self.assertFalse(policy['files']['new-name.json']['allow_fast'])
        self.assertNotIn('index1', policy['accounts'])

    def test_atomic_write_syncs_file_and_directory_and_keeps_old_on_rename_failure(self):
        path = self.base / 'atomic.json'
        fast.write_json(path, {'old': True}, 0o640)
        events = []
        original_sync, original_replace = os.fsync, Path.replace
        def sync(fd):
            events.append('sync')
            original_sync(fd)
        def replace(source, target):
            events.append('replace')
            self.assertEqual(json.loads(path.read_text()), {'old': True})
            return original_replace(source, target)
        with patch.object(fast.os, 'fsync', side_effect=sync), patch.object(Path, 'replace', replace):
            fast.write_json(path, {'new': True}, 0o640)
        self.assertEqual(events, ['sync', 'replace', 'sync'])
        with patch.object(Path, 'replace', side_effect=OSError('fixture')), self.assertRaises(OSError):
            fast.write_json(path, {'bad': True}, 0o640)
        self.assertEqual(json.loads(path.read_text()), {'new': True})
        self.assertFalse(list(self.base.glob('*.tmp')))

    def test_toggle_history_lookup_boundaries_persistence_and_account_identity(self):
        self.manager.reconcile(self.accounts)
        with patch.object(fast, 'enforcement', return_value='active'):
            for offset, allowed in ((0, False), (10, True), (20, False)):
                with patch.object(fast.time, 'time', return_value=self.now + offset):
                    self.manager.set_allowed('index1', allowed, self.accounts)
        for offset, allowed in ((-1, True), (0, False), (9, False), (10, True), (19, True), (20, False), (30, False)):
            with self.subTest(offset=offset):
                self.assertIs(self.manager.allowed_at('index1', fast.stamp(self.now + offset)), allowed)
                self.assertTrue(self.manager.allowed_at('index2', fast.stamp(self.now + offset)))
        local_stamp = fast.dt.datetime.fromtimestamp(
            self.now + 5, fast.dt.timezone(fast.dt.timedelta(hours=-3))).isoformat()
        self.assertFalse(self.manager.allowed_at('index1', local_stamp))
        self.assertTrue(self.manager.allowed_at('unknown', fast.stamp(self.now + 30)))
        self.assertTrue(self.manager.allowed_at('index1', None))
        rebuilt = fast.FastModeManager(self.manager.path, self.manager.policy_path, self.manager.status_path)
        renamed = copy.deepcopy(self.accounts)
        renamed[0]['id'] = 'new-index'
        rebuilt.reconcile(renamed)
        self.assertFalse(rebuilt.allowed_at('new-index', fast.stamp(self.now + 5)))
        self.assertTrue(rebuilt.allowed_at('new-index', fast.stamp(self.now + 15)))
        self.assertEqual(len(rebuilt.history('one@example.test')), 3)

    def test_history_legacy_upgrade_noops_bounding_and_equal_timestamps(self):
        self.manager.path.write_text(json.dumps({'accounts': {'one@example.test': {
            'allow_fast': False, 'changed_at': fast.stamp(self.now - 10)}}}))
        self.manager.reconcile(self.accounts)
        self.assertTrue(self.manager.allowed_at('index1', fast.stamp(self.now - 11)))
        self.assertFalse(self.manager.allowed_at('index1', fast.stamp(self.now - 10)))
        with patch.object(fast, 'enforcement', return_value='active'):
            with patch.object(fast.time, 'time', return_value=self.now):
                self.manager.set_allowed('index1', False, self.accounts)
                self.assertEqual(len(self.manager.history('one@example.test')), 1)
                self.manager.set_allowed('index1', True, self.accounts)
                self.manager.set_allowed('index1', False, self.accounts)
                self.assertFalse(self.manager.allowed_at('index1', fast.stamp(self.now)))
            for offset in range(1, 61):
                with patch.object(fast.time, 'time', return_value=self.now + offset):
                    self.manager.set_allowed('index1', offset % 2 == 1, self.accounts)
        history = self.manager.history('one@example.test')
        self.assertEqual(len(history), fast.HISTORY_LIMIT)
        self.assertEqual(history[0]['changed_at'], fast.stamp(self.now + 11))
        self.assertTrue(self.manager.allowed_at('index1', fast.stamp(self.now + 10)))
        self.assertFalse(self.manager.allowed_at('index1', fast.stamp(self.now + 12)))
        self.assertEqual(json.loads(self.manager.path.read_text())['accounts']['one@example.test']['history'], history)

    def test_history_failed_save_rolls_back_both_current_and_past_values(self):
        with patch.object(fast, 'enforcement', return_value='active'), \
                patch.object(fast.time, 'time', return_value=self.now):
            self.manager.set_allowed('index1', False, self.accounts)
        previous = copy.deepcopy(self.manager.history('one@example.test'))
        original = fast.write_json
        failed = False
        def write(path, value, mode):
            nonlocal failed
            if path == self.manager.policy_path and not failed:
                failed = True
                raise OSError('fixture')
            original(path, value, mode)
        with patch.object(fast, 'write_json', side_effect=write), \
                patch.object(fast.time, 'time', return_value=self.now + 10), self.assertRaises(OSError):
            self.manager.set_allowed('index1', True, self.accounts)
        self.assertEqual(self.manager.history('one@example.test'), previous)
        self.assertFalse(self.manager.allowed_at('index1', fast.stamp(self.now + 15)))
        self.assertEqual(json.loads(self.manager.path.read_text())['accounts']['one@example.test']['history'], previous)

    def test_recent_requests_return_both_tiers_and_historical_toggle(self):
        con = self.connect()
        with patch.object(fast, 'enforcement', return_value='active'):
            for offset, allowed in ((0, False), (10, True)):
                with patch.object(fast.time, 'time', return_value=self.now + offset):
                    self.manager.set_allowed('index1', allowed, self.accounts)
        for offset, account in ((-1, 'index1'), (1, 'index1'), (2, 'index2'), (11, 'index1')):
            self.row(con, offset, asked='ultrafast', served='priority', account=account)
        report = app.usage_summary(fast.stamp(self.now - 100), self.db, self.manager)
        self.assertTrue(report['available'])
        self.assertEqual([r['fast_mode_allowed'] for r in report['recent']], [True, True, False, True])
        for row in report['recent']:
            self.assertEqual(row['service_tier'], 'ultrafast')
            self.assertEqual(row['response_service_tier'], 'priority')

    def test_fixture_recent_requests_cover_every_mode_case(self):
        script = '''import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
import fast_mode_fixture as fixture
state = fixture.build_state(Path(sys.argv[1]))
state.snapshot()
report = fixture.app.usage_summary('1970-01-01T00:00:00Z', fixture.app.DB, state.fast_modes)
print(json.dumps(report))
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.base / 'fixture')],
                                capture_output=True, text=True, timeout=15, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        recent = json.loads(result.stdout)['recent'][:12]
        self.assertEqual([(r['service_tier'], r['response_service_tier'], r['fast_mode_allowed']) for r in recent], [
            (None, None, True), ('priority', 'priority', False), ('ultrafast', 'ultrafast', True),
            ('ultrafast', 'priority', True), ('priority', 'priority', True),
            ('priority', 'normal', False), ('priority', 'default', True), ('normal', 'normal', True),
            (None, 'priority', True), (None, 'ultrafast', True), (None, 'normal', True),
            ('priority', None, True)])
        self.assertEqual([r['forwarded_service_tier'] for r in recent], [
            'unknown', 'normal', 'ultrafast', 'priority', 'priority', 'normal', 'priority',
            'normal', 'unknown', 'unknown', 'unknown', 'priority'])

    def test_failed_policy_or_state_write_rolls_back_and_reconcile_recovers(self):
        self.manager.reconcile(self.accounts)
        original = fast.write_json
        for failed_path in (self.manager.path, self.manager.policy_path):
            failed = False
            def write(path, value, mode):
                nonlocal failed
                if path == failed_path and not failed:
                    failed = True
                    raise OSError('fixture')
                original(path, value, mode)
            with patch.object(fast, 'write_json', side_effect=write), self.assertRaises(OSError):
                self.manager.set_allowed('index1', False, self.accounts)
            self.assertTrue(self.manager.setting('one@example.test')['allowed'])
            self.assertTrue(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['allow_fast'])
            self.assertEqual(json.loads(self.manager.path.read_text())['accounts'], {})
        with patch.object(fast, 'enforcement', return_value='active'):
            self.manager.set_allowed('index1', False, self.accounts)
        with patch.object(fast, 'write_json', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            self.manager.set_allowed('index1', True, self.accounts)
        self.manager.reconcile(self.accounts)
        self.assertFalse(self.manager.setting('one@example.test')['allowed'])

    def test_restore_retries_authoritative_state_after_persistent_disk_failure(self):
        self.manager.reconcile(self.accounts)
        original = fast.write_json
        writes = 0
        def write(path, value, mode):
            nonlocal writes
            writes += 1
            if writes > 1:
                raise OSError('disk failure after state changed')
            original(path, value, mode)
        with patch.object(fast, 'write_json', side_effect=write), self.assertRaises(OSError):
            self.manager.set_allowed('index1', False, self.accounts)
        self.assertTrue(self.manager.restore_pending)
        self.assertFalse(json.loads(self.manager.path.read_text())['accounts']['one@example.test']['allow_fast'])
        self.manager.reconcile(self.accounts)
        self.assertFalse(self.manager.restore_pending)
        self.assertEqual(json.loads(self.manager.path.read_text())['accounts'], {})
        restarted = fast.FastModeManager(self.manager.path, self.manager.policy_path, self.manager.status_path)
        self.assertTrue(restarted.setting('one@example.test')['allowed'])

    def test_invalid_state_does_not_silently_enable_fast(self):
        self.manager.path.write_text('{broken')
        with self.assertRaises(ValueError):
            self.manager.set_allowed('index1', False, self.accounts)
        self.assertFalse(self.manager.policy_path.exists())

    def post(self, data, headers=None):
        handler = object.__new__(app.Handler)
        handler.path = '/api/fast-mode'
        content = json.dumps(data).encode()
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                           'Content-Type': 'application/json', 'Content-Length': str(len(content)),
                           **(headers or {})}
        handler.rfile = io.BytesIO(content)
        handler.connection = Mock()
        handler.server = Mock()
        handler.server.state = self.gateway
        handler.send = Mock()
        handler.do_POST()
        return handler.send.call_args.args[0], json.loads(handler.send.call_args.args[1])

    def test_endpoint_validation_account_resolution_persistence_and_failure(self):
        self.gateway = app.GatewayState(Mock(), Mock(), self.manager)
        self.gateway.data['accounts'] = [{'id': 'index1', 'plan': 'Pro (10x)'},
                                          {'id': 'index2', 'plan': 'Pro (25x)'}]
        files = [dict(auth_index=a['id'], type='codex', email=a['email'], name=a['name']) for a in self.accounts]
        body = {'account_id': 'index1', 'allowed': False}
        with patch.object(app, 'api', return_value={'files': files}) as api, patch.object(fast, 'enforcement', return_value='active'):
            for invalid in ([], {}, {'account_id': 'index1', 'enabled': False},
                            {**body, 'allowed': 0}, {**body, 'account_id': ''}, {**body, 'extra': True}):
                self.assertEqual(self.post(invalid)[0], 400)
            for headers, code in (({'Host': 'evil.test'}, 421), ({'X-Router-Read-Only': '1'}, 403),
                                  ({'Origin': 'http://evil.test'}, 403), ({'X-Router-Request': '0'}, 403),
                                  ({'Content-Type': 'text/plain'}, 400), ({'Content-Length': '1025'}, 400)):
                self.assertEqual(self.post(body, headers)[0], code)
            api.assert_not_called()
            status, value = self.post(body)
            self.assertEqual(status, 200)
            self.assertFalse(value['allowed'])
            self.assertEqual(api.call_args.args, ('/auth-files',))
            self.assertFalse(json.loads(self.manager.policy_path.read_text())['files']['codex-one.json']['allow_fast'])
            self.assertTrue(json.loads(self.manager.policy_path.read_text())['accounts']['index2']['supports_ultrafast'])
            self.assertEqual(self.post({**body, 'account_id': 'missing'})[0], 400)
            with patch.object(fast, 'write_json', side_effect=OSError('secret fixture')):
                code, value = self.post({**body, 'allowed': True})
                self.assertEqual(code, 503)
                self.assertNotIn('secret', json.dumps(value))
            self.assertFalse(self.manager.setting('one@example.test')['allowed'])
            api.return_value = {'files': files + [files[0]]}
            self.assertEqual(self.post(body)[0], 400)

    def test_overview_reconciles_policy_hides_names_and_preserves_saved_off_on_failure(self):
        con = self.connect()
        self.row(con, asked='priority', served='normal')
        resets = Mock()
        resets.view.return_value = {}
        state = app.GatewayState(resets, Mock(), self.manager)
        state.data.update(online=True, checked_at=fast.stamp(self.now), accounts=[{
            'id': a['id'], 'email': a['email'], '_fast_file': a['name'], 'active': True,
            'plan': 'Pro (25x)' if a['supports_ultrafast'] else 'Pro (10x)',
            'windows': [], 'availability': {'state': 'ready'}} for a in self.accounts])
        with patch.object(app, 'DB', self.db), patch.object(app, 'billing_records', return_value={}), \
                patch.object(app, 'preference_status', return_value=None), \
                patch.object(fast.time, 'time', return_value=self.now), \
                patch.object(fast, 'enforcement', return_value='active'):
            value = state.snapshot()['accounts'][0]
            self.assertNotIn('_fast_file', value)
            self.assertNotIn('usage', value['fast_mode'])
            self.assertTrue(value['fast_mode']['allowed'])
            self.manager.set_allowed('index1', False, self.accounts)
            self.manager.policy_path.unlink()
            with patch.object(fast, 'write_json', side_effect=OSError('fixture')):
                value = state.snapshot()['accounts'][0]
                self.assertFalse(value['fast_mode']['allowed'])
                self.assertEqual(value['fast_mode']['enforcement'], 'unknown')
            state.snapshot()
            self.assertFalse(json.loads(self.manager.policy_path.read_text())['accounts']['index1']['allow_fast'])

    def test_enforcement_requires_current_mainpid_gateway_process_and_valid_status(self):
        status = {'plugin': 'fast-mode-policy', 'pid': 321, 'policy_path': str(self.manager.policy_path)}
        self.manager.status_path.write_text(json.dumps(status))
        with patch.object(fast.subprocess, 'run', return_value=Mock(stdout='321\n')) as run, \
                patch.object(Path, 'resolve', return_value=Path('/opt/cliproxyapi/cli-proxy-api')):
            self.assertEqual(fast.enforcement(self.manager.status_path, self.manager.policy_path), 'active')
            self.assertEqual(run.call_args.args[0], ['systemctl', 'show', 'cliproxyapi', '--property=MainPID', '--value'])
            for changed in ({'disabled': True}, {'enabled': False}, {'pid': 320}, {'pid': True},
                            {'plugin': 'other'}, {'policy_path': '/wrong'}):
                self.manager.status_path.write_text(json.dumps({**status, **changed}))
                self.assertEqual(fast.enforcement(self.manager.status_path, self.manager.policy_path), 'unavailable')
            self.manager.status_path.write_text(json.dumps(status))
            run.side_effect = subprocess.TimeoutExpired('systemctl', 3)
            self.assertEqual(fast.enforcement(self.manager.status_path, self.manager.policy_path), 'unknown')
        with patch.object(fast.subprocess, 'run', return_value=Mock(stdout='321')), \
                patch.object(Path, 'resolve', side_effect=FileNotFoundError), \
                patch.object(Path, 'read_bytes', return_value=b'/opt/cli-proxy-api\x00'):
            self.assertEqual(fast.enforcement(self.manager.status_path, self.manager.policy_path), 'active')
        with patch.object(fast.subprocess, 'run', return_value=Mock(stdout='321')), \
                patch.object(Path, 'resolve', return_value=Path('/usr/bin/python3')), \
                patch.object(Path, 'read_bytes', return_value=b'python3\x00'):
            self.assertEqual(fast.enforcement(self.manager.status_path, self.manager.policy_path), 'unavailable')
        self.manager.status_path.unlink()
        self.assertEqual(fast.enforcement(self.manager.status_path, self.manager.policy_path), 'unavailable')
        for raw in ('{broken', '[]', 'null'):
            self.manager.status_path.write_text(raw)
            self.assertNotEqual(fast.enforcement(self.manager.status_path, self.manager.policy_path), 'active')

    def test_echo_does_not_override_forwarded_tier_or_make_requests_unknown(self):
        con = self.connect()
        cases = [('priority', 'normal'), ('priority', 'default'), ('priority', 'auto'),
                 ('priority', 'priority'), ('priority', None), ('ultrafast', 'unknown'),
                 ('normal', None), ('normal', 'ultrafast'), (None, 'priority')]
        self.manager.reconcile(self.accounts)
        for index, (asked, served) in enumerate(cases):
            self.row(con, -index, asked=asked, served=served)
        report = app.usage_summary('1970-01-01T00:00:00Z', self.db, self.manager)
        self.assertEqual([(r['service_tier'], r['response_service_tier']) for r in report['recent']], cases)
        self.assertEqual([r['forwarded_service_tier'] for r in report['recent']],
                         ['priority', 'priority', 'priority', 'priority', 'priority', 'priority', 'normal', 'normal', 'unknown'])

    def test_forwarded_tier_policy_defaults_off_mapping_and_unknown_request(self):
        for requested in ('normal', 'priority', 'ultrafast', 'unknown', None):
            for allowed in (True, False, None):
                for supports in (True, False, None):
                    with self.subTest(requested=requested, allowed=allowed, supports=supports):
                        expected = 'unknown' if requested in (None, 'unknown') else (
                            'normal' if allowed is False else
                            'priority' if requested == 'ultrafast' and supports is not True else requested)
                        self.assertEqual(fast.forwarded_tier(requested, allowed, supports), expected)
        self.assertEqual(fast.forwarded_tier('priority'), 'priority')
        self.assertEqual(fast.forwarded_tier('ultrafast'), 'priority')

    def test_recent_apply_toggle_history_and_only_positive_echo_for_leaks(self):
        con = self.connect()
        with patch.object(fast, 'enforcement', return_value='active'):
            for offset, allowed in ((-100, False), (-10, True)):
                with patch.object(fast.time, 'time', return_value=self.now + offset):
                    self.manager.set_allowed('index1', allowed, self.accounts)
        cases = [(-101, 'priority', 'normal', 'index1'),
                 (-100, 'priority', 'default', 'index1'),
                 (-99, 'ultrafast', None, 'index1'),
                 (-98, 'priority', 'priority', 'index1'),
                 (-97, 'normal', 'ultrafast', 'index1'),
                 (-96, 'normal', None, 'index1'),
                 (-10, 'priority', 'default', 'index1'),
                 (-9, 'ultrafast', 'normal', 'index1'),
                 (-8, 'ultrafast', 'default', 'index2')]
        for offset, asked, served, account in cases:
            self.row(con, offset, asked, served, account)
        report = app.usage_summary('1970-01-01T00:00:00Z', self.db, self.manager)
        self.assertEqual([r['forwarded_service_tier'] for r in report['recent']],
                         ['ultrafast', 'priority', 'priority', 'normal', 'normal', 'normal', 'normal', 'normal', 'priority'])
        self.assertEqual([r['fast_mode_allowed'] for r in report['recent']],
                         [True, True, True, False, False, False, False, False, True])
        with patch.object(fast.time, 'time', return_value=self.now), patch.object(fast, 'enforcement', return_value='active'):
            views = self.manager.views(self.accounts, self.db)
        for view in views.values():
            self.assertNotIn('usage', view)
            self.assertTrue(view['allowed'])
            self.assertEqual(view['enforcement'], 'active')
        self.assertEqual(views['index1']['changed_at'], fast.stamp(self.now - 10))
        self.assertIsNone(views['index2']['changed_at'])

    def test_collector_tier_sanitizer_migration_preserves_all_old_data_and_replay(self):
        for source, expected in ((None, 'normal'), ('', 'normal'), ('default', 'normal'), ('AUTO', 'normal'),
                                 ('normal', 'normal'), (' Priority ', 'priority'), ('fast', 'priority'),
                                 ('ultrafast', 'ultrafast'), ('flex', 'unknown'), ({'token': 'secret'}, 'unknown')):
            with self.subTest(source=source):
                reduced = collector.reduce_record({'service_tier': source, 'response_service_tier': source,
                                                   'secret': 'credential'})
                self.assertEqual(reduced, {'service_tier': expected, 'response_service_tier': 'unknown' if source in (None, '') else expected})
                self.assertEqual(collector.reduce_record(reduced), reduced)
                self.assertEqual(collector.normalize(reduced)['response_service_tier'], 'unknown' if source in (None, '') else expected)
        self.assertEqual(collector.normalize({})['service_tier'], 'normal')
        with sqlite3.connect(self.db) as con:
            con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT NOT NULL,
                timestamp TEXT, provider TEXT, model TEXT, auth_index TEXT, failed INTEGER, latency_ms REAL,
                input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cached_tokens INTEGER,
                total_tokens INTEGER, reasoning_effort TEXT)''')
            con.execute("INSERT INTO usage VALUES (7, 'old', 'old', 'codex', 'model', 'index1', 0, 5, 1, 2, 3, 4, 5, 'high')")
            old = con.execute('SELECT * FROM usage').fetchone()
        con.close()
        con = self.connect()
        self.assertEqual(con.execute('SELECT * FROM usage').fetchone(), (*old, None, None, None, None))
        other = collector.connect(self.db)
        other.close()
        spool = self.base / 'batch.json'
        spool.write_text(json.dumps([{'auth_index': 'index1', 'service_tier': 'ultrafast', 'response_service_tier': 'default'}, {}]))
        self.assertEqual(collector.replay_spool(con, spool), (2, 0, 0))
        self.assertEqual(con.execute('SELECT service_tier,response_service_tier FROM usage ORDER BY id').fetchall(),
                         [(None, None), ('ultrafast', 'normal'), ('normal', 'unknown')])


if __name__ == '__main__':
    unittest.main()
