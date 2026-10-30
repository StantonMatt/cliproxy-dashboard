#!/usr/bin/env python3
"""Probe a gateway binary with dummy OAuth files and a loopback TLS mock.

No production files, credentials, listeners, or services are accessed. The
installed OAuth-file loader does not support a Codex base_url override. Instead
the supported proxy_url directs HTTPS to a mock CONNECT server, which terminates
TLS locally without forwarding. Its certificate is trusted only by the child.
Requires openssl and the Python h2 package (tested with h2 4.3.0).
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config

ROOT = Path(__file__).resolve().parents[1]
TEMP = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'agent-scratch/personal-codex-router/quota-probe'
MODEL = 'gpt-6-astra'
# Prevent the Python client from using environment proxy settings.
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Mock:
    def __init__(self):
        self.lock = threading.Lock()
        self.modes = {'A': 'ok', 'B': 'ok'}
        self.reset_seconds = 12
        self.retry_after = 2
        self.reset_at = None
        self.attempts = []
        self.blocked_egress = 0
        self.tls = None
        self.tokens = {}

    def set(self, **modes):
        with self.lock:
            self.modes.update(modes)

    def quota(self):
        error = {'type': 'usage_limit_reached', 'code': 'usage_limit_reached',
                 'message': "You've hit your usage limit. Try again later.",
                 'plan_type': 'plus'}
        if self.reset_seconds is not None:
            error['resets_in_seconds'] = self.reset_seconds
        if self.reset_at is not None:
            error['resets_at'] = self.reset_at
        return {'error': error}

    def handler(self):
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        from h2.events import RequestReceived, DataReceived, StreamEnded

        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                if self.path != 'chatgpt.com:443':
                    with mock.lock:
                        mock.blocked_egress += 1
                    self.send_error(403)
                    self.close_connection = True
                    return
                self.send_response(200, 'Connection established')
                self.end_headers()
                self.wfile.flush()
                self.connection = mock.tls.wrap_socket(self.connection, server_side=True)
                self.close_connection = True
                self.connection.settimeout(5)
                h2 = H2Connection(H2Configuration(client_side=False, header_encoding='utf-8'))
                h2.initiate_connection()
                self.connection.sendall(h2.data_to_send())
                requests = {}
                try:
                    while True:
                        data = self.connection.recv(65536)
                        if not data:
                            return
                        for event in h2.receive_data(data):
                            if isinstance(event, RequestReceived):
                                requests[event.stream_id] = [dict(event.headers), bytearray()]
                            elif isinstance(event, DataReceived):
                                requests[event.stream_id][1].extend(event.data)
                                h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                            elif isinstance(event, StreamEnded):
                                headers, payload = requests.pop(event.stream_id)
                                if self.respond(h2, event.stream_id, headers, payload):
                                    return
                        self.connection.sendall(h2.data_to_send())
                except (OSError, ssl.SSLError):
                    return
                finally:
                    self.connection.close()

            def respond(self, h2, stream_id, headers, payload):
                token = headers.get('authorization', '').removeprefix('Bearer ')
                account = mock.tokens.get(token)
                if (headers.get(':method') != 'POST' or
                        headers.get(':path') != '/backend-api/codex/responses' or account is None):
                    with mock.lock:
                        mock.blocked_egress += 1
                    h2.send_headers(stream_id, [(':status', '403')], end_stream=True)
                    self.connection.sendall(h2.data_to_send())
                    return
                body = json.loads(payload)
                with mock.lock:
                    mode = mock.modes[account]
                    mock.attempts.append({'account': account, 'mode': mode,
                                          'at': time.monotonic()})
                if mode == 'quota':
                    data = json.dumps(mock.quota()).encode()
                    h2.send_headers(stream_id, [(':status', '429'),
                        ('content-type', 'application/json'), ('retry-after', str(mock.retry_after)),
                        ('x-ratelimit-remaining-requests', '0'), ('content-length', str(len(data)))])
                    h2.send_data(stream_id, data, end_stream=True)
                    self.connection.sendall(h2.data_to_send())
                    return
                if mode == 'disconnect_before_headers':
                    return True
                h2.send_headers(stream_id, [(':status', '200'), ('content-type', 'text/event-stream')])
                if mode == 'disconnect_before_output':
                    self.connection.sendall(h2.data_to_send())
                    time.sleep(.2)
                    return True

                def event(value):
                    data = ('data: ' + json.dumps(value) + '\n\n').encode()
                    h2.send_data(stream_id, data)
                    self.connection.sendall(h2.data_to_send())

                response_id = 'resp_mock_' + uuid.uuid4().hex
                event({'type': 'response.created', 'response': {'id': response_id,
                       'object': 'response', 'status': 'in_progress', 'model': body['model']}})
                event({'type': 'response.output_text.delta', 'output_index': 0,
                       'content_index': 0, 'item_id': 'msg_mock', 'delta': 'MOCK_' + account})
                if mode in ('stream_error', 'disconnect'):
                    # Give the gateway time to commit the generated delta downstream.
                    time.sleep(.2)
                    if mode == 'stream_error':
                        event({'type': 'error', **mock.quota()})
                        h2.end_stream(stream_id)
                        self.connection.sendall(h2.data_to_send())
                    # For disconnect, omit END_STREAM: a real transport fault.
                    return mode == 'disconnect'
                event({'type': 'response.completed', 'response': {
                    'id': response_id, 'object': 'response', 'status': 'completed',
                    'model': body['model'], 'output': [{'id': 'msg_mock', 'type': 'message',
                    'role': 'assistant', 'status': 'completed', 'content': [
                    {'type': 'output_text', 'text': 'MOCK_' + account, 'annotations': []}]}],
                    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})
                h2.end_stream(stream_id)
                self.connection.sendall(h2.data_to_send())

        return Handler


def private_json(path, value):
    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as file:
        json.dump(value, file)


class Gateway:
    def __init__(self, base, key, management_key, mock):
        self.base, self.key, self.management_key, self.mock = base, key, management_key, mock
        self.accounts = {}

    def management(self, path, body=None, method=None):
        request = urllib.request.Request(self.base + '/v0/management/' + path,
                  data=None if body is None else json.dumps(body).encode(), method=method,
                  headers={'Authorization': 'Bearer ' + self.management_key,
                           'Content-Type': 'application/json'})
        with CLIENT.open(request, timeout=5) as response:
            return json.load(response)

    def files(self):
        return self.management('auth-files')['files']

    def call(self, session=None, stream=True):
        session = session or str(uuid.uuid4())
        data = {'model': MODEL, 'stream': stream, 'store': False,
                'instructions': 'Reply with the mock marker.',
                'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'test'}]}],
                'prompt_cache_key': session}
        start = time.monotonic()
        with self.mock.lock:
            offset = len(self.mock.attempts)
        request = urllib.request.Request(self.base + '/v1/responses',
                  data=json.dumps(data).encode(), headers={
                  'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                  'session_id': session})
        read_error = None
        raw = b''
        try:
            with CLIENT.open(request, timeout=8) as response:
                status = response.status
                headers = dict(response.headers)
                try:
                    raw = response.read()
                except http.client.IncompleteRead as error:
                    raw, read_error = error.partial, type(error).__name__
        except urllib.error.HTTPError as error:
            status, raw, headers = error.code, error.read(), dict(error.headers)
        events = []
        for line in raw.splitlines():
            if line.startswith(b'data: ') and line[6:] != b'[DONE]':
                events.append(json.loads(line[6:]))
        body = None
        if not events and raw:
            body = json.loads(raw)
        with self.mock.lock:
            attempts = [{'account': x['account'], 'mode': x['mode'],
                         'after_seconds': round(x['at'] - start, 3)}
                        for x in self.mock.attempts[offset:]]
        result = {'status': status, 'upstream_attempts': attempts,
                  'elapsed_seconds': round(time.monotonic() - start, 3),
                  'completed': any(x.get('type') == 'response.completed' for x in events)
                      or (isinstance(body, dict) and body.get('status') == 'completed'),
                  'event_types': [x.get('type') for x in events],
                  'text': ''.join(x.get('delta', '') for x in events
                                  if x.get('type') == 'response.output_text.delta'),
                  'errors': [x for x in events if x.get('type') in ('error', 'response.failed')],
                  'body': body, 'read_error': read_error,
                  'retry_after': headers.get('Retry-After')}
        return result

    def clear(self, account):
        return self.management('reset-quota', {'auth_index': self.accounts[account]['auth_index']})

    def state(self):
        files = {row['auth_index']: row for row in self.files()}
        return {name: {key: files[row['auth_index']].get(key) for key in
                       ('status', 'unavailable', 'priority', 'cooldowns')}
                for name, row in self.accounts.items()}


@contextlib.contextmanager
def instance(policy, binary):
    mock = Mock()
    server = ThreadingHTTPServer(('127.0.0.1', 0), mock.handler())
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='quota-probe-', dir=TEMP) as directory:
            root = Path(directory)
            auth = root / 'auth'
            auth.mkdir(mode=0o700)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            assert port != 8317
            upstream = 'http://127.0.0.1:' + str(server.server_port)
            cert, cert_key = root / 'mock.crt', root / 'mock.key'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-days', '1', '-subj', '/CN=chatgpt.com',
                            '-addext', 'subjectAltName=DNS:chatgpt.com',
                            '-keyout', str(cert_key), '-out', str(cert)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
            mock.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            mock.tls.load_cert_chain(cert, cert_key)
            mock.tls.set_alpn_protocols(['h2'])
            for name, priority in [('A', 100), ('B', 0)]:
                token = secrets.token_urlsafe(32)
                mock.tokens[token] = name
                private_json(auth / (name + '.json'), {
                    'type': 'codex', 'access_token': token, 'refresh_token': '',
                    'id_token': '', 'account_id': 'mock-account-' + name,
                    'email': name.lower() + '@example.invalid', 'plan_type': 'plus',
                    'expired': '2099-01-01T00:00:00Z', 'disabled': False,
                    'priority': priority, 'proxy_url': upstream})
            key, management_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            config = {'host': '127.0.0.1', 'port': port, 'auth-dir': str(auth),
                      'api-keys': [key], 'usage-statistics-enabled': True,
                      'debug': False, 'logging-to-file': False, 'request-log': False,
                      'discovery': {'enabled': False}, 'pprof': {'enable': False},
                      # Block accidental external HTTP/HTTPS at the local mock.
                      'proxy-url': upstream,
                      'remote-management': {'allow-remote': False, 'secret-key': management_key,
                          'disable-control-panel': True, 'disable-auto-update-panel': True},
                      'request-retry': policy['request-retry'],
                      'max-retry-credentials': policy['max-retry-credentials'],
                      'max-retry-interval': policy['max-retry-interval'],
                      'routing': {'strategy': policy['routing'],
                          **{k: policy[k] for k in ('session-affinity', 'session-affinity-ttl',
                                                   'session-affinity-subagents')}}}
            config_path = root / 'config.json'
            private_json(config_path, config)
            gateway = Gateway(f'http://127.0.0.1:{port}', key, management_key, mock)
            with (root / 'process.log').open('wb') as log:
                environment = dict(os.environ, SSL_CERT_FILE=str(cert), SSL_CERT_DIR=str(root))
                process = subprocess.Popen([str(binary), '-config', str(config_path), '-local-model'],
                           cwd=root, stdout=log, stderr=log, env=environment)
                try:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError('Isolated gateway exited during startup')
                        try:
                            files = gateway.files()
                            if len(files) == 2:
                                request = urllib.request.Request(gateway.base + '/v1/models',
                                    headers={'Authorization': 'Bearer ' + key})
                                with CLIENT.open(request, timeout=5) as response:
                                    models = json.load(response)['data']
                                if MODEL not in {x['id'] for x in models}:
                                    time.sleep(.1)
                                    continue
                                gateway.accounts = {('A' if x.get('priority', 0) == 100 else 'B'): x
                                                    for x in files}
                                assert set(gateway.accounts) == {'A', 'B'}
                                break
                        except (OSError, urllib.error.URLError):
                            pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Isolated gateway did not become ready')
                    yield gateway
                    assert mock.blocked_egress == 0, 'Unexpected egress was blocked by mock'
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def accounts(result):
    return [x['account'] for x in result['upstream_attempts']]


def settle_cooldown(gateway, expected):
    start = time.monotonic()
    deadline = start + 2
    while time.monotonic() < deadline:
        state = gateway.state()
        if all(state[name]['unavailable'] for name in expected):
            return {'observed_after_seconds': round(time.monotonic() - start, 3), 'state': state}
        time.sleep(.02)
    return {'observed_after_seconds': None, 'state': gateway.state()}


def run(policy, binary, version='7.3.11'):
    tests = []
    checks = []
    # v8.0.7 (#6247) makes an empty SSE disconnect credential-retryable.
    disconnect_fix = tuple(int(x) for x in version.split('.')) >= (8, 0, 7)

    def check(name, passed):
        # Behavioral changes are evaluation results, not reasons to lose the report.
        checks.append({'name': name, 'passed': bool(passed)})

    with instance(policy, binary) as gateway:
        mock = gateway.mock
        mock.set(A='quota')
        failed_at = time.monotonic()
        first = gateway.call()
        check('streaming quota failover completes on B',
              first['completed'] and accounts(first) == ['A', 'B'])
        second = gateway.call()
        settled = settle_cooldown(gateway, ['A'])
        after_settling = gateway.call()
        check('settled quota cooldown excludes A',
              after_settling['completed'] and accounts(after_settling) == ['B'])
        tests.append({'scenario': 1, 'name': 'quota failover in the same request', 'result': first})
        tests.append({'scenario': 2, 'name': 'next new request and settled cooldown',
                      'immediate_next_request': second, 'cooldown': settled,
                      'after_cooldown_visible': after_settling})
        mock.set(A='ok')
        before = gateway.call()
        check('provider recovery alone preserves cooldown',
              accounts(before) == ['B'])
        # Body says 12 seconds, Retry-After says 2: probe after the header deadline.
        time.sleep(max(0, failed_at + 3 - time.monotonic()))
        after_header = gateway.call()
        check('body reset outlasts Retry-After header',
              accounts(after_header) == ['B'])
        time.sleep(max(0, failed_at + 12.3 - time.monotonic()))
        after_body = gateway.call()
        check('A recovers after body reset deadline',
              accounts(after_body) == ['A'] and after_body['completed'])
        tests.append({'scenario': 6, 'name': 'natural cooldown expiration',
                      'resets_in_seconds': 12, 'retry_after_header': 2,
                      'provider_healthy_while_cooled': before,
                      'after_header_deadline': after_header,
                      'after_body_deadline': after_body})
    with instance(policy, binary) as gateway:
        session = str(uuid.uuid4())
        baseline = gateway.call(session)
        check('new session initially selects A',
              accounts(baseline) == ['A'] and baseline['completed'])
        time.sleep(.1)
        gateway.mock.set(A='quota')
        failover = gateway.call(session)
        settle_cooldown(gateway, ['A'])
        followup = gateway.call(session)
        check('pinned session fails over to B',
              accounts(failover) == ['A', 'B'] and failover['completed'])
        check('pinned session stays on B while A cools',
              accounts(followup) == ['B'] and followup['completed'])
        gateway.mock.set(A='ok')
        cleared = gateway.clear('A')
        time.sleep(.15)
        pinned = gateway.call(session)
        fresh = gateway.call()
        check('affinity stays on B after A cooldown clear; fresh session selects A',
              accounts(pinned) == ['B'] and accounts(fresh) == ['A'])
        tests.append({'scenario': 3, 'name': 'affinity rebinds after quota exhaustion',
                      'baseline': baseline, 'failover': failover, 'followup': followup,
                      'after_A_clear_same_session': pinned, 'after_A_clear_new_session': fresh,
                      'clear_status': cleared['status']})
    with instance(policy, binary) as gateway:
        gateway.mock.reset_seconds = 3
        gateway.mock.retry_after = 1
        gateway.mock.set(A='quota', B='quota')
        failed_at = time.monotonic()
        exhausted = gateway.call()
        gateway.mock.set(A='ok', B='ok')
        immediate = gateway.call()
        settled = settle_cooldown(gateway, ['A', 'B'])
        after_settling = gateway.call()
        time.sleep(max(0, failed_at + 4 - time.monotonic()))
        after_reset = gateway.call()
        time.sleep(max(0, failed_at + 10.4 - time.monotonic()))
        recovered = gateway.call()
        check('both exhausted returns upstream 429 after trying A and B',
              exhausted['status'] == 429 and accounts(exhausted) == ['A', 'B'])
        check('both exhausted remains local 429 at four seconds',
              after_reset['status'] == 429 and not accounts(after_reset))
        check('both exhausted recovers at 10.4 seconds',
              recovered['completed'] and accounts(recovered) == ['A'])
        tests.append({'scenario': 4, 'name': 'both exhausted and subsequent recovery',
                      'resets_in_seconds': 3, 'retry_after_header': 1,
                      'both_exhausted': exhausted, 'provider_healthy_immediately': immediate,
                      'cooldown': settled, 'after_cooldown_visible': after_settling,
                      'at_4_seconds': after_reset, 'at_10_4_seconds': recovered})
    for mode in ('stream_error', 'disconnect', 'disconnect_before_output',
                 'disconnect_before_headers'):
        with instance(policy, binary) as gateway:
            gateway.mock.set(A=mode)
            partial = gateway.call()
            state_after_first = gateway.state()
            immediate = gateway.call()
            settled = settle_cooldown(gateway, ['A'])
            followup = gateway.call()
            third = gateway.call()
            name = (mode + ' after a generated delta' if mode in ('stream_error', 'disconnect')
                    else mode.replace('_', ' '))
            followups = [immediate, followup, third]
            if mode == 'disconnect_before_headers':
                check(name + ': transport failure retries B before response headers',
                      partial['completed'] and accounts(partial) == ['A', 'B'])
                check(name + ': three new requests retry A then B',
                      all(x['completed'] and accounts(x) == ['A', 'B'] for x in followups))
            elif mode == 'disconnect_before_output':
                if disconnect_fix:
                    check(name + ': empty upstream SSE failure retries B and completes',
                          partial['status'] == 200 and partial['completed']
                          and partial['text'] == 'MOCK_B' and accounts(partial) == ['A', 'B'])
                    check(name + ': three new requests complete on B',
                          all(x['status'] == 200 and x['completed'] and x['text'] == 'MOCK_B'
                              and accounts(x) == ['B'] for x in followups))
                else:
                    check(name + ': empty upstream SSE failure returns 408 without retry',
                          partial['status'] == 408 and not partial['completed']
                          and not partial['text'] and accounts(partial) == ['A'])
                    check(name + ': three new requests still fail on A',
                          all(x['status'] == 408 and not x['completed']
                              and accounts(x) == ['A'] for x in followups))
            else:
                check(name + ': partial stream has an error and no completion or retry',
                      partial['status'] == 200 and not partial['completed']
                      and accounts(partial) == ['A'] and partial['text'] == 'MOCK_A'
                      and bool(partial['errors']))
                check(name + ': three new requests ' +
                      ('complete on B' if mode == 'stream_error' else 'still fail on A'),
                      all(accounts(x) == ['B'] and x['completed'] for x in followups)
                      if mode == 'stream_error' else
                      all(accounts(x) == ['A'] and not x['completed'] for x in followups))
                if mode == 'disconnect':
                    # SSE error events already use server_error in 7.3.11.
                    expected_type = 'server_error'
                    check(name + ': request_timeout classified as ' + expected_type,
                          all(any(e.get('error', {}).get('code') == 'request_timeout'
                                  and e['error'].get('type') == expected_type
                                  for e in x['errors']) for x in [partial, *followups]))
            empty_disconnect_cooldown = mode == 'disconnect_before_output' and disconnect_fix
            if empty_disconnect_cooldown:
                check(name + ': A receives a 60-second model cooldown',
                      settled['state']['A']['unavailable']
                      and any(c.get('scope') == 'model' and c.get('model_key') == MODEL
                              and c.get('reason') == 'unknown'
                              and 58 <= c.get('remaining_seconds', 0) <= 60
                              for c in settled['state']['A']['cooldowns']))
            else:
                check(name + ': ' + ('quota cooldown recorded' if mode == 'stream_error'
                                    else 'disconnect leaves A eligible'),
                      bool(settled['state']['A']['unavailable']) == (mode == 'stream_error'))
            tests.append({'scenario': 5, 'name': name,
                          'result': partial, 'state_after_first_request': state_after_first,
                          'immediate_next_request': immediate,
                          'cooldown': settled, 'after_cooldown_poll': followup,
                          'three_consecutive_followups': followups})
    with instance(policy, binary) as gateway:
        gateway.mock.reset_seconds = 30
        gateway.mock.set(A='quota')
        triggered = gateway.call()
        check('priority-patch setup fails over to B',
              triggered['completed'] and accounts(triggered) == ['A', 'B'])
        settled = settle_cooldown(gateway, ['A'])
        gateway.mock.set(A='ok')
        patch = gateway.management('auth-files/fields',
                    {'name': gateway.accounts['A']['id'], 'priority': 101}, method='PATCH')
        time.sleep(.3)
        after_patch = gateway.call()
        cleared = gateway.clear('A')
        time.sleep(.15)
        after_clear = gateway.call()
        tests.append({'scenario': 6, 'name': 'priority patch versus explicit cooldown clear',
                      'resets_in_seconds': 30, 'patch_status': patch['status'],
                      'cooldown_before_patch': settled,
                      'after_patch': after_patch, 'clear_status': cleared['status'],
                      'after_clear': after_clear})
        check('priority patch preserves cooldown',
              accounts(after_patch) == ['B'] and after_patch['completed'])
        check('explicit reset clears cooldown',
              accounts(after_clear) == ['A'] and after_clear['completed'])
    with instance(policy, binary) as gateway:
        gateway.mock.set(A='quota')
        result = gateway.call(stream=False)
        check('non-streaming quota failover returns 200 on B',
              result['status'] == 200 and accounts(result) == ['A', 'B'])
        check('non-streaming quota failover completes',
              isinstance(result['body'], dict) and result['body'].get('status') == 'completed')
        tests.append({'scenario': 1, 'name': 'non-streaming client quota failover', 'result': result})
    for profile in ('absolute_reset', 'header_only'):
        with instance(policy, binary) as gateway:
            mock = gateway.mock
            mock.set(A='quota')
            mock.reset_seconds = 60 if profile == 'absolute_reset' else None
            mock.reset_at = int(time.time()) + 14 if profile == 'absolute_reset' else None
            mock.retry_after = 2 if profile == 'absolute_reset' else 90
            failed_at = time.monotonic()
            result = gateway.call()
            check('reset-timing setup fails over to B',
                  result['completed'] and accounts(result) == ['A', 'B'])
            state = settle_cooldown(gateway, ['A'])
            timing = {'scenario': 2, 'name': profile + ' cooldown timing',
                          'resets_at': mock.reset_at, 'resets_in_seconds': mock.reset_seconds,
                          'retry_after_header': mock.retry_after, 'result': result, 'cooldown': state}
            cooldowns = state['state']['A']['cooldowns']
            deadline = (dt.datetime.fromisoformat(cooldowns[0]['retry_at']).timestamp()
                        if cooldowns else None)
            if profile == 'absolute_reset':
                check('absolute reset takes precedence over relative reset',
                      deadline is not None and abs(deadline - mock.reset_at) < .1)
            else:
                mock.set(A='ok')
                time.sleep(max(0, failed_at + 1.3 - time.monotonic()))
                timing['provider_healthy_at_1_3_seconds'] = gateway.call()
                check('header-only quota returns to A by 1.3 seconds',
                      accounts(timing['provider_healthy_at_1_3_seconds']) == ['A'])
                check('header-only recovery completes',
                      timing['provider_healthy_at_1_3_seconds']['completed'])
            tests.append(timing)
    return tests, checks


def main(argv=None):
    global MODEL, TEMP
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default=MODEL, help='Model advertised by the isolated gateway')
    parser.add_argument('--scratch-dir', type=Path, default=TEMP)
    parser.add_argument('--binary', type=Path,
                        help='Gateway executable (default: effective cliproxyapi ExecStart); isolated config only')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/quota-failover-verification.json')
    args = parser.parse_args(argv)
    MODEL, TEMP = args.model, args.scratch_dir
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Quota verification interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    TEMP.mkdir(parents=True, exist_ok=True)
    binary = (args.binary or gateway_config.resolve_gateway_binary()).resolve()
    banner = subprocess.run([str(binary), '-h'], cwd=TEMP, capture_output=True,
                            text=True, timeout=5)
    match = re.search(r'CLIProxyAPI Version: ([^,]+), Commit: ([^,]+),',
                      banner.stdout + banner.stderr)
    if match is None:
        raise RuntimeError('Could not identify gateway binary version')
    version, revision = match.groups()
    policy = json.loads((ROOT / 'docs/failover-verification.json').read_text())['production_policy_used']
    tests, checks = run(policy, binary, version)
    report = {'tested_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'binary': str(binary),
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'model': MODEL,
              'binary_version': version, 'upstream_revision': revision,
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'mock_http_version': 'HTTP/2 over TLS; SSE payloads',
              'dependency': 'Python h2 4.3.0, with hpack 4.1.0 and hyperframe 6.1.0',
              'scope': 'isolated loopback gateway with two dummy Codex OAuth files and local TLS/SSE mock',
              'credential_type': 'codex OAuth auth files, matching production provider type',
              'upstream_override': 'Supported per-file proxy_url to a local CONNECT mock; '
                  'no outbound forwarding. SSL_CERT_FILE/SSL_CERT_DIR trust the temporary mock certificate '
                  'only in the isolated gateway process. OAuth base_url override is unsupported.',
              'source_references': [
                  'https://github.com/router-for-me/CLIProxyAPI/blob/' + revision + '/' + name for name in
                  ('internal/watcher/synthesizer/file.go', 'internal/watcher/synthesizer/config.go',
                   'internal/runtime/executor/codex_executor_auth.go',
                   'internal/runtime/executor/helps/utls_client.go', 'config.example.yaml')],
              'policy_source': 'docs/failover-verification.json; production config and overview not accessed',
              'production_policy_used': policy, 'priorities': {'A': 100, 'B': 0},
              'other_settings': 'gateway defaults, including Codex stream bootstrap buffering off',
              'tests': tests, 'baseline_behavior_checks': checks,
              'expectation_profile': 'v8.0.7 disconnect fix' if
                  tuple(int(x) for x in version.split('.')) >= (8, 0, 7) else 'pre-v8.0.7',
              'baseline_behavior_checks_passed': all(x['passed'] for x in checks),
              'not_covered': ['Real OAuth token validation/refresh and real OpenAI behavior',
                  'WebSocket transport, client cancellation, concurrent load',
                  'Week-long cooldowns, reset credits, dashboard/controller integration',
                  'Other models, prefixed routes, HTTP-date Retry-After headers, affinity TTL expiry',
                  'Repeated header-only quota errors and escalating fallback backoff'],
              'real_credentials_used': False, 'openai_contacted': False,
              'production_files_accessed': False, 'live_gateway_accessed': False,
              'temporary_instances_stopped_and_files_removed': True}
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(args.output), 'scenario_records': len(report['tests']),
                      'baseline_behavior_checks_passed': report['baseline_behavior_checks_passed'],
                      'temporary_instances_stopped_and_files_removed': True}))
    if not report['baseline_behavior_checks_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
