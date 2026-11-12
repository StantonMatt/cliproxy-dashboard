#!/usr/bin/env python3
"""Rotate the private preview certificate atomically, then refresh systemd credentials."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tempfile

CONFIG = Path('/etc/personal-codex-router/previews.json')
STATE = Path('/var/lib/personal-codex-router-preview-tls')
SERVICE = 'personal-codex-router-preview.service'


def command(*args):
    # Do not print cert material or subprocess output on failures.
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


def renew():
    if os.geteuid() != 0:
        raise RuntimeError('Certificate renewal must run as root')
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / 'renew.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(CONFIG.read_text())
        hostname = config.get('tls_hostname', config['dns'])
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+\.ts\.net', hostname):
            raise RuntimeError('Preview TLS requires the full Tailscale DNS name')
        with tempfile.TemporaryDirectory(prefix='candidate-', dir=STATE) as staging:
            candidate = Path(staging)
            cert, key = candidate / 'cert.pem', candidate / 'key.pem'
            command('/usr/bin/tailscale', 'cert', '--cert-file=' + str(cert), '--key-file=' + str(key), hostname)
            cert.chmod(0o600)
            key.chmod(0o600)
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
            checked = command('/usr/bin/openssl', 'x509', '-in', str(cert), '-noout', '-checkhost', hostname)
            command('/usr/bin/openssl', 'x509', '-in', str(cert), '-noout', '-checkend', '0')
            if b'does match certificate' not in checked.stdout:
                raise RuntimeError('Certificate hostname mismatch')
            fingerprint = hashlib.sha256(cert.read_bytes() + key.read_bytes()).hexdigest()
            generation = STATE / fingerprint
            if not generation.exists():
                generation.mkdir(mode=0o700)
                shutil.copyfile(cert, generation / 'cert.pem')
                shutil.copyfile(key, generation / 'key.pem')
            link = STATE / 'current.next'
            link.unlink(missing_ok=True)
            link.symlink_to(generation.name)
            link.replace(STATE / 'current')
        # Commit the marker only after success, so a failed restart is retried next run.
        marker = STATE / 'applied'
        if not marker.exists() or marker.read_text() != fingerprint:
            command('/usr/bin/systemctl', 'try-restart', SERVICE)
            marker.write_text(fingerprint)
            print('Preview certificate installed; active preview service refreshed.')
        else:
            print('Preview certificate unchanged.')
        # Keep only the published generation; never accumulate expired private keys.
        for old in STATE.iterdir():
            if old.is_dir() and not old.is_symlink() and re.fullmatch(r'[0-9a-f]{64}', old.name) and old != generation:
                shutil.rmtree(old)


if __name__ == '__main__':
    try:
        renew()
    except Exception:
        raise SystemExit('Preview certificate renewal failed; inspect configuration and tailscaled/certificate availability.')
