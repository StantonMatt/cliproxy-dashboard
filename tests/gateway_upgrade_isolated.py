#!/usr/bin/env python3
"""Validate model routes, TTL pin release, failover and collection on an exact binary.

Uses four synthetic OAuth files and a non-forwarding loopback TLS mock. Loads
the nested v8 fixture, with private paths, keys, ports and production retry
values substituted. Never reads production config, auth or the live usage queue.
Run through heavy: python3 tests/gateway_upgrade_isolated.py --binary <path>.
"""
import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gateway_config
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CLIENT_KEY, MANAGEMENT_KEY = 'isolated-upgrade-client', 'isolated-upgrade-management'
CODEX_MODELS = ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-5.6-sol',
                'gpt-6-luna', 'gpt-5.6-terra', 'gpt-5.6-luna')
CLAUDE_MODELS = ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5')


def synthetic_signature():
    # The same protobuf shape as upstream's synthetic signature tests; no real signature.
    model = b'claude-sonnet-5-5'
    channel = b'\x08\x0c\x10\x02\x32' + bytes([len(model)]) + model
    container = b'\x0a' + bytes([len(channel)]) + channel
    payload = b'\x12' + bytes([len(container)]) + container + b'\x18\x01\x22\x40' + bytes(range(64))
    return base64.b64encode(payload).decode()


