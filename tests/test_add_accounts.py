"""Isolated OAuth and file mutations against a synthetic HTTP management gateway."""
import copy
import fcntl
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import account_add
import account_mutations
import credential_replace
import providers
import routing_policy
import server as app
import signin

# Independent response fixtures from v8.0.47 provider implementations/tests.
LOGIN_URLS = {
    'claude': 'https://claude.ai/oauth/authorize?state=fixture-state',
    'codex': 'https://auth.openai.com/oauth/authorize?state=fixture-state',
    'antigravity': 'https://accounts.google.com/o/oauth2/v2/auth?state=fixture-state',
    'xai': 'https://accounts.x.ai/oauth2/device?user_code=ABCD-1234',
    'kimi': 'https://auth.kimi.com/authorize?user_code=ABCD-1234',
    'kimi-ai': 'https://auth.kimi.ai/authorize?user_code=ABCD-1234',
    'meta': 'https://auth.meta.com/device?user_code=TEST-1234',
}


class Gateway:
    def __init__(self, directory):
        self.directory = directory
        self.rows, self.calls = [], []
        self.version, self.status = '8.0.47', 'wait'
        self.login_url = None
        self.is_webui = True
        self.on_list = None
        self.lists = 0
        self.ignore_patch = self.ignore_delete = False
        self.unavailable = False
        self.before_delete = None
        self.cancelled = True
        self.runtime_models = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if outer.unavailable:
                    self.send_response(503)
                    self.end_headers()
                    return
                split = urlsplit(self.path)
                query = parse_qs(split.query)
                size = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(size)) if size else None
                outer.calls.append((self.command, self.path, body))
                if split.path == '/v0/management/config':
                    result = {'routing': {}}
                elif split.path == '/v0/management/auth-files' and self.command == 'GET':
                    outer.lists += 1
                    if outer.on_list:
                        outer.on_list(outer.lists)
                    if not outer.ignore_delete:
                        outer.rows = [r for r in outer.rows if (outer.directory / r['name']).exists()]
                    result = {'files': copy.deepcopy(outer.rows)}
                elif split.path == '/v0/management/auth-files/models':
                    result = {'models': outer.runtime_models}
                elif split.path == '/v8/management/oauth/auth-url':
                    provider = query['provider'][0]
                    if query.get('is_webui') != ['true' if outer.is_webui and provider in ('claude', 'codex', 'antigravity') else 'false']:
                        raise AssertionError('Incorrect WebUI callback mode')
                    flow = 'browser' if provider in ('claude', 'codex', 'antigravity') else 'device'
                    result = {'status': 'ok', 'url': outer.login_url or LOGIN_URLS[provider],
                              'state': 'fixture-state', 'flow': flow, 'expires_in': 60}
                    if flow == 'device':
                        result['user_code'] = 'SYNTHETIC'
                elif split.path == '/v8/management/oauth/status':
                    result = {'status': outer.status, 'error': 'untrusted-gateway-detail'}
                elif split.path in ('/v8/management/oauth/session', '/v8/management/oauth/callback'):
                    result = {'status': 'ok', 'cancelled': outer.cancelled}
                elif split.path == '/v0/management/auth-files/status':
                    if not outer.ignore_patch:
                        outer.rows[0]['disabled'] = body['disabled']
                    result = {'status': 'ok'}
                elif split.path == '/v0/management/auth-files' and self.command == 'DELETE':
                    if outer.before_delete:
                        outer.before_delete()
                    if not outer.ignore_delete:
                        name = query['name'][0]
                        outer.rows = [r for r in outer.rows if r['name'] != name]
                        (outer.directory / name).unlink()
                    result = {'status': 'ok'}
                else:
                    raise AssertionError('Unexpected fake management route')
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('X-CPA-VERSION', outer.version)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_PATCH = do_DELETE = do_GET
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def call(self, path, body=None, method=None, timeout=5):
        route = path if path.startswith('/v8/management/') else '/v0/management' + path
        request = Request(f'http://127.0.0.1:{self.server.server_port}' + route,
                          data=json.dumps(body).encode() if body is not None else None,
                          method=method, headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=timeout) as response:
                return signin.ManagementResponse(json.load(response), response.headers.get('X-CPA-VERSION'))
        except HTTPError as error:
            error.close()
            raise

    def row(self, name='first.json', provider='xai', index='first'):
        (self.directory / name).write_text(json.dumps({'type': provider, 'fixture': 'private'}))
        row = dict(name=name, type=provider, auth_index=index, email='fixture@example.invalid',
                   path=str(self.directory / name), source='file', disabled=False,
                   unavailable=False, status='active', updated_at='before')
        self.rows.append(row)
        return row

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class AddAccountsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.auth = self.root / 'auth'
        self.auth.mkdir(mode=0o700)
        self.backups = self.root / 'disconnected'
        self.backups.mkdir(mode=0o700)
        for obj, name, value in ((routing_policy, 'STATE', self.root / 'routing'),
                                 (credential_replace, 'AUTH_DIR', str(self.auth)),
                                 (account_mutations, 'BACKUP_DIR', self.backups)):
            change = patch.object(obj, name, value)
            change.start()
            self.addCleanup(change.stop)
        self.gateway = Gateway(self.auth)
        self.addCleanup(self.gateway.close)
        self.clock = [1800000000.0]
        self.changed = Mock()
        self.lock = threading.RLock()
        self.adds = account_add.AddManager(self.gateway.call, self.lock, self.changed, lambda: self.clock[0])
        self.mutations = account_mutations.AccountMutations(self.gateway.call, self.lock, self.changed,
                                                           app.GatewayState.other_ready_accounts)

    def start(self, provider='xai'):
        return self.adds.start(provider)['session']

    def test_browser_auto_callback_and_device_field(self):
        for provider in LOGIN_URLS:
            with self.subTest(provider=provider):
                result = self.adds.start(provider)
                self.assertIs(result['auto_callback'], provider in ('claude', 'codex', 'antigravity'))
                if result['flow'] == 'browser':
                    port, path = account_add.FLOWS[provider][2:]
                    callback = f'http://localhost:{port}{path}?code=synthetic&state=fixture-state'
                    self.assertEqual(self.adds.callback(result['session'], callback), {'accepted': True})
                self.adds.cancel(result['session'])

    def busy_start(self, state=None, cancelled=True, error='failed to start callback server'):
        calls = []
        def call(path, body=None, method=None, **kwargs):
            calls.append((method, path))
            if '/auth-url?' in path and 'is_webui=true' in path:
                payload = dict(error=error)
                if state:
                    payload['state'] = state
                raise HTTPError('http://fixture.invalid', 500, 'failure', {},
                                io.BytesIO(json.dumps(payload).encode()))
            if method == 'DELETE':
                return dict(status='ok', cancelled=cancelled)
            return self.gateway.call(path, body, method, **kwargs)
        # Only this test's fallback may start a browser flow without a listener.
        original = self.gateway.call
        def fallback(path, body=None, method=None, **kwargs):
            if '/auth-url?' in path:
                return dict(status='ok', state='fallback-state', flow='browser',
                            url='https://auth.openai.com/oauth/authorize?state=fallback-state')
            return original(path, body, method, **kwargs)
        self.adds.call = call
        return calls, patch.object(self.gateway, 'call', fallback)

    def test_busy_listener_cleans_known_session_before_single_paste_retry(self):
        calls, fallback = self.busy_start('failed-state')
        with fallback:
            result = self.adds.start('codex')
        self.assertFalse(result['auto_callback'])
        oauth_calls = [(method, path) for method, path in calls if '/oauth/' in path]
        self.assertEqual(oauth_calls, [
            (None, account_add.PREFIX + 'auth-url?provider=codex&is_webui=true'),
            ('DELETE', account_add.PREFIX + 'session?state=failed-state'),
            (None, account_add.PREFIX + 'auth-url?provider=codex&is_webui=false')])
        self.assertEqual(self.adds.callback(result['session'],
            'http://localhost:1455/auth/callback?code=synthetic&state=fallback-state'), {'accepted': True})

    def test_stock_listener_error_without_state_retries_once(self):
        calls, fallback = self.busy_start()
        with fallback:
            self.assertFalse(self.adds.start('codex')['auto_callback'])
        self.assertEqual(sum('/auth-url?' in path for _, path in calls), 2)
        self.assertFalse(any(method == 'DELETE' for method, _ in calls))

    def test_listener_cleanup_refusal_retains_busy_slot_and_does_not_retry(self):
        calls, fallback = self.busy_start('failed-state', cancelled=False)
        with fallback, self.assertRaises(account_add.AccountError):
            self.adds.start('codex')
        self.assertEqual(sum('/auth-url?' in path for _, path in calls), 1)
        with self.assertRaises(account_add.AccountError) as caught:
            self.adds.start('xai')
        self.assertEqual(caught.exception.code, 'busy')

    def test_other_start_errors_do_not_fallback(self):
        calls, fallback = self.busy_start(error='untrusted private detail')
        with fallback, self.assertRaises(account_add.AccountError) as caught:
            self.adds.start('codex')
        self.assertNotIn('untrusted', str(caught.exception))
        self.assertEqual(sum('/auth-url?' in path for _, path in calls), 1)

    def test_gateway_version_policy_table_and_fresh_start_gate(self):
        for version, supported, tested in (
                ('8.0.46', False, False), ('8.0.47', True, True),
                ('8.0.52', True, False), ('8.1.0', True, False),
                ('9.0.0', False, False), ('dev', False, False),
                ('v8.0.47', False, False), ('8.0.47-dev', False, False),
                ('8.0', False, False), ('8.00.47', False, False), (None, False, False)):
            with self.subTest(version=version):
                self.assertEqual(account_add.gateway_compat(version),
                                 dict(version=version, tested=tested, supported=supported))
                self.assertEqual(bool(account_add.catalog(version)), supported)
                self.gateway.version = version or ''
                if supported:
                    handle = self.start()
                    self.adds.cancel(handle)
                else:
                    with self.assertRaises(account_add.AccountError) as caught:
                        self.start()
                    self.assertEqual(caught.exception.code, 'unsupported_gateway')

    def test_email_less_confirmation_uses_provider_display_names(self):
        for provider in (*providers.CATALOG, 'other-provider'):
            with self.subTest(provider=provider):
                self.assertEqual(account_add.confirmation_text(dict(type=provider, name='fixture.json', label='Card')),
                                 providers.catalog(provider)['name'])

    def test_kimi_names_distinguish_code_regions_across_catalogs_and_confirmation(self):
        names = {row['id']: row['name'] for row in account_add.catalog('8.0.47')}
        for provider, expected in (('kimi', 'Kimi Code (China)'), ('kimi-ai', 'Kimi Code (Global)')):
            self.assertEqual(names[provider], expected)
            self.assertEqual(providers.catalog(provider)['name'], expected)
            self.assertEqual(account_add.confirmation_text(dict(type=provider)), expected)

    def test_disconnect_typed_confirmation_missing_mismatch_and_provider(self):
        row = self.gateway.row()
        for text in (None, '', 'wrong@example.invalid', True):
            with self.subTest(text=text), self.assertRaises(account_add.AccountError) as caught:
                self.mutations.apply('first.json', 'first', disconnect=True, confirm_text=text)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, 'confirm_mismatch'))
            self.assertTrue((self.auth / 'first.json').exists())
            self.assertEqual(list(self.backups.iterdir()), [])
        row.pop('email')
        row['label'] = 'My card'
        for text in ('My card', 'first.json'):
            with self.assertRaises(account_add.AccountError):
                self.mutations.apply('first.json', 'first', disconnect=True, confirm_text=text)
        self.assertTrue(self.mutations.apply('first.json', 'first', disconnect=True,
                                            confirm_text='  GROK  ')['disconnected'])

    def test_disconnect_accepts_newer_supported_gateway_and_trimmed_email(self):
        for version in ('8.0.52', '8.1.0'):
            self.gateway.version = version
            self.gateway.row()
            self.assertTrue(self.mutations.apply('first.json', 'first', disconnect=True,
                confirm_text='  FIXTURE@EXAMPLE.INVALID  ')['disconnected'])

    def test_device_happy_path_busy_success_and_normal_refresh(self):
        handle = self.start()
        with self.assertRaises(account_add.AccountError) as caught:
            self.start('codex')
        self.assertEqual(caught.exception.code, 'busy')
        self.assertEqual(self.adds.status(handle), {'state': 'waiting'})
        self.gateway.row()
        self.gateway.status = 'ok'
        result = self.adds.status(handle)
        self.assertEqual(result, {'state': 'ok', 'account': {'key': 'first.json', 'provider': 'xai',
                         'label': 'fixture@example.invalid', 'replaced': False}})
        self.adds.status(handle)
        self.changed.assert_called_once()
        self.assertEqual(self.changed.call_args.args[0]['name'], 'first.json')
        self.assertNotIn('gateway_state', json.dumps(result))

    def test_upstream_provider_signin_origins_accept_exact_https_hosts_only(self):
        for provider, valid in LOGIN_URLS.items():
            with self.subTest(provider=provider):
                self.gateway.login_url = valid
                handle = self.start(provider)
                self.adds.cancel(handle)
                host = urlsplit(valid).hostname
                for invalid in (valid.replace('https:', 'http:'),
                                valid.replace(host, host + '.evil.invalid'),
                                valid.replace(host, 'unverified.' + host),
                                valid.replace(host, host + ':444'),
                                valid.replace(host, 'user@' + host)):
                    self.gateway.login_url = invalid
                    with self.assertRaises(account_add.AccountError):
                        self.start(provider)

    def test_invalid_start_retains_busy_session_until_cleanup_is_confirmed(self):
        for unavailable in (False, True):
            with self.subTest(cancel_unavailable=unavailable):
                self.adds.sessions.clear()
                self.gateway.login_url = LOGIN_URLS['claude'].replace('fixture-state', 'wrong-state')
                self.gateway.cancelled = False
                self.gateway.status = 'wait'
                original = self.adds.call
                def call(path, *args, **kwargs):
                    if unavailable and kwargs.get('method') == 'DELETE':
                        raise OSError('private failure')
                    return original(path, *args, **kwargs)
                with patch.object(self.adds, 'call', call):
                    with self.assertRaises(account_add.AccountError) as caught:
                        self.start('claude')
                    self.assertEqual(caught.exception.code, 'gateway_error')
                    retained = next(iter(self.adds.sessions.values()))
                    self.assertEqual(retained['result']['state'], 'finishing')
                    starts = sum('/auth-url?' in path for _, path, _ in self.gateway.calls)
                    with self.assertRaises(account_add.AccountError) as caught:
                        self.start('claude')
                    self.assertEqual(caught.exception.code, 'busy')
                    self.assertEqual(sum('/auth-url?' in path for _, path, _ in self.gateway.calls), starts)
                self.gateway.status = 'error'
                self.gateway.login_url = None
                handle = self.start('claude')
                self.gateway.cancelled = True
                self.adds.cancel(handle)

    def test_cancel_and_expiry_retain_busy_slot_until_gateway_terminal_status(self):
        for action in ('cancel', 'expire', 'unavailable_expire'):
            with self.subTest(action=action):
                self.gateway.status = 'wait'
                self.gateway.cancelled = False
                handle = self.start()
                if action == 'cancel':
                    self.assertEqual(self.adds.cancel(handle), {'cancelled': False, 'state': 'finishing'})
                else:
                    self.clock[0] += 61
                    self.gateway.unavailable = action == 'unavailable_expire'
                self.assertEqual(self.adds.status(handle)['state'], 'finishing')
                self.gateway.unavailable = False
                self.clock[0] += 700  # Active sessions must never be pruned by local expiry.
                self.assertEqual(self.adds.status(handle)['state'], 'finishing')
                with self.assertRaises(account_add.AccountError) as caught:
                    self.start('codex')
                self.assertEqual(caught.exception.code, 'busy')
                self.gateway.status = 'ok'
                self.assertEqual(self.adds.status(handle)['state'], 'ok')
                self.assertEqual(self.adds.cancel(handle), {'cancelled': False, 'state': 'ok'})

    def test_concurrent_starts_allow_only_one_pending_session(self):
        def start(provider):
            try:
                self.start(provider)
                return 'started'
            except account_add.AccountError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, ['xai', 'codex']))
        self.assertEqual(sorted(results), ['busy', 'started'])
        self.assertEqual(sum('/auth-url?' in c[1] for c in self.gateway.calls), 1)

    def test_abandoned_sessions_reconcile_on_cleanup_and_busy_start(self):
        for expired in (False, True):
            for status in ('ok', 'error', 'unknown'):
                with self.subTest(expired=expired, status=status):
                    self.gateway.status = 'wait'
                    self.gateway.cancelled = False
                    handle = self.start()
                    if expired:
                        self.clock[0] += 61
                    # These are the gateway's actual unknown/expired-state response fields.
                    self.gateway.status = 'error' if status == 'unknown' else status
                    original_call = self.adds.call
                    def call(path, *args, **kwargs):
                        if status == 'unknown' and path.startswith(account_add.PREFIX + 'status?'):
                            return {'status': 'error', 'error': 'unknown or expired state'}
                        return original_call(path, *args, **kwargs)
                    with patch.object(self.adds, 'call', side_effect=call):
                        if expired:
                            # Cleanup must work without a status request from the original tab.
                            self.adds._expire(self.adds.call)
                            self.assertEqual(self.adds.sessions[handle]['result']['state'],
                                             'ok' if status == 'ok' else 'error')
                        successor = self.start('codex')
                    self.assertNotEqual(successor, handle)
                    self.assertEqual(self.adds.sessions[handle]['result']['state'],
                                     'ok' if status == 'ok' else 'error')
                    self.gateway.cancelled = True
                    self.adds.cancel(successor)

    def test_replacement_and_ambiguous_inventory(self):
        original = self.gateway.row()
        handle = self.start()
        original['updated_at'] = 'after'
        self.gateway.status = 'ok'
        self.assertTrue(self.adds.status(handle)['account']['replaced'])
        handle = self.start()
        self.gateway.row('second.json', index='second')
        self.gateway.row('third.json', index='third')
        result = self.adds.status(handle)
        self.assertEqual(result['state'], 'ok')
        self.assertNotIn('account', result)

    def test_all_browser_callbacks_validated_and_forwarded_as_data(self):
        for provider in ('claude', 'codex', 'antigravity'):
            handle = self.start(provider)
            _, _, port, path = account_add.FLOWS[provider]
            callback = f'http://localhost:{port}{path}?code=synthetic&state=fixture-state'
            invalid = [callback.replace('localhost', 'evil.invalid'), callback.replace(str(port), '9999'),
                       callback.replace(path+'?', '/wrong?'), callback.replace('fixture-state', 'other'),
                       callback.replace('code=synthetic&', ''), callback+'&state=fixture-state',
                       callback+'&code=again', callback+'#fragment', callback.replace('http:', 'file:'),
                       callback.replace('localhost', 'user@localhost'), callback.replace('code=synthetic', 'code='),
                       callback+'&error=denied', callback+'\n']
            for url in invalid:
                with self.subTest(provider=provider), self.assertRaises(account_add.AccountError) as caught:
                    self.adds.callback(handle, url)
                self.assertEqual(caught.exception.code, 'invalid_callback')
            self.assertFalse(any(c[1].endswith('/callback') for c in self.gateway.calls))
            self.assertEqual(self.adds.callback(handle, callback), {'accepted': True})
            self.assertEqual(self.gateway.calls[-1], ('POST', account_add.PREFIX+'callback',
                             {'provider': provider, 'redirect_url': callback}))
            self.adds.cancel(handle)
            self.gateway.calls.clear()

    def test_device_rejects_callbacks_cancel_idempotent_and_expiry(self):
        handle = self.start()
        with self.assertRaises(account_add.AccountError):
            self.adds.callback(handle, 'http://localhost:1455/auth/callback?code=x&state=fixture-state')
        self.assertEqual(self.adds.cancel(handle), {'cancelled': True})
        self.adds.cancel(handle)
        self.assertEqual(self.adds.status(handle), {'state': 'cancelled'})
        self.assertEqual(sum(c[0] == 'DELETE' for c in self.gateway.calls), 1)
        handle = self.start()
        self.clock[0] += 61
        self.assertEqual(self.adds.status(handle)['state'], 'expired')
        self.start('codex')
        self.clock[0] += 700
        with self.assertRaises(account_add.AccountError):
            self.adds.status(handle)

    def test_gateway_errors_are_sanitized_transient_errors_retry(self):
        handle = self.start()
        self.gateway.unavailable = True
        self.assertEqual(self.adds.status(handle)['state'], 'waiting')
        self.gateway.unavailable = False
        self.gateway.status = 'error'
        result = self.adds.status(handle)
        self.assertEqual(result['state'], 'error')
        self.assertNotIn('untrusted-gateway-detail', json.dumps(result))

    def test_tested_version_catalog_and_origins_fail_closed(self):
        self.assertEqual(len(account_add.catalog('8.0.47')), 7)
        self.assertEqual(len(account_add.catalog('8.0.48')), 7)
        self.gateway.version = '8.0.46'
        with self.assertRaises(account_add.AccountError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, 'unsupported_gateway')
        self.assertFalse(any('/oauth/' in c[1] for c in self.gateway.calls))
        self.gateway.version = '8.0.47'
        for url in ('https://evil.invalid/', 'https://auth.x.ai.evil.invalid/', 'https://auth.x.ai:444/',
                    'http://auth.x.ai/', 'https://user@auth.x.ai/', 'https://auth.x.ai/#x'):
            self.gateway.login_url = url
            with self.assertRaises(account_add.AccountError):
                self.start()
        self.gateway.login_url = 'https://auth.openai.com/oauth/authorize?state=wrong'
        with self.assertRaises(account_add.AccountError):
            self.start('codex')

    def test_disconnect_backup_is_durable_private_before_delete_and_last_ready(self):
        for more_ready in (False, True):
            with self.subTest(more_ready=more_ready):
                self.gateway.rows.clear()
                self.gateway.row(provider='codex')
                if more_ready:
                    self.gateway.row('second.json', provider='codex', index='second')
                before = len(list(self.backups.iterdir()))
                def verify_backup_before_readback(number):
                    if not (self.auth / 'first.json').exists():
                        self.assertEqual(len(list(self.backups.iterdir())), before+1)
                self.gateway.on_list = verify_backup_before_readback
                with patch.object(app, 'fetch_account', return_value={'availability': {'state': 'ready'}}):
                    result = self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
                self.assertEqual(result, dict(disconnected=True, key='first.json', last_ready=not more_ready))
                files = list(self.backups.iterdir())
                self.assertEqual(len(files), before+1)
                for saved in files:
                    self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(json.loads(saved.read_bytes()), {'type': 'codex', 'fixture': 'private'})
                self.assertFalse((self.auth / 'first.json').exists())
                self.assertFalse(any(r['name'] == 'first.json' for r in self.gateway.rows))

    def test_disconnect_changed_identity_readback_and_unsafe_files(self):
        row = self.gateway.row(provider='codex')
        with self.assertRaises(account_add.AccountError) as caught:
            self.mutations.apply('first.json', 'stale', disconnect=True, confirm_text='fixture@example.invalid')
        self.assertEqual(caught.exception.status, 409)
        self.gateway.ignore_delete = True
        with self.assertRaises(account_add.AccountError) as caught:
            self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
        self.assertEqual(caught.exception.code, 'unconfirmed')
        self.assertTrue(list(self.backups.iterdir()))
        (self.auth / 'first.json').write_text('{"type":"codex","fixture":"private"}')
        row['source'] = 'memory'
        self.gateway.calls.clear()
        with self.assertRaises(ValueError):
            self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
        self.assertFalse(any(c[0] == 'DELETE' for c in self.gateway.calls))
        row['source'] = 'file'
        (self.auth / 'first.json').unlink()
        (self.root / 'external').write_text('fixture')
        (self.auth / 'first.json').symlink_to(self.root / 'external')
        with self.assertRaises(account_add.AccountError):
            self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')

    def test_identity_is_rechecked_after_backup_before_delete(self):
        row = self.gateway.row(provider='codex')
        def replace_on_third_read(number):
            if number == 3:
                row['auth_index'] = 'replacement'
        self.gateway.on_list = replace_on_third_read
        with self.assertRaises(account_add.AccountError) as caught:
            self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
        self.assertEqual(caught.exception.code, 'account_changed')
        self.assertFalse(any(c[0] == 'DELETE' for c in self.gateway.calls))

    def test_disconnect_holds_gateway_lock_through_backup_and_local_removal(self):
        row = self.gateway.row(provider='codex')
        source = self.auth / 'first.json'
        source.write_text('{"type":"codex","fixture":"private"}')
        original = source.read_bytes()
        real_backup = account_mutations.backup_file
        real_unlink = os.unlink
        phases = []

        def assert_locked():
            with open(str(source) + '.lock', 'a+b') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def backup(record):
            assert_locked()
            phases.append('backup')
            return real_backup(record)

        def unlink(path, *args, **kwargs):
            if path == row['name']:
                assert_locked()
                self.assertEqual(next(self.backups.iterdir()).read_bytes(), original)
                phases.append('unlink')
            return real_unlink(path, *args, **kwargs)

        def call(*args, **kwargs):
            self.assertFalse(credential_replace.holding(), 'Management calls under the file lock can deadlock')
            return self.gateway.call(*args, **kwargs)

        self.mutations.call = call
        with patch.object(account_mutations, 'backup_file', side_effect=backup), \
                patch.object(account_mutations.os, 'unlink', side_effect=unlink):
            result = self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
        self.assertTrue(result['disconnected'])
        self.assertEqual(phases, ['backup', 'unlink'])
        self.assertFalse(any(c[0] == 'DELETE' for c in self.gateway.calls))

    def test_non_codex_disconnect_backs_up_before_management_delete_and_confirms(self):
        for provider in ('claude', 'xai', 'kimi', 'kimi-ai', 'meta', 'antigravity'):
            with self.subTest(provider=provider):
                self.gateway.rows.clear()
                self.gateway.calls.clear()
                self.gateway.row(provider=provider)
                source = self.auth / 'first.json'
                original = source.read_bytes()
                before = set(self.backups.iterdir())

                def before_delete():
                    self.assertTrue(source.exists())
                    saved, = set(self.backups.iterdir()) - before
                    self.assertEqual(saved.read_bytes(), original)
                    self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                    self.assertGreaterEqual(sync.call_count, 2)
                    self.assertFalse(credential_replace.holding())

                self.gateway.before_delete = before_delete
                with patch.object(account_mutations.os, 'fsync', wraps=os.fsync) as sync:
                    result = self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
                self.assertEqual(result, dict(disconnected=True, key='first.json', last_ready=True))
                self.assertFalse(source.exists())
                self.assertEqual(self.gateway.rows, [])
                self.assertIn(('DELETE', '/v0/management/auth-files?name=first.json', None), self.gateway.calls)
                self.assertEqual(self.gateway.calls[-2][1], '/v0/management/auth-files')
                self.assertEqual(self.gateway.calls[-1][1], '/v0/management/auth-files/models?name=first.json')
        self.assertEqual(self.changed.call_count, 6)

    def test_non_codex_disconnect_retries_changing_backup_once_then_refuses(self):
        for phase in ('during_backup', 'after_backup'):
            with self.subTest(phase=phase):
                self.gateway.rows.clear()
                self.gateway.calls.clear()
                self.gateway.row()
                source = self.auth / 'first.json'
                real_copy = account_mutations.shutil.copyfileobj
                real_backup = account_mutations.backup_file
                before = len(list(self.backups.iterdir()))

                def change_during_copy(original, saved):
                    real_copy(original, saved)
                    with source.open('ab') as changed:
                        changed.write(b' ')

                def change_after_backup(record):
                    result = real_backup(record)
                    with source.open('ab') as changed:
                        changed.write(b' ')
                    return result

                target, replacement = ((account_mutations.shutil, 'copyfileobj'), change_during_copy) \
                    if phase == 'during_backup' else ((account_mutations, 'backup_file'), change_after_backup)
                with patch.object(*target, side_effect=replacement) as backup, \
                        self.assertRaises(account_add.AccountError) as caught:
                    self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
                self.assertEqual((caught.exception.status, caught.exception.code), (409, 'account_changed'))
                self.assertEqual(backup.call_count, 2)
                self.assertEqual(len(list(self.backups.iterdir())), before + 2)
                self.assertTrue(source.exists())
                self.assertFalse(any(c[0] == 'DELETE' for c in self.gateway.calls))
        self.changed.assert_not_called()

    def test_non_codex_disconnect_succeeds_when_retry_has_stable_backup(self):
        self.gateway.row()
        source = self.auth / 'first.json'
        real_backup = account_mutations.backup_file
        attempts = []

        def refresh_once(record):
            result = real_backup(record)
            attempts.append(result)
            if len(attempts) == 1:
                source.write_text('{"type":"xai","fixture":"refreshed"}')
            return result

        with patch.object(account_mutations, 'backup_file', side_effect=refresh_once):
            self.assertTrue(self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')['disconnected'])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(list(self.backups.iterdir())), 2)
        self.assertTrue(any(json.loads(saved.read_bytes())['fixture'] == 'refreshed'
                            for saved in self.backups.iterdir()))

    def test_disconnect_refuses_unsupported_gateway_before_backup_or_removal(self):
        self.gateway.version = '8.0.46'
        for provider in ('codex', 'claude', 'xai'):
            self.gateway.rows.clear()
            self.gateway.row(provider=provider)
            with self.subTest(provider=provider), self.assertRaises(account_add.AccountError) as caught:
                self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
            self.assertEqual((caught.exception.status, caught.exception.code), (409, 'disconnect_unsafe'))
            self.assertTrue((self.auth / 'first.json').exists())
            self.assertEqual(list(self.backups.iterdir()), [])
            self.assertFalse(any(c[0] == 'DELETE' for c in self.gateway.calls))

    def test_non_codex_disconnect_requires_disk_inventory_and_model_readback(self):
        for lingering in ('file', 'inventory', 'models'):
            with self.subTest(lingering=lingering):
                self.gateway.rows.clear()
                self.gateway.row()
                source = self.auth / 'first.json'
                self.gateway.ignore_delete = lingering in ('file', 'inventory')
                self.gateway.before_delete = source.unlink if lingering == 'inventory' else None
                self.gateway.runtime_models = [{'id': 'synthetic-model'}] if lingering == 'models' else []
                with patch.object(account_mutations.time, 'sleep'), \
                        self.assertRaises(account_add.AccountError) as caught:
                    self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
                self.assertEqual((caught.exception.status, caught.exception.code), (503, 'unconfirmed'))
                self.assertTrue(list(self.backups.iterdir()))
        self.changed.assert_not_called()

    def test_non_codex_disconnect_refuses_unsafe_file_shapes(self):
        for shape in ('virtual', 'runtime', 'external', 'symlink', 'directory'):
            with self.subTest(shape=shape):
                self.gateway.rows.clear()
                row = self.gateway.row()
                source = self.auth / 'first.json'
                if shape == 'virtual':
                    row['source'] = 'memory'
                elif shape == 'runtime':
                    row['runtime_only'] = True
                elif shape == 'external':
                    row['path'] = str(self.root / 'outside.json')
                elif shape == 'symlink':
                    source.unlink()
                    outside = self.root / 'outside.json'
                    outside.write_text('fixture')
                    source.symlink_to(outside)
                else:
                    source.unlink()
                    source.mkdir()
                with self.assertRaises(account_add.AccountError) as caught:
                    self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
                self.assertEqual((caught.exception.status, caught.exception.code), (409, 'disconnect_unsafe'))
                self.assertFalse(any(c[0] == 'DELETE' for c in self.gateway.calls))
                self.assertEqual(list(self.backups.iterdir()), [])
                if shape == 'directory':
                    source.rmdir()
                else:
                    source.unlink()

    def test_non_codex_disconnect_rechecks_identity_before_backup(self):
        for change in ('mismatch', 'duplicate'):
            with self.subTest(change=change):
                self.gateway.rows.clear()
                self.gateway.lists = 0
                row = self.gateway.row()

                def change_identity(number):
                    if number == 3:
                        if change == 'mismatch':
                            row['auth_index'] = 'successor'
                        else:
                            self.gateway.rows.append(dict(row))

                self.gateway.on_list = change_identity
                with self.assertRaises(account_add.AccountError) as caught:
                    self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
                self.assertEqual((caught.exception.status, caught.exception.code), (409, 'account_changed'))
                self.assertTrue((self.auth / 'first.json').exists())
                self.assertEqual(list(self.backups.iterdir()), [])
                self.assertFalse(any(c[0] == 'DELETE' for c in self.gateway.calls))

    def test_disconnect_does_not_confirm_while_runtime_models_remain(self):
        self.gateway.row(provider='codex')
        self.gateway.runtime_models = [{'id': 'synthetic-model'}]
        with self.assertRaises(account_add.AccountError) as caught:
            self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
        self.assertEqual(caught.exception.code, 'unconfirmed')

    def test_disconnect_preserves_successor_replaced_after_inventory_binding(self):
        self.gateway.row(provider='codex')
        source = self.auth / 'first.json'
        successor = self.auth / 'replacement.tmp'
        successor.write_text('{"type":"codex","fixture":"successor"}')
        def replace_on_final_identity_read(number):
            if number == 3:
                os.replace(successor, source)
        self.gateway.on_list = replace_on_final_identity_read
        with self.assertRaises(account_add.AccountError) as caught:
            self.mutations.apply('first.json', 'first', disconnect=True, confirm_text='fixture@example.invalid')
        self.assertEqual(caught.exception.code, 'account_changed')
        self.assertEqual(json.loads(source.read_text())['fixture'], 'successor')
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_generic_routing_readback_and_existing_provider_refusal(self):
        row = self.gateway.row()
        self.assertEqual(self.mutations.apply('first.json', 'first', False), {'key': 'first.json', 'enabled': False})
        public = providers.groups({'inventory': self.gateway.rows}, [])[0]['accounts'][0]
        self.assertFalse(public['routing_enabled'])
        self.gateway.ignore_patch = True
        with self.assertRaises(account_add.AccountError) as caught:
            self.mutations.apply('first.json', 'first', True)
        self.assertEqual(caught.exception.code, 'unconfirmed')
        row['type'] = 'codex'
        with self.assertRaises(account_add.AccountError):
            self.mutations.apply('first.json', 'first', True)


class AccountHTTPTests(unittest.TestCase):
    # Reuse isolated transport setup, but don't run base behavioral cases twice.
    def setUp(self):
        AddAccountsTests.setUp(self)
        self.state = Mock()
        self.state.adds = self.adds
        self.state.account_mutations = self.mutations
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.httpd.state = self.state
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        def close():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(2)
        self.addCleanup(close)

    def request(self, path, data=None, headers=None, method='POST'):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=5)
        connection.request(method, path, json.dumps(data) if method == 'POST' else None,
            headers={'Host': 'router.localhost', 'X-Router-Request': '1', 'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_overview_catalog_and_generic_account_projection(self):
        self.gateway.row()
        self.state.snapshot.return_value = {'accounts': [], 'inventory': providers.inventory_view(self.gateway.rows),
                                            'gateway_version': '8.0.47'}
        self.state.notices.view.return_value = []
        with patch.object(app, 'watch_accounts', return_value=[]), \
                patch.object(app, 'usage_summary', return_value={}), patch.object(app, 'health', return_value={}):
            status, result = self.request('/api/overview', method='GET')
            self.assertEqual(status, 200)
            self.assertEqual(len([p for p in result['add_providers'] if p['kind'] == 'oauth']), 7)
            self.assertEqual(len([p for p in result['add_providers'] if p['kind'] == 'api_key']), 5)
            self.assertEqual(result['gateway_compat'], dict(version='8.0.47', tested=True, supported=True))
            self.assertTrue(result['providers'][0]['accounts'][0]['routing_enabled'])
            self.assertEqual(result['providers'][0]['accounts'][0]['confirm_text'], 'fixture@example.invalid')
            self.state.snapshot.return_value['inventory'][0].pop('email')
            result = self.request('/api/overview', method='GET')[1]
            self.assertEqual(result['providers'][0]['accounts'][0]['confirm_text'], 'Grok')
            self.state.snapshot.return_value['gateway_version'] = '8.0.48'
            self.assertEqual(len(self.request('/api/overview', method='GET')[1]['add_providers']), 12)
            self.state.snapshot.return_value['gateway_version'] = '8.0.46'
            self.assertEqual(self.request('/api/overview', method='GET')[1]['add_providers'], [])

    def test_browser_add_transport_controls_callback_listener(self):
        for provider in ('claude', 'codex', 'antigravity'):
            for transport in ('local', 'ssh', 'tailscale'):
                with self.subTest(provider=provider, transport=transport):
                    self.gateway.is_webui = transport != 'tailscale'
                    status, result = self.request('/api/accounts/add/start', {'provider': provider},
                                                  {'X-Router-Transport': transport})
                    self.assertEqual(status, 200)
                    self.assertIs(result['auto_callback'], transport != 'tailscale')
                    starts = [path for _, path, _ in self.gateway.calls if '/auth-url?' in path]
                    self.assertEqual(parse_qs(urlsplit(starts[-1]).query)['is_webui'],
                                     ['false' if transport == 'tailscale' else 'true'])
                    self.assertEqual(self.request('/api/accounts/add/cancel', {'session': result['session']})[0], 200)

    def test_disconnect_tailscale_confirmation_and_signin_are_dispatched(self):
        self.gateway.row()
        headers = {'X-Router-Transport': 'tailscale'}
        body = dict(key='first.json', auth_index='first', confirmed=True)
        for text in (None, '', 'wrong'):
            payload = dict(body, confirm_text=text) if text is not None else body
            status, result = self.request('/api/accounts/disconnect', payload, headers)
            self.assertEqual((status, result['error']), (400, 'confirm_mismatch'))
        status, result = self.request('/api/accounts/disconnect',
            dict(body, confirm_text='  FIXTURE@EXAMPLE.INVALID '), headers)
        self.assertEqual(status, 200)
        self.assertTrue(result['disconnected'])
        self.state.start_signin.return_value = {'handle': 'synthetic-handle'}
        status, result = self.request('/api/accounts/signin/start',
            dict(account_id='first', identity='a'*64), headers)
        self.assertEqual((status, result), (200, {'handle': 'synthetic-handle'}))
        self.state.start_signin.assert_called_once()

    def test_http_transport_refusals_csrf_dispatch_and_schema(self):
        self.gateway.row()
        for headers in ({'X-Router-Read-Only': '1'},):
            status, result = self.request('/api/accounts/disconnect', {'key': 'first.json', 'auth_index': 'first', 'confirmed': True, 'confirm_text': 'fixture@example.invalid'}, headers)
            self.assertEqual((status, result['error']), (403, 'read_only'))
        self.assertEqual(self.gateway.calls, [])
        status, result = self.request('/api/accounts/routing', {'key': 'first.json', 'auth_index': 'first', 'enabled': False}, {'X-Router-Transport': 'tailscale'})
        self.assertEqual((status, result), (200, {'key': 'first.json', 'enabled': False}))
        for headers in ({'X-Router-Request': '0'}, {'Origin': 'http://evil.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('/api/accounts/add/start', {'provider': 'xai'}, headers)[0], 403)
        self.assertEqual(self.request('/api/accounts/add/start', {'provider': 'xai', 'extra': 1})[0], 400)
        self.assertEqual(self.request('/api/accounts/add/start', {'provider': 'xai'})[0], 200)
        self.assertEqual(self.request('/api/accounts/add/start', {'provider': 'codex'})[0], 409)
        handle = next(iter(self.adds.sessions))
        self.assertEqual(self.request('/api/accounts/add/status?'+urlencode({'session': handle}), method='GET'), (200, {'state': 'waiting'}))
        self.assertEqual(self.request('/api/accounts/add/cancel', {'session': handle}), (200, {'cancelled': True}))
        self.assertEqual(self.request('/api/accounts/disconnect', {'key': 'first.json', 'auth_index': 'first', 'confirmed': False})[0], 400)
        self.assertEqual(self.request('/api/accounts/disconnect', {'key': 'first.json', 'auth_index': 'stale', 'confirmed': True, 'confirm_text': 'fixture@example.invalid'})[0], 409)
        self.assertEqual(self.request('/api/accounts/disconnect', {'key': 'first.json', 'auth_index': 'first', 'confirmed': True, 'confirm_text': 'fixture@example.invalid'})[0], 200)
        self.gateway.rows.clear()
        self.gateway.row(provider='codex')
        self.assertEqual(self.request('/api/accounts/disconnect', {'key': 'first.json', 'auth_index': 'first', 'confirmed': True, 'confirm_text': 'fixture@example.invalid'})[0], 200)
