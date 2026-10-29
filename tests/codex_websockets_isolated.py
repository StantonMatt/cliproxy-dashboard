#!/usr/bin/env python3
"""Offline WebSocket regression matrix for the installed gateway and plugin.

Run integration through heavy; --check-only runs fixture-based preflight tests.
Only synthetic credentials and a non-forwarding loopback CONNECT mock are used.
No live gateway endpoints or real upstream traffic. Results retain no credentials.
"""
import argparse
import base64
from collections import Counter
import contextlib
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

import yaml
from websockets.sync.client import connect

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from gateway_config import write_same_inode
import fast_mode_isolated as framing

spec = importlib.util.spec_from_file_location('ws_check', ROOT / 'scripts/check-codex-websockets.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MODEL = 'gpt-6-luna'


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(.2)
        return sock.connect_ex(('127.0.0.1', port)) == 0


class PreflightTests(unittest.TestCase):
    def check(self, text):
        return preflight.inspect_config(preflight.load_config(text))

    def test_explicit_and_default(self):
        self.assertTrue(self.check('upstream: {codex: {response-steering: false}}')['rollout_ready'])
        r = self.check('{}')
        self.assertTrue(r['steering_disabled'] and r['steering_default_false'])
        self.assertFalse(r['rollout_ready'])
        self.assertFalse(self.check('upstream: {codex: {response-steering: true}}')['steering_disabled'])

    def test_alias_precedence(self):
        for alias in ['codex', 'oauth.providers.codex']:
            config = {}
            node = config
            for part in alias.split('.'):
                node = node.setdefault(part, {})
            node['response-steering'] = True
            self.assertFalse(preflight.inspect_config(config)['steering_disabled'])
            config['upstream'] = {'codex': {'response-steering': False}}
            self.assertTrue(preflight.inspect_config(config)['rollout_ready'])

    def test_all_setter_families_and_protocols(self):
        for base in ['requests:\n  payload:', 'payload:']:
            indent = '    ' if base.startswith('requests') else '  '
            for family in ['default', 'default-raw', 'override', 'override-raw']:
                for protocol in ['', 'codex', ' CoDeX ', 'claude']:
                    text = base + '\n' + indent + family + ':\n' + indent + '  - models: [{name: "*", protocol: "' + protocol + '"}]\n' + indent + '    params: {service_tier: priority}\n'
                    self.assertEqual(self.check(text)['payload_safe'], protocol == 'claude')

    def test_escaped_path_and_filter(self):
        config = {'requests': {'payload': {'override': [{'models': [{'name': '*'}], 'params': {r'service_\tier': 'priority'}}]}}}
        self.assertFalse(preflight.inspect_config(config)['payload_safe'])
        # SJSON's ':' force-object prefix still writes service_tier.
        config = {'requests': {'payload': {'override': [{'models': [{'name': '*'}], 'params': {':service_tier': 'priority'}}]}}}
        self.assertFalse(preflight.inspect_config(config)['payload_safe'])
        self.assertTrue(self.check('requests: {payload: {filter: [{models: [{name: "*", protocol: codex}], params: [service_tier]}]}}')['payload_safe'])
        # A gate or a model name does not prove that the setter is harmless.
        r = self.check('payload: {default: [{models: [{name: future-model, match: [{x: y}]}], params: {service_tier: default}}]}')
        self.assertFalse(r['payload_safe'])

    def test_invalid_config_and_no_secret_output(self):
        for text in ['[]', 'upstream: {codex: {response-steering: "false"}}', 'requests: {payload: []}', 'payload: {override: {}}', 'upstream: {codex: null}', 'a: 1\na: 2']:
            with self.subTest(text=text), self.assertRaises((ValueError, TypeError, yaml.YAMLError)):
                self.check(text)
        result = self.check('management: {secret-key: NEVER_ECHO}\npayload: {override: [{models: [{name: NEVER_ECHO, protocol: codex}], params: {service_tier: NEVER_ECHO}}]}')
        self.assertNotIn('NEVER_ECHO', json.dumps(result))

    def test_cli_exit_codes_and_parse_error_redaction(self):
        with tempfile.TemporaryDirectory(prefix='ws-check-') as directory:
            path = Path(directory) / 'config.yaml'
            for text, code in [
                ('{}', 1),
                ('upstream: {codex: {response-steering: false}}', 0),
                ('upstream: {codex: {response-steering: true}}', 1),
                ('management: [NEVER_ECHO', 2),
            ]:
                path.write_text(text)
                result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/check-codex-websockets.py'),
                                         '--config', str(path)], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, code)
                self.assertNotIn('NEVER_ECHO', result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertIsInstance(json.loads(result.stdout)['rollout_ready'], bool)


class MockServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cert, key):
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(cert, key)
        self.records = []
        self.refused = 0
        self.errors = []
        self.sockets = set()
        self.lock = threading.Lock()
        self.connection_seq = 0
        super().__init__(('127.0.0.1', 0), MockHandler)

    def close_owned(self):
        self.shutdown()
        self.server_close()
        with self.lock:
            sockets = list(self.sockets)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()


class MockHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        # No DNS, forwarding, or outbound connect exists in this handler.
        if self.path != 'chatgpt.com:443':
            self.server.refused += 1
            self.send_error(403)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        try:
            self.connection = self.server.tls.wrap_socket(self.connection, server_side=True)
            self.connection.settimeout(20)
            with self.server.lock:
                self.server.sockets.add(self.connection)
            self.rfile = self.connection.makefile('rb')
            self.wfile = self.connection.makefile('wb')
            self.close_connection = False
            while not self.close_connection:
                self.handle_one_request()
        except (OSError, EOFError):
            pass
        finally:
            with self.server.lock:
                self.server.sockets.discard(self.connection)
            self.close_connection = True

    def record(self, body, transport, connection=None):
        account = {'Bearer synthetic-A': 'A', 'Bearer synthetic-B': 'B'}.get(self.headers.get('Authorization'), 'unknown')
        with self.server.lock:
            number = len(self.server.records) + 1
            rec = {'account': account, 'transport': transport, 'connection': connection,
                   'tier': body.get('service_tier', '<absent>'), 'generate': body.get('generate', True),
                   'previous_response_id': body.get('previous_response_id'), 'sequence': number,
                   'tokens': {'input_tokens': 10 + number, 'output_tokens': 20 + number,
                              'total_tokens': 30 + 2 * number, 'reasoning_tokens': 2, 'cached_tokens': 1}}
            self.server.records.append(rec)
        if account == 'unknown':
            self.server.errors.append('unknown_synthetic_account')
        return rec

    def response(self, rec):
        n = rec['sequence']
        item = {'id': f'msg_{n}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'synthetic', 'annotations': []}]}
        tokens = rec['tokens']
        return {'id': f'resp_{n}', 'object': 'response', 'model': MODEL, 'status': 'completed',
                'output': [item], 'service_tier': rec['tier'] if rec['tier'] != '<absent>' else 'default',
                'usage': {k: tokens[k] for k in ('input_tokens', 'output_tokens', 'total_tokens')} |
                         {'input_tokens_details': {'cached_tokens': 1}, 'output_tokens_details': {'reasoning_tokens': 2}}}, item

    def do_POST(self):
        rec = self.record(json.loads(self.rfile.read(int(self.headers['Content-Length']))), 'http')
        response, _ = self.response(rec)
        raw = ('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() != 'websocket':
            self.send_error(403)
            return
        with self.server.lock:
            self.server.connection_seq += 1
            connection = self.server.connection_seq
        accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        fragments = b''
        try:
            while True:
                # Native Codex can fragment large inference payloads. Preserve FIN.
                head = framing.recv_exact(self.connection, 2)
                final, op, size = bool(head[0] & 128), head[0] & 15, head[1] & 127
                if size == 126:
                    size = struct.unpack('!H', framing.recv_exact(self.connection, 2))[0]
                elif size == 127:
                    size = struct.unpack('!Q', framing.recv_exact(self.connection, 8))[0]
                if size > 8 * 1024 * 1024:
                    raise ValueError('mock frame too large')
                mask = framing.recv_exact(self.connection, 4) if head[1] & 128 else b''
                raw = framing.recv_exact(self.connection, size)
                if mask:
                    raw = bytes(x ^ mask[i % 4] for i, x in enumerate(raw))
                if op == 8:
                    framing.send_frame(self.connection, raw, 8)
                    break
                if op == 9:
                    framing.send_frame(self.connection, raw, 10)
                    continue
                if op == 1:
                    fragments = raw
                elif op == 0:
                    fragments += raw
                else:
                    continue
                if not final:
                    continue
                body = json.loads(fragments)
                fragments = b''
                rec = self.record(body, 'websocket', connection)
                response, item = self.response(rec)
                for event in [
                    {'type': 'response.created', 'response': {'id': response['id'], 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_item.done', 'response_id': response['id'], 'output_index': 0, 'item': item},
                    {'type': 'response.completed', 'response': response},
                ]:
                    framing.send_frame(self.connection, json.dumps(event).encode())
        except (OSError, EOFError):
            pass
        except Exception:
            self.server.errors.append('mock_protocol_error')


class Experiment:
    def __init__(self, args, name, steering=False):
        self.args, self.name, self.steering = args, name, steering
        self.root = Path(tempfile.mkdtemp(prefix=name + '-', dir=args.scratch))
        self.root.chmod(0o700)
        self.proc = self.server = self.thread = self.log = None
        self.result = {'case': name, 'binary': str(args.binary), 'checks': []}

    def check(self, name, condition):
        self.result['checks'].append({'name': name, 'passed': bool(condition)})
        assert condition, name

    def start(self):
        root = self.root
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-keyout', str(key), '-out', str(cert), '-subj', '/CN=chatgpt.com', '-addext', 'subjectAltName=DNS:chatgpt.com'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        self.server = MockServer(cert, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = framing.free_port()
        proxy = f'http://127.0.0.1:{self.server.server_port}'
        auth = root / 'auth'
        auth.mkdir()
        for account in ['A', 'B']:
            save(auth / f'account-{account}.json', {'type': 'codex', 'email': f'{account.lower()}@example.invalid', 'plan_type': 'plus', 'access_token': f'synthetic-{account}', 'prefix': account, 'priority': 2 if account == 'A' else 1, 'websockets': True, 'proxy_url': proxy})
        save(root / 'policy.json', {'version': 1, 'updated_at': '2026-10-27T00:00:00Z', 'files': {'account-A.json': {'allow_fast': False}, 'account-B.json': {'allow_fast': True}}})
        artifacts = root / 'plugins/linux/amd64'
        artifacts.mkdir(parents=True)
        shutil.copyfile(self.args.library, artifacts / 'fast-mode-policy-v0.1.2.so')
        self.cfg = yaml.safe_load((ROOT / 'tests/fixtures/config-v8-nested.yaml').read_text())
        self.cfg['server'].update(host='127.0.0.1', port=self.port)
        self.cfg['management'].update({'secret-key': 'synthetic-management', 'disable-control-panel': True, 'disable-auto-update-panel': True})
        self.cfg['access']['api-keys'] = ['synthetic-client']
        self.cfg['oauth']['auth-dir'] = str(auth)
        self.cfg['requests']['proxy-url'] = proxy
        self.cfg['routing'].update({'strategy': 'fill-first', 'force-model-prefix': False})
        self.cfg['observability']['usage']['usage-statistics-enabled'] = True
        self.cfg['upstream'] = {'codex': {'response-steering': self.steering}}
        self.cfg['plugins'] = {'enabled': True, 'dir': str(root / 'plugins'), 'configs': {'fast-mode-policy': {'enabled': True, 'priority': 100, 'policy_path': str(root / 'policy.json'), 'status_path': str(root / 'status.json'), 'store': {'version': '0.1.2'}, 'map_unsupported_ultrafast': False, 'audit': True}}}
        self.config = root / 'config.yaml'
        self.config.write_text(yaml.safe_dump(self.cfg))
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(root), 'LANG': 'C.UTF-8', 'SSL_CERT_FILE': str(cert), 'SSL_CERT_DIR': str(root), 'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy, 'ALL_PROXY': proxy, 'NO_PROXY': '127.0.0.1,localhost', 'TMPDIR': str(root)}
        self.log = (root / 'gateway.log').open('w')
        self.proc = subprocess.Popen([str(self.args.binary), '--local-model', '-config', str(self.config)], cwd=root, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        self.result.update(gateway_pid=self.proc.pid, harness_pid=os.getpid(), gateway_port=self.port, mock_port=self.server.server_port)
        save(root / 'result.json', self.result)
        print(f'START {self.name}: gateway PID {self.proc.pid} port {self.port}; mock PID {os.getpid()} port {self.server.server_port}; {root}', flush=True)
        end = time.monotonic() + 25
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError('isolated gateway exited')
            try:
                files = self.mgmt('/auth-files')['files']
                req = urllib.request.Request(f'http://127.0.0.1:{self.port}/v1/models', headers={'Authorization': 'Bearer synthetic-client'})
                with HTTP.open(req, timeout=2) as response:
                    models = json.load(response)['data']
                if len(files) == 2 and (root / 'status.json').exists() and any(m['id'] == 'A/' + MODEL for m in models):
                    self.indexes = {f['auth_index']: ('A' if f['name'] == 'account-A.json' else 'B') for f in files}
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.1)
        raise TimeoutError('isolated startup')

    def mgmt(self, path, body=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/v0/management' + path, data=json.dumps(body).encode() if body is not None else None, method='PATCH' if body is not None else 'GET', headers={'Authorization': 'Bearer synthetic-management', 'Content-Type': 'application/json'})
        with HTTP.open(req, timeout=5) as response:
            return json.load(response)

    def ws(self):
        return connect(f'ws://127.0.0.1:{self.port}/v1/responses', additional_headers={'Authorization': 'Bearer synthetic-client'}, open_timeout=5, close_timeout=2, proxy=None)

    def turn(self, ws, account='A', previous=None):
        body = {'type': 'response.create', 'model': account + '/' + MODEL, 'input': [{'role': 'user', 'content': 'synthetic'}], 'instructions': 'synthetic', 'service_tier': 'priority'}
        if previous:
            body['previous_response_id'] = previous
        ws.send(json.dumps(body))
        end = time.monotonic() + 15
        while time.monotonic() < end:
            event = json.loads(ws.recv(timeout=max(.1, end - time.monotonic())))
            if event['type'] == 'response.completed':
                return event['response']['id']
            if event['type'] in ['error', 'response.failed']:
                raise AssertionError('synthetic turn failed')
        raise TimeoutError('synthetic turn')

    def reload(self):
        original = self.config.read_bytes()
        inode = self.config.stat().st_ino
        offset = (self.root / 'gateway.log').stat().st_size
        # Preserve startup bcrypt normalization as the existing installer does.
        current = yaml.safe_load(original)
        self.cfg['management']['secret-key'] = current['management']['secret-key']
        write_same_inode(self.config, yaml.safe_dump(self.cfg).encode(), original)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if 'config successfully reloaded' in (self.root / 'gateway.log').read_text()[offset:]:
                time.sleep(.3)  # Handler/executor publication follows the log.
                self.check('hot_reload_same_pid_inode', self.proc.poll() is None and self.config.stat().st_ino == inode)
                return
            time.sleep(.1)
        raise TimeoutError('same-inode config reload')

    def pair(self):
        start = len(self.server.records)
        with self.ws() as ws:
            rid = self.turn(ws)
            self.turn(ws, previous=rid)
        return self.server.records[start:]

    def matrix(self):
        self.check('safe_config_preflight', preflight.inspect_config(self.cfg)['rollout_ready'])
        for account, tier in [('A', '<absent>'), ('B', 'priority')]:
            start = len(self.server.records)
            with self.ws() as ws:
                first = self.turn(ws, account)
                second = self.turn(ws, account, first)
                self.turn(ws, account, second)
            with self.ws() as ws:
                self.turn(ws, account)
            records = self.server.records[start:]
            self.check(account + '_first_followups_reconnect_policy', len(records) == 4 and all(r['account'] == account and r['tier'] == tier and r['transport'] == 'websocket' for r in records))
            self.check(account + '_upstream_connection_reused', len({r['connection'] for r in records[:3]}) == 1 and records[0]['connection'] is not None)
            self.check(account + '_continuation_ids_survive', [r['previous_response_id'] for r in records[1:3]] == [first, second])
        for flag, transport in [(False, 'http'), (True, 'websocket'), (False, 'http')]:
            self.mgmt('/auth-files/fields', {'name': 'account-A.json', 'websockets': flag})
            listed = next(f for f in self.mgmt('/auth-files')['files'] if f['name'] == 'account-A.json')
            disk = json.loads((self.root / 'auth/account-A.json').read_text())
            self.check('scoped_flag_runtime_disk', listed.get('websockets') is flag and disk.get('websockets') is flag)
            observed = self.pair()
            self.check('new_connection_' + transport, len(observed) == 2 and all(r['transport'] == transport and r['tier'] == '<absent>' for r in observed))
            self.check('flag_patch_same_pid', self.proc.poll() is None)
        self.mgmt('/auth-files/fields', {'name': 'account-A.json', 'websockets': True})
        # Toggle the canonical key in place, proving its *behavior* hot-applies
        # on fresh sockets, rather than trusting a generic reload log alone.
        self.cfg['upstream']['codex']['response-steering'] = True
        self.reload()
        observed = self.pair()
        self.check('EXPECTED_FAILURE_duplex_denied_followup_bypasses_policy', [r['tier'] for r in observed] == ['<absent>', 'priority'])
        self.cfg['upstream']['codex']['response-steering'] = False
        self.reload()
        observed = self.pair()
        self.check('steering_false_hot_applies_new_socket', [r['tier'] for r in observed] == ['<absent>', '<absent>'] and all(r['transport'] == 'websocket' for r in observed) and observed[0]['connection'] == observed[1]['connection'])
        self.cfg['requests']['payload'] = {'override': [{'models': [{'name': '*', 'protocol': 'codex'}], 'params': {'service_tier': 'priority'}}]}
        self.check('late_tier_setter_detected', not preflight.inspect_config(self.cfg)['payload_safe'])
        self.reload()
        observed = self.pair()
        self.check('late_rule_really_reintroduces_priority', [r['tier'] for r in observed] == ['priority', 'priority'])

    def native(self):
        home, cwd = self.root / 'codex-home', self.root / 'workspace'
        home.mkdir()
        cwd.mkdir()
        # No inherited user homes, auth, MCP servers, skills or launch overrides.
        (home / 'config.toml').write_text(f'''model = "{MODEL}"
model_provider = "mock"
service_tier = "priority"
approval_policy = "never"
sandbox_mode = "read-only"
[analytics]
enabled = false
[feedback]
enabled = false
[model_providers.mock]
name = "Isolated synthetic mock"
base_url = "http://127.0.0.1:{self.port}/v1"
env_key = "WEBSOCKETS_SYNTHETIC_CLIENT"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = true
request_max_retries = 0
stream_max_retries = 1
stream_idle_timeout_ms = 5000
websocket_connect_timeout_ms = 2000
''')
        env = self.env | {'CODEX_HOME': str(home), 'WEBSOCKETS_SYNTHETIC_CLIENT': 'synthetic-client'}
        cli = None
        with (self.root / 'codex-output.jsonl').open('w') as out, (self.root / 'codex-error.log').open('w') as err:
            try:
                cli = subprocess.Popen([str(self.args.codex), 'exec', '--skip-git-repo-check', '--json', '-C', str(cwd), 'Reply synthetic. Do not use tools.'], cwd=cwd, env=env, stdout=out, stderr=err)
                self.result['codex_pid'] = cli.pid
                print(f'START native Codex PID {cli.pid}, case {self.name}', flush=True)
                self.check('native_codex_exit', cli.wait(timeout=40) == 0)
            finally:
                if cli:
                    if cli.poll() is None:
                        cli.terminate()
                        try:
                            cli.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            cli.kill()
                            cli.wait(timeout=5)
                    self.result['codex_exit'] = cli.returncode
                    self.result['codex_stopped'] = cli.poll() is not None
        events = [json.loads(line).get('type') for line in (self.root / 'codex-output.jsonl').read_text().splitlines()]
        self.check('native_turn_completed', 'turn.completed' in events)
        records = self.server.records
        self.check('native_warmup_then_first_inference', [r['generate'] for r in records] == [False, True])
        self.check('native_same_upstream_socket', len({r['connection'] for r in records}) == 1 and all(r['transport'] == 'websocket' for r in records))
        expected = ['<absent>', 'priority' if self.steering else '<absent>']
        label = 'EXPECTED_FAILURE_native_duplex_bypass' if self.steering else 'native_serial_enforces_first_user_turn'
        self.check(label, [r['tier'] for r in records] == expected and all(r['account'] == 'A' for r in records))

    def usage(self):
        expected = Counter((r['account'], *[r['tokens'][k] for k in ['input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens', 'cached_tokens']]) for r in self.server.records)
        actual = Counter()
        sanitized = []
        end = time.monotonic() + 5
        while time.monotonic() < end:
            raw = self.mgmt('/usage-queue?count=1000')
            if not isinstance(raw, list):
                raw = raw.get('records', raw.get('data', []))
            for record in raw:
                tokens = record.get('tokens', {})
                account = self.indexes.get(record.get('auth_index'), 'unknown')
                sanitized.append({'account': account, 'tokens': tokens, 'failed': record.get('failed', False)})
                actual[(account, *[tokens.get(k) for k in ['input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens', 'cached_tokens']])] += 1
            if sum(actual.values()) >= sum(expected.values()):
                break
            time.sleep(.1)
        self.result['usage'] = sanitized
        self.check('exact_usage_per_turn_account_all_token_fields', expected == actual and not any(r['failed'] for r in sanitized))
        # Gateway background catalog/update probes can attempt other hosts.
        # The mock rejects them without resolving or dialing; they are not
        # inference failures and should not make an offline test flaky.
        self.result['blocked_external_connects'] = self.server.refused
        self.result['mock_errors'] = self.server.errors
        self.check('no_mock_protocol_errors', not self.server.errors)

    def stop(self):
        cleanup = {}
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            cleanup['gateway_exit'] = self.proc.returncode
            cleanup['gateway_listening'] = listening(self.port)
        if self.log:
            self.log.close()
        if self.server:
            self.result['records'] = self.server.records
            port = self.server.server_port
            self.server.close_owned()
            self.thread.join(timeout=3)
            cleanup.update(mock_stopped=not self.thread.is_alive(), mock_listening=listening(port))
        # Retain sanitized evidence/logs only. Remove synthetic auth, private CA,
        # plugin artifacts/shadows, disposable Codex homes and config secrets.
        for path in list(self.root.iterdir()):
            if path.name not in {'gateway.log', 'result.json', 'codex-output.jsonl', 'codex-error.log'}:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        self.result['cleanup'] = cleanup
        save(self.root / 'result.json', self.result)
        print(f'STOP {self.name}: {json.dumps(cleanup, sort_keys=True)}', flush=True)
        assert not cleanup.get('gateway_listening') and not cleanup.get('mock_listening') and cleanup.get('mock_stopped', True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--library', type=Path, default=Path('/opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.1.2.so'))
    parser.add_argument('--codex', type=Path, default=Path('/home/user/.local/bin/codex'))
    parser.add_argument('--skip-native', action='store_true', help='Explicitly omit native warmup acceptance; record why externally')
    parser.add_argument('--scratch', type=Path, default=Path('~/.cache/agent-scratch/personal-codex-router/websockets/implementation-20261027').expanduser())
    args = parser.parse_args()
    tests = unittest.defaultTestLoader.loadTestsFromTestCase(PreflightTests)
    if not unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful():
        return 1
    if args.check_only:
        return 0
    if not args.binary or not args.binary.is_file() or not args.library.is_file():
        parser.error('--binary and installed --library are required for integration')
    if not args.skip_native and not args.codex.is_file():
        parser.error('--codex must exist, or explicitly use --skip-native')
    args.binary = args.binary.resolve()
    args.scratch.mkdir(parents=True, exist_ok=True)
    results = []
    for name, steering in [('serial-matrix', False)] + ([] if args.skip_native else [('native-serial', False), ('native-duplex-EXPECTED-FAILURE', True)]):
        experiment = Experiment(args, name, steering)
        try:
            experiment.start()
            if name == 'serial-matrix':
                experiment.matrix()
            else:
                experiment.native()
            experiment.usage()
        except Exception as error:
            experiment.result['error_type'] = type(error).__name__
            # Assertion labels contain no request/config secrets.
            if isinstance(error, AssertionError):
                experiment.result['failed_check'] = str(error)
            raise
        finally:
            experiment.stop()
            results.append(experiment.result)
            save(args.scratch / ('summary-' + args.binary.parent.name + '.json'), results)
    print(f'PASS {args.binary.parent.name}: {sum(len(r["checks"]) for r in results)} checks, {sum(len(r["records"]) for r in results)} completed synthetic responses; native_skipped={args.skip_native}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
