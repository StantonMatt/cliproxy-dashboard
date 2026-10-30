#!/usr/bin/env python3
"""One-time Mac admin installation for http://router.localhost, loopback only."""
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import socket
import subprocess


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this installer with sudo in Terminal.')
    user = os.environ.get('SUDO_USER')
    if not user or pwd.getpwnam(user).pw_uid < 500:
        raise SystemExit('Run with sudo from your normal Mac account.')
    directory = Path('/Library/Application Support/PersonalCodexRouter')
    plist = Path('/Library/LaunchDaemons/org.personal-codex-router.url-helper.plist')
    if directory.exists() or plist.exists():
        raise SystemExit('The address helper already exists. Refusing to overwrite it.')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 80))
    directory.mkdir(mode=0o755)
    os.chown(directory, 0, 0)
    script = directory / 'loopback-forwarder.py'
    shutil.copyfile(Path(__file__).with_name('loopback-forwarder.py'), script)
    script.chmod(0o644);os.chown(script, 0, 0)
    config = {
        'Label': 'org.personal-codex-router.url-helper',
        'ProgramArguments': ['/usr/bin/python3', str(script)],
        'UserName': user,
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 15,
        'Sockets': {'Listener': {'SockServiceName': '80', 'SockNodeName': '127.0.0.1',
                                 'SockFamily': 'IPv4', 'SockType': 'stream'}},
        'StandardErrorPath': str(Path(pwd.getpwnam(user).pw_dir) / 'Library/Logs/PersonalCodexRouter/url-helper.log'),
    }
    with plist.open('xb') as f:
        plistlib.dump(config, f)
    plist.chmod(0o644);os.chown(plist, 0, 0)
    subprocess.run(['/usr/bin/plutil', '-lint', str(plist)], check=True)
    subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(plist)], check=True)
    print('Installed. Open http://router.localhost in Chrome.')
    print('The listener is loopback-only, and its process runs as ' + user + '.')


if __name__ == '__main__':
    main()
