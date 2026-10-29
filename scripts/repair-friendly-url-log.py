#!/usr/bin/env python3
"""Repair only the helper's stderr path so its unprivileged process can start."""
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import subprocess

assert os.geteuid() == 0, 'Run with sudo.'
p = Path('/Library/LaunchDaemons/com.user.personal-router-url.plist')
with p.open('rb') as f:
    config = plistlib.load(f)
assert config['Label'] == 'com.user.personal-router-url'
assert config['Sockets']['Listener']['SockNodeName'] == '127.0.0.1'
assert config['Sockets']['Listener']['SockServiceName'] == '80'
user = pwd.getpwnam(config['UserName'])
assert user.pw_uid >= 500
backup = Path('/Library/Application Support/PersonalCodexRouter/launchdaemon-before-log-fix.plist')
if not backup.exists():
    shutil.copyfile(p, backup)
    backup.chmod(0o600)
log = Path(user.pw_dir) / 'Library/Logs/PersonalCodexRouter/url-helper.log'
assert log.parent.is_dir()
config['StandardErrorPath'] = str(log)
subprocess.run(['/bin/launchctl', 'bootout', 'system/com.user.personal-router-url'], check=True)
with p.open('wb') as f:
    plistlib.dump(config, f)
p.chmod(0o644)
subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(p)], check=True)
print('Address helper log path repaired; loopback listener and user unchanged.')
