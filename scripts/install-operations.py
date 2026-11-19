#!/usr/bin/env python3
"""Install the reviewed local operations files, preserving running task services."""
import argparse
import fcntl
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
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import site_config
DEST = Path('/opt/personal-codex-router')
CONFIG = Path('/etc/personal-codex-router/previews.json')
OPS = Path('/var/lib/personal-codex-router-ops')
ROUTING = Path('/var/lib/personal-codex-router-routing')
WATCH = Path('/var/lib/personal-codex-router-watch')
SITE_SETTINGS = site_config.validate({}, repo=ROOT)
WATCH_USER = SITE_SETTINGS['desktop_user']
USER_UNIT_DIR = Path(site_config.desktop(SITE_SETTINGS).pw_dir) / '.config/systemd/user'
USER_UNITS = ('claude-usage.service', 'claude-usage.timer', 'claude-redeemer.service',
              'claude-keepalive.service', 'claude-keepalive.timer',
              'claude-b-usage.service', 'claude-b-usage.timer', 'nightly-tests.service', 'nightly-tests.timer')
CLAUDE_STATE = Path(site_config.desktop(SITE_SETTINGS).pw_dir) / '.local/state/personal-codex-router'
GUARD = Path('/etc/tailscale/host-guard.nft')
# Conversation moves need the helper, its gateway_config module, both move units
# and the dashboard unit's request RuntimeDirectory; only a full install ships them all.
MOVE_FILES = ('move-conversations.py', 'gateway_config.py', 'personal-codex-router-move.path',
              'personal-codex-router-move.service')
MOVE_REQUESTS = Path('/run/personal-codex-router-move-request')
UNITS = ('backup.service', 'backup.timer', 'dashboard.service', 'move.path', 'move.service', 'ops.service', 'ops.timer',
         'preference.service', 'preference.timer', 'preview.service', 'preview-cert.service', 'preview-cert.timer', 'url.service',
         'url.socket', 'usage.service', 'usage.timer', 'request-outcomes.service', 'request-outcomes.timer',
         'provider-status.service', 'provider-status.timer',
         'retention.service', 'retention.timer', 'release-check.service', 'release-check.timer')
SCRIPTS = ('site_config.py', 'collect-usage.py', 'collect-request-outcomes.py', 'collect-claude-usage.py', 'collect-provider-status.py', 'claude-redeemer.py', 'claude-token-keepalive.py', 'gateway_config.py', 'move-conversations.py', 'prefer-earliest-reset.py', 'router-backup.py',
           'router-ops.py', 'router-preview.py', 'router-preview.mjs', 'renew-preview-cert.py', 'retain-usage.py',
           'check-gateway-release.py', 'run-tests.py', 'nightly-tests.py')
CACHES = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
PREVIEW_BIND = None
PREVIEW_TLS_HOSTNAME = None
PREVIEW_BACKUPS = Path('/var/lib/personal-codex-router-backups/preview-deployments')
# Ship a consistent backend with server.py, even over an older installation.
T3_USAGE_FILES = ('server.py', 'series.py', 't3_usage.py', 'providers.py', 'usage_adapters.py', 'quota_headers.py', 'subscriptions.py', 'routing_policy.py', 'operations.py', 'claude_warnings.py',
                  'account_access.py', 'account_add.py', 'account_mutations.py', 'api_keys.py', 'signin.py', 'credential_replace.py', 'request_deadline.py',
                  'availability.py', 'reset_credits.py', 'fast_mode.py', 'codex_catalog.py', 'usage_time.py', 'provider_status.py')
