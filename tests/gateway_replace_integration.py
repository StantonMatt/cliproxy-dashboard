#!/usr/bin/env python3
"""Integration check: dashboard Codex sign-in replacement against a scratch, stock CLIProxyAPI.

Runs the real dashboard code (credential_replace and SigninManager.complete) against an
unmodified gateway binary on 127.0.0.1:18417, with synthetic credentials in a scratch auth
dir. Refreshes use the credential's proxy_url: a loopback CONNECT proxy on 18419 terminates
TLS for auth.openai.com with a scratch CA (SSL_CERT_FILE) and serves a fake token endpoint;
it refuses every other host. Never reads or writes /etc/cliproxyapi or /var/lib/cliproxyapi.

    python3 tests/gateway_replace_integration.py \\
        --binary /opt/cliproxyapi/<version>/cli-proxy-api [--runs 50] [--race-runs 12] \\
        [--scratch ~/.cache/agent-scratch/personal-codex-router/replace-integration] [--assume-tested]

--assume-tested evaluates a build that is not in signin.TESTED_GATEWAYS yet.
Prints a JSON summary; exits 0 with "SKIP" when the binary or openssl is unavailable.
"""
import argparse
import base64
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import account_access
import credential_replace
import routing_policy
import signin

PORT, PROXY_PORT = 18417, 18419
KEY = 'synthetic-management-key-' + secrets.token_hex(8)
EMAIL, WORKSPACE, USER = 'synthetic@example.invalid', 'acct-synthetic-1', 'user-synthetic'
NAME = 'codex-0a1b2c3d-synthetic@example.invalid-pro.json'
PROTECTED = ('/etc/cliproxyapi', '/var/lib/cliproxyapi')
SCRATCH_BASE = Path('~/.cache/agent-scratch').expanduser()
# Written into every scratch root; only a marked root is ever cleared.
MARKER = '.gateway-replace-integration'


def b64(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()


def tag(token):
    part = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))).get('tag')


def tokens(label, plan='pro'):
    now = int(time.time())
    auth = {'chatgpt_account_id': WORKSPACE, 'chatgpt_plan_type': plan, 'chatgpt_user_id': USER, 'user_id': USER}
    jwt = lambda claims: b64({'alg': 'none', 'typ': 'JWT'}) + '.' + b64(claims) + '.' + b64({'sig': secrets.token_hex(4)})
    return {'id_token': jwt({'iss': 'https://auth.openai.com', 'aud': [signin.CLIENT_ID], 'email': EMAIL,
                             'sub': USER, 'iat': now, 'exp': now + 864000, 'tag': label, signin.AUTH: auth}),
            'access_token': jwt({'iat': now, 'exp': now + 864000, 'tag': label, signin.AUTH: auth}),
            'refresh_token': 'rt-%s-%s' % (label, secrets.token_hex(8)), 'expires_in': 864000}


class TokenProxy(socketserver.ThreadingTCPServer):
    """CONNECT proxy: TLS for auth.openai.com only, answering refresh grants with fake tokens."""
    daemon_threads = allow_reuse_address = True

    def __init__(self, cert, key):
        self.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.context.load_cert_chain(cert, key)
        self.context.set_alpn_protocols(['http/1.1'])
        self.delay, self.received, self.count, self.refused = 0.0, [], 0, []
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', PROXY_PORT), Tunnel)


