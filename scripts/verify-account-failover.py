#!/usr/bin/env python3
"""Exercise the installed gateway in an isolated, short-lived loopback instance.

Copy access credentials into a private temporary directory, disable refresh in
those copies, bind a session, then invalidate only its copied access token.
The real gateway and its files are never changed. Report only safe metadata.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
from routing_policy import EMAILS
CONFIG = Path('/etc/cliproxyapi/config.yaml')
AUTH = Path('/var/lib/cliproxyapi/auth')
MARKER = 'ROUTER_FAILOVER_OK'


def snapshot():
    paths = [CONFIG, *sorted(AUTH.glob('*.json'))]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def request_json(base, path, key):
    req = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def wait_for(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(.3)
    raise RuntimeError('Timed out waiting for isolated test instance')


def write_private(path, data, owner):
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    os.chown(path, owner.pw_uid, owner.pw_gid)


def stream(base, key, session):
    body = {'model': 'gpt-6-astra', 'stream': True, 'store': False,
            'instructions': 'Reply with the exact requested marker only.',
            'input': [{'role': 'user', 'content': [{'type': 'input_text',
                       'text': 'Reply exactly: ' + MARKER}]}],
            'reasoning': {'effort': 'low'}, 'prompt_cache_key': session}
    req = urllib.request.Request(base + '/v1/responses', data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + key,
                                          'Content-Type': 'application/json',
                                          'session_id': session})
    output, completed = '', False
    with urllib.request.urlopen(req, timeout=50) as response:
        for line in response:
            if not line.startswith(b'data: '):
                continue
            text = line[6:].strip()
            if text == b'[DONE]':
                continue
            event = json.loads(text)
            if event.get('type') == 'response.output_text.delta':
                output += event.get('delta', '')
            if event.get('type') == 'response.completed':
                completed = True
            if event.get('type') in ('error', 'response.failed'):
                raise RuntimeError('Isolated test stream reported failure')
    if not completed or output.strip() != MARKER:
        raise RuntimeError('Isolated test response did not match marker')


def isolated_config(production, port, auth_dir, api_key, management_key):
    config = gateway_config.isolated_policy(production)
    config.update({'host': '127.0.0.1', 'port': port, 'auth-dir': str(auth_dir),
        'api-keys': [api_key], 'usage-statistics-enabled': True,
        'debug': False, 'logging-to-file': False, 'request-log': False,
        'discovery': {'enabled': False}, 'pprof': {'enable': False},
        'remote-management': {'allow-remote': False, 'secret-key': management_key,
            'disable-control-panel': True, 'disable-auto-update-panel': True}})
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, help='Gateway executable (default: effective cliproxyapi ExecStart)')
    args = parser.parse_args(argv)
    if len(EMAILS) != 2:
        print('Skipped: Codex account failover requires exactly two configured accounts; '
              f'{len(EMAILS)} configured. No credentials read or gateway started.')
        return 0
    binary = (args.binary or gateway_config.resolve_gateway_binary()).resolve()
    assert os.geteuid() == 0, 'Run through sudo on Linux.'
    owner = pwd.getpwnam('cliproxyapi')
    before = snapshot()
    production = yaml.safe_load(CONFIG.read_text())
    normalized = gateway_config.isolated_policy(production)
    originals = {}
    for path in AUTH.glob('*.json'):
        value = json.loads(path.read_text())
        if value.get('type') == 'codex' and value.get('email') in EMAILS:
            if value['email'] in originals:
                raise RuntimeError('Configured Codex credentials are ambiguous')
            originals[value['email']] = value
    assert set(originals) == set(EMAILS)
    report = {'tested_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'binary': str(binary), 'scope': 'isolated instance, copied OAuth access tokens',
              'fault': 'HTTP 401 from deliberately invalid copied access token',
              'production_retry_policy': {k: normalized.get(k) for k in gateway_config.RETRY},
              'routing': normalized.get('routing'), 'tests': []}
    try:
        for first in EMAILS:
            second = next(x for x in EMAILS if x != first)
            with tempfile.TemporaryDirectory(prefix='personal-router-failover-', dir='/run') as temp:
                root = Path(temp)
                os.chown(root, owner.pw_uid, owner.pw_gid)
                auth_dir = root / 'auth'
                auth_dir.mkdir(mode=0o700)
                os.chown(auth_dir, owner.pw_uid, owner.pw_gid)
                copies = {}
                for email, original in originals.items():
                    data = dict(original)
                    data.update(refresh_token='', disabled=False,
                                priority=100 if email == first else 0)
                    path = auth_dir / (('first' if email == first else 'second') + '.json')
                    write_private(path, data, owner)
                    copies[email] = (path, data)
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                base = f'http://127.0.0.1:{port}'
                api_key, management_key = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
                config = isolated_config(production, port, auth_dir, api_key, management_key)
                config_path = root / 'config.json'
                write_private(config_path, config, owner)

                def drop_privileges():
                    os.setgroups([])
                    os.setgid(owner.pw_gid)
                    os.setuid(owner.pw_uid)

                with (root / 'process.log').open('wb') as log:
                    process = subprocess.Popen([str(binary), '-config', str(config_path), '-local-model'],
                                               cwd=root, stdout=log, stderr=log,
                                               preexec_fn=drop_privileges)
                    try:
                        files = lambda: request_json(base, '/v0/management/auth-files', management_key)['files']
                        entries = wait_for(lambda: files() if len(files()) == 2 else None)
                        ids = {a['email']: a['auth_index'] for a in entries}
                        queue = lambda: request_json(base, '/v0/management/usage-queue?count=100', management_key)
                        session = str(uuid.uuid4())
                        # Establish affinity to the deliberately higher-priority first account.
                        stream(base, api_key, session)
                        baseline = queue()
                        # Change only the test copy, retaining the established session identity.
                        path, bad = copies[first]
                        bad['access_token'] = 'invalid-isolated-failover-test-' + secrets.token_hex(8)
                        write_private(path, bad, owner)
                        time.sleep(2)
                        stream(base, api_key, session)
                        after_failure = queue()
                        state = files()
                        stream(base, api_key, session)
                        followup = queue()
                        # Queue shape and auth error state are inspected without credential values.
                        def safe_queue(data):
                            if isinstance(data, dict):
                                for key in ('records', 'items', 'data', 'requests'):
                                    if isinstance(data.get(key), list):
                                        data = data[key]
                                        break
                            if not isinstance(data, list):
                                raise RuntimeError('Unknown isolated usage queue shape')
                            return [{k: row.get(k) for k in ('auth_index', 'model', 'failed')} for row in data]
                        baseline, after_failure, followup = map(safe_queue, (baseline, after_failure, followup))
                        assert any(r['auth_index'] == ids[first] and not r['failed'] for r in baseline), 'Initial account not proven'
                        assert any(r['auth_index'] == ids[first] and r['failed'] for r in after_failure), 'Failed account attempt not proven'
                        assert any(r['auth_index'] == ids[second] and not r['failed'] for r in after_failure), 'Automatic alternate attempt not proven'
                        assert any(r['auth_index'] == ids[second] and not r['failed'] for r in followup), 'Affinity rebinding not proven'
                        assert not any(r['auth_index'] == ids[first] for r in followup), 'Followup retried failed account'
                        item = {'from': first, 'to': second, 'baseline_stream': True,
                                'failed_attempt_observed': True, 'same_request_stream_recovered': True,
                                'followup_stayed_on_alternate': True, 'model': 'gpt-6-astra',
                                'auth_state': [{k: a.get(k) for k in ('email', 'status', 'unavailable')} for a in state]}
                        report['tests'].append(item)
                        print(json.dumps({'direction_passed': first + ' -> ' + second}), flush=True)
                    finally:
                        process.terminate()
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
    finally:
        report['production_files_unchanged'] = snapshot() == before
        report['temporary_instances_stopped_and_credentials_removed'] = True
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
