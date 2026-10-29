#!/usr/bin/env python3
"""Preview by default; --apply disables the dashboard without touching other changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = Path('/etc/cliproxyapi/config.yaml')
sys.path.insert(0, str(PROJECT / 'dashboard'))
import routing_policy


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    manifest = json.loads((PROJECT / 'docs/activation.json').read_text())
    original = Path(manifest['backup']) / 'config.before.yaml'
    assert os.geteuid() == 0, 'Run with sudo.'
    config = yaml.safe_load(CONFIG.read_bytes())
    if isinstance(config, dict) and gateway_config.layout(config) == 'v8':
        raise RuntimeError('Nested v8 config cannot use dashboard rollback. Follow the manual, reviewed '
                           'procedure in docs/gateway-config-v8-migration.md; do not change activation hashes.')
    assert hashlib.sha256(CONFIG.read_bytes()).hexdigest() == manifest['enabled_sha256'], 'Config has changed since activation; merge the rollback manually.'
    before = original.read_bytes()
    assert hashlib.sha256(before).hexdigest() == manifest['before_sha256'], 'Backup does not match.'
    print('Restore the pre-dashboard config and disable usage collection. Preserve accounts, usage database, keys, panel asset, and backups.')
    print('Disable the routing preference timer and stop any active invocation.')
    print('Suspend saved routing intent until a new selection is saved in the dashboard.')
    if (routing_policy.STATE / 'baseline.json').exists():
        print('Restore recorded baseline account priorities.')
    else:
        print('No routing baseline recorded; restore neutral account priorities.')
    if args.apply:
        subprocess.run(['systemctl', 'disable', '--now', 'personal-codex-router-preference.timer'], check=True)
        subprocess.run(['systemctl', 'stop', 'personal-codex-router-preference.service'], check=True)
        subprocess.run([sys.executable, '/opt/personal-codex-router/dashboard/routing_policy.py', '--rollback'],
                       user='cliproxyapi', group='cliproxyapi', check=True)
        subprocess.run(['systemctl', 'disable', '--now', 'personal-codex-router-usage.timer'], check=True)
        subprocess.run(['systemctl', 'stop', 'personal-codex-router-usage.service'], check=True)
        # Preserve the inode watched by this gateway version.
        with CONFIG.open('r+b') as f:
            f.write(before)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
        print('Config restored; verify management routes return 404 after hot reload.')


if __name__ == '__main__':
    main()
