#!/usr/bin/env python3
"""Validate reset-priority changes and affinity using an isolated gateway."""
import argparse
import importlib.util
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
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


support = load('support', 'verify-account-failover.py')
policy = load('policy', 'prefer-earliest-reset.py')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, help='Gateway executable (default: effective cliproxyapi ExecStart)')
    args = parser.parse_args(argv)
    binary = (args.binary or gateway_config.resolve_gateway_binary()).resolve()
    assert os.geteuid() == 0
    owner = pwd.getpwnam('cliproxyapi')
    before = support.snapshot()
    production = support.yaml.safe_load(support.CONFIG.read_text())
    report = {'scope': 'isolated installed gateway with copied access credentials',
              'binary': str(binary), 'tests': []}
    with tempfile.TemporaryDirectory(prefix='personal-router-reset-test-', dir='/run') as temp:
        root = Path(temp); os.chown(root, owner.pw_uid, owner.pw_gid)
        auth = root / 'auth'; auth.mkdir(mode=0o700); os.chown(auth, owner.pw_uid, owner.pw_gid)
        for source in support.AUTH.glob('*.json'):
            data = json.loads(source.read_text())
            if data.get('email') not in policy.EMAILS:
                continue
            data.update(refresh_token='', disabled=False)
            data.pop('priority', None)
            support.write_private(auth / source.name, data, owner)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        key, management = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
        config = support.isolated_config(production, port, auth, key, management)
        config_path = root / 'config.json'; support.write_private(config_path, config, owner)
        def drop():
            os.setgroups([]); os.setgid(owner.pw_gid); os.setuid(owner.pw_uid)
        def api(path, body=None, method=None):
            req = urllib.request.Request(base + '/v0/management' + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer ' + management, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as response: return json.load(response)
        with (root / 'process.log').open('wb') as log:
            process = subprocess.Popen([str(binary), '-config', str(config_path), '-local-model'],
                cwd=root, stdout=log, stderr=log, preexec_fn=drop)
            try:
                support.wait_for(lambda: len(api('/auth-files')['files']) == 2)
                ids = {r['email']: r['auth_index'] for r in api('/auth-files')['files']}
                state = root / 'policy'; state.mkdir(mode=0o700)
                a, b = policy.EMAILS
                def prefer(email):
                    now = time.time()
                    data = {'gateway': {'online': True, 'checked_at': policy.stamp(now), 'accounts': [
                        {'email': e, 'active': True, 'quota_checked_at': policy.stamp(now),
                         'windows': [{'seconds': 604800, 'remaining': 50,
                                      'resets_at': now + (86400 if e == email else 3 * 86400)}]}
                        for e in policy.EMAILS]}}
                    policy.apply_plan(policy.choose(data, now), api, state)
                def check(session, email, label):
                    support.stream(base, key, session)
                    rows = api('/usage-queue?count=100')
                    if isinstance(rows, dict):
                        rows = next(rows[k] for k in ('records','items','data','requests') if isinstance(rows.get(k),list))
                    successful = [r for r in rows if not r.get('failed')]
                    assert successful and all(r['auth_index'] == ids[email] for r in successful), label
                    report['tests'].append({'case': label, 'account': email, 'passed': True})
                    print(json.dumps(report['tests'][-1]), flush=True)
                first, second = str(uuid.uuid4()), str(uuid.uuid4())
                prefer(a); check(first, a, 'initial_preference')
                prefer(b); check(first, a, 'healthy_existing_session_kept')
                check(second, b, 'new_session_uses_earlier_reset')
                prefer(a); check(second, b, 'existing_session_kept_after_reset_rollover')
                check(str(uuid.uuid4()), a, 'new_session_follows_reset_rollover')
                policy.rollback(api, state)
                report['rollback_restored_original_priorities'] = all(r.get('priority') is None for r in api('/auth-files')['files'])
                assert report['rollback_restored_original_priorities']
            finally:
                process.terminate()
                try: process.wait(timeout=8)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
    report['production_files_unchanged'] = support.snapshot() == before
    report['temporary_credentials_removed'] = True
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
