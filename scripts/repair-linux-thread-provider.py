#!/usr/bin/env python3
"""Repair a stored provider through Codex's API while the task is closed."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import time
import tomllib

BINARY = '/usr/lib/chatgpt/resources/codex'
THREAD = '00000000-0000-4000-8000-000000000002'


class AppServer:
    def __init__(self, home, diagnostics):
        self.log = open(diagnostics, 'ab')
        os.chmod(diagnostics, 0o600)
        env = os.environ.copy()
        env['CODEX_HOME'] = str(home)
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL'):
            env.pop(key, None)
        self.process = subprocess.Popen(
            [BINARY, 'app-server', '--listen', 'stdio://'], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            bufsize=0)
        self.buffer = b''
        self.next_id = 0
        try:
            self.call('initialize', {'clientInfo': {'name': 'personal_router_provider_repair',
                      'version': '1.0'}, 'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + '\n').encode())
        self.process.stdin.flush()

    def receive(self, deadline):
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError('Codex did not answer in time')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Codex server exited before responding')
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def call(self, method, params=None, timeout=90):
        self.next_id += 1
        rid = self.next_id
        self.send({'id': rid, 'method': method, **({'params': params} if params is not None else {})})
        deadline = time.monotonic() + timeout
        while True:
            value = self.receive(deadline)
            if value.get('id') == rid:
                if 'error' in value:
                    # Avoid echoing provider configuration or credentials from errors.
                    self.log.write((json.dumps(value) + '\n').encode())
                    self.log.flush()
                    code = value['error'].get('code')
                    raise RuntimeError(f'{method} failed (code {code}); private diagnostics retained')
                return value.get('result')

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.process.stdout.close()
        self.log.close()


def record(home, thread):
    with sqlite3.connect(f'file:{home / "state_5.sqlite"}?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute('SELECT * FROM threads WHERE id=?', (thread,)).fetchone()
        if row is None:
            raise RuntimeError('Task not found in this Codex home')
        return dict(row)


def history_fingerprint(path, limit=None):
    digest, count = hashlib.sha256(), 0
    with open(path, 'rb') as stream:
        for line in stream:
            item = json.loads(line)
            if item.get('type') == 'session_meta':
                continue
            if limit is not None and count == limit:
                break
            digest.update(line)
            count += 1
    return count, digest.hexdigest()


def migrate(home, thread, provider):
    config = tomllib.loads((home / 'config.toml').read_text())
    if provider != 'openai':
        definition = config.get('model_providers', {}).get(provider, {})
        if not definition.get('http_headers', {}).get('X-Api-Key'):
            raise RuntimeError('The target provider has no independent gateway key')
    lock = home / 'thread-writer-locks' / (thread + '.lock')
    if lock.exists():
        with open(lock, 'rb') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Fully quit Codex on the Linux desktop, then run this command again')
            fcntl.flock(handle, fcntl.LOCK_UN)
    before = record(home, thread)
    if before['model_provider'] == provider:
        print('This task already has the requested provider. No changes made.')
        return None
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = home / 'gateway-integration' / ('stored-provider-' + stamp + '-' + thread[:8])
    backup.mkdir(parents=True, mode=0o700)
    (backup / 'thread.before.json').write_text(json.dumps(before, indent=2))
    (backup / 'thread.before.json').chmod(0o600)
    rollout = Path(before['rollout_path'])
    shutil.copy2(rollout, backup / 'rollout.before.jsonl')
    (backup / 'rollout.before.jsonl').chmod(0o600)
    fingerprint = history_fingerprint(rollout)
    resume_params = {'threadId': thread, 'excludeTurns': True}
    if before.get('model'):
        resume_params['model'] = before['model']
    if before.get('reasoning_effort'):
        resume_params['config'] = {'model_reasoning_effort': before['reasoning_effort']}
    if before.get('approval_mode'):
        resume_params['approvalPolicy'] = before['approval_mode']
    last_context = {}
    with rollout.open() as stream:
        for line in stream:
            entry = json.loads(line)
            if entry.get('type') == 'turn_context':
                last_context = entry['payload']
    profile = last_context.get('active_permission_profile') or {}
    if profile.get('id'):
        resume_params['permissions'] = profile['id']
    elif last_context.get('sandbox_policy', {}).get('type') in (
            'read-only', 'workspace-write', 'danger-full-access'):
        resume_params['sandbox'] = last_context['sandbox_policy']['type']
    if last_context.get('approvals_reviewer'):
        resume_params['approvalsReviewer'] = last_context['approvals_reviewer']
    first = AppServer(home, backup / 'repair.stderr')
    try:
        first.call('thread/resume', {**resume_params, 'modelProvider': provider})
    finally:
        first.close()
    second = AppServer(home, backup / 'verify.stderr')
    try:
        result = second.call('thread/resume', resume_params)
        actual = result.get('thread', {}).get('modelProvider') or result.get('modelProvider')
        if actual != provider:
            raise RuntimeError('A fresh resume did not retain the requested provider')
    finally:
        second.close()
    after = record(home, thread)
    if after['model_provider'] != provider:
        raise RuntimeError('The stored provider did not match after verification')
    if history_fingerprint(rollout, fingerprint[0]) != fingerprint:
        raise RuntimeError('History verification failed; preserve the backup and inspect before reopening')
    for key in ('id', 'name', 'title', 'cwd', 'model', 'reasoning_effort', 'archived',
                'is_pinned', 'sandbox_policy', 'approval_mode'):
        if before.get(key) != after.get(key):
            raise RuntimeError(f'Unexpected metadata change: {key}; inspect before reopening')
    evidence = {'thread': thread, 'previous_provider': before['model_provider'],
                'provider': provider, 'history_preserved': True, 'fresh_resume_verified': True,
                'user_turns_submitted': False, 'backup': str(backup)}
    (backup / 'result.json').write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence, indent=2))
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thread', default=THREAD)
    parser.add_argument('--provider', default='personal_codex_gateway')
    args = parser.parse_args()
    try:
        migrate(Path.home() / '.codex', args.thread, args.provider)
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        raise SystemExit(str(error))
    print('DONE. It is safe to reopen Codex. The repair did not change your sign-in.')


if __name__ == '__main__':
    main()
