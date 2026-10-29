#!/usr/bin/env python3
"""Prove the dashboard sandbox shares the credential flock and can write only its auth dir.

Runs dashboard/credential_replace.py as cliproxyapi under the dashboard unit's systemd
properties, with ReadWritePaths pointed at a SCRATCH auth dir, while a process outside the
sandbox holds <file>.lock until the probe has seen it blocked. Uses only scratch dirs under
/var/lib/pcr-sandbox-* (ProtectHome and PrivateTmp hide /home and /tmp from the sandbox) and
removes them afterwards. Never touches /etc/cliproxyapi or /var/lib/cliproxyapi. Needs
passwordless sudo; prints SKIP otherwise.

    python3 tests/sandbox_lock_check.py
"""
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / 'scripts/personal-codex-router-dashboard.service'
NAME = 'codex-0a1b2c3d-sandbox@example.invalid-pro.json'
# The dashboard unit's interpreter, not this caller's (possibly a home venv the sandbox hides).
PYTHON = '/usr/bin/python3'
AUTH_DIR = '/var/lib/cliproxyapi/auth'
RELEASE = 'probe-blocked'
# Outside the sandbox: hold the gateway's sidecar lock, as an in-flight refresh would, until the
# probe reports it was blocked; then hold one more second so the second wait is measurable.
HOLD = ('import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); '
        'fcntl.flock(fd,fcntl.LOCK_EX); print("locked",flush=True); end=time.monotonic()+90\n'
        'while not os.path.exists(sys.argv[2]) and time.monotonic()<end: time.sleep(0.05)\n'
        'time.sleep(1)')
# Properties copied from the dashboard unit; ReadWritePaths is replaced with the scratch dir.
KEEP = ('NoNewPrivileges', 'PrivateTmp', 'ProtectSystem', 'ProtectHome', 'ProtectKernelTunables',
        'ProtectKernelModules', 'ProtectControlGroups', 'RestrictSUIDSGID', 'LockPersonality',
        'CapabilityBoundingSet', 'RestrictAddressFamilies', 'IPAddressDeny', 'IPAddressAllow', 'UMask')

PROBE = '''
import errno, json, os, time
auth, other, name = PROBE_AUTH, PROBE_OTHER, PROBE_NAME
overlay = {k: 'SANDBOX_' + k for k in TOKEN_FIELDS}
result = {}
started = time.monotonic()
try:
    replace(name, os.path.join(auth, name), overlay, lambda current: None, auth_dir=auth, wait=1.0)
    result['blocked_while_held'] = False
except ReplaceError as error:
    result['blocked_while_held'] = error.code == 'lock_timeout'
result['first_wait'] = round(time.monotonic() - started, 2)
open(os.path.join(auth, PROBE_RELEASE), 'w').close()
started = time.monotonic()
replace(name, os.path.join(auth, name), overlay, lambda current: None, auth_dir=auth, wait=30)
result['second_wait'] = round(time.monotonic() - started, 2)
saved = json.load(open(os.path.join(auth, name)))
result['written'] = all(saved[k] == overlay[k] for k in TOKEN_FIELDS) and saved['priority'] == 7
result['mode'] = oct(os.stat(os.path.join(auth, name)).st_mode & 0o777)
for label, path in (('other_scratch_dir', os.path.join(other, 'probe')), ('etc', '/etc/pcr-sandbox-probe'),
                    ('opt', '/opt/pcr-sandbox-probe')):
    try:
        open(path, 'w').close()
        result[label] = 'WRITABLE'
        os.unlink(path)
    except OSError as error:
        result[label] = errno.errorcode.get(error.errno, str(error.errno))
# The runtime echo listens on 127.0.0.1 inside the sandbox (IPAddressAllow=localhost).
import socket
with socket.create_server(('127.0.0.1', 0)) as listener, socket.create_connection(listener.getsockname(), 2) as client:
    peer, _ = listener.accept()
    client.sendall(b'ping')
    result['loopback_listener'] = peer.recv(4) == b'ping'
    peer.close()
print(json.dumps(result))
'''


