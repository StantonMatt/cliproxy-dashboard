#!/usr/bin/env python3
"""Install the reviewed local operations files, preserving running task services."""
import argparse
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import routing_policy
DEST = Path('/opt/personal-codex-router')
CONFIG = Path('/etc/personal-codex-router/previews.json')
OPS = Path('/var/lib/personal-codex-router-ops')
ROUTING = Path('/var/lib/personal-codex-router-routing')
WATCH = Path('/var/lib/personal-codex-router-watch')
WATCH_USER = 'user'
USER_UNIT_DIR = Path('/home/user/.config/systemd/user')
USER_UNITS = ('claude-usage.service', 'claude-usage.timer', 'claude-redeemer.service',
              'claude-keepalive.service', 'claude-keepalive.timer',
              'claude-b-usage.service', 'claude-b-usage.timer')
CLAUDE_STATE = Path('/home/user/.local/state/personal-codex-router')
GUARD = Path('/etc/tailscale/host-guard.nft')
# Conversation moves need the helper, its gateway_config module, both move units
# and the dashboard unit's request RuntimeDirectory; only a full install ships them all.
MOVE_FILES = ('move-conversations.py', 'gateway_config.py', 'personal-codex-router-move.path',
              'personal-codex-router-move.service')
MOVE_REQUESTS = Path('/run/personal-codex-router-move-request')
UNITS = ('backup.service', 'backup.timer', 'dashboard.service', 'move.path', 'move.service', 'ops.service', 'ops.timer',
         'preference.service', 'preference.timer', 'preview.service', 'url.service',
         'url.socket', 'usage.service', 'usage.timer')
SCRIPTS = ('collect-usage.py', 'collect-claude-usage.py', 'claude-redeemer.py', 'claude-token-keepalive.py', 'gateway_config.py', 'move-conversations.py', 'prefer-earliest-reset.py', 'router-backup.py',
           'router-ops.py', 'router-preview.py', 'router-preview.mjs')
CACHES = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
PHONE = '100.64.0.12'
MAC = '100.64.0.11'
T3_USAGE_FILES = ('server.py', 't3_usage.py')
T3_USAGE_BACKUPS = Path('/var/lib/personal-codex-router-backups/t3-usage-deployments')
ACCOUNT_ACCESS_FILES = ('t3_usage.py', 'account_access.py', 'signin.py', 'credential_replace.py', 'request_deadline.py', 'server.py',
                        'availability.py', 'operations.py', 'routing_policy.py', 'static/index.html', 'static/app.js',
                        'static/billing.js', 'static/style.css')


def install_files(claude_watch_only=False, account_access_only=False, t3_usage_only=False):
    if t3_usage_only:
        return [(ROOT / 'dashboard' / name, DEST / 'dashboard' / name, 0o644)
                for name in T3_USAGE_FILES]
    if account_access_only:
        return [(ROOT / 'dashboard' / name, DEST / 'dashboard' / name, 0o644)
                for name in ACCOUNT_ACCESS_FILES] + [
                    (ROOT / 'scripts/router-backup.py', DEST / 'router-backup.py', 0o755),
                    (ROOT / 'scripts/router-preview.mjs', DEST / 'router-preview.mjs', 0o755),
                    (ROOT / 'scripts/personal-codex-router-preference.service',
                     Path('/etc/systemd/system/personal-codex-router-preference.service'), 0o644),
                    # Sign-in writes Codex credentials under the gateway's lock (ReadWritePaths).
                    (ROOT / 'scripts/personal-codex-router-dashboard.service',
                     Path('/etc/systemd/system/personal-codex-router-dashboard.service'), 0o644)]
    files = [(ROOT / 'scripts' / name, DEST / name, 0o755) for name in SCRIPTS]
    dashboard = ROOT / 'dashboard'
    for source in sorted(dashboard.rglob('*')):
        relative = source.relative_to(dashboard)
        if source.is_file() and not CACHES.intersection(relative.parts) and source.suffix not in ('.pyc', '.pyo'):
            files.append((source, DEST / 'dashboard' / relative, 0o644))
    for suffix in UNITS:
        name = 'personal-codex-router-' + suffix
        files.append((ROOT / 'scripts' / name, Path('/etc/systemd/system') / name, 0o644))
    for suffix in USER_UNITS:
        name = 'personal-codex-router-' + suffix
        files.append((ROOT / 'scripts' / name, USER_UNIT_DIR / name, 0o644))
    files.append((ROOT / 'scripts/router-preview.py', Path('/usr/local/bin/router-preview'), 0o755))
    if claude_watch_only:
        files = [(source, dest, mode) for source, dest, mode in files
                 if dest.is_relative_to(DEST / 'dashboard') or dest in (
                     DEST / 'collect-claude-usage.py', DEST / 'claude-token-keepalive.py', DEST / 'router-preview.mjs',
                     DEST / 'router-backup.py',
                     Path('/etc/systemd/system/personal-codex-router-preference.service'),
                     # The dashboard code it ships writes Codex credentials (ReadWritePaths).
                     Path('/etc/systemd/system/personal-codex-router-dashboard.service')) or
                 dest.name in ('personal-codex-router-claude-b-usage.service',
                               'personal-codex-router-claude-b-usage.timer')]
    return files