class Tunnel(socketserver.StreamRequestHandler):
    def handle(self):
        head = self.rfile.readline(4096).decode('latin-1').split()
        while self.rfile.readline(4096) not in (b'\r\n', b'\n', b''):
            pass
        if len(head) < 2 or head[0] != 'CONNECT' or head[1] != 'auth.openai.com:443':
            self.server.refused.append(head[1] if len(head) > 1 else '?')
            self.wfile.write(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            return
        self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        self.wfile.flush()
        try:
            with self.server.context.wrap_socket(self.connection, server_side=True) as tls:
                FakeToken(tls, self.client_address, self.server)
        except (OSError, ssl.SSLError):
            pass

    def finish(self):
        with contextlib.suppress(OSError):
            super().finish()


class FakeToken(BaseHTTPRequestHandler):
    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
        server = self.server
        with server.lock:
            server.received.append(form.get('refresh_token', [''])[0])
            server.count += 1
            label = 'REF%d' % server.count
        time.sleep(server.delay)
        body = json.dumps(dict(tokens(label), token_type='Bearer')).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Scratch:
    def __init__(self, root, binary):
        self.root = Path(root).expanduser().resolve()
        if any(str(self.root).startswith(p) for p in PROTECTED) or not self.root.is_relative_to(SCRATCH_BASE.resolve()):
            raise SystemExit('Scratch must be under %s: %s' % (SCRATCH_BASE, self.root))
        self.binary = binary
        self.auth = self.root / 'auth'
        self.config = self.root / 'config.yaml'
        self.log = self.root / 'gateway.log'
        self.process = None

    def prepare(self):
        if self.root.exists():
            if not (self.root / MARKER).is_file():
                raise SystemExit('Refusing to clear %s: this harness did not create it' % self.root)
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, mode=0o700)
        (self.root / MARKER).write_text('Scratch for tests/gateway_replace_integration.py; safe to delete.\n')
        self.auth.mkdir(mode=0o700)
        (self.root / 'routing').mkdir(mode=0o700)
        self.ca()
        self.config.write_text('\n'.join([
            'config-version: 8', 'server:', '  host: "127.0.0.1"', '  port: %d' % PORT,
            'management:', '  allow-remote: false', '  secret-key: "%s"' % KEY, '  disable-control-panel: true',
            'access:', '  api-keys:', '    - "synthetic-client-key"',
            'oauth:', '  auth-dir: "%s"' % self.auth, '']))
        credential = dict({k: v for k, v in tokens('OLD').items() if k != 'expires_in'},
            type='codex', email=EMAIL, account_id=WORKSPACE, plan_type='pro', disabled=False,
            last_refresh='2026-10-16T12:00:00Z', expired='2030-01-01T00:00:00Z', priority=7, prefix='syn',
            proxy_url='http://127.0.0.1:%d' % PROXY_PORT, custom_unknown={'keep': ['me', 1, 2.5, None]})
        path = self.auth / NAME
        path.write_text(json.dumps(credential))
        path.chmod(0o600)

    def ca(self):
        run = lambda *args: subprocess.run(['openssl', *args], cwd=self.root, check=True, capture_output=True, timeout=60)
        run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.crt', '-days', '2',
            '-subj', '/CN=scratch integration CA', '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        run('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr', '-subj', '/CN=auth.openai.com')
        (self.root / 'leaf.ext').write_text('subjectAltName=DNS:auth.openai.com\nextendedKeyUsage=serverAuth\n')
        run('x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial',
            '-out', 'leaf.crt', '-days', '2', '-extfile', 'leaf.ext')

    def start(self):
        env = dict(os.environ, SSL_CERT_FILE=str(self.root / 'ca.crt'), SSL_CERT_DIR=str(self.root),
                   HTTP_PROXY=f'http://127.0.0.1:{PROXY_PORT}', HTTPS_PROXY=f'http://127.0.0.1:{PROXY_PORT}',
                   NO_PROXY='127.0.0.1,localhost')
        log = open(self.log, 'ab')
        self.process = subprocess.Popen([self.binary, '-local-model', '-config', str(self.config)], cwd=self.root, env=env,
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        print(f'Started isolated gateway PID {self.process.pid}, port {PORT}; mock port {PROXY_PORT}', file=sys.stderr, flush=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if any(r.get('name') == NAME for r in mgmt('/auth-files').get('files', [])):
                    return
            except (OSError, ValueError):
                pass
            if self.process.poll() is not None:
                raise RuntimeError('Gateway exited; see ' + str(self.log))
            time.sleep(0.2)
        raise RuntimeError('Gateway did not list the synthetic credential')

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
        self.process = None


def mgmt(path, body=None, method=None, timeout=60):
    request = Request('http://127.0.0.1:%d/v0/management%s' % (PORT, path),
                      data=json.dumps(body).encode() if body is not None else None, method=method,
                      headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        return signin.ManagementResponse(account_access.json_object(response.read()),
                                         response.headers.get('X-CPA-VERSION'))


class Harness:
    def __init__(self, scratch, proxy):
        self.scratch, self.proxy = scratch, proxy
        self.path = scratch.auth / NAME
        self.manager = signin.SigninManager(mgmt, threading.RLock(), lambda _: None)
        # A retry must never start a device sign-in, which would reach the real auth.openai.com.
        self.manager.relay = self.no_device_flow

    @staticmethod
    def no_device_flow(*_, **__):
        raise RuntimeError('Device sign-in is not part of the integration check')

    def row(self):
        rows = [r for r in mgmt('/auth-files')['files'] if r.get('name') == NAME]
        assert len(rows) == 1, rows
        return rows[0]

    def disk(self):
        return json.loads(self.path.read_text())

    def runtime(self):
        """Tag of the gateway's in-memory access token, through /api-call to a loopback echo."""
        seen = []

        class Echo(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization', ''))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = HTTPServer(('127.0.0.1', 0), Echo)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        server.timeout = 10
        thread.start()
        try:
            mgmt('/api-call', {'auth_index': self.row()['auth_index'], 'method': 'GET', 'proxy_url': 'direct',
                               'url': 'http://127.0.0.1:%d/echo' % server.server_address[1],
                               'header': {'Authorization': 'Bearer $TOKEN$'}}, method='POST')
        finally:
            thread.join(10)
            server.server_close()
        return tag(seen[0].split(' ', 1)[1]) if seen else None

    def converged(self, wait=3.0):
        """runtime == disk; returns (matched, matched_without_waiting)."""
        deadline, first = time.time() + wait, True
        while True:
            matched = self.runtime() == tag(self.disk()['access_token'])
            if matched or time.time() >= deadline:
                return matched, matched and first
            first = False
            time.sleep(0.2)

    def replace(self, new, enable=False):
        row = self.row()
        now = time.time()
        session = {'account_id': row['auth_index'], 'identity': account_access.identity(row),
                   'handle': secrets.token_urlsafe(32), 'state': 'pending', 'expected_email': row['email'],
                   'started_at': now, 'expires_at': now + 900, 'routing_generation': 0, 'enable': enable,
                   'stop': threading.Event()}
        self.manager.sessions[row['auth_index']] = session
        with open(self.scratch.root / 'signin-journal.log', 'a') as journal, contextlib.redirect_stdout(journal):
            self.manager.complete(session, new)
        return self.manager.outcomes[row['auth_index']]

    def refresh(self, out=None):
        started = time.time()
        try:
            mgmt('/auth-files/refresh', {'name': NAME}, method='POST')
            status = 'ok'
        except HTTPError as error:
            status = error.code
        if out is not None:
            out.update(status=status, seconds=round(time.time() - started, 2))

    def later(self, delay, action):
        thread = threading.Thread(target=lambda: (time.sleep(delay), action()), daemon=True)
        thread.start()
        return thread


def check(results, name, condition, detail=None):
    results.setdefault(name, {'passed': 0, 'failed': 0, 'failures': []})
    results[name]['passed' if condition else 'failed'] += 1
    if not condition and len(results[name]['failures']) < 5:
        results[name]['failures'].append(detail)


def scenarios(h, args, results):
    stable = ('name', 'auth_index', 'priority')
    before_row, before_disk = h.row(), h.disk()

    # Control: the pre-fix import (fields PATCH only). On 8.0.30 it returns 200 and changes nothing.
    old = tokens('PATCHONLY', 'free')
    mgmt('/auth-files/fields', dict({k: old[k] for k in ('access_token', 'refresh_token', 'id_token')},
                                    name=NAME, plan_type='free'), method='PATCH')
    results['patch_only_control'] = {'disk_changed': h.disk()['refresh_token'] == old['refresh_token'],
                                     'runtime_changed': h.runtime() == 'PATCHONLY'}
    before_disk = h.disk()

    # Idle Pro -> Free: tokens, plan and runtime change; everything else is kept.
    new = tokens('FREE1', 'free')
    outcome = h.replace(new)
    row, disk = h.row(), h.disk()
    check(results, 'idle_pro_to_free', outcome['kind'] == 'signed_in'
          and all(disk[k] == new[k] for k in ('access_token', 'refresh_token', 'id_token'))
          and disk['plan_type'] == 'free' and row['id_token']['plan_type'] == 'free' and h.runtime() == 'FREE1'
          and all(row.get(k) == before_row.get(k) for k in stable)
          and all(disk.get(k) == before_disk.get(k) for k in ('priority', 'prefix', 'disabled', 'custom_unknown',
                                                                'proxy_url', 'email', 'account_id', 'type')),
          {'outcome': outcome['kind'], 'runtime': h.runtime(), 'row': {k: row.get(k) for k in stable}})

    # The next refresh uses the new refresh token.
    h.proxy.delay = 0.1
    h.refresh()
    time.sleep(0.5)
    check(results, 'next_refresh_uses_new_token', h.proxy.received[-1:] == [new['refresh_token']]
          and h.converged()[0], {'received': [r[:8] for r in h.proxy.received[-1:]]})

    # Replacement while a refresh holds the credential lock across its network call.
    results['race_waits'] = []
    for run in range(args.race_runs):
        h.proxy.delay = 1.5
        out, new = {}, tokens('RACE%d' % run, random.choice(('pro', 'free')))
        refresher = h.later(0, lambda: h.refresh(out))
        time.sleep(random.uniform(0.3, 0.7))
        started = time.time()
        outcome = h.replace(new)
        waited = time.time() - started
        results['race_waits'].append(round(waited, 2))
        refresher.join(30)
        matched, immediate = h.converged()
        disk = h.disk()
        check(results, 'replace_during_inflight_refresh', outcome['kind'] == 'signed_in' and matched
              and disk['refresh_token'] == new['refresh_token'] and h.runtime() == 'RACE%d' % run,
              {'run': run, 'outcome': outcome['kind'], 'waited': round(waited, 2), 'refresh': out,
               'disk': tag(disk['access_token']), 'runtime': h.runtime()})

    # Replacement, then the enable PATCH and a priority PATCH at once: tokens stay new.
    mgmt('/auth-files/status', {'name': NAME, 'disabled': True}, method='PATCH')
    new = tokens('ENABLE1')
    outcome = h.replace(new, enable=True)
    mgmt('/auth-files/fields', {'name': NAME, 'priority': 3}, method='PATCH')
    mgmt('/auth-files/status', {'name': NAME, 'disabled': False}, method='PATCH')
    row, disk = h.row(), h.disk()
    check(results, 'replace_then_priority_and_enable_patches', outcome == dict(outcome, kind='signed_in', enabled=True)
          and disk['refresh_token'] == new['refresh_token'] and h.runtime() == 'ENABLE1'
          and row['priority'] == 3 and row['disabled'] is False and disk['disabled'] is False,
          {'outcome': outcome, 'runtime': h.runtime(), 'priority': row.get('priority')})
    mgmt('/auth-files/fields', {'name': NAME, 'priority': 7}, method='PATCH')

    # Randomized timing against refreshes and routing PATCHes.
    kinds = {}
    for run in range(args.runs):
        mode = random.choice(('idle', 'refresh_before', 'refresh_after', 'priority', 'refresh_and_priority'))
        h.proxy.delay = random.uniform(0, 1.2)
        received = len(h.proxy.received)
        new = tokens('RND%d' % run, random.choice(('pro', 'free')))
        threads = []
        if mode in ('refresh_before', 'refresh_and_priority'):
            threads.append(h.later(0, h.refresh))
            time.sleep(random.uniform(0, 0.6))
        if mode == 'refresh_after':
            threads.append(h.later(random.uniform(0, 0.3), h.refresh))
        if mode in ('priority', 'refresh_and_priority'):
            threads.append(h.later(random.uniform(0, 0.3), lambda: mgmt(
                '/auth-files/fields', {'name': NAME, 'priority': random.randint(0, 9)}, method='PATCH')))
        outcome = h.replace(new)
        for thread in threads:
            thread.join(30)
        matched, immediate = h.converged()
        disk = h.disk()
        # The new refresh token is never lost: still on disk, or it was spent by a later refresh.
        kept = disk['refresh_token'] == new['refresh_token'] or new['refresh_token'] in h.proxy.received[received:]
        kinds[outcome['kind']] = kinds.get(outcome['kind'], 0) + 1
        if outcome['kind'] == 'saved_unconfirmed':
            # The watcher reloads disk without any dashboard follow-up or deferred enable.
            # A refresh may already have spent the newly saved refresh token.
            check(results, 'randomized_unconfirmed_watcher_runtime_equals_disk', matched and kept
                  and h.manager.outcomes[h.row()['auth_index']]['kind'] == 'saved_unconfirmed',
                  {'run': run, 'mode': mode, 'disk': tag(disk['access_token']),
                   'runtime': h.runtime(), 'new_token_kept_or_spent': kept})
            kinds.setdefault('unconfirmed_modes', []).append(mode)
        check(results, 'randomized_runtime_equals_disk', matched and kept and outcome['kind'] in ('signed_in', 'saved_unconfirmed'),
              {'run': run, 'mode': mode, 'outcome': outcome['kind'], 'disk': tag(disk['access_token']),
               'runtime': h.runtime(), 'kept': kept})
    results['randomized_outcomes'] = kinds

    # A gateway restart keeps the replaced tokens.
    new = tokens('RESTART1')
    h.replace(new)
    h.scratch.stop()
    h.scratch.start()
    disk = h.disk()
    check(results, 'restart_keeps_new_tokens', disk['refresh_token'] == new['refresh_token']
          and h.runtime() == 'RESTART1' and h.row()['auth_index'] == before_row['auth_index'],
          {'runtime': h.runtime()})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--binary', required=True, help='Exact gateway build to validate')
    parser.add_argument('--scratch', default='~/.cache/agent-scratch/personal-codex-router/replace-integration')
    parser.add_argument('--runs', type=int, default=50)
    parser.add_argument('--race-runs', type=int, default=12)
    parser.add_argument('--assume-tested', action='store_true')
    parser.add_argument('--seed', type=int, default=None)
    args = parser.parse_args()
    if not os.access(args.binary, os.X_OK) or not shutil.which('openssl'):
        print('SKIP: gateway binary or openssl unavailable')
        return 0
    for port in (PORT, PROXY_PORT):
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                print('SKIP: loopback port %d is in use' % port)
                return 0
    seed = args.seed if args.seed is not None else secrets.randbelow(1 << 30)
    random.seed(seed)
    scratch = Scratch(Path(args.scratch) / Path(args.binary).parent.name, args.binary)
    scratch.prepare()
    credential_replace.AUTH_DIR = str(scratch.auth)
    routing_policy.STATE = scratch.root / 'routing'
    proxy = TokenProxy(str(scratch.root / 'leaf.crt'), str(scratch.root / 'leaf.key'))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    results = {}
    try:
        scratch.start()
        version = mgmt('/config').version
        if args.assume_tested:
            signin.TESTED_GATEWAYS = (version,)
        results['gateway'] = {'binary': args.binary, 'version': version, 'tested': version in signin.TESTED_GATEWAYS,
                              'seed': seed}
        if version not in signin.TESTED_GATEWAYS:
            print(json.dumps(dict(results, error='untested gateway; pass --assume-tested to evaluate it')))
            return 1
        scenarios(Harness(scratch, proxy), args, results)
    finally:
        scratch.stop()
        proxy.shutdown()
        proxy.server_close()
    results['proxy_refused_hosts'] = sorted(set(proxy.refused))
    failed = sum(v.get('failed', 0) for v in results.values() if isinstance(v, dict))
    results['ok'] = failed == 0
    print(json.dumps(results, indent=2))
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