def wait_for(check, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise AssertionError('Timed out waiting for isolated gateway')
        time.sleep(.1)


class Mock(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cert, key):
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(cert, key)
        self.records, self.refused, self.failures = [], [], {}
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        if self.path not in ('chatgpt.com:443', 'api.anthropic.com:443'):
            self.server.refused.append(self.path)
            self.send_error(403)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        try:
            self.connection = self.server.tls.wrap_socket(self.connection, server_side=True)
            self.connection.settimeout(15)
            self.rfile = self.connection.makefile('rb')
            self.wfile = self.connection.makefile('wb')
            self.close_connection = False
            while not self.close_connection:
                self.handle_one_request()
        except (OSError, ssl.SSLError):
            pass

    def do_GET(self):
        self.send_error(403)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        token = token or self.headers.get('X-Api-Key', '')
        account = token.removeprefix('sk-ant-oat-').removeprefix('synthetic-')
        assert account in ('codex-A', 'codex-B', 'claude-A', 'claude-B'), account
        with self.server.lock:
            status = self.server.failures.get(account, 200)
            self.server.records.append({'account': account, 'status': status, 'body': body,
                                        'user_agent': self.headers.get('User-Agent')})
        headers = {}
        if status != 200:
            headers['Retry-After'] = '1'
            payload = {'error': {'type': 'rate_limit_error' if status == 429 else 'api_error',
                                 'message': 'synthetic isolated failure'}}
        elif account.startswith('codex'):
            response = {'id': 'resp_isolated', 'object': 'response', 'status': 'completed',
                        'model': body['model'], 'output': [],
                        'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}
            payload = 'data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n'
            headers['Content-Type'] = 'text/event-stream'
        else:
            payload = {'id': 'msg_isolated', 'type': 'message', 'role': 'assistant',
                       'model': body['model'], 'content': [{'type': 'text', 'text': 'ISOLATED_OK'}],
                       'stop_reason': 'end_turn', 'stop_sequence': None,
                       'usage': {'input_tokens': 3, 'output_tokens': 2}}
        raw = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self.send_response(status)
        for key, value in {'Content-Type': 'application/json', **headers}.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Lab:
    def __init__(self, root, binary):
        self.root, self.binary = root, binary
        self.proc = self.mock = self.thread = self.log = None
        self.evidence = {'binary': str(binary), 'checks': []}

    def start(self):
        cert, key = self.root / 'mock.crt', self.root / 'mock.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=chatgpt.com', '-addext',
                        'subjectAltName=DNS:chatgpt.com,DNS:api.anthropic.com',
                        '-keyout', str(key), '-out', str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        self.mock = Mock(cert, key)
        self.thread = threading.Thread(target=self.mock.serve_forever, daemon=True)
        self.thread.start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        assert self.port != 8317 and self.mock.server_port != 8317
        auth = self.root / 'auth'
        auth.mkdir(mode=0o700)
        proxy = f'http://127.0.0.1:{self.mock.server_port}'
        for provider in ('codex', 'claude'):
            for label in ('A', 'B'):
                name = f'{provider}-{label}'
                data = {'type': provider, 'access_token': ('sk-ant-oat-' if provider == 'claude' else '') + 'synthetic-' + name,
                        'email': name + '@example.invalid', 'priority': 0,
                        'expired': '2099-01-01T00:00:00Z', 'proxy_url': proxy,
                        'plan_type': 'pro', 'account_id': 'synthetic-' + name,
                        'cloak_mode': 'never', 'rebuild_mid_system_message': False}
                (auth / (name + '.json')).write_text(json.dumps(data))
                (auth / (name + '.json')).chmod(0o600)
        cfg = yaml.safe_load((ROOT / 'tests/fixtures/config-v8-nested.yaml').read_text())
        cfg['server']['port'] = self.port
        cfg['server']['kv-store'] = {'disabled': True}
        cfg['management']['secret-key'] = MANAGEMENT_KEY
        cfg['access']['api-keys'] = [CLIENT_KEY]
        cfg['oauth']['auth-dir'] = str(auth)
        cfg['requests']['proxy-url'] = proxy
        cfg['routing']['force-model-prefix'] = False
        cfg['routing']['cooldown']['disable-cooling'] = False
        cfg['routing']['retry'].update({'request-retry': 5, 'max-retry-interval': 45})
        cfg['observability']['usage']['usage-statistics-enabled'] = True
        cfg['observability']['logs']['debug'] = True
        self.config = self.root / 'config.yaml'
        self.config.write_text(yaml.safe_dump(cfg))
        env = dict(os.environ, SSL_CERT_FILE=str(cert), SSL_CERT_DIR=str(self.root),
                   HTTP_PROXY=proxy, HTTPS_PROXY=proxy, NO_PROXY='127.0.0.1,localhost')
        self.log = (self.root / 'gateway.log').open('w')
        self.proc = subprocess.Popen([str(self.binary), '-local-model', '-config', str(self.config)],
                                     cwd=self.root, env=env, stdout=self.log, stderr=self.log)
        print(f'Isolated gateway PID {self.proc.pid}, port {self.port}; mock port {self.mock.server_port}', flush=True)
        self.evidence.update(gateway_pid=self.proc.pid, gateway_port=self.port, mock_port=self.mock.server_port)
        wait_for(lambda: len(self.management('/auth-files').get('files', [])) == 4)
        self.evidence['version'] = self.version

    def request(self, path, body=None, key=CLIENT_KEY, method=None, headers=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}' + path,
            data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', **(headers or {})})
        with HTTP.open(req, timeout=60) as response:
            self.version = response.headers.get('X-CPA-VERSION', getattr(self, 'version', None))
            return json.load(response)

    def management(self, path, body=None, method=None):
        return self.request('/v0/management' + path, body, MANAGEMENT_KEY, method)

    def check(self, name, passed, **detail):
        self.evidence['checks'].append({'name': name, 'passed': bool(passed), **detail})
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)
        assert passed, name

    def call(self, provider, session=None, model=None, extra=None, extra_headers=None):
        session = session or str(uuid.uuid4())
        if provider == 'codex':
            body = {'model': model or 'gpt-6-luna', 'input': 'synthetic', 'instructions': 'synthetic',
                    'prompt_cache_key': session, 'stream': False}
            path = '/v1/responses'
        else:
            body = {'model': model or 'claude-sonnet-5-5', 'max_tokens': 64, 'stream': False,
                    'messages': [{'role': 'user', 'content': 'synthetic'}]}
            path = '/v1/messages'
        headers = {'session_id': session, 'X-Claude-Code-Session-Id': session,
                   'anthropic-version': '2023-06-01', **(extra_headers or {})}
        if extra:
            body.update(extra)
        offset = len(self.mock.records)
        result = self.request(path, body, headers=headers)
        attempts = self.mock.records[offset:]
        self.check(f'{provider} {body["model"]} completes',
                   result.get('status') == 'completed' if provider == 'codex' else result.get('type') == 'message')
        return attempts

    def run(self):
        cfg = self.management('/config')
        self.check('nested retry policy consumed', cfg['request-retry'] == 5 and cfg['max-retry-interval'] == 45)
        if self.version == '8.0.47':
            # Startup tolerates the future key, but strict persistence rejects it.
            for provider in ('codex', 'claude'):
                self.call(provider)
            try:
                self.request('/v8/management/config', yaml.safe_load(self.config.read_text()), MANAGEMENT_KEY, 'PATCH')
            except urllib.error.HTTPError as error:
                detail = error.read().decode()
                self.check('rollback strict PATCH rejects future KV key',
                           error.code == 400, response=detail)
            else:
                self.check('rollback strict PATCH rejects future KV key', False)
            logs = (self.root / 'gateway.log').read_text()
            self.check('rollback migration warns about future KV key',
                       'unrecognized configuration section "server.kv-store"' in logs)
            original = self.config.read_bytes()
            inode = self.config.stat().st_ino
            gateway_config.write_same_inode(self.config, gateway_config.remove_local_kv(original), original)
            self.check('rollback KV key removed in place', self.config.stat().st_ino == inode
                       and 'kv-store' not in yaml.safe_load(self.config.read_text())['server'])
            self.evidence['rollback_kv'] = {'startup_with_key': True, 'inference_with_key': True,
                'strict_patch_accepts_key': False, 'migration_warning': True, 'removed_same_inode': True}
        self.request('/v8/management/config', yaml.safe_load(self.config.read_text()), MANAGEMENT_KEY, 'PATCH')
        self.check('nested fixture passes strict v8 config validation', True)
        if self.version != '8.0.47':
            self.check('local KV explicitly disabled in persisted nested config',
                       yaml.safe_load(self.config.read_text())['server']['kv-store']['disabled'] is True)
        self.check_no_kv()
        models = {m['id'] for m in self.request('/v1/models')['data']}
        self.check('plain model routes advertised', set(CODEX_MODELS + CLAUDE_MODELS) <= models,
                   missing=sorted(set(CODEX_MODELS + CLAUDE_MODELS) - models))
        for provider, names in (('codex', CODEX_MODELS), ('claude', CLAUDE_MODELS)):
            for model in names:
                attempts = self.call(provider, model=model)
                self.check(f'{model} reaches matching provider',
                           len(attempts) == 1 and attempts[0]['account'].startswith(provider))
        for provider in ('codex', 'claude'):
            session = str(uuid.uuid4())
            first = self.call(provider, session)[0]['account']
            offset = len((self.root / 'gateway.log').read_text())
            second = self.call(provider, session)[0]['account']
            logs = (self.root / 'gateway.log').read_text()[offset:]
            self.check(provider + ' affinity cache hit before TTL change', first == second and 'cache hit' in logs)
            raw = self.config.read_bytes()
            old = yaml.safe_load(raw)['routing']['session-affinity-ttl']
            new = '1h0m1s' if old != '1h0m1s' else '1h0m0s'
            inode = self.config.stat().st_ino
            with self.config.open('r+b') as stream:
                stream.write(gateway_config.validated_edit(raw, {'session-affinity-ttl': new}))
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            wait_for(lambda: self.management('/config')['routing']['session-affinity-ttl'] == new)
            time.sleep(.3)
            offset = len((self.root / 'gateway.log').read_text())
            self.call(provider, session)
            logs = (self.root / 'gateway.log').read_text()[offset:]
            self.check(provider + ' in-place TTL change releases pin',
                       self.config.stat().st_ino == inode and 'cache miss, new binding' in logs,
                       previous_ttl=old, new_ttl=new)
        for provider in ('codex', 'claude'):
            for status in (429, 503):
                session = str(uuid.uuid4())
                account = self.call(provider, session)[0]['account']
                self.mock.failures[account] = status
                try:
                    attempts = self.call(provider, session)
                    self.check(f'{provider} {status} fails over in same request',
                               len(attempts) == 2 and attempts[0]['account'] == account
                               and attempts[0]['status'] == status and attempts[1]['account'] != account,
                               attempts=[{'account': a['account'], 'status': a['status']} for a in attempts])
                    followup = self.call(provider, session)
                    self.check(f'{provider} {status} rebind sticks', len(followup) == 1
                               and followup[0]['account'] == attempts[-1]['account'])
                finally:
                    self.mock.failures.clear()
                    rows = self.management('/auth-files')['files']
                    for row in rows:
                        if row.get('type') == provider:
                            self.management('/reset-quota', {'auth_index': row['auth_index']}, 'POST')
                    time.sleep(.2)
        for path in ('/v1/models', '/v0/management/config'):
            try:
                self.request(path, key='')
            except urllib.error.HTTPError as error:
                self.check(path + ' rejects unauthenticated access', error.code == 401)
            else:
                self.check(path + ' rejects unauthenticated access', False)
        if tuple(map(int, self.version.split('.'))) >= (8, 0, 43):
            self.claude_wire()
        if self.version == '8.0.79':
            self.request_error_rules()
        self.collect()
        logs = (self.root / 'gateway.log').read_text().lower()
        if self.version == '8.0.47':
            logs = '\n'.join(line for line in logs.splitlines()
                             if 'unrecognized configuration section \"server.kv-store\"' not in line)
        self.check('no unexpected unknown or removed config warnings',
                   not any(term in logs for term in ('unknown field', 'unknown key', 'deprecated',
                                                     'removed key', 'unrecognized configuration')))
        self.evidence['refused_external_hosts'] = sorted(set(self.mock.refused))
        self.check_no_kv()

    def check_no_kv(self):
        files = [p for p in (self.root / 'auth').rglob('*') if p.is_file()]
        self.check('disabled local KV creates no database files',
                   not any(p.suffix in ('.db', '.bbolt', '.bolt') for p in files))
        # bbolt has little-endian magic 0xED0CDAED at offset 16 in page zero.
        self.check('disabled local KV creates no bbolt files regardless of extension',
                   not any(p.read_bytes()[16:20] == b'\xed\xda\x0c\xed' for p in files))

    def claude_wire(self):
        # v8.0.63 advances the native fingerprint baseline (b4e7dbd2).
        # Keep the rollback fixture pinned and verify the candidate's current
        # native profile, plus its deliberate normalization of the older caller.
        candidate = self.version == '8.0.79'
        version = '2.1.321' if candidate else '2.1.304'
        runtime = 'v26.12.1' if candidate else 'v26.3.0'
        ua = f'claude-cli/{version} (external, sdk-ts, agent-sdk/0.3.276)'
        system = [{'type': 'text', 'text': f'x-anthropic-billing-header: cc_version={version}.179; cc_entrypoint=sdk-ts;'},
                  {'type': 'text', 'text': "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK."},
                  {'type': 'text', 'text': 'isolated-sdk-system-marker'}]
        messages = [{'role': 'user', 'content': 'synthetic'},
                    {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'synthetic',
                                                     'signature': synthetic_signature()},
                                                    {'type': 'text', 'text': 'synthetic'}]},
                    {'role': 'user', 'content': 'continue'}]
        attempts = self.call('claude', extra={'system': system, 'messages': messages,
                            'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'low'},
                            'metadata': {'user_id': json.dumps({'account_uuid': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                                         'device_id': 'c' * 64, 'session_id': '33333333-4444-4555-8666-777777777777'})},
                            'tools': [{'name': 'Bash', 'description': 'synthetic', 'input_schema': {'type': 'object'}}]},
                            extra_headers={'User-Agent': ua, 'X-App': 'cli', 'Anthropic-Beta': 'claude-code-20250219',
                                           'X-Stainless-Package-Version': '0.128.0', 'X-Stainless-Runtime-Version': runtime,
                                           'X-Stainless-Os': 'MacOS', 'X-Stainless-Arch': 'arm64'})
        body = attempts[-1]['body']
        self.evidence['claude_wire'] = {'thinking': body.get('thinking'),
                                       'user_agent': attempts[-1]['user_agent'],
                                       'history_types': [[b.get('type') for b in m.get('content', [])]
                                                         for m in body.get('messages', []) if isinstance(m.get('content'), list)]}
        self.check('Claude preserved thinking opts into drop_block',
                   body.get('thinking', {}).get('block_binding', {}).get('prefix_mismatch_behavior') == 'drop_block')
        self.check('sdk-ts native request preserves caller profile and system',
                   attempts[-1]['user_agent'] == ua
                   and any(b.get('text') == 'isolated-sdk-system-marker' for b in body.get('system', [])))
        if candidate:
            legacy = self.call('claude', extra={'system': system}, extra_headers={
                'User-Agent': 'claude-cli/2.1.304 (external, sdk-ts, agent-sdk/0.3.276)',
                'X-App': 'cli', 'Anthropic-Beta': 'claude-code-20250219',
                'X-Stainless-Package-Version': '0.128.0', 'X-Stainless-Runtime-Version': 'v26.3.0'})
            self.check('legacy SDK caller normalizes to candidate fingerprint baseline',
                       legacy[-1]['user_agent'] == 'claude-cli/2.1.321 (external, cli)')

    def request_error_rules(self):
        # Request-scoped classification and usage propagation changed in 8.0.58
        # and 8.0.64. Exercise the deployed rule *shapes* with synthetic text.
        cfg = yaml.safe_load(self.config.read_text())
        cfg['routing']['cooldown']['transient-error-cooldown-seconds'] = 15
        cfg.setdefault('upstream', {}).setdefault('claude', {})['model-level-cooling'] = True
        cfg['oauth']['request-scoped-errors'] = {'claude': [
            {'status': 404, 'match': ['synthetic isolated failure'], 'action': 'continue'},
            {'status': 503, 'match': ['synthetic isolated failure'], 'action': 'stop'}]}
        self.request('/v8/management/config', cfg, MANAGEMENT_KEY, 'PATCH')
        wait_for(lambda: self.management('/config').get('transient-error-cooldown-seconds') == 15)
        self.check('15 second transient cooldown consumed with retry policy preserved',
                   self.management('/config')['request-retry'] == 5
                   and self.management('/config')['max-retry-interval'] == 45)
        for status in (404, 503):
            session = str(uuid.uuid4())
            account = self.call('claude', session)[0]['account']
            self.mock.failures[account] = status
            offset = len(self.mock.records)
            try:
                if status == 404:
                    attempts = self.call('claude', session)
                    self.check('Claude matching 404 continue reaches peer in same request',
                               len(attempts) == 2 and attempts[0]['status'] == 404
                               and attempts[1]['account'] != account and attempts[1]['status'] == 200)
                else:
                    try:
                        self.call('claude', session)
                    except urllib.error.HTTPError as error:
                        self.check('Claude matching overload stop returns original status', error.code == 503)
                    else:
                        self.check('Claude matching overload stop returns original status', False)
                    self.check('Claude matching overload stop does not rotate or replay',
                               len(self.mock.records) - offset == 1)
            finally:
                self.mock.failures.clear()

    def collect(self):
        spec = importlib.util.spec_from_file_location('isolated_collector', ROOT / 'scripts/collect-usage.py')
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        db, key = self.root / 'usage.sqlite3', self.root / 'management-key'
        key.write_text(MANAGEMENT_KEY)
        real_urlopen = HTTP.open
        def scratch_queue(req, **kwargs):
            assert req.full_url == 'http://127.0.0.1:8317/v0/management/usage-queue?count=1000'
            rewritten = urllib.request.Request(f'http://127.0.0.1:{self.port}/v0/management/usage-queue?count=1000',
                                              headers=dict(req.header_items()))
            response = real_urlopen(rewritten, **kwargs)
            records = json.load(response)
            response.close()
            self.check('8.x usage queue is JSON list', isinstance(records, list) and bool(records))
            self.check('additive effective tier discarded', all('effective_service_tier' not in collector.reduce_record(r)
                                                               for r in records))
            self.evidence['queue_records'] = len(records)
            return io.BytesIO(json.dumps(records).encode())
        time.sleep(1)
        output = io.StringIO()
        with patch.object(collector.urllib.request, 'urlopen', side_effect=scratch_queue), contextlib.redirect_stdout(output):
            collector.collect(db, key)
        self.evidence['collection'] = json.loads(output.getvalue().splitlines()[-1])
        with sqlite3.connect(db) as con:
            providers = {r[0] for r in con.execute('SELECT DISTINCT provider FROM usage')}
            count = con.execute('SELECT count(*) FROM usage').fetchone()[0]
        self.check('existing collector persists both providers', providers == {'codex', 'claude'}
                   and count == self.evidence['queue_records']
                   and self.evidence['collection']['quarantined_records'] == 0,
                   rows=count)

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.evidence['gateway_exit'] = self.proc.returncode
        if self.mock:
            self.mock.shutdown()
            self.mock.server_close()
        if self.thread:
            self.thread.join(timeout=5)
        if self.log:
            self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--scratch', type=Path,
                        default=Path('~/.cache/agent-scratch/personal-codex-router').expanduser())
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gateway-upgrade-', dir=args.scratch) as directory:
        lab = Lab(Path(directory), args.binary.resolve())
        try:
            lab.start()
            lab.run()
        finally:
            lab.close()
            if (lab.root / 'gateway.log').exists():
                args.output.with_suffix('.log').write_bytes((lab.root / 'gateway.log').read_bytes())
            args.output.write_text(json.dumps(lab.evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