T3_USAGE_BACKUPS = Path('/var/lib/personal-codex-router-backups/t3-usage-deployments')
DISCONNECTED = Path('/var/lib/personal-codex-router/disconnected')
PROVIDER_STATUS_USER = 'router-provider-status'
ACCOUNT_ACCESS_FILES = ('series.py', 't3_usage.py', 'providers.py', 'usage_adapters.py', 'quota_headers.py', 'subscriptions.py', 'account_access.py', 'account_add.py', 'account_mutations.py', 'api_keys.py', 'signin.py', 'credential_replace.py', 'request_deadline.py', 'server.py',
                        'availability.py', 'operations.py', 'claude_warnings.py', 'routing_policy.py', 'static/index.html', 'static/app.js',
                        'static/billing.js', 'static/style.css', 'static/boards.js', 'static/boards.css',
                        'static/charts.js', 'static/widgets.js', 'static/charts.css', 'provider_status.py',
                        'fast_mode.py', 'codex_catalog.py')
FAST_MODE_FAILURE = 4


def fast_mode_installer():
    spec = importlib.util.spec_from_file_location('fast_mode_installer', ROOT / 'scripts/install-fast-mode-plugin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fast_mode_step(*, apply=False, skip=False, gateway_pid=None):
    """Last full-install step; failures leave the completed operations install intact."""
    if skip:
        print('Fast-mode plugin: skipped (--skip-fast-mode-plugin).')
        return 0
    try:
        plugin = fast_mode_installer()
        pid = gateway_pid or command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi')
        action, message = plugin.full_install_action(pid)
        print('Fast-mode plugin: ' + message + '.', flush=True)
        if apply and action == 'install':
            result = plugin.main([])
            if result not in (None, 0):
                raise RuntimeError('Plugin installer did not confirm live enforcement')
        return 0
    except Exception as error:
        # YAML/config/subprocess exception text may contain secrets. The plugin
        # emits its own safe build/backup diagnostics; never print raw exceptions.
        if not apply:
            print('Fast-mode plugin: state unavailable; rerun this read-only plan with sudo and check gateway config/status.')
            return 0
        detail = str(error) if 'plugin' in locals() and isinstance(
            error, (plugin.SandboxError, plugin.RuntimeConfirmationError)) else type(error).__name__
        print('The rest of the install is complete; the fast-mode plugin step failed (' + detail + '). '
              'Check the build output and private plugin backup, ensure gcc and libc6-dev '
              'are installed and HTTPS downloads are reachable, then rerun '
              'sudo python3 scripts/install-operations.py --apply. '
              'See docs/fast-mode-policy.md for gateway/reload recovery; '
              '--skip-fast-mode-plugin leaves enforcement unchanged.', file=sys.stderr)
        return FAST_MODE_FAILURE


def preview_files():
    return [(ROOT / 'scripts' / name, DEST / name, 0o755)
            for name in ('router-preview.mjs', 'router-preview.py', 'renew-preview-cert.py')] + [
        (ROOT / 'scripts' / ('personal-codex-router-' + suffix),
         Path('/etc/systemd/system') / ('personal-codex-router-' + suffix), 0o644)
        for suffix in ('preview.service', 'preview-cert.service', 'preview-cert.timer')] + [
        (ROOT / 'scripts/router-preview.py', Path('/usr/local/bin/router-preview'), 0o755)]


def install_files(claude_watch_only=False, account_access_only=False, t3_usage_only=False):
    if t3_usage_only:
        return [(ROOT / 'dashboard' / name, DEST / 'dashboard' / name, 0o644)
                for name in T3_USAGE_FILES]
    if account_access_only:
        return [(ROOT / 'dashboard' / name, DEST / 'dashboard' / name, 0o644)
                for name in ACCOUNT_ACCESS_FILES] + [
                    (ROOT / 'scripts/router-backup.py', DEST / 'router-backup.py', 0o755),
                    (ROOT / 'scripts/site_config.py', DEST / 'site_config.py', 0o755),
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
        if suffix.startswith('claude-b-') and not SITE_SETTINGS.get('claude_b'):
            continue
        name = 'personal-codex-router-' + suffix
        files.append((ROOT / 'scripts' / name, USER_UNIT_DIR / name, 0o644))
    files.append((ROOT / 'scripts/router-preview.py', Path('/usr/local/bin/router-preview'), 0o755))
    if claude_watch_only:
        files = [(source, dest, mode) for source, dest, mode in files
                 if dest.is_relative_to(DEST / 'dashboard') or dest in (
                     DEST / 'collect-claude-usage.py', DEST / 'claude-token-keepalive.py', DEST / 'router-preview.mjs',
                     DEST / 'router-backup.py', DEST / 'site_config.py',
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


def command(*args, timeout=30):
    return subprocess.run(args, capture_output=True, check=True, timeout=timeout).stdout.decode().strip()


def version_fingerprint():
    pid = Path(site_config.desktop(SITE_SETTINGS).pw_dir) / '.codex/app-server-daemon/app-server.pid'
    return hashlib.sha256(pid.read_bytes()).hexdigest() if pid.exists() else None


def overview():
    request = Request('http://127.0.0.1:8320/api/overview', headers={'X-Router-Request': '1'})
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def install_file(source, dest, mode=0o644, uid=0, gid=0):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.installing')
    shutil.copyfile(source, tmp)
    if source.suffix == '.service':
        tmp.write_text(render_unit(source.read_text()))
    tmp.chmod(mode)
    os.chown(tmp, uid, gid)
    tmp.replace(dest)


def prepare_disconnected_directory():
    # The existing dashboard ReadWritePaths includes this private service-owned directory.
    proxy = pwd.getpwnam('cliproxyapi')
    if DISCONNECTED.is_symlink():
        raise RuntimeError('Disconnect backup directory is symlinked; inspect before applying')
    DISCONNECTED.mkdir(mode=0o700, parents=True, exist_ok=True)
    DISCONNECTED.chmod(0o700)
    os.chown(DISCONNECTED, proxy.pw_uid, proxy.pw_gid)


def prepare_provider_status_user():
    try:
        user = pwd.getpwnam(PROVIDER_STATUS_USER)
    except KeyError:
        command('useradd', '--system', '--user-group', '--no-create-home',
                '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', PROVIDER_STATUS_USER)
        user = pwd.getpwnam(PROVIDER_STATUS_USER)
    gateway = pwd.getpwnam('cliproxyapi')
    if (user.pw_uid in (0, gateway.pw_uid) or user.pw_gid in (0, gateway.pw_gid) or
            user.pw_dir != '/nonexistent' or user.pw_shell != '/usr/sbin/nologin' or
            grp.getgrnam(PROVIDER_STATUS_USER).gr_gid != user.pw_gid or
            set(os.getgrouplist(PROVIDER_STATUS_USER, user.pw_gid)).intersection({0, gateway.pw_gid})):
        raise RuntimeError('Provider status user must be a dedicated unprivileged identity')


def configure_site(site):
    global SITE_SETTINGS, WATCH_USER, USER_UNIT_DIR, CLAUDE_STATE
    SITE_SETTINGS = site
    WATCH_USER = site['desktop_user']
    home = Path(site_config.desktop(site).pw_dir)
    USER_UNIT_DIR = home / '.config/systemd/user'
    CLAUDE_STATE = home / '.local/state/personal-codex-router'


def systemd_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render_unit(text):
    devices = SITE_SETTINGS['devices']
    dependencies = 'After=tailscaled.service tailscale-host-guard.service\nRequires=tailscale-host-guard.service' if devices else ''
    addresses = ([PREVIEW_BIND] if PREVIEW_BIND else []) + [d['tailscale_ip'] for d in devices]
    allows = '\n'.join(f'IPAddressAllow={address}/32' for address in addresses)
    claude = SITE_SETTINGS.get('claude_b', {})
    environment = '\n'.join('Environment=' + systemd_quote('CLAUDE_B_' + key.upper() + '=' + value)
                            for key, value in claude.items())
    return text.replace('@DEVICE_DEPENDENCIES@', dependencies).replace('@DEVICE_ALLOW@', allows).replace('@CLAUDE_B_ENVIRONMENT@', environment)


def device_preflight():
    """Verify identity and the complete firewall candidate before any install writes."""
    global PREVIEW_BIND, PREVIEW_TLS_HOSTNAME
    devices = SITE_SETTINGS['devices']
    if not devices:
        PREVIEW_BIND = None
        return None
    ts = json.loads(command('tailscale', 'status', '--json'))
    own = ts.get('Self', {})
    PREVIEW_TLS_HOSTNAME = own.get('DNSName', '').rstrip('.') or SITE_SETTINGS.get('tailscale_hostname')
    PREVIEW_BIND = next((ip for ip in own.get('TailscaleIPs', []) if ':' not in ip), None)
    if not PREVIEW_BIND or not own.get('UserID'):
        raise RuntimeError('Tailscale must be signed in with a local IPv4 address')
    site_config.tailscale_ip(PREVIEW_BIND)
    for device in devices:
        peer = next((p for p in ts.get('Peer', {}).values() if device['tailscale_ip'] in p.get('TailscaleIPs', [])), None)
        if not peer or peer.get('UserID') != own['UserID']:
            raise RuntimeError('Configured device identity changed; inspect before allowing previews')
    if not GUARD.is_file():
        raise RuntimeError('Device previews require /etc/tailscale/host-guard.nft and tailscale-host-guard.service; omit devices for loopback only')
    if command('systemctl', 'is-active', 'tailscale-host-guard.service') != 'active':
        raise RuntimeError('The Tailscale host guard must be active before allowing previews')
    rules = GUARD.read_text()
    marker = 'Personal project previews IPv4'
    anchor = '    counter drop\n  }\n  chain forward'
    boundary = '    iifname != "tailscale0" return'
    rule = ('    ip saddr { ' + ', '.join(d['tailscale_ip'] for d in devices) +
            ' } tcp dport 8400-8409 ct state new counter accept comment "' + marker + '"\n')
    if marker in rules:
        lines = [line for line in rules.splitlines(keepends=True) if marker in line]
        if len(lines) != 1 or not lines[0].lstrip().startswith('ip saddr {'):
            raise RuntimeError('Firewall layout changed; inspect before updating')
        rules = rules.replace(lines[0], rule)
    else:
        if rules.count(anchor) != 1 or rules.count(boundary) != 1:
            raise RuntimeError('Firewall layout changed; inspect before updating')
        preview_boundary = '    tcp dport 8400-8409 iifname != "lo" iifname != "tailscale0" counter drop comment "Previews blocked on non-private interfaces"\n'
        rules = rules.replace(boundary, preview_boundary + boundary).replace(anchor, rule + anchor)
    if 'Previews blocked on non-private interfaces' not in rules:
        raise RuntimeError('Firewall preview boundary missing; inspect before updating')
    with tempfile.NamedTemporaryFile(mode='w', suffix='.nft') as candidate:
        candidate.write(rules)
        candidate.flush()
        command('nft', '--check', '--file', candidate.name)
    return rules


def write_site():
    site_config.SITE.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.NamedTemporaryFile(mode='w', dir=site_config.SITE.parent, delete=False) as candidate:
        path = Path(candidate.name)
        try:
            json.dump({**SITE_SETTINGS, 'source_checkout': str(ROOT)}, candidate, indent=2)
            candidate.write('\n')
            candidate.flush()
            os.fchmod(candidate.fileno(), 0o644)
            os.fchown(candidate.fileno(), 0, 0)
            path.replace(site_config.SITE)
        finally:
            path.unlink(missing_ok=True)


def write_previews():
    # Preserve registered previews and existing friendly aliases during migration.
    CONFIG.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with CONFIG.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {'base_port': 8400, 'previews': []}
        hostname = SITE_SETTINGS.get('tailscale_hostname', PREVIEW_BIND or 'localhost')
        config['tls_hostname'] = PREVIEW_TLS_HOSTNAME or SITE_SETTINGS.get('tailscale_hostname', config.get('tls_hostname', config.get('dns')))
        config.update(bind=PREVIEW_BIND or '127.0.0.1', dns=hostname, hostname=hostname.split('.')[0],
                      devices=[{'name': d['name'], 'ips': [d['tailscale_ip']]} for d in SITE_SETTINGS['devices']])
        with tempfile.NamedTemporaryFile(mode='w', dir=CONFIG.parent, delete=False) as candidate:
            path = Path(candidate.name)
            try:
                json.dump(config, candidate, indent=2)
                candidate.write('\n')
                candidate.flush()
                os.fchmod(candidate.fileno(), 0o644)
                os.fchown(candidate.fileno(), 0, 0)
                path.replace(CONFIG)
            finally:
                path.unlink(missing_ok=True)


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
    # The restarted dashboard creates this runtime directory shortly after systemctl returns.
    deadline = time.monotonic() + 10
    while not MOVE_REQUESTS.is_dir():
        if time.monotonic() >= deadline:
            raise RuntimeError(f'{MOVE_REQUESTS} is missing; conversation moves are unavailable')
        time.sleep(0.2)


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


def require_preview_tls():
    if SITE_SETTINGS['devices']:
        unit = Path('/etc/systemd/system/personal-codex-router-preview.service')
        if not unit.exists() or 'LoadCredential=preview.key:' not in unit.read_text():
            raise RuntimeError('Run --preview-only --apply first to migrate the preview TLS credentials')


def install_previews(apply=False):
    """Upgrade previews, preserving registrations, firewall and other services."""
    global PREVIEW_BIND
    files = preview_files()
    if not apply:
        print('Back up and install preview helpers and TLS units only; preserve registrations and firewall; add the current TLS hostname.')
        print('Issue first Tailscale certificate, enable weekly renewal, restart preview service only.')
        for source, dest, mode in files:
            print(f'Install {source.relative_to(ROOT)} -> {dest} ({mode:o})')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo')
    config = json.loads(CONFIG.read_text())
    PREVIEW_BIND = site_config.tailscale_ip(config['bind'])
    devices = [{'name': d['name'], 'tailscale_ip': ip} for d in config['devices'] for ip in d['ips']]
    # Use the installed allowlist verbatim. No device, firewall or Tailscale mutations.
    configure_site({**SITE_SETTINGS, 'devices': devices})
    ts = json.loads(command('tailscale', 'status', '--json'))
    hostname = ts.get('Self', {}).get('DNSName', '').rstrip('.')
    if not hostname.endswith('.ts.net') or '.' not in hostname[:-7]:
        raise RuntimeError('Preview TLS requires this machine full Tailscale DNS name')
    if PREVIEW_BIND not in ts.get('Self', {}).get('TailscaleIPs', []):
        raise RuntimeError('Preview bind address is not this Tailscale device')
    if command('systemctl', 'is-active', 'tailscale-host-guard.service') != 'active':
        raise RuntimeError('The existing host guard must remain active')
    backup_dir = PREVIEW_BACKUPS
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('preview_backup', ROOT / 'scripts/router-backup.py')
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    backup.BACKUPS = backup_dir
    backup.STATUS = backup_dir / 'status.json'
    pre_apply_backup(backup, files + [(CONFIG, CONFIG, 0o644)], account_access_only=True)
    with CONFIG.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(CONFIG.read_text())
        current['tls_hostname'] = hostname
        tmp = CONFIG.with_suffix('.installing')
        tmp.write_text(json.dumps(current, indent=2) + '\n')
        tmp.chmod(0o644)
        tmp.replace(CONFIG)
    for source, dest, mode in files:
        install_file(source, dest, mode)
    command('systemctl', 'daemon-reload')
    # systemctl start waits for the oneshot, including its first certificate issue.
    command('systemctl', 'start', 'personal-codex-router-preview-cert.service', timeout=190)
    command('systemctl', 'enable', '--now', 'personal-codex-router-preview-cert.timer')
    command('systemctl', 'enable', '--now', 'personal-codex-router-preview.service')
    command('systemctl', 'restart', 'personal-codex-router-preview.service')
    print(json.dumps({'deployed': True, 'scope': 'preview',
                      'pre_deploy_snapshot': json.loads(backup.STATUS.read_text())['snapshot']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--skip-fast-mode-plugin', action='store_true',
                        help='Leave the fast-mode plugin unchanged during a full install')
    parser.add_argument('--site-config', type=Path, help='Site JSON to validate (and save as /etc/personal-codex-router/site.json on full apply)')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--preview-only', action='store_true', help='Upgrade preview TLS only; preserve registrations, firewall and other services')
    scope.add_argument('--claude-watch-only', action='store_true',
                        help='Install watch collector/dashboard/B units; back up only replaced files and watch snapshots')
    scope.add_argument('--account-access-only', action='store_true',
                       help='Install the explicit account-access dashboard manifest; restart only dashboard and preview')
    scope.add_argument('--t3-usage-only', action='store_true',
                       help='Back up and install only the usage shim backend; restart only dashboard; no keys provisioned')
    args = parser.parse_args()
    if not args.t3_usage_only:
        configure_site(site_config.load(args.site_config, repo=ROOT))
    if args.preview_only:
        if args.site_config:
            raise RuntimeError('Preview-only installs preserve the installed config')
        install_previews(args.apply)
        return
    if args.site_config and args.apply and (args.account_access_only or args.claude_watch_only or args.t3_usage_only):
        raise RuntimeError('Site config changes require a full install')
    if args.apply and (args.account_access_only or args.claude_watch_only) and not site_config.SITE.exists():
        raise RuntimeError('Run a full install first to migrate site.json before scoped deployments')
    files = install_files(args.claude_watch_only, args.account_access_only, args.t3_usage_only)
    if not args.t3_usage_only and not args.apply:
        print(f'Site user: {WATCH_USER}; device proxy: {len(SITE_SETTINGS["devices"])} devices; Claude B watch: {bool(SITE_SETTINGS.get("claude_b"))}.')
        print('Full apply saves the resolved settings as root-owned /etc/personal-codex-router/site.json.')
        print('Device identity and nft guard checks run before any installation writes when devices are configured.')
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
    if not args.apply:
        print(f'Create {DISCONNECTED} (cliproxyapi, 0700) for private disconnect backups (0600).')
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
        print('Reload systemd units; restart personal-codex-router-dashboard.service and configured device proxy.')
        print('Enable/start personal-codex-router-claude-b-usage.timer (systemd --user).' if SITE_SETTINGS.get('claude_b') else 'Claude B watch stays off.')
        for source, dest, mode in files:
            print(f'Install {source.relative_to(ROOT)} -> {dest} ({mode:o})')
        for path in backup_paths(files, True):
            print(f'Back up if present: {path}')
        return
    if not args.apply:
        print('Will back up current runtime/settings; install backup/health timers and dashboard changes.')
        print('Install and enable/start personal-codex-router-provider-status.timer (public HTTPS feeds every five minutes).')
        print('Enable daily verified raw usage retention (90 days, private usage-archive; permanent hourly history).')
        print('Enable weekly anonymous GitHub release checks; notices only, never auto-upgrade.')
        print('Enable nightly source tests through heavy (systemd --user); retain 14 logs and notify only on failure.')
        print(f'Create dedicated system user {PROVIDER_STATUS_USER} (no home, no login).')
        print('The collector preserves /run/personal-codex-router-provider-status '
              '(0755) with a public status.json (0644); systemd manages the directory.')
        print('Enable device-restricted previews.' if SITE_SETTINGS['devices'] else 'Loopback-only dashboard; device proxy stays off.')
        print('Restart personal-codex-router-dashboard.service and personal-codex-router-preview.service after installing files.')
        print('Enable personal-codex-router-move.path to accept dashboard conversation-move requests; '
              f'check it is active and {MOVE_REQUESTS} exists after the dashboard restart.')
        print(f'Create {WATCH} ({WATCH_USER}:cliproxyapi, 2750); user units in {USER_UNIT_DIR} owned by {WATCH_USER}.')
        print(f'Enable/start personal-codex-router-claude-usage.timer for {WATCH_USER} (systemd --user).')
        if SITE_SETTINGS.get('claude_b'):
            print(f'Enable/start personal-codex-router-claude-b-usage.timer for {WATCH_USER} (systemd --user).')
        print(f'Enable/start personal-codex-router-claude-keepalive.timer for {WATCH_USER} (systemd --user).')
        print(f'Create {CLAUDE_STATE} ({WATCH_USER}, 0700); never back up the UNIX socket.')
        print(f'Enable/start personal-codex-router-claude-redeemer.service for {WATCH_USER} (systemd --user).')
        print(f'Restart personal-codex-router-claude-redeemer.service for {WATCH_USER} (systemd --user) after installing files.')
        for source, dest, mode in files:
            print(f'Install {source.relative_to(ROOT)} -> {dest} ({mode:o})')
        for path in backup_paths(files):
            print(f'Back up if present: {path}')
        fast_mode_step(skip=args.skip_fast_mode_plugin)
        return
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo')
    if args.account_access_only or args.claude_watch_only:
        require_preview_tls()
    before_gateway = command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi')
    if before_gateway in ('', '0'):
        raise RuntimeError('Start cliproxyapi.service before installing operations')
    before_codex = version_fingerprint()
    # Existing deployments keep their online/reset safety check; a fresh host has no dashboard yet.
    if (DEST / 'dashboard/server.py').exists() or args.claude_watch_only or args.account_access_only:
        initial = overview()
        if not initial['gateway']['online']:
            raise RuntimeError('Gateway must be online before deployment')
        accounts = initial['gateway']['accounts']
        if any(a.get('resets', {}).get('automatic') and any(
            c.get('expires_at') and 0 < c['expires_at'] - time.time() < 360
            for c in a['resets'].get('credits', [])) for a in accounts):
            raise RuntimeError('An automatic reset is imminent; deploy after its expiry window')
    full_install = not args.claude_watch_only and not args.account_access_only
    # Preflight must precede even directory creation or backup writes.
    guard_rules = device_preflight() if full_install else None
    for source, _, _ in files:
        if not source.is_file():
            raise RuntimeError('An installation source is missing')
    watch_user = None if args.account_access_only else pwd.getpwnam(WATCH_USER)
    grp.getgrnam('cliproxyapi')
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
        if not (DEST / 'dashboard/server.py').exists():
            # First install has no usage DB, reset state or Codex desktop files yet.
            backup.sources = lambda: [p for p in (Path('/etc/cliproxyapi'), Path('/var/lib/cliproxyapi/auth')) if p.exists()]
        pre_apply_backup(backup, files)
    backup_status = OPS / ('account-access-deployment-backup.json' if args.account_access_only else
                          'claude-watch-deployment-backup.json' if args.claude_watch_only else 'backup.json')
    saved = json.loads(backup_status.read_text())['snapshot']
    if full_install:
        prepare_provider_status_user()
        write_site()
    if not args.account_access_only:
        prepare_watch_paths(watch_user)
    prepare_disconnected_directory()
    for source, dest, mode in files:
        if dest.parent == USER_UNIT_DIR:
            install_file(source, dest, mode, watch_user.pw_uid, watch_user.pw_gid)
        else:
            install_file(source, dest, mode)
    if args.account_access_only:
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', 'personal-codex-router-dashboard.service',
                *(['personal-codex-router-preview.service'] if SITE_SETTINGS['devices'] else []))
        if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
            raise RuntimeError('A protected process changed during deployment; investigate')
        print(json.dumps({'deployed': True, 'scope': 'account-access', 'pre_deploy_snapshot': saved,
                          'gateway_pid': before_gateway, 'account_status_verified': False}))
        return
    if args.claude_watch_only:
        user_command(watch_user, 'daemon-reload')
        if SITE_SETTINGS.get('claude_b'):
            user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-b-usage.timer')
        elif (USER_UNIT_DIR / 'personal-codex-router-claude-b-usage.service').exists():
            user_command(watch_user, 'disable', '--now', 'personal-codex-router-claude-b-usage.timer',
                         'personal-codex-router-claude-b-usage.service')
        command('systemctl', 'daemon-reload')
        command('systemctl', 'restart', 'personal-codex-router-dashboard.service',
                *(['personal-codex-router-preview.service'] if SITE_SETTINGS['devices'] else []))
        if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
            raise RuntimeError('A protected process changed during deployment; investigate')
        print(json.dumps({'deployed': True, 'scope': 'claude-watch', 'pre_deploy_snapshot': saved,
                          'gateway_pid': before_gateway, 'codex_process_unchanged': True}))
        return
    write_previews()
    if guard_rules is not None:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.nft') as candidate:
            candidate.write(guard_rules)
            candidate.flush()
            command('nft', '--file', candidate.name)
            install_file(Path(candidate.name), GUARD)
    command('systemctl', 'daemon-reload')
    user_command(watch_user, 'daemon-reload')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-usage.timer')
    if SITE_SETTINGS.get('claude_b'):
        user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-b-usage.timer')
    elif (USER_UNIT_DIR / 'personal-codex-router-claude-b-usage.service').exists():
        user_command(watch_user, 'disable', '--now', 'personal-codex-router-claude-b-usage.timer',
                     'personal-codex-router-claude-b-usage.service')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-keepalive.timer')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-nightly-tests.timer')
    user_command(watch_user, 'enable', '--now', 'personal-codex-router-claude-redeemer.service')
    user_command(watch_user, 'restart', 'personal-codex-router-claude-redeemer.service')
    # Create the state directories before starting the dashboard's sandbox.
    proxy = pwd.getpwnam('cliproxyapi')
    for path in (Path('/var/lib/personal-codex-router'), ROUTING):
        path.mkdir(mode=0o700, exist_ok=True)
        os.chown(path, proxy.pw_uid, proxy.pw_gid)
    command('systemctl', 'enable', '--now', 'personal-codex-router-usage.timer',
            'personal-codex-router-request-outcomes.timer',
            'personal-codex-router-retention.timer', 'personal-codex-router-release-check.timer',
            'personal-codex-router-provider-status.timer',
            'personal-codex-router-preference.timer', 'personal-codex-router-dashboard.service',
            'personal-codex-router-url.socket', 'personal-codex-router-backup.timer',
            'personal-codex-router-ops.timer', 'personal-codex-router-move.path')
    if SITE_SETTINGS['devices']:
        command('systemctl', 'start', 'personal-codex-router-preview-cert.service', timeout=190)
        command('systemctl', 'enable', '--now', 'personal-codex-router-preview-cert.timer')
        command('systemctl', 'enable', '--now', 'personal-codex-router-preview.service')
    else:
        command('systemctl', 'disable', '--now', 'personal-codex-router-preview-cert.timer',
                'personal-codex-router-preview.service')
    command('systemctl', 'restart', 'personal-codex-router-dashboard.service',
            *(['personal-codex-router-preview.service'] if SITE_SETTINGS['devices'] else []))
    if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
        raise RuntimeError('A protected process changed during deployment; investigate')
    check_move_ready()
    (OPS / 'deployment.json').write_text(json.dumps({'pre_deploy_snapshot': saved, 'gateway_pid': before_gateway,
        'codex_process_unchanged': True, 'at': time.time()}, indent=2))
    print(json.dumps({'deployed': True, 'pre_deploy_snapshot': saved, 'gateway_pid': before_gateway,
                      'codex_process_unchanged': True, 'preview_devices': [d['name'] for d in SITE_SETTINGS['devices']]}))
    result = fast_mode_step(apply=True, skip=args.skip_fast_mode_plugin, gateway_pid=before_gateway)
    # Keep the protected-process guarantee across the plugin's same-PID reload,
    # including failed deployment/rollback attempts.
    if before_gateway != command('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi') or before_codex != version_fingerprint():
        raise RuntimeError('A protected process changed during deployment; investigate')
    return result


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # Do not dump subprocess stderr or configuration contents.
        print('Installation stopped: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__))
        raise SystemExit(1)
