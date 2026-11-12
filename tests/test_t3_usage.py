"""Protocol fixtures transcribed from installed T3 cliproxyApi.ts regions.

Linux binCli-Bhw4WaiS.mjs:310840-311076, Mac binCli-HOOJOCti.mjs:323899-324162.
Both builds send identical polling bodies; Mac additionally accepts id_token.plan_type.
See docs/t3-usage-hub.md for reproduction and the optional-credit failure evidence.
"""
from contextlib import ExitStack
import copy
import datetime as dt
from email.message import Message
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import t3_usage as hub
spec = importlib.util.spec_from_file_location('t3_dashboard', ROOT / 'dashboard/server.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
NOW = 1793000000
LINUX_KEY = 't3u_' + '1' * 64
MAC_KEY = 't3u_' + '2' * 64


def stamp(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()


# Exact provider headers and request shape shared by both researched builds.
BUILDS = ('0.1.1-nightly.20261018.3315-linux', '0.1.2-nightly.20261021.3473-mac')


def polling(account, url=None):
    if account['provider'] == 'codex':
        header = {'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json',
                  'OpenAI-Beta': 'codex-1', 'Originator': 'Codex Desktop'}
        if account.get('id_token', {}).get('chatgpt_account_id'):
            header['Chatgpt-Account-Id'] = account['id_token']['chatgpt_account_id']
    else:
        header = {'Authorization': 'Bearer $TOKEN$', 'anthropic-beta': 'oauth-2025-04-20'}
    return {'auth_index': account['auth_index'], 'method': 'GET',
            'url': url or (hub.CODEX_URL if account['provider'] == 'codex' else hub.CLAUDE_URL), 'header': header}


def fixture():
    return {'online': True, 'checked_at': stamp(NOW), 'accounts': [{
        'id': 'runtime-a', 'email': 'codex@example.test', 'access_identity': 'identity-a',
        'disabled': False, 'quota_error': None, 'quota_checked_at': stamp(NOW - 10),
        '_t3_usage': hub.codex_projection({'plan_type': 'pro', 'secret': 'must-not-escape', 'rate_limit': {
            'primary_window': {'used_percent': 37, 'reset_at': NOW + 2000, 'limit_window_seconds': 18000,
                               'secret': 'must-not-escape'}}}, 'identity-a', 'workspace-a')}],
        'claude_accounts': [dict(auth_index='runtime-b', email='claude@example.test', routing_id='identity-b', disabled=False)],
        'watch_accounts': [dict(id='claude-b', independent_windows=True, email='claude@example.test', status='ok', checked_at=stamp(NOW - 10), windows=[
            dict(kind='five_hour', used=12, resets_at=NOW + 1000, measured_at=stamp(NOW - 10)),
            dict(kind='seven_day', used=24, resets_at=NOW + 10000, measured_at=stamp(NOW - 20)),
            dict(kind='weekly_model', model='Fable', used=32, resets_at=NOW + 12000, measured_at=stamp(NOW - 30))])]}


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='t3-usage-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.keys = self.root / 'keys.json'
        self.keys.write_text(json.dumps({'linux': LINUX_KEY, 'mac': MAC_KEY}))
        self.cache = fixture()
        self.state = SimpleNamespace(t3_usage_cache=Mock(side_effect=lambda _: copy.deepcopy(self.cache)))

    def request(self, method='GET', path='/v0/management/auth-files', value=None, raw=None, headers=None):
        handler = object.__new__(app.Handler)
        handler.path = path
        content = raw if raw is not None else json.dumps(value).encode() if value is not None else b''
        handler.headers = Message()
        for name, val in {'Host': '127.0.0.1:8320', 'Authorization': 'Bearer ' + LINUX_KEY,
                          'Content-Length': str(len(content)), 'Content-Type': 'application/json', **(headers or {})}.items():
            if val is not None:
                handler.headers[name] = val
        handler.server = SimpleNamespace(state=self.state, t3_usage_keys=self.keys)
        handler.rfile, handler.connection, handler.send = io.BytesIO(content), Mock(), Mock()
        with patch.object(hub.time, 'time', return_value=NOW):
            getattr(handler, 'do_' + method)()
        status, body = handler.send.call_args.args[:2]
        return status, json.loads(body)

    def listed(self, provider='codex'):
        return next(a for a in self.request()[1]['files'] if a['provider'] == provider)

    def usage(self, provider='codex', **kw):
        return self.request('POST', '/v0/management/api-call', polling(self.listed(provider)), **kw)

    def test_both_builds_poll_and_decode_single_window_and_models(self):
        for build in BUILDS:
            for token in (LINUX_KEY, MAC_KEY):
                with self.subTest(build=build, machine=token == MAC_KEY):
                    status, data = self.request(headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(status, 200)
                    for account in data['files']:
                        self.assertEqual(set(account) - {'id_token'}, {'id', 'auth_index', 'provider', 'email', 'disabled'})
                        self.assertEqual(account['id'], account['auth_index'])
                        status, envelope = self.request('POST', '/v0/management/api-call', polling(account),
                                                        headers={'Authorization': 'Bearer ' + token})
                        self.assertEqual((status, envelope['status_code']), (200, 200))
                        self.assertIsInstance(envelope['body'], str)
                        body = json.loads(envelope['body'])
                        if account['provider'] == 'codex':
                            self.assertEqual(body, {'plan_type': 'pro', 'rate_limit': {'primary_window': {
                                'used_percent': 37, 'reset_at': NOW + 2000, 'limit_window_seconds': 18000}}})
                            self.assertNotIn('secondary_window', body['rate_limit'])
                            # Both readAccount implementations wrap credits with orElseSucceed(undefined).
                            self.assertEqual(self.request('POST', '/v0/management/api-call', polling(account,
                                'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits'))[0], 403)
                        else:
                            self.assertEqual(body['five_hour']['utilization'], 12)
                            self.assertIsInstance(body['five_hour']['resets_at'], str)
                            self.assertEqual(body['seven_day']['utilization'], 24)
                            self.assertEqual(body['limits'], [{'kind': 'weekly_scoped', 'percent': 32,
                                'resets_at': stamp(NOW + 12000), 'scope': {'model': {'display_name': 'Fable'}}}])
                        self.assertNotIn('must-not-escape', json.dumps(account) + envelope['body'])

    def test_auth_fail_closed_and_revoke_immediately(self):
        for value in (None, '', 'Bearer wrong', 'Bearer gateway-management-key', 'Basic ' + LINUX_KEY):
            with self.subTest(auth=value):
                self.assertEqual(self.request(headers={'Authorization': value})[0], 401)
        self.state.t3_usage_cache.assert_not_called()
        self.keys.unlink()
        self.assertEqual(self.request()[0], 401)
        for invalid in ('not json', '[]', '{}', json.dumps({'linux': LINUX_KEY, 'mac': LINUX_KEY}),
                        json.dumps({'linux': 'gateway-management-key'}), json.dumps({'linux': LINUX_KEY}) + ' ' * 17000):
            self.keys.write_text(invalid)
            self.assertEqual(self.request()[0], 401)
        self.keys.write_text(json.dumps({'linux': LINUX_KEY, 'mac': MAC_KEY}))
        self.assertEqual(self.request()[0], 200)
        self.keys.write_text(json.dumps({'mac': MAC_KEY}))
        self.assertEqual(self.request()[0], 401)
        with patch.object(Path, 'open', side_effect=PermissionError('private path')):
            self.assertEqual(self.request()[1], {'error': 'Usage hub unavailable or unauthorized'})

    def test_constant_time_compares_all_machine_keys(self):
        with patch.object(hub.hmac, 'compare_digest', wraps=hub.hmac.compare_digest) as compare:
            self.request()
        self.assertEqual(compare.call_count, 2)

    def test_native_host_and_transport_guards(self):
        for headers, expected in (({'Host': 'evil.test'}, 421), ({'Host': '127.0.0.1:8317'}, 421),
            ({'Origin': 'http://127.0.0.1:8320'}, 403), ({'Origin': 'null'}, 403),
            ({'Origin': ''}, 403), ({'Sec-Fetch-Site': 'none'}, 403),
            ({'Sec-Fetch-Site': 'same-origin'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
            ({'X-Router-Transport': 'tailscale'}, 403)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], expected)
        self.state.t3_usage_cache.assert_not_called()
        self.assertEqual(self.request(headers={'Host': '127.0.0.1:18320', 'X-Router-Request': None})[0], 200)

    def test_all_management_routes_and_mutations_are_denied(self):
        for path in ('/v0/management', '/v0/management/', '/v0/management/config', '/v0/management/reset-quota',
                     '/v0/management/auth-files/download?name=private.json', '/v0/management/auth-files/status',
                     '/v0/management/auth-files?name=x', '/v0/management/auth-files/',
                     '/v0/management/%61uth-files', '/v0/management/auth-files%3f',
                     '/v0/management/api-call?query=1', '/v0/management/api-call/',
                     '/v0/management/../api/refresh', '/v0/management//auth-files'):
            for method in ('GET', 'POST'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.request(method, path, {})[0], 404)
        self.state.t3_usage_cache.assert_not_called()
        account = self.listed()
        for value in (dict(polling(account), method='POST', data='{}'),
                      dict(polling(account), url='https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume', method='POST'),
                      dict(polling(account), data='{}')):
            self.assertEqual(self.request('POST', '/v0/management/api-call', value)[0], 400)

    def test_strict_body_schema_framing_and_header_validation(self):
        good = polling(self.listed())
        for raw in (b'[]', b'null', b'{', b'{"auth_index":"x","auth_index":"y"}',
                    b'{"method":NaN}', b'\xff', b'[' * 1200):
            self.assertEqual(self.request('POST', '/v0/management/api-call', raw=raw)[0], 400)
        for field, values in (('method', ['POST', 'get', None, [], 'DELETE']),
                              ('auth_index', [None, 1, [], {}]), ('url', [1, [], None]),
                              ('header', [[], None, {'x': 1}, {'x\r\ny': 'a'}, {'x': '\n'}, {'x': 'v' * 1025}])):
            for value in values:
                self.assertEqual(self.request('POST', '/v0/management/api-call', {**good, field: value})[0], 400)
        for value in ({**good, 'extra': True}, {}, {k: v for k, v in good.items() if k != 'url'}):
            self.assertEqual(self.request('POST', '/v0/management/api-call', value)[0], 400)
        for headers in ({'Content-Length': '16385'}, {'Content-Length': '-1'}, {'Content-Length': '0'},
                        {'Content-Length': '12,12'}, {'Content-Length': None},
                        {'Content-Type': 'text/plain'}, {'Transfer-Encoding': 'chunked'}, {'Content-Length': '10000'}):
            self.assertEqual(self.request('POST', '/v0/management/api-call', good, headers=headers)[0], 400)
        self.assertEqual(self.request('POST', '/v0/management/api-call', raw=b' ' * 16385)[0], 400)
        # Header is optional and its contents never select a different upstream or identity.
        self.assertEqual(self.request('POST', '/v0/management/api-call', {k: v for k, v in good.items() if k != 'header'})[1]['status_code'], 200)
        self.assertEqual(self.request('POST', '/v0/management/api-call', {**good, 'header': {'Authorization': 'ignored'}})[1]['status_code'], 200)
        with patch.object(app.Handler, 'read_body', side_effect=TimeoutError('private')):
            self.assertEqual(self.request('POST', '/v0/management/api-call', good)[0], 400)

    def test_exact_url_allowlist_and_account_namespace(self):
        for provider in ('codex', 'claude'):
            account = self.listed(provider)
            good = polling(account)
            for url in (good['url'] + '?', good['url'] + '?x=1', good['url'] + '#x', good['url'] + '/',
                        good['url'].replace('/usage', '/%75sage'), good['url'].replace('https:', 'http:'),
                        good['url'].replace('/usage', '//usage'), 'http://127.0.0.1:8317/v0/management/config',
                        hub.CLAUDE_URL if provider == 'codex' else hub.CODEX_URL):
                self.assertEqual(self.request('POST', '/v0/management/api-call', {**good, 'url': url})[0], 403)
            self.assertEqual(self.request('POST', '/v0/management/api-call', {**good, 'auth_index': 'runtime-a'})[0], 403)

    def test_disabled_replacement_missing_identity_and_collisions(self):
        old = polling(self.listed())
        self.cache['accounts'][0]['disabled'] = True
        self.assertTrue(self.listed()['disabled'])
        self.assertEqual(self.request('POST', '/v0/management/api-call', old)[0], 403)
        self.cache = fixture()
        old_claude = polling(self.listed('claude'))
        self.cache['claude_accounts'][0]['disabled'] = True
        self.assertTrue(self.listed('claude')['disabled'])
        self.assertEqual(self.request('POST', '/v0/management/api-call', old_claude)[0], 403)
        self.cache['claude_accounts'][0].update(disabled=False, routing_id='replacement')
        self.assertEqual(self.request('POST', '/v0/management/api-call', old_claude)[0], 403)
        self.assertEqual(self.usage('claude')[1]['status_code'], 200)
        self.cache['accounts'][0]['access_identity'] = 'replacement'
        self.assertFalse(any(a['provider'] == 'codex' for a in self.request()[1]['files']))
        self.cache['accounts'][0]['_t3_usage']['identity'] = 'replacement'
        self.assertEqual(self.request('POST', '/v0/management/api-call', old)[0], 403)
        self.assertEqual(self.usage()[1]['status_code'], 200)
        previous_workspace = polling(self.listed())
        self.cache['accounts'][0]['_t3_usage']['account_id'] = 'replacement-workspace'
        self.assertEqual(self.request('POST', '/v0/management/api-call', previous_workspace)[0], 403)
        for field in ('id', 'access_identity', 'email'):
            self.cache = fixture()
            self.cache['accounts'][0][field] = None
            self.assertFalse(any(a['provider'] == 'codex' for a in self.request()[1]['files']))
        for provider, field in (('accounts', 'id'), ('accounts', 'email'), ('accounts', 'access_identity'),
                                ('claude_accounts', 'auth_index'), ('claude_accounts', 'email'), ('claude_accounts', 'routing_id')):
            self.cache = fixture()
            original = self.cache[provider][0]
            duplicate = copy.deepcopy(original)
            if provider == 'accounts':
                duplicate.update(id='other', email='other@example.test', access_identity='other')
                duplicate['_t3_usage'].update(identity='other', account_id='other-workspace')
            else:
                duplicate.update(auth_index='other', email='other@example.test', routing_id='other')
            duplicate[field] = original[field].upper() if field == 'email' else original[field]
            self.cache[provider].append(duplicate)
            self.assertFalse(any(a['provider'] == ('codex' if provider == 'accounts' else 'claude') for a in self.request()[1]['files']))
        self.cache = fixture()
        other = copy.deepcopy(self.cache['accounts'][0])
        other.update(id='other', email='other@example.test', access_identity='other')
        other['_t3_usage']['identity'] = 'other'
        self.cache['accounts'].append(other)
        self.assertFalse(any(a['provider'] == 'codex' for a in self.request()[1]['files']))
        self.cache = fixture()
        self.cache['watch_accounts'].append(copy.deepcopy(self.cache['watch_accounts'][0]))
        self.assertEqual(self.usage('claude')[1]['status_code'], 503)
        self.cache['watch_accounts'] = [{**self.cache['watch_accounts'][0], 'email': 'replacement@example.test'}]
        self.assertEqual(self.usage('claude')[1]['status_code'], 503)

    def test_aged_exhausted_observations_remain_available_only_until_fresh_until(self):
        for provider in ('codex', 'claude'):
            self.cache = fixture()
            if provider == 'codex':
                row = self.cache['accounts'][0]
                row['quota_checked_at'] = stamp(NOW - 1800)
                row['_t3_usage']['body']['rate_limit']['primary_window']['used_percent'] = 100
            else:
                row = self.cache['watch_accounts'][0]
                row.update(checked_at=stamp(NOW - 1800))
                for window in row['windows']:
                    window['measured_at'] = stamp(NOW - 1800)
                row['windows'][0]['used'] = 100
            row['fresh_until'] = stamp(NOW + 60)
            status, envelope = self.usage(provider)
            self.assertEqual((status, envelope['status_code']), (200, 200))
            if provider == 'claude':
                # Last-known model windows retain their own reset and measurement.
                self.assertIn('limits', json.loads(envelope['body']))
            row['fresh_until'] = stamp(NOW - 1)
            self.assertEqual(self.usage(provider)[1]['status_code'], 200)
            if provider == 'claude':
                row['windows'][0]['resets_at'] = NOW
            else:
                row['_t3_usage']['body']['rate_limit']['primary_window']['reset_at'] = NOW
            if provider == 'claude':
                self.assertEqual(self.usage(provider)[1]['status_code'], 503)
            else:
                self.assertEqual(self.usage(provider)[1]['status_code'], 503)

    def test_stale_failed_future_and_missing_windows_never_zero(self):
        for key, invalid in (('quota_checked_at', stamp(NOW + 1)),
                             ('quota_checked_at', None), ('quota_error', 'failed')):
            self.cache = fixture()
            self.cache['accounts'][0][key] = invalid
            status, envelope = self.usage()
            self.assertEqual((status, envelope['status_code']), (200, 503))
            self.assertNotIn('used_percent', envelope['body'])
        for key, invalid in (('online', False), ('checked_at', stamp(NOW - 181)), ('checked_at', None)):
            self.cache = fixture()
            self.cache[key] = invalid
            for provider in ('codex', 'claude'):
                self.assertEqual(self.usage(provider)[1]['status_code'], 503)
        for changed in (dict(status='error'), dict(status='stale'),
                        dict(checked_at=stamp(NOW + 1)), dict(windows=[])):
            self.cache = fixture()
            self.cache['watch_accounts'][0].update(changed, id='claude', independent_windows=False)
            self.assertEqual(self.usage('claude')[1]['status_code'], 503)
        self.cache = fixture()
        self.cache['accounts'][0]['_t3_usage']['body']['rate_limit'] = {}
        self.assertEqual(self.usage()[1]['status_code'], 503)
        # A secondary-only observation must retain its actual slot.
        self.cache['accounts'][0]['_t3_usage']['body']['rate_limit']['secondary_window'] = {'used_percent': 19, 'reset_at': NOW + 2000}
        body = json.loads(self.usage()[1]['body'])
        self.assertEqual(body['rate_limit'], {'secondary_window': {'used_percent': 19, 'reset_at': NOW + 2000}})

    def test_claude_b_independent_ages_and_absent_windows(self):
        windows = self.cache['watch_accounts'][0]['windows']
        windows[-1]['measured_at'] = stamp(NOW - 901)
        body = json.loads(self.usage('claude')[1]['body'])
        self.assertIn('limits', body)
        windows[0]['measured_at'] = stamp(NOW + 1)
        self.assertEqual(self.usage('claude')[1]['status_code'], 503)
        # Absent 5h is permitted; an explicitly malformed reported 5h is not.
        windows.pop(0)
        body = json.loads(self.usage('claude')[1]['body'])
        self.assertNotIn('five_hour', body)
        self.assertEqual(body['seven_day']['utilization'], 24)
        windows[0]['used'] = None
        self.assertEqual(self.usage('claude')[1]['status_code'], 503)
        self.cache = fixture()
        self.cache['watch_accounts'][0].update(id='claude', independent_windows=False,
            windows=[{'kind': 'seven_day_sonnet', 'used': 18, 'resets_at': None}])
        self.assertEqual(self.usage('claude')[1]['status_code'], 503)

    def test_projection_allowlist_and_numeric_validation(self):
        for invalid in (None, True, '10', -1, 101, float('nan'), float('inf')):
            self.cache = fixture()
            self.cache['watch_accounts'][0]['windows'][0]['used'] = invalid
            self.assertEqual(self.usage('claude')[1]['status_code'], 503)
        for invalid in (None, True, '10', -1, 101, float('nan'), float('inf'), 10 ** 400):
            result = hub.codex_projection({'rate_limit': {'primary_window': {'used_percent': invalid}}}, 'identity')
            self.assertEqual(result['body'], {'rate_limit': {}})
        result = hub.codex_projection({'plan_type': 'pro', 'rate_limit': {'secondary_window': {
            'used_percent': 0, 'reset_at': True, 'limit_window_seconds': -1, 'credentials': 'private'}}}, 'identity')
        self.assertEqual(result, {'identity': 'identity', 'body': {'plan_type': 'pro', 'rate_limit': {
            'secondary_window': {'used_percent': 0}}}})

    def test_shared_proxy_denies_entire_management_namespace(self):
        # Evaluate the real allowlist function without starting its ten listeners.
        script = """
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('scripts/router-preview.mjs','utf8');
const fn=source.slice(source.indexOf('function dashboardRequestAllowed('),source.indexOf('function upstreamTimeout('));
const c={};vm.createContext(c);vm.runInContext(fn,c);
for(const method of ['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'])
 for(const url of ['/v0/management/auth-files','/v0/management/api-call','/v0/management/reset-quota',
 '/v0/management/auth-files/download?name=x','/v0/management/%61uth-files','/v0/management/auth-files?x=y'])
  assert.equal(c.dashboardRequestAllowed({method,url}),false);
assert.equal(c.dashboardRequestAllowed({method:'GET',url:'/api/overview'}),true);
"""
        subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, timeout=10)


class CacheIntegrationTests(unittest.TestCase):
    def test_existing_poller_projection_private_snapshot_and_cache_only_http(self):
        with tempfile.TemporaryDirectory(prefix='t3-usage-http-') as directory, ExitStack() as stack:
            root = Path(directory)
            key = root / 'keys.json'
            key.write_text(json.dumps({'linux': LINUX_KEY, 'mac': MAC_KEY}))
            watch = root / 'claude.json'
            watch.write_text(json.dumps(dict(email='claude@example.test', status='ok', checked_at=stamp(NOW),
                windows=[dict(kind='seven_day', used=13, resets_at=NOW + 2000)])))
            watch.with_name('claude-b.json').write_text(json.dumps(dict(email='claude-b@example.test', status='ok',
                checked_at=stamp(NOW), windows=[dict(kind='seven_day', used=25, resets_at=NOW + 2000,
                    measured_at=stamp(NOW - 10))])))
            file = dict(type='codex', name='private-file.json', auth_index='runtime-a', email='codex@example.test',
                        disabled=False, id_token={'chatgpt_account_id': 'workspace-a', 'plan_type': 'pro'})
            with patch.object(app, 'utcnow', return_value=stamp(NOW)):
                account = app.fetch_account(file, cache=app.usage_adapters.UsageCache(
                    wall_clock=lambda: NOW), call=Mock(return_value={'status_code': 200, 'body': json.dumps({
                    'plan_type': 'pro', 'rate_limit': {'primary_window': {'used_percent': 37, 'reset_at': NOW + 2000}}})}))
            app.account_access.AccountAccess().project(file, account)
            state = object.__new__(app.GatewayState)
            state.lock, state.running = threading.Lock(), False
            state.notices = app.NoticeStore(root / 'notices.json', root)
            state.models_cache = app.providers.ModelsCache()
            state.profiles = app.subscriptions.Profiles(wall_clock=lambda: NOW)
            state.codex_billing = app.subscriptions.CodexBilling(wall_clock=lambda: NOW)
            state.usage_cache = app.usage_adapters.UsageCache(wall_clock=lambda: NOW)
            state.data = dict(online=True, checked_at=stamp(NOW), accounts=[account], claude_accounts=[
                dict(auth_index='c-a', email='claude@example.test', routing_id='ca'),
                dict(auth_index='c-b', email='claude-b@example.test', routing_id='cb')])
            state.fast_modes, state.resets, state.signins = Mock(), Mock(), Mock()
            state.fast_modes.views.return_value = {}
            state.catalog = Mock()
            state.catalog.view.return_value = {}
            state.resets.view.return_value = {}
            state.signins.view.return_value = {}
            stack.enter_context(patch.object(app, 'CLAUDE_USAGE', watch))
            stack.enter_context(patch.object(hub.time, 'time', return_value=NOW))
            for name, value in (('pool_status', {}), ('preference_status', {}), ('billing_records', {}),
                                ('billing_status', {})):
                stack.enter_context(patch.object(app, name, return_value=value))
            stack.enter_context(patch.object(app.routing_policy, 'routing_linked', return_value=False))
            public_snapshot = state.snapshot()
            self.assertNotIn('_t3_usage', public_snapshot['accounts'][0])
            self.assertIn('_t3_usage', state.data['accounts'][0])
            server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            server.state, server.t3_usage_keys = state, key
            thread = threading.Thread(target=server.serve_forever, name='t3-fixture-http')
            thread.start()
            self.addCleanup(thread.join, 3)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            port = server.server_port
            print(f'T3 fixture HTTP: PID {__import__("os").getpid()}, port {port}; shutdown registered')
            spies = []
            for owner, name in ((app, 'api'), (app, 'urlopen'), (app, 'claude_request'), (app, 'claude_reset_view'),
                                (app.GatewayState, 'refresh'), (app.GatewayState, 'snapshot'),
                                (app.GatewayState, 'set_routing_enabled'), (app.GatewayState, 'set_claude_routing_enabled'),
                                (app.GatewayState, 'after_reset'), (subprocess, 'run'), (subprocess, 'Popen')):
                spies.append(stack.enter_context(patch.object(owner, name, side_effect=AssertionError('Upstream side effect'))))

            def request(method, path, payload=None, headers=None):
                client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                try:
                    client.request(method, path, json.dumps(payload) if payload is not None else None,
                        {'Host': '127.0.0.1:8320', 'Authorization': 'Bearer ' + LINUX_KEY,
                         'Content-Type': 'application/json', **(headers or {})})
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()
            status, listed = request('GET', '/v0/management/auth-files')
            self.assertEqual(status, 200)
            self.assertEqual(len(listed['files']), 3)
            for build in BUILDS:
                for account in listed['files']:
                    with self.subTest(build=build, provider=account['provider']):
                        self.assertEqual(request('POST', '/v0/management/api-call', polling(account))[1]['status_code'], 200)
            self.assertEqual(request('POST', '/v0/management/reset-quota', {})[0], 404)
            self.assertEqual(request('GET', '/v0/management/auth-files/download?name=x')[0], 404)
            for path in ('/v0/management/%61uth-files', '/v0/%6danagement/auth-files', '/v0//management/auth-files',
                         '/v0/management/auth-files?x=1', '//v0/management/auth-files'):
                self.assertGreaterEqual(request('GET', path)[0], 400)
            for method in ('PUT', 'PATCH', 'DELETE', 'OPTIONS'):
                # BaseHTTPRequestHandler rejects methods absent from its dispatcher.
                client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                client.request(method, '/v0/management/auth-files', headers={'Host': '127.0.0.1:8320'})
                response = client.getresponse()
                self.assertEqual(response.status, 501)
                response.read()
                client.close()
            self.assertEqual(request('GET', '/v0/management/auth-files', headers={'Origin': 'null'})[0], 403)
            # A weekly-only B observation also passes through the real snapshot parser.
            b_path = watch.with_name('claude-b.json')
            b_path.write_text(json.dumps(dict(email='claude-b@example.test', status='ok', checked_at=stamp(NOW), windows=[
                dict(kind='seven_day', used=45, resets_at=NOW + 2000, measured_at=stamp(NOW - 10)),
                dict(kind='weekly_model', model='Fable', used=15, resets_at=NOW + 2000, measured_at=stamp(NOW - 901))])))
            b_account = next(a for a in listed['files'] if a['email'] == 'claude-b@example.test')
            b_body = json.loads(request('POST', '/v0/management/api-call', polling(b_account))[1]['body'])
            self.assertEqual(b_body['seven_day'], {'utilization': 45, 'resets_at': stamp(NOW + 2000)})
            self.assertNotIn('five_hour', b_body)
            self.assertEqual(b_body['limits'][0]['percent'], 15)
            # Missing hub keys leave ordinary static routes and dashboard request guards intact.
            key.unlink()
            self.assertEqual(request('GET', '/v0/management/auth-files')[0], 401)
            self.assertEqual(request('GET', '/api/health')[0], 403)
            client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            client.request('GET', '/', headers={'Host': '127.0.0.1:8320'})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'<!doctype html>', response.read().lower())
            client.close()
            key.write_text(json.dumps({'linux': LINUX_KEY, 'mac': MAC_KEY}))
            # Ambiguous HTTP framing and authentication cannot select whichever duplicate is convenient.
            for duplicate, expected in (('Content-Length: 2', 400), ('Authorization: Bearer wrong', 401)):
                with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
                    client.sendall(('POST /v0/management/api-call HTTP/1.1\r\nHost: 127.0.0.1:8320\r\n'
                        'Authorization: Bearer ' + LINUX_KEY + '\r\nContent-Type: application/json\r\n'
                        'Content-Length: 2\r\n' + duplicate + '\r\n\r\n{}').encode())
                    self.assertIn((' ' + str(expected) + ' ').encode(), client.recv(4096).split(b'\r\n', 1)[0])
            # A stalled body has a three-second total deadline, bounded independently of T3's 15 s timeout.
            with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
                client.sendall(('POST /v0/management/api-call HTTP/1.1\r\nHost: 127.0.0.1:8320\r\n'
                    'Authorization: Bearer ' + LINUX_KEY + '\r\nContent-Type: application/json\r\n'
                    'Content-Length: 100\r\n\r\n{').encode())
                self.assertEqual(client.recv(4096), b'')
            for spy in spies:
                spy.assert_not_called()
            # No writes to either fixture cache or keys during requests.
            self.assertEqual(json.loads(key.read_text()), {'linux': LINUX_KEY, 'mac': MAC_KEY})
