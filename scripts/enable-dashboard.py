#!/usr/bin/env python3
"""Stage the pinned panel and localhost management; verify runtime activation separately."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import sys
import tempfile

import bcrypt
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config
import site_config

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = Path('/etc/cliproxyapi/config.yaml')
KEY = Path('/etc/cliproxyapi/dashboard-management-key')
BACKUPS = Path('/var/lib/cliproxyapi-dashboard-backups')


def atomic_write(path, data, mode, uid, gid):
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def activation_candidate(original, hashed, key_exists):
    cfg = yaml.safe_load(original)
    assert not gateway_config.get(cfg, 'secret-key'), 'Management is already configured.'
    assert gateway_config.get(cfg, 'allow-remote') is False
    assert not key_exists, 'A management key already exists; refusing to replace it.'
    return gateway_config.validated_edit(original, {
        'secret-key': hashed, 'disable-control-panel': False,
        'disable-auto-update-panel': True, 'usage-statistics-enabled': True,
    }, allow_add=('disable-auto-update-panel',))


def main():
    assert os.geteuid() == 0, 'Run with sudo.'
    os.umask(0o077)
    user = site_config.desktop()
    proxy = pwd.getpwnam('cliproxyapi')
    original = CONFIG.read_bytes()
    # Validate guards and the edit before staging any activation files.
    activation_candidate(original, 'preflight-placeholder', KEY.exists())
    release = json.loads((PROJECT / 'references/management-release.json').read_text())
    asset = release['assets'][0]
    panel = (PROJECT / 'references/management.html').read_bytes()
    assert 'sha256:' + hashlib.sha256(panel).hexdigest() == asset['digest']
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BACKUPS / stamp
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    shutil.copy2(CONFIG, backup / 'config.before.yaml')
    (backup / 'config.before.yaml').chmod(0o600)
    key = secrets.token_urlsafe(36)
    hashed = bcrypt.hashpw(key.encode(), bcrypt.gensalt(rounds=12)).decode()
    candidate = activation_candidate(original, hashed, KEY.exists())
    static = CONFIG.parent / 'static'
    static.mkdir(mode=0o750, exist_ok=True)
    os.chown(static, 0, proxy.pw_gid)
    static.chmod(0o750)
    target = static / 'management.html'
    assert not target.exists(), 'Existing panel needs an explicit update instead.'
    atomic_write(target, panel, 0o640, 0, proxy.pw_gid)
    atomic_write(KEY, (key + '\n').encode(), 0o640, 0, proxy.pw_gid)
    userdir = Path(user.pw_dir) / '.config/personal-codex-router'
    userdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(userdir, user.pw_uid, user.pw_gid)
    userdir.chmod(0o700)
    atomic_write(userdir / 'management-key', (key + '\n').encode(), 0o600, user.pw_uid, user.pw_gid)
    assert CONFIG.read_bytes() == original, 'Config changed during preparation.'
    gateway_config.write_same_inode(CONFIG, candidate, original)
    manifest = {
        'enabled_at': stamp, 'backup': str(backup),
        'before_sha256': hashlib.sha256(original).hexdigest(),
        'enabled_sha256': hashlib.sha256(candidate).hexdigest(),
        'panel_version': release['tag_name'], 'panel_digest': asset['digest'],
        'management_remote': False, 'usage_statistics': True,
    }
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    public = PROJECT / 'docs/activation.json'
    atomic_write(public, (json.dumps(manifest, indent=2) + '\n').encode(), 0o644, user.pw_uid, user.pw_gid)
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
