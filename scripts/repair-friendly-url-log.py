#!/usr/bin/env python3
"""Repair only the helper's stderr path so its unprivileged process can start."""
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import subprocess

DAEMONS = Path('/Library/LaunchDaemons')
SUPPORT = Path('/Library/Application Support/PersonalCodexRouter')


def main():
    assert os.geteuid() == 0, 'Run with sudo.'
    candidates = list(DAEMONS.glob('*.personal-router-url.plist'))
    current = DAEMONS / 'org.personal-codex-router.url-helper.plist'
    if current.exists():
        candidates.append(current)
    assert len(candidates) == 1, 'Expected one router URL helper.'
    p = candidates[0]
    with p.open('rb') as f:
        config = plistlib.load(f)
    assert config['Label'] == p.stem
    assert config['Sockets']['Listener']['SockNodeName'] == '127.0.0.1'
    assert config['Sockets']['Listener']['SockServiceName'] == '80'
    user = pwd.getpwnam(config['UserName'])
    assert user.pw_uid >= 500
    backup = SUPPORT / 'launchdaemon-before-log-fix.plist'
    if not backup.exists():
        shutil.copyfile(p, backup)
        backup.chmod(0o600)
    log = Path(user.pw_dir) / 'Library/Logs/PersonalCodexRouter/url-helper.log'
    assert log.parent.is_dir()
    config['StandardErrorPath'] = str(log)
    subprocess.run(['/bin/launchctl', 'bootout', 'system/' + config['Label']], check=True)
    with p.open('wb') as f:
        plistlib.dump(config, f)
    p.chmod(0o644)
    subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(p)], check=True)
    print('Address helper log path repaired; loopback listener and user unchanged.')


if __name__ == '__main__':
    main()