def backup_paths(files, claude_watch_only=False, account_access_only=False, t3_usage_only=False):
    if account_access_only or t3_usage_only:
        return [dest for _, dest, _ in files]
    if claude_watch_only:
        return [WATCH] + [dest for _, dest, _ in files]
    return [ROUTING, OPS, WATCH, CONFIG.parent, GUARD] + [dest for _, dest, _ in files]


def pre_apply_backup(backup, files, claude_watch_only=False, account_access_only=False, t3_usage_only=False):
    original_sources = backup.sources

    def sources():
        paths = [] if claude_watch_only or account_access_only or t3_usage_only else original_sources()
        for path in backup_paths(files, claude_watch_only, account_access_only, t3_usage_only):
            if path.is_symlink():
                raise RuntimeError('Deployment backup source is symlinked; inspect before applying')
            if path.exists():
                paths.append(path)
        return paths

    # Reuse the verified snapshot format and include installer-specific state.
    backup.sources = sources
    try:
        backup.run()
    finally:
        backup.sources = original_sources


def command(*args):
    return subprocess.run(args, capture_output=True, check=True, timeout=30).stdout.decode().strip()


def version_fingerprint():
    return hashlib.sha256(Path('/home/user/.codex/app-server-daemon/app-server.pid').read_bytes()).hexdigest()


def overview():
    request = Request('http://127.0.0.1:8320/api/overview', headers={'X-Router-Request': '1'})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def install_file(source, dest, mode=0o644, uid=0, gid=0):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.installing')
    shutil.copyfile(source, tmp)
    tmp.chmod(mode)
    os.chown(tmp, uid, gid)
    tmp.replace(dest)


def prepare_watch_paths(user):
    WATCH.mkdir(mode=0o2750, exist_ok=True)
    os.chown(WATCH, user.pw_uid, grp.getgrnam('cliproxyapi').gr_gid)
    WATCH.chmod(0o2750)
    # Own any newly created ancestors as well as the unit directory itself.
    missing = [path for path in (USER_UNIT_DIR.parent.parent, USER_UNIT_DIR.parent, USER_UNIT_DIR)
               if not path.exists()]
    state_missing = [path for path in (CLAUDE_STATE.parent.parent, CLAUDE_STATE.parent) if not path.exists()]
    CLAUDE_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    CLAUDE_STATE.chmod(0o700)
    for path in state_missing + [CLAUDE_STATE]:
        os.chown(path, user.pw_uid, user.pw_gid)
    USER_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    for path in missing + [USER_UNIT_DIR]:
        os.chown(path, user.pw_uid, user.pw_gid)


