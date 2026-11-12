"""Isolated API-key management; synthetic credentials never leave private fixtures."""
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import account_add
import account_access
import api_keys
import providers
import routing_policy
import server as app
import usage_adapters


class Gateway:
    def __init__(self, root, backups):
        (root / 'config').mkdir(mode=0o700)
        self.config_path = root / 'config' / 'gateway-config.yaml'
        self.backups = backups
        self.config = {'config-version': 8, 'api-keys': {},
                       'routing': {'strategy': 'round-robin', 'session-affinity': True},
                       'models': {'opaque': ['preserve']}, 'plugin': {'unknown': [1, None]}}
        self.version = '8.0.47'
        self.conflicts = 0
        self.ignore_write = self.fail_readback = self.fail_write = False
        self.puts = 0
        self.calls = []
        self.backup_counts = []
        self.persist()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                size = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(size)) if size else None
                outer.calls.append((self.command, self.path, body))
                if self.path.startswith('/v0/management/model-definitions/'):
                    return self.reply(200, {'channel': self.path.rsplit('/', 1)[-1],
                                            'models': [{'id': 'fixture-model', 'private': 'not public'}]})
                if self.path == api_keys.PREFIX:
                    return self.reply(200, outer.config)
                channel = self.path.rsplit('/', 1)[-1]
                if self.command == 'PUT':
                    outer.puts += 1
                    saved = list(outer.backups.iterdir())
                    outer.backup_counts.append(len(saved))
                    if not saved or any(stat.S_IMODE(p.stat().st_mode) != 0o600 for p in saved):
                        return self.reply(500, {'error': 'unsafe_backup'})
                    if not any(p.read_bytes() == outer.config_path.read_bytes() for p in saved):
                        return self.reply(500, {'error': 'missing_current_backup'})
                    if outer.fail_write:
                        return self.reply(500, {'error': 'private upstream config failure'})
                    if outer.conflicts:
                        outer.conflicts -= 1
                        outer.config['routing']['external-writer'] = outer.puts
                        outer.persist()
                        return self.reply(412, {'error': 'precondition_failed'})
                    if self.headers.get('If-Match') != outer.etag():
                        return self.reply(412, {'error': 'precondition_failed'})
                    if not outer.ignore_write:
                        outer.config['api-keys'][channel] = copy.deepcopy(body)
                        outer.persist()
                    return self.reply(200, {'status': 'ok'})
                if outer.fail_readback and outer.puts:
                    return self.reply(503, {'private': 'never relay upstream bodies'})
                if channel not in outer.config['api-keys']:
                    return self.reply(404, {'error': 'not_found'})
                groups = copy.deepcopy(outer.config['api-keys'][channel])
                for i, group in enumerate(groups):
                    for j, key in enumerate(group['keys']):
                        key['auth_index'] = f'{channel}-{i}-{j}'
                return self.reply(200, groups)

            do_PUT = do_GET

            def reply(self, status, body):
                # Root GET also carries injected runtime indexes in real v8.
                if self.path == api_keys.PREFIX:
                    body = copy.deepcopy(body)
                    for channel, groups in body['api-keys'].items():
                        for i, group in enumerate(groups):
                            for j, key in enumerate(group['keys']):
                                key['auth_index'] = f'{channel}-{i}-{j}'
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('X-CPA-VERSION', outer.version)
                self.send_header('ETag', outer.etag())
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def persist(self):
        self.config_path.write_bytes(json.dumps(self.config).encode())
        self.config_path.chmod(0o600)

    def etag(self):
        return '"' + hashlib.sha256(self.config_path.read_bytes()).hexdigest() + '"'

    def call(self, path, body=None, method=None, timeout=5, headers=None):
        target = path if path.startswith('/v8/') else '/v0/management' + path
        request = Request(f'http://127.0.0.1:{self.server.server_port}' + target,
                          data=json.dumps(body).encode() if body is not None else None,
                          method=method, headers=headers or {})
        try:
            with urlopen(request, timeout=timeout) as response:
                return api_keys.ConfigResponse(json.load(response), response.headers.get('ETag'),
                                               response.headers.get('X-CPA-VERSION'))
        except HTTPError as error:
            error.close()
            raise

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class APIKeyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='router-apikey-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.backups = self.root / 'private-backups'
        self.backups.mkdir(mode=0o700)
        self.gateway = Gateway(self.root, self.backups)
        self.addCleanup(self.gateway.close)
        self.changed = Mock()
        self.manager = api_keys.APIKeyManager(threading.RLock(), self.changed, self.gateway.call,
                                             self.gateway.config_path, self.backups)
        self.secret = secrets.token_urlsafe(32)

    def fields(self, provider='claude-api-key'):
        result = {'api_key': self.secret}
        if provider == 'openai-compatibility':
            result.update(name='fixture', base_url='https://example.invalid/v1',
                          models=['model-a', {'id': 'model-b', 'alias': 'public-b'}])
        return result

    def add(self, provider='claude-api-key'):
        return self.manager.add(provider, self.fields(provider))

    def test_all_provider_kinds_add_backup_and_exact_scoped_write(self):
        preserved = copy.deepcopy(self.gateway.config)
        for provider in api_keys.NAMES:
            with self.subTest(provider=provider):
                before = self.gateway.config_path.read_bytes()
                public = self.add(provider)
                self.assertEqual(public['provider'], provider)
                self.assertTrue(api_keys.owns(public['key']))
                self.assertNotIn(self.secret, json.dumps(public))
                self.assertEqual(public['label'], 'fixture' if provider == 'openai-compatibility'
                                 else 'API key ••••' + self.secret[-4:])
                self.assertTrue(any(p.read_bytes() == before for p in self.backups.iterdir()))
                stored = self.gateway.config['api-keys'][api_keys.CHANNELS[provider]][0]
                self.assertEqual(stored['keys'], [{'api-key': self.secret}])
                self.assertTrue(stored['base-url'].startswith('https://'))
        self.assertEqual(self.gateway.backup_counts, [1, 2, 3, 4, 5])
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in self.backups.iterdir()))
        for key in ('routing', 'models', 'plugin'):
            self.assertEqual(self.gateway.config[key], preserved[key])
        self.assertEqual(self.changed.call_count, 5)

    def test_validation_is_field_specific_and_does_not_touch_management(self):
        cases = [(None, {}, 'provider'), ([], {}, 'provider'), ('invalid', {}, 'provider'),
                 ('claude-api-key', None, 'fields'), ('claude-api-key', {}, 'api_key')]
        for field in ('api_key', 'base_url', 'name', 'models'):
            for invalid in ({'api_key': ['', 'tiny', 3, 'space key', 'bad\nkey'],
                             'base_url': ['', None, 'http://example.invalid', 'https://u:p@example.invalid',
                                          'https://example.invalid?key=x', 'https://example.invalid/#x',
                                          'https://example.invalid:999999'],
                             'name': ['', 'UPPER', 'has space', 'x' * 41],
                             'models': [[], None, [{'id': 'a', 'alias': ''}], ['a', 'a'],
                                        [{'id': 'a', 'extra': True}]]}[field]):
                fields = self.fields('openai-compatibility')
                fields[field] = invalid
                cases.append(('openai-compatibility', fields, field))
        for provider, fields, field in cases:
            with self.subTest(field=field, provider=repr(provider)):
                with self.assertRaises(api_keys.InvalidField) as caught:
                    self.manager.add(provider, fields)
                self.assertEqual(caught.exception.field, field)
                self.assertNotIn(self.secret, str(caught.exception))
        self.assertEqual(self.gateway.calls, [])

    def test_models_text_normalization_and_limits(self):
        fields = self.fields('openai-compatibility')
        fields['models'] = ' model-a, model-b\nmodel-a, ,\n model-c \r\n'
        public = self.manager.add('openai-compatibility', fields)
        self.assertEqual(self.gateway.config['api-keys']['openai-compatibility'][0]['models'],
                         [{'name': m, 'alias': m} for m in ('model-a', 'model-b', 'model-c')])
        self.assertEqual(self.manager.overview()[0]['key'], public['key'])
        for models in ('', ' , \n', 'has space', 'a' * 201, ','.join('m' + str(i) for i in range(201)),
                       [{'id': 'm' + str(i)} for i in range(201)]):
            with self.subTest(models_length=len(models)), self.assertRaises(api_keys.InvalidField) as caught:
                api_keys.validate('openai-compatibility', dict(fields, models=models))
            self.assertEqual(caught.exception.field, 'models')
        for models in ('one', ','.join('m' + str(i) for i in range(200)), 'one,' * 201):
            self.assertTrue(api_keys.validate('openai-compatibility', dict(fields, models=models))['models'])

    def test_duplicate_key_and_name_rejected_before_backup(self):
        self.add('openai-compatibility')
        with self.assertRaises(account_add.AccountError) as caught:
            self.add('openai-compatibility')
        self.assertEqual((caught.exception.code, caught.exception.status), ('duplicate', 409))
        fields = self.fields('openai-compatibility')
        fields['api_key'] = secrets.token_urlsafe(32)
        with self.assertRaises(api_keys.InvalidField) as caught:
            self.manager.add('openai-compatibility', fields)
        self.assertEqual(caught.exception.field, 'name')
        self.assertEqual(self.gateway.puts, 1)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_cas_retries_once_and_preserves_external_change(self):
        self.gateway.conflicts = 1
        result = self.add()
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertEqual(self.gateway.puts, 2)
        self.assertEqual(self.gateway.backup_counts, [1, 2])
        self.assertEqual(self.gateway.config['routing']['external-writer'], 1)
        self.assertEqual(len(self.gateway.config['api-keys']['claude']), 1)

    def test_second_cas_conflict_returns_conflict_and_keeps_backups(self):
        self.gateway.conflicts = 2
        with self.assertRaises(account_add.AccountError) as caught:
            self.add()
        self.assertEqual((caught.exception.code, caught.exception.status), ('account_changed', 409))
        self.assertEqual(self.gateway.puts, 2)
        self.assertEqual(len(list(self.backups.iterdir())), 2)
        self.changed.assert_not_called()

    def test_stale_disk_snapshot_retried_without_any_write(self):
        with patch.object(api_keys, 'backup_config', side_effect=account_add.AccountError('account_changed', 'changed', 409)) as backup:
            with self.assertRaises(account_add.AccountError):
                self.add()
        self.assertEqual(backup.call_count, 2)
        self.assertEqual(self.gateway.puts, 0)

    def test_readback_mismatch_or_failure_keeps_private_backup(self):
        self.gateway.ignore_write = True
        with self.assertRaises(account_add.AccountError) as caught:
            self.add()
        self.assertEqual((caught.exception.code, caught.exception.status), ('unconfirmed', 503))
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        self.changed.assert_not_called()

    def test_backup_fail_closed_for_permissions_symlinks_and_source_shape(self):
        self.backups.chmod(0o755)
        with self.assertRaises(account_add.AccountError):
            self.add()
        self.backups.chmod(0o700)
        link = self.root / 'linked-backups'
        link.symlink_to(self.backups)
        self.manager.backup_dir = link
        with self.assertRaises(account_add.AccountError):
            self.add()
        self.manager.backup_dir = self.backups
        source_link = self.root / 'config-link'
        source_link.symlink_to(self.gateway.config_path)
        self.manager.config_path = source_link
        with self.assertRaises(account_add.AccountError):
            self.add()
        self.assertEqual(self.gateway.puts, 0)

    def test_world_permissions_refuse_writes_before_backup(self):
        before = copy.deepcopy(self.gateway.config)
        for mode in (0o604, 0o602, 0o601):
            with self.subTest(mode=mode):
                self.gateway.config_path.chmod(mode)
                with self.assertRaises(account_add.AccountError) as caught:
                    self.add()
                self.assertEqual(caught.exception.code, 'config_unsafe')
                self.assertIn('world permissions', caught.exception.message)
                self.assertNotIn(self.secret, caught.exception.message)
        self.assertEqual(self.gateway.puts, 0)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(self.gateway.config, before)
        self.changed.assert_not_called()
        self.gateway.config_path.chmod(0o660)
        self.add()  # Group access is intentional for the gateway service.

    def test_removal_distinguishes_upstream_identity_fields(self):
        for field, value in (('prefix', 'other'), ('proxy-url', 'https://proxy.invalid'),
                             ('headers', {'X-Fixture': 'other'})):
            for removed in (0, 1):
                with self.subTest(field=field, removed=removed):
                    first = api_keys.validate('claude-api-key', self.fields())
                    second = copy.deepcopy(first)
                    second[field] = value
                    self.gateway.config['api-keys']['claude'] = [first, second]
                    self.gateway.persist()
                    rows = self.manager.overview()
                    self.assertNotEqual(rows[0]['key'], rows[1]['key'])
                    row = rows[removed]
                    self.manager.remove(row['key'], row['auth_index'], self.secret[-4:])
                    self.assertEqual(self.gateway.config['api-keys']['claude'], [second if removed == 0 else first])

    def test_auth_index_disambiguates_identical_config_entries(self):
        for removed in (0, 1):
            first = api_keys.validate('claude-api-key', self.fields())
            second = copy.deepcopy(first)
            second['disabled'] = True  # Does not change upstream identity.
            self.gateway.config['api-keys']['claude'] = [first, second]
            self.gateway.persist()
            rows = self.manager.overview()
            self.assertEqual(rows[0]['key'], rows[1]['key'])
            with self.assertRaises(account_add.AccountError):
                self.manager.remove(rows[0]['key'], confirm_text=self.secret[-4:])
            self.manager.remove(rows[removed]['key'], rows[removed]['auth_index'], self.secret[-4:])
            self.assertEqual(self.gateway.config['api-keys']['claude'], [second if removed == 0 else first])

    def test_backup_hash_mismatch_fails_closed(self):
        with self.assertRaises(account_add.AccountError) as caught:
            api_keys.backup_config('"' + '0' * 64 + '"', self.gateway.config_path, self.backups)
        self.assertEqual(caught.exception.code, 'account_changed')
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_unsupported_gateway_or_missing_etag_refuses_write(self):
        self.gateway.version = '8.0.46'
        with self.assertRaises(account_add.AccountError):
            self.add()
        self.assertEqual(self.gateway.puts, 0)
        with patch.object(self.manager, 'call', return_value=api_keys.ConfigResponse([], None, '8.0.47')):
            with self.assertRaises(account_add.AccountError):
                self.add()
        self.assertEqual(self.gateway.puts, 0)

    def test_remove_confirmation_auth_index_and_preserve_other_keys(self):
        public = self.add()
        fields = {'api_key': secrets.token_urlsafe(32)}
        other = self.manager.add('claude-api-key', fields)
        for confirmation in (None, '', 'wrong', self.secret, public['label']):
            with self.assertRaises(account_add.AccountError) as caught:
                self.manager.remove(public['key'], confirm_text=confirmation)
            self.assertEqual(caught.exception.code, 'confirm_mismatch')
        with self.assertRaises(account_add.AccountError) as caught:
            self.manager.remove(public['key'], 'wrong-index', self.secret[-4:])
        self.assertEqual(caught.exception.code, 'account_changed')
        result = self.manager.remove(public['key'], 'claude-0-0', '  ' + self.secret[-4:].swapcase() + '  ')
        self.assertEqual(result, dict(disconnected=True, key=public['key'], last_ready=False))
        self.assertEqual([row['key'] for row in self.manager.overview()], [other['key']])
        self.assertTrue(self.manager.remove(other['key'], confirm_text=fields['api_key'][-4:])['last_ready'])
        self.assertEqual(self.gateway.config['api-keys']['claude'], [])

    def test_remove_one_key_from_existing_multi_key_compatible_group(self):
        group = api_keys.validate('openai-compatibility', self.fields('openai-compatibility'))
        second = secrets.token_urlsafe(32)
        group['keys'].append({'api-key': second, 'proxy-url': 'opaque'})
        group['headers'] = {'X-Fixture': 'keep', 'auth_index': 'keep-this-header'}
        group['keys'][1]['disabled'] = True
        self.gateway.config['api-keys']['openai-compatibility'] = [group]
        self.gateway.persist()
        rows = self.manager.overview()
        self.assertEqual(len(rows), 2)
        self.assertTrue(self.manager.remove(rows[0]['key'], rows[0]['auth_index'], '  FIXTURE  ')['last_ready'])
        self.assertEqual(group['models'], self.gateway.config['api-keys']['openai-compatibility'][0]['models'])
        self.assertEqual(group['headers'], self.gateway.config['api-keys']['openai-compatibility'][0]['headers'])
        self.assertEqual(self.gateway.config['api-keys']['openai-compatibility'][0]['keys'],
                         [{'api-key': second, 'proxy-url': 'opaque', 'disabled': True}])

    def test_projection_models_host_only_usage_join_and_no_secret_fields(self):
        for provider in api_keys.NAMES:
            self.add(provider)
        channels = [dict(name=c) for p, c in api_keys.CHANNELS.items() if p != 'openai-compatibility']
        cache = providers.ModelsCache(load=api_keys.model_definitions)
        self.assertIsNone(self.manager.overview()[0]['models'])
        for channel in channels:
            cache.fetch(channel, self.gateway.call)
        rows = self.manager.overview(models=cache.view(channels))
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertEqual(row['confirm_text'], 'fixture' if row['provider'] == 'openai-compatibility'
                             else self.secret[-4:])
        grouped = providers.groups({'api_key_accounts': rows}, [], {})
        self.assertEqual([a['confirm_text'] for p in grouped for a in p['accounts']],
                         [row['confirm_text'] for row in rows])
        usage = {'providers': {'claude': {'today_accounts': [
            {'auth_index': rows[0]['auth_index'], 'requests': 5, 'failures': 1, 'hours': [0] * 24}]},
            'codex': {'today_accounts': [{'auth_index': rows[0]['auth_index'], 'requests': 99}]},
            'openai-compatible-fixture': {'today_accounts': [
                {'auth_index': rows[-1]['auth_index'], 'requests': 3, 'failures': 0}]}}}
        projected = providers.groups({'inventory': [], 'api_key_accounts': rows}, [], usage)
        self.assertEqual(len(projected), 5)
        self.assertEqual(projected[0]['accounts'][0]['today']['success_rate'], .8)
        self.assertEqual(projected[-1]['accounts'][0]['today']['requests'], 3)
        for row in rows:
            self.assertEqual(row['kind'], 'api_key')
            self.assertEqual(row['usage_refresh'], dict(supported=False, available_at=None))
            self.assertNotIn('/', row['base_url'])
            self.assertTrue(row['models'])
            self.assertNotIn('private', json.dumps(row))
            self.assertNotIn(self.secret, json.dumps(row))
            self.assertNotIn('api-key', row)
        self.assertEqual(rows[-1]['models'], dict(count=2, items=[{'id': 'model-a'},
                         {'id': 'model-b', 'display_name': 'public-b'}]))
        self.assertEqual(rows[0]['models'], dict(count=1, items=[{'id': 'fixture-model'}]))
        self.assertIsNone(self.manager.overview(models={'claude': dict(count=0, items=[])})[0]['models'])

    def test_compatible_group_keeps_provider_name_and_each_account_label(self):
        for name in ('first', 'second'):
            fields = dict(self.fields('openai-compatibility'), name=name, api_key=self.secret + name)
            self.manager.add('openai-compatibility', fields)
        rows = self.manager.overview()
        usage = {'providers': {f'openai-compatible-{row["label"]}': {'today_accounts': [
            dict(auth_index=row['auth_index'], requests=i + 1)]} for i, row in enumerate(rows)}}
        group = api_keys.groups(rows, usage)[0]
        self.assertEqual(group['name'], api_keys.NAMES['openai-compatibility'])
        self.assertEqual([a['label'] for a in group['accounts']], ['first', 'second'])
        self.assertEqual([a['today']['requests'] for a in group['accounts']], [1, 2])

    def test_api_key_runtime_rows_excluded_from_identity_routing_usage_and_generic_cards(self):
        for provider in ('codex', 'claude', 'xai'):
            api_row = dict(type=provider, name=provider + ':apikey:hash', auth_index='api',
                           email='fixture@example.invalid', label=provider + '-apikey',
                           account_type='api_key', runtime_only=True, source='memory')
            oauth = dict(type=provider, name='oauth.json', auth_index='oauth', email='oauth@example.invalid')
            rows = [oauth, api_row]
            self.assertEqual(routing_policy.identities(provider, rows), ('oauth@example.invalid',))
            self.assertEqual(list(routing_policy.records(provider=provider, inventory=rows)), ['oauth@example.invalid'])
            self.assertEqual(len(routing_policy.membership(rows, provider)), 1)
            self.assertEqual(len(routing_policy.inventory_snapshot(lambda _: {'files': rows})), 1)
            self.assertEqual(len(providers.inventory_view(rows)), 1)
            self.assertEqual(len(providers.groups({'inventory': rows}, [])[0]['accounts']), 1)
            self.assertIsNone(account_access.identity(api_row))
            self.assertIsNone(account_access.claude_routing_id(api_row))
            if provider == 'codex':
                self.assertEqual(account_access.AccountAccess().reconcile(rows, Mock()), [oauth])
            if provider == 'claude':
                self.assertEqual(len(app.claude_routing_projection(rows, 'fixture')), 1)
            cache = usage_adapters.UsageCache()
            call = Mock(side_effect=AssertionError('API keys must not issue OAuth quota reads'))
            self.assertFalse(cache.available(api_row)['supported'])
            cache.fetch(api_row, call)
            call.assert_not_called()