def sudo(*args, **kwargs):
    return subprocess.run(['sudo', '-n', *args], check=True, capture_output=True, text=True, timeout=120, **kwargs)


def main():
    try:
        sudo('true')
        sudo('id', 'cliproxyapi')
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print('SKIP: passwordless sudo or the cliproxyapi user is unavailable')
        return 0
    tag = secrets.token_hex(4)
    auth, other = '/var/lib/pcr-sandbox-auth-' + tag, '/var/lib/pcr-sandbox-other-' + tag
    properties, granted = [], []
    for line in UNIT.read_text().splitlines():
        key, _, value = line.partition('=')
        if key in KEEP:
            properties += ['-p', '%s=%s' % (key, value)]
        if key == 'ReadWritePaths':
            # A leading "-" makes the path optional; it still grants write access when present.
            granted += [path.removeprefix('-') for path in value.split()]
    # The repo unit must grant the real auth dir; the sandbox substitutes the scratch one.
    unit_grants_auth = AUTH_DIR in granted
    properties += ['-p', 'ReadWritePaths=' + auth]
    holder = None
    try:
        for path in (auth, other):
            sudo('install', '-d', '-m', '0700', '-o', 'cliproxyapi', '-g', 'cliproxyapi', path)
        credential = json.dumps({'type': 'codex', 'email': 'sandbox@example.invalid', 'account_id': 'acct-sandbox',
                                 'access_token': 'A', 'refresh_token': 'R', 'id_token': 'I', 'expired': 'E',
                                 'last_refresh': 'L', 'plan_type': 'pro', 'priority': 7})
        sudo('-u', 'cliproxyapi', 'sh', '-c', 'umask 077; cat > "$0"', auth + '/' + NAME, input=credential)
        holder = subprocess.Popen(['sudo', '-n', '-u', 'cliproxyapi', PYTHON, '-I', '-c', HOLD,
                                   auth + '/' + NAME + '.lock', auth + '/' + RELEASE],
                                  stdout=subprocess.PIPE, text=True, start_new_session=True)
        if holder.stdout.readline().strip() != 'locked':
            raise RuntimeError('Lock holder did not start')
        source = ((ROOT / 'dashboard/credential_replace.py').read_text()
                  + '\nPROBE_AUTH, PROBE_OTHER, PROBE_NAME, PROBE_RELEASE = %r, %r, %r, %r\n'
                  % (auth, other, NAME, RELEASE) + PROBE)
        try:
            run = sudo('systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--uid=cliproxyapi', '--gid=cliproxyapi',
                       *properties, PYTHON, '-I', '-', input=source)
        except subprocess.CalledProcessError as error:
            print(error.stderr[-2000:], file=sys.stderr)
            raise
        holder.wait(10)
        result = json.loads(run.stdout.strip().splitlines()[-1])
        result['properties'] = [p for p in properties if p != '-p']
        result['unit_grants_auth_dir'] = unit_grants_auth
        ok = (unit_grants_auth and result['blocked_while_held'] and result['second_wait'] > 0.5 and result['written']
              and result['mode'] == '0o600' and result['other_scratch_dir'] == 'EROFS'
              and result['etc'] in ('EROFS', 'EACCES') and result['opt'] in ('EROFS', 'EACCES')
              and result['loopback_listener'])
        result['ok'] = ok
        print(json.dumps(result, indent=2))
        return 0 if ok else 1
    finally:
        if holder:
            if holder.poll() is None:
                # sudo may run the holder in its own session (use_pty), so also match its unique lock path.
                subprocess.run(['sudo', '-n', 'kill', '-KILL', '--', '-%d' % holder.pid], timeout=30, capture_output=True)
                subprocess.run(['sudo', '-n', 'pkill', '-KILL', '-u', 'cliproxyapi', '-f', auth + '/'],
                               timeout=30, capture_output=True)
                holder.wait(10)
            holder.stdout.close()
        for path in (auth, other):
            subprocess.run(['sudo', '-n', 'rm', '-rf', '--', path], timeout=30)


if __name__ == '__main__':
    sys.exit(main())