def user_command(user, *args):
    return command('/usr/sbin/runuser', '-u', WATCH_USER, '--', '/usr/bin/env',
                   f'XDG_RUNTIME_DIR=/run/user/{user.pw_uid}',
                   f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{user.pw_uid}/bus',
                   '/usr/bin/systemctl', '--user', *args)


def check_move_ready():
    state = command('systemctl', 'show', '-p', 'ActiveState', '--value', 'personal-codex-router-move.path')
    if state != 'active':
        raise RuntimeError('personal-codex-router-move.path is not active; conversation moves are unavailable')
    if not MOVE_REQUESTS.is_dir():
        raise RuntimeError(f'{MOVE_REQUESTS} is missing; conversation moves are unavailable')


SCOPED_MOVE_NOTE = ("This install doesn't install or update the conversation-move helper or its units; "
                    'moves use what the last full install set up, so run a full install whenever they change.')


def install_t3_usage(files):
    """Separate scope: no overview, credentials, watch setup, units or firewall."""
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo')
    before_gateway = command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi')
    before_codex = version_fingerprint()
    T3_USAGE_BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    T3_USAGE_BACKUPS.chmod(0o700)
    spec = importlib.util.spec_from_file_location('t3_usage_backup', ROOT / 'scripts/router-backup.py')
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    backup.BACKUPS = T3_USAGE_BACKUPS
    backup.STATUS = T3_USAGE_BACKUPS / 'status.json'
    pre_apply_backup(backup, files, t3_usage_only=True)
    saved = json.loads(backup.STATUS.read_text())['snapshot']
    for source, dest, mode in files:
        install_file(source, dest, mode)
    command('systemctl', 'restart', 'personal-codex-router-dashboard.service')
    if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
        raise RuntimeError('A protected process changed during deployment; investigate')
    print(json.dumps({'deployed': True, 'scope': 't3-usage', 'pre_deploy_snapshot': saved,
                      'gateway_pid': before_gateway, 'codex_process_unchanged': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--claude-watch-only', action='store_true',
                        help='Install watch collector/dashboard/B units; back up only replaced files and watch snapshots')
    scope.add_argument('--account-access-only', action='store_true',
                       help='Install the explicit account-access dashboard manifest; restart only dashboard and preview')
    scope.add_argument('--t3-usage-only', action='store_true',
                       help='Back up and install only the usage shim backend; restart only dashboard; no keys provisioned')
    args = parser.parse_args()
    files = install_files(args.claude_watch_only, args.account_access_only, args.t3_usage_only)
    if args.t3_usage_only:
        if args.apply:
            install_t3_usage(files)
        else:
            print('Back up replaced backend files only; restart personal-codex-router-dashboard.service only.')
            print('No keys provisioned; no gateway, preview, watch, credential or settings changes.')
            for source, dest, mode in files:
                print(f'Install {source.relative_to(ROOT)} -> {dest} ({mode:o})')
            for path in backup_paths(files, t3_usage_only=True):
                print(f'Back up if present: {path}')
        return
    if args.account_access_only and not args.apply:
        print('Back up replaced files only; no new account-access state file.')
        print(SCOPED_MOVE_NOTE)
        print('Reload systemd units; restart personal-codex-router-dashboard.service and personal-codex-router-preview.service only; deployment does not confirm account status.')
        for source, dest, mode in files:
            print(f'Install {source.relative_to(ROOT)} -> {dest} ({mode:o})')
        for path in backup_paths(files, account_access_only=True):
            print(f'Back up if present: {path}')
        return
    if args.claude_watch_only and not args.apply:
        print('Back up watch snapshots and replaced files only; do not read or copy the setup-token.')
        print(SCOPED_MOVE_NOTE)
        print('Reload systemd units; restart personal-codex-router-dashboard.service and personal-codex-router-preview.service; enable/start personal-codex-router-claude-b-usage.timer (systemd --user).')
        for source, dest, mode in files:
            print(f'Install {source.relative_to(ROOT)} -> {dest} ({mode:o})')
        for path in backup_paths(files, True):
            print(f'Back up if present: {path}')
        return
    if not args.apply:
        print('Will back up current runtime/settings; install backup/health timers, dashboard changes, and device-restricted previews.')
        print('Restart personal-codex-router-dashboard.service and personal-codex-router-preview.service after installing files.')
        print('Enable personal-codex-router-move.path to accept dashboard conversation-move requests; '
              f'check it is active and {MOVE_REQUESTS} exists after the dashboard restart.')
        print(f'Create {WATCH} ({WATCH_USER}:cliproxyapi, 2750); user units in {USER_UNIT_DIR} owned by {WATCH_USER}.')
        print(f'Enable/start personal-codex-router-claude-usage.timer for {WATCH_USER} (systemd --user).')
        print(f'Enable/start personal-codex-router-claude-b-usage.timer for {WATCH_USER} (systemd --user).')
        print(f'Enable/start personal-codex-router-claude-keepalive.timer for {WATCH_USER} (systemd --user).')
        print(f'Create {CLAUDE_STATE} ({WATCH_USER}, 0700); never back up the UNIX socket.')
        print(f'Enable/start personal-codex-router-claude-redeemer.service for {WATCH_USER} (systemd --user).')
        print(f'Restart personal-codex-router-claude-redeemer.service for {WATCH_USER} (systemd --user) after installing files.')
        for source, dest, mode in files:
            print(f'Install {source.relative_to(ROOT)} -> {dest} ({mode:o})')
        for path in backup_paths(files):
            print(f'Back up if present: {path}')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo')
    before_gateway = command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi')
    before_codex = version_fingerprint()
    initial = overview()
    accounts = initial['gateway']['accounts']
    expected = routing_policy.identities('codex')
    configured = [a for a in accounts if a.get('email') in expected]
    if (not initial['gateway']['online'] or len(configured) != len(expected)
            or {a.get('email') for a in configured} != set(expected)):
        raise RuntimeError('Expected configured Codex accounts before deployment')
    if any(a.get('resets', {}).get('automatic') and any(
        c.get('expires_at') and 0 < c['expires_at'] - time.time() < 360 for c in a['resets'].get('credits', [])) for a in accounts):
        raise RuntimeError('An automatic reset is imminent; deploy after its expiry window')
    if not args.claude_watch_only and not args.account_access_only:
        ts = json.loads(command('tailscale', 'status', '--json'))
        peers = list(ts.get('Peer', {}).values())
        for address in (PHONE, MAC):
            peer = next((p for p in peers if address in p.get('TailscaleIPs', [])), None)
            if not peer or peer.get('UserID') != ts['Self'].get('UserID'):
                raise RuntimeError('Personal device identity changed; inspect before allowing previews')
    OPS.mkdir(mode=0o2750, exist_ok=True)
    OPS.chmod(0o2750)
    os.chown(OPS, 0, grp.getgrnam('cliproxyapi').gr_gid)
    backup_dir = Path('/var/lib/personal-codex-router-backups')
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_dir.chmod(0o700)
    spec = importlib.util.spec_from_file_location('router_backup', ROOT / 'scripts/router-backup.py')
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    if args.account_access_only:
        backup.BACKUPS = backup_dir / 'account-access-deployments'
        backup.STATUS = OPS / 'account-access-deployment-backup.json'
        pre_apply_backup(backup, files, account_access_only=True)
    elif args.claude_watch_only:
        # A scoped deployment snapshot is not a full router recovery backup.
        backup.BACKUPS = backup_dir / 'claude-watch-deployments'
        backup.STATUS = OPS / 'claude-watch-deployment-backup.json'
        pre_apply_backup(backup, files, True)
    else:
        pre_apply_backup(backup, files)
    backup_status = OPS / ('account-access-deployment-backup.json' if args.account_access_only else
                          'claude-watch-deployment-backup.json' if args.claude_watch_only else 'backup.json')
    saved = json.loads(backup_status.read_text())['snapshot']
    if not args.account_access_only:
        watch_user = pwd.getpwnam(WATCH_USER)
        prepare_watch_paths(watch_user)
    for source, dest, mode in files:
        if dest.parent == USER_UNIT_DIR:
            install_file(source, dest, mode, watch_user.pw_uid, watch_user.pw_gid)
        else:
            install_file(source, dest, mode)
    if args.account_access_only:
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', 'personal-codex-router-dashboard.service',
                'personal-codex-router-preview.service')
        if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
            raise RuntimeError('A protected process changed during deployment; investigate')
        print(json.dumps({'deployed': True, 'scope': 'account-access', 'pre_deploy_snapshot': saved,
                          'gateway_pid': before_gateway, 'account_status_verified': False}))
        return
    if args.claude_watch_only:
        user_command(watch_user, 'daemon-reload')
        user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-b-usage.timer')
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', 'personal-codex-router-dashboard.service',
                'personal-codex-router-preview.service')
        if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
            raise RuntimeError('A protected process changed during deployment; investigate')
        print(json.dumps({'deployed': True, 'scope': 'claude-watch', 'pre_deploy_snapshot': saved,
                          'gateway_pid': before_gateway, 'codex_process_unchanged': True}))
        return
    CONFIG.parent.mkdir(mode=0o755, exist_ok=True)
    CONFIG.parent.chmod(0o755)
    if not CONFIG.exists():
        config = {'bind': '100.64.0.10', 'dns': 'user-desktop.tailnet-example.ts.net',
                  'hostname': 'user-desktop', 'base_port': 8400,
                  'devices': [{'name': 'Personal Mac', 'ips': [MAC]},
                              {'name': "User's S25+", 'ips': [PHONE]}], 'previews': []}
        CONFIG.write_text(json.dumps(config, indent=2) + '\n')
        CONFIG.chmod(0o644)
    guard = GUARD
    rules = guard.read_text()
    marker = 'Personal project previews IPv4'
    if marker not in rules:
        anchor = '    counter drop\n  }\n  chain forward'
        if rules.count(anchor) != 1:
            raise RuntimeError('Firewall layout changed; inspect before updating')
        rule = f'    ip saddr {{ {MAC}, {PHONE} }} tcp dport 8400-8409 ct state new counter accept comment "{marker}"\n'
        preview_boundary = '    tcp dport 8400-8409 iifname != "lo" iifname != "tailscale0" counter drop comment "Previews blocked on non-private interfaces"\n'
        candidate = OPS / 'preview-guard.candidate.nft'
        rules = rules.replace('    iifname != "tailscale0" return', preview_boundary + '    iifname != "tailscale0" return', 1)
        candidate.write_text(rules.replace(anchor, rule + anchor))
        command('nft', '--check', '--file', str(candidate))
        # nft atomically replaces only the dedicated host guard table.
        command('nft', '--file', str(candidate))
        install_file(candidate, guard)
        candidate.unlink()
    command('systemctl', 'daemon-reload')
    user_command(watch_user, 'daemon-reload')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-usage.timer')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-b-usage.timer')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-keepalive.timer')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-redeemer.service')
    user_command(watch_user, 'restart', 'personal-codex-router-claude-redeemer.service')
    command('systemctl', 'enable', '--now', 'personal-codex-router-backup.timer',
            'personal-codex-router-ops.timer', 'personal-codex-router-preview.service',
            'personal-codex-router-move.path')
    command('systemctl', 'restart', 'personal-codex-router-dashboard.service',
            'personal-codex-router-preview.service')
    if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
        raise RuntimeError('A protected process changed during deployment; investigate')
    check_move_ready()
    (OPS / 'deployment.json').write_text(json.dumps({'pre_deploy_snapshot': saved, 'gateway_pid': before_gateway,
        'codex_process_unchanged': True, 'at': time.time()}, indent=2))
    print(json.dumps({'deployed': True, 'pre_deploy_snapshot': saved, 'gateway_pid': before_gateway,
                      'codex_process_unchanged': True, 'preview_devices': ['Personal Mac', "User's S25+"]}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not dump subprocess stderr or configuration contents.
        print('Installation stopped: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__))
        raise SystemExit(1)
