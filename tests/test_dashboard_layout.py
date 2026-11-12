"""Saved display order contracts, using temporary state and synthetic accounts only."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import server


def card_order(layout):
    return {key: layout[key] for key in ('providers', 'accounts')}


class LayoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'layout.json'
        for context in (patch.object(server, 'LAYOUT', self.path),
                        patch.object(server, 'LAYOUT_WARNING_LOGGED', False),
                        patch.object(server.logging.getLogger(server.__name__), 'warning')):
            context.start()
            self.addCleanup(context.stop)
        self.groups = [dict(id='codex', accounts=[dict(key='codex-a.json'), dict(key='codex-b.json')]),
                       dict(id='claude', accounts=[dict(key='claude-a.json')]),
                       dict(id='xai', accounts=[dict(key='xai-a.json')])]
        self.default = dict(providers=['codex', 'claude', 'xai'], accounts={
            'codex': ['codex-a.json', 'codex-b.json'], 'claude': ['claude-a.json'], 'xai': ['xai-a.json']})

    def post(self, value=None, *, raw=None, headers=None):
        raw = json.dumps(value).encode() if raw is None else raw
        handler = object.__new__(server.Handler)
        handler.path = '/api/layout'
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                           'Content-Type': 'application/json', 'Content-Length': str(len(raw))} | (headers or {})
        handler.connection, handler.rfile, handler.send = Mock(), io.BytesIO(raw), Mock()
        handler.server = SimpleNamespace(state=Mock())
        handler.server.state.snapshot.return_value = {}
        with patch.object(server.providers, 'groups', return_value=self.groups), patch.object(server, 'watch_accounts', return_value=[]):
            handler.do_POST()
        status, body = handler.send.call_args.args
        return status, json.loads(body)

    def overview(self, gateway, watched=None):
        handler = object.__new__(server.Handler)
        handler.path = '/api/overview'
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1'}
        handler.send = Mock()
        handler.server = SimpleNamespace(state=Mock())
        handler.server.state.snapshot.return_value = gateway
        handler.server.state.notices.view.return_value = []
        with patch.object(server, 'watch_accounts', return_value=copy.deepcopy(watched or [])), \
                patch.object(server, 'usage_summary', return_value={}), \
                patch.object(server, 'billing_records', return_value={}), \
                patch.object(server, 'health', return_value={}), \
                patch.object(server.provider_status, 'read_snapshot', return_value={}):
            handler.do_GET()
        status, body = handler.send.call_args.args
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['layout']['version'], 2)
        result['layout'] = card_order(result['layout'])
        return result

    def test_default_claude_order_uses_recognised_watch_labels_and_saved_order_wins(self):
        rows = [dict(type='claude', name='claude-111-b.json', email='first@example.test', auth_index='b'),
                dict(type='claude', name='claude-999-a.json', email='last@example.test', auth_index='a')]
        watched = [dict(id='claude', label='Claude A', email='last@example.test'),
                   dict(id='claude-b', label='Claude B', email='first@example.test')]
        gateway = dict(inventory=rows)
        before = copy.deepcopy((gateway, watched))
        expected = [rows[1]['name'], rows[0]['name']]
        for inventory in (rows, rows[::-1]):
            result = self.overview(dict(inventory=inventory), watched)
            self.assertEqual([a['key'] for a in result['providers'][0]['accounts']], expected)
            self.assertEqual(result['layout']['accounts']['claude'], expected)
        self.assertEqual(self.post(dict(providers=['claude'], accounts={'claude': expected[::-1]}))[0], 200)
        self.assertEqual(self.overview(gateway, watched)['layout']['accounts']['claude'], expected[::-1])
        self.assertEqual((gateway, watched), before)

    def test_default_account_order_is_natural_and_stable_for_every_provider(self):
        rows = [dict(type=p, name=f'{p}-{key}.json', auth_index=f'{p}-{key}', label=label)
                for p in server.providers.CATALOG
                for key, label in (('z', 'Account 10'), ('y', 'account 2'), ('x', 'Account 1'), ('a', 'Account 2'))]
        keys = [dict(provider='openai-compatibility', key=key, label=label, disabled=False)
                for key, label in (('key-z', 'Service 10'), ('key-b', 'Service 2'), ('key-a', 'Service 1'))]
        for inventory, accounts in ((rows, keys), (rows[::-1], keys[::-1])):
            result = self.overview(dict(inventory=inventory, api_key_accounts=accounts))
            for group in result['providers']:
                expected = (['key-a', 'key-b', 'key-z'] if group['id'] == 'openai-compatibility' else
                            [f"{group['id']}-{key}.json" for key in ('x', 'a', 'y', 'z')])
                self.assertEqual([a['key'] for a in group['accounts']], expected)
                self.assertEqual(result['layout']['accounts'][group['id']], expected)

    def test_save_read_round_trip_private_mode_and_full_replacement(self):
        value = dict(providers=['xai', 'codex', 'claude'], accounts={
            'codex': ['codex-b.json', 'codex-a.json'], 'claude': [], 'xai': ['xai-a.json']})
        self.path.write_text('{}')
        self.path.chmod(0o644)
        self.assertEqual(self.post(value)[0], 200)
        self.assertEqual(card_order(json.loads(self.path.read_text())), value)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        expected = copy.deepcopy(value)
        expected['accounts']['claude'] = ['claude-a.json']
        self.assertEqual(card_order(server.layout_view(self.groups)), expected)
        replacement = dict(providers=[], accounts={})
        self.assertEqual(self.post(replacement)[0], 200)
        self.assertEqual(card_order(json.loads(self.path.read_text())), replacement)
        self.assertEqual(card_order(server.layout_view(self.groups)), self.default)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_normalization_drops_missing_and_appends_new_ids_without_mutating_inputs(self):
        value = dict(providers=['removed', 'xai', 'codex'], accounts={
            'removed': ['old'], 'codex': ['old', 'codex-b.json'], 'xai': ['old']})
        self.assertEqual(self.post(value)[0], 200)
        before = copy.deepcopy(self.groups)
        self.assertEqual(card_order(server.layout_view(self.groups)), dict(providers=['xai', 'codex', 'claude'],
            accounts=dict(self.default['accounts'], codex=['codex-b.json', 'codex-a.json'])))
        self.assertEqual(self.groups, before)
        self.assertEqual(card_order(json.loads(self.path.read_text())), value)
        self.assertEqual(card_order(server.layout_view([])), dict(providers=[], accounts={}))

    def test_overview_uses_actual_provider_keys_and_leaves_existing_arrays_unchanged(self):
        rows = [dict(type=provider, name=provider + '-' + index + '.json', auth_index=provider + index)
                for provider in server.providers.CATALOG for index in ('a', 'b')]
        key_accounts = [dict(provider=provider, key='apikey:' + provider + ':synthetic', disabled=False,
                             label='Synthetic API key') for provider in server.api_keys.NAMES]
        gateway = dict(accounts=[], inventory=rows, api_key_accounts=key_accounts)
        before = copy.deepcopy(gateway)
        default = self.overview(gateway)
        expected_ids = [group['id'] for group in default['providers']]
        expected_accounts = {group['id']: [a['key'] for a in group['accounts']]
                             for group in default['providers']}
        self.assertEqual(default['layout'], dict(providers=expected_ids, accounts=expected_accounts))
        for row in rows:
            self.assertIn(row['name'], expected_accounts[row['type']])
        for row in key_accounts:
            self.assertEqual(expected_accounts[row['provider']], [row['key']])
        value = dict(providers=expected_ids[::-1],
                     accounts={provider: keys[::-1] for provider, keys in expected_accounts.items()})
        self.assertEqual(self.post(value)[0], 200)
        saved = self.overview(gateway)
        self.assertEqual(saved['layout'], value)
        self.assertEqual(saved['providers'], default['providers'])
        self.assertEqual(saved['gateway'], default['gateway'])
        self.assertEqual(gateway, before)

    def test_validation_rejects_invalid_shapes_identifiers_duplicates_and_limits(self):
        invalid = [None, [], True, {}, {'providers': []}, {'accounts': {}},
                   dict(providers=[], accounts={}, extra=True)]
        for value in (None, {}, '', 1, True, ['codex', 'codex'], [''], ['x' * 256],
                      [None], [1], [True], [[]], [{}], [str(i) for i in range(65)]):
            invalid.append(dict(providers=value, accounts={}))
        for value in (None, [], '', True, {'': []}, {'x' * 256: []},
                      {str(i): [] for i in range(65)}):
            invalid.append(dict(providers=[], accounts=value))
        for value in (None, {}, '', True, ['same', 'same'], [''], ['x' * 256],
                      [None], [1], [True], [[]], [{}], [str(i) for i in range(65)]):
            invalid.append(dict(providers=[], accounts={'unknown': value}))
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.post(value), (400, {'error': 'invalid_request'}))
        self.assertFalse(self.path.exists())

    def test_valid_boundaries_unknown_ids_and_non_ascii_round_trip(self):
        values = [dict(providers=[], accounts={}),
                  dict(providers=['x' * 255], accounts={'y' * 255: ['z' * 255]}),
                  dict(providers=[str(i) for i in range(64)],
                       accounts={'unknown': [str(j) for j in range(64)]}),
                  dict(providers=[], accounts={str(i): [] for i in range(64)}),
                  dict(providers=['未知'], accounts={'未知': ['clé.json']})]
        # Use compact JSON so the maximum list-count case fits the body limit.
        for value in values:
            with self.subTest(value=value):
                raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
                self.assertLessEqual(len(raw), 262144)
                self.assertEqual(self.post(raw=raw)[0], 200)
                self.assertEqual(card_order(json.loads(self.path.read_text())), value)
        unicode_groups = [dict(id='未知', accounts=[dict(key='clé.json')])]
        self.assertEqual(card_order(server.layout_view(unicode_groups)), values[-1])

    def test_body_size_type_json_and_incomplete_body(self):
        value = dict(providers=[], accounts={})
        for headers in ({'Content-Type': 'text/plain'}, {'Content-Type': 'application/json; charset=utf-8'},
                        {'Content-Length': '262145'}, {'Content-Length': '0'}, {'Content-Length': '-1'},
                        {'Content-Length': 'invalid'}, {'Content-Length': '100'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(value, headers=headers), (400, {'error': 'invalid_request'}))
        for raw in (b'', b'{', b'\xff', b'null'):
            self.assertEqual(self.post(raw=raw), (400, {'error': 'invalid_request'}))
        raw = json.dumps(value).encode()
        self.assertEqual(self.post(raw=raw + b' ' * (262144 - len(raw)))[0], 200)
        self.assertEqual(self.post(raw=raw + b' ' * (262145 - len(raw))), (400, {'error': 'invalid_request'}))

    def test_same_origin_header_host_and_read_only_protection(self):
        for headers, expected in (({'Host': 'evil.test'}, 421),
                                  ({'Origin': 'https://evil.test'}, 403),
                                  ({'Sec-Fetch-Site': 'cross-site'}, 403),
                                  ({'X-Router-Request': '0'}, 403),
                                  ({'X-Router-Read-Only': '1'}, 403)):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(dict(providers=[], accounts={}), headers=headers)[0], expected)
        self.assertFalse(self.path.exists())

    def test_missing_corrupt_and_unreadable_files_fall_back_and_log_once(self):
        logger = server.logging.getLogger(server.__name__)
        self.assertEqual(card_order(server.layout_view(self.groups)), self.default)
        for content in ('bad json', 'null', '{"providers":[],"accounts":[]}',
                        '{"providers":["codex","codex"],"accounts":{}}'):
            self.path.write_text(content)
            self.assertEqual(card_order(server.layout_view(self.groups)), self.default)
        self.path.write_bytes(b'\xff')
        self.assertEqual(card_order(server.layout_view(self.groups)), self.default)
        with patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertEqual(card_order(server.layout_view(self.groups)), self.default)
        logger.warning.assert_called_once()
        # A later successful save repairs invalid state without restarting.
        self.assertEqual(self.post(self.default)[0], 200)
        self.assertEqual(card_order(server.layout_view(self.groups)), self.default)

    def test_storage_failure_and_lock_deadline_leave_previous_file_intact(self):
        self.assertEqual(self.post(self.default)[0], 200)
        previous = self.path.read_bytes()
        with patch.object(server.os, 'replace', side_effect=OSError):
            self.assertEqual(self.post(dict(providers=[], accounts={})), (503, {'error': 'unavailable'}))
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with patch.object(server, 'LAYOUT_WRITE_LOCK') as lock:
            lock.acquire.return_value = False
            self.assertEqual(self.post(self.default), (503, {'error': 'unavailable'}))
            self.assertGreater(lock.acquire.call_args.kwargs['timeout'], 0)
            self.assertLessEqual(lock.acquire.call_args.kwargs['timeout'], server.LOCK_WAIT_SECONDS)
            lock.release.assert_not_called()
        with patch.object(server.Handler, 'read_body', side_effect=TimeoutError):
            self.assertEqual(self.post(self.default), (503, {'error': 'unavailable'}))
        self.assertEqual(self.path.read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