class APIKeyHTTPTests(unittest.TestCase):
    # Inherit helpers only; behavioral API cases remain on the base class.
    fields = APIKeyTests.fields
    add = APIKeyTests.add

    def setUp(self):
        APIKeyTests.setUp(self)
        self.state = Mock()
        self.state.api_keys = self.manager
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.httpd.state = self.state
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        def close():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(2)
        self.addCleanup(close)

    def request(self, data, headers=None, path='/api/accounts/api-keys/add'):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=5)
        connection.request('POST', path, json.dumps(data),
                           headers={'Host': 'router.localhost', 'X-Router-Request': '1',
                                    'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        connection.close()
        self.assertNotIn(self.secret, json.dumps(result))
        return result

    def test_http_add_invalid_duplicate_and_no_store(self):
        data = {'provider': 'claude-api-key', 'fields': self.fields()}
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            status, public, headers = self.request(data, {'X-Router-Transport': 'tailscale'})
            self.assertEqual(status, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(self.request(data)[0], 409)
            data['fields']['base_url'] = 'http://example.invalid'
            self.assertEqual(self.request(data)[:2], (400, {'error': 'invalid_field', 'field': 'base_url'}))
            self.assertNotIn(self.secret, log.getvalue())

    def test_http_failed_gateway_config_write_is_clear_and_changes_nothing(self):
        before = copy.deepcopy(self.gateway.config)
        raw = self.gateway.config_path.read_bytes()
        self.gateway.fail_write = True
        status, result, _ = self.request({'provider': 'claude-api-key', 'fields': self.fields()})
        self.assertEqual((status, result), (503, {'error': 'config_write_failed',
                         'message': "The gateway can't save its config."}))
        self.assertEqual(self.gateway.config, before)
        self.assertEqual(self.gateway.config_path.read_bytes(), raw)
        self.changed.assert_not_called()

    def test_http_host_csrf_origin_and_read_only_refused_before_management(self):
        data = {'provider': 'claude-api-key', 'fields': self.fields()}
        for headers, status in (({'Host': 'example.invalid'}, 421), ({'X-Router-Request': ''}, 403),
                                ({'Origin': 'https://example.invalid'}, 403),
                                ({'Sec-Fetch-Site': 'cross-site'}, 403), ({'X-Router-Read-Only': '1'}, 403)):
            self.assertEqual(self.request(data, headers)[0], status)
        self.assertEqual(self.gateway.calls, [])

    def test_http_disconnect_requires_confirmation_and_handles_optional_auth_index(self):
        public = self.add()
        data = dict(key=public['key'], confirmed=True, confirm_text=self.secret[-4:])
        data['confirmed'] = False
        self.assertEqual(self.request(data, path='/api/accounts/disconnect')[0], 400)
        data['confirmed'] = True
        data['confirm_text'] = 'wrong'
        self.assertEqual(self.request(data, path='/api/accounts/disconnect')[0], 400)
        data['confirm_text'] = '  ' + self.secret[-4:].swapcase() + '  '
        status, result, _ = self.request(data, path='/api/accounts/disconnect')
        self.assertEqual(status, 200)
        self.assertTrue(result['disconnected'])

    def test_http_compatible_add_accepts_ui_text_fields(self):
        fields = self.fields('openai-compatibility')
        fields['models'] = 'model-a, model-b\nmodel-a'
        status, public, _ = self.request({'provider': 'openai-compatibility', 'fields': fields})
        self.assertEqual(status, 200)
        self.assertEqual(self.manager.overview()[0]['confirm_text'], 'fixture')
        status, result, _ = self.request(dict(key=public['key'], confirmed=True, confirm_text='  FIXTURE  '),
                                         {'X-Router-Transport': 'tailscale'}, path='/api/accounts/disconnect')
        self.assertEqual(status, 200)
        self.assertTrue(result['disconnected'])

    def test_http_upstream_readback_failure_never_relays_body(self):
        self.gateway.fail_readback = True
        status, result, _ = self.request({'provider': 'claude-api-key', 'fields': self.fields()})
        self.assertEqual(status, 503)
        self.assertEqual(result, {'error': 'gateway_error'})


if __name__ == '__main__':
    unittest.main()
