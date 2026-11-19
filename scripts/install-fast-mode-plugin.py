#!/usr/bin/env python3
"""Install the immutable fast-mode library and verify a same-inode hot reload.

Dry-run is deliberately unprivileged: it reads --config and prints only a diff
of this plugin's public settings, never the secret-bearing YAML document.
"""
import argparse
import copy
import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import shlex
import stat
import subprocess
import sys
import tempfile
import time

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path('/etc/cliproxyapi/config.yaml')
PLUGIN_DIR = Path('/opt/personal-codex-router/plugins')
POLICY = '/var/lib/personal-codex-router/fast-mode-policy.json'
STATUS = Path('/var/lib/cliproxyapi/fast-mode-plugin-status.json')
BACKUPS = Path('/var/lib/personal-codex-router-backups/fast-mode-plugin')
VERSION = '0.2.0'
DEACTIVATION_UNCONFIRMED = 3
PUBLIC_KEYS = ('enabled', 'priority', 'policy_path', 'status_path',
               'map_unsupported_ultrafast', 'audit')


def full_install_action(pid):
    """Read-only decision; explicit disables take precedence over a live marker."""
    config, _ = validated_config(CONFIG.read_bytes())
    plugins = config.get('plugins', {})
    if not isinstance(plugins, dict):
        raise ValueError('plugins must be a mapping')
    configs = plugins.get('configs', {})
    if not isinstance(configs, dict):
        raise ValueError('plugins.configs must be a mapping')
    item = configs.get('fast-mode-policy', {})
    if not isinstance(item, dict):
        raise ValueError('fast-mode-policy must be a mapping')
    if plugins.get('enabled') is False or item.get('enabled') is False:
        return 'disabled', 'left off (explicitly disabled in gateway config)'
    try:
        status = json.loads(Path(item.get('status_path', str(STATUS))).read_bytes())
    except (FileNotFoundError, ValueError):
        status = {}
    if not isinstance(status, dict):
        status = {}
    if status.get('disabled') is True:
        return 'disabled', 'left off (explicitly disabled in plugin status)'
    if (str(status.get('pid')) == str(pid) and str(pid) not in ('', '0') and
            status.get('version') == VERSION):
        return 'loaded', f'already loaded {VERSION}'
    return 'install', f'build, install and verify hot reload of {VERSION} (no gateway restart)'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validated_config(original):
    # Reject duplicate keys and aliases: an alias could change unrelated settings.
    tree = yaml.compose(original)
    seen = set()

    def walk(node):
        if node is None:
            return
        if id(node) in seen:
            raise ValueError('YAML aliases are unsupported; inspect the config manually')
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            keys = [key.value for key, _ in node.value]
            if len(set(keys)) != len(keys):
                raise ValueError('Duplicate YAML configuration keys')
            for key, value in node.value:
                walk(key)
                walk(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                walk(value)

    walk(tree)
    config = yaml.safe_load(original)
    if gateway_config.layout(config) != 'v8':
        raise ValueError('A native v8 configuration is required')
    return config, tree


def replace_plugins(original, plugins):
    config, tree = validated_config(original)
    expected = copy.deepcopy(config)
    if plugins is None:
        expected.pop('plugins', None)
    else:
        expected['plugins'] = plugins
    text = original.decode()
    # Keep all bytes outside the plugins block, including secrets/comments.
    pairs = tree.value
    for i, (key, value) in enumerate(pairs):
        if key.value == 'plugins':
            if tree.flow_style or key.start_mark.column != 0:
                raise ValueError('Expected a top-level block plugins mapping')
            start = key.start_mark.index
            # value.end_mark can include comments preceding the next key. Find
            # the end of the last content line instead to preserve those comments.
            def content_end(node):
                if isinstance(node, yaml.MappingNode) and node.value:
                    return max(content_end(child) for pair in node.value for child in pair)
                if isinstance(node, yaml.SequenceNode) and node.value:
                    return max(content_end(child) for child in node.value)
                return node.end_mark.index
            end = text.find('\n', content_end(value))
            end = len(text) if end == -1 else end + 1
            if i + 1 < len(pairs):
                end = min(end, pairs[i + 1][0].start_mark.index)
            replacement = '' if plugins is None else yaml.safe_dump(
                {'plugins': plugins}, sort_keys=False)
            text = text[:start] + replacement + text[end:]
            break
    else:
        if plugins is not None:
            text += ('' if text.endswith('\n') else '\n') + yaml.safe_dump(
                {'plugins': plugins}, sort_keys=False)
    candidate = text.encode()
    actual, _ = validated_config(candidate)
    if actual != expected:
        raise ValueError('Unexpected configuration change')
    return candidate


def configuration_candidate(original, disable=False):
    config, _ = validated_config(original)
    plugins = copy.deepcopy(config.get('plugins', {}))
    if not isinstance(plugins, dict):
        raise ValueError('plugins must be a mapping')
    configs = plugins.setdefault('configs', {})
    if not isinstance(configs, dict):
        raise ValueError('plugins.configs must be a mapping')
    if disable:
        if not isinstance(configs.get('fast-mode-policy'), dict):
            raise ValueError('fast-mode-policy is not configured')
        configs['fast-mode-policy']['enabled'] = False
    else:
        # Moving another active plugin's directory silently would disable it.
        other_enabled = any(name != 'fast-mode-policy' and isinstance(item, dict)
                            and item.get('enabled') is True for name, item in configs.items())
        if other_enabled and plugins.get('dir', 'plugins') != str(PLUGIN_DIR):
            raise ValueError('Existing active plugins use another directory; reconcile their libraries first')
        plugins.update(enabled=True, dir=str(PLUGIN_DIR))
        item = configs.setdefault('fast-mode-policy', {})
        if not isinstance(item, dict):
            raise ValueError('fast-mode-policy must be a mapping')
        item.update(enabled=True, priority=100, policy_path=POLICY,
                    status_path=str(STATUS), map_unsupported_ultrafast=True, audit=False)
        store = item.setdefault('store', {})
        if not isinstance(store, dict):
            raise ValueError('fast-mode-policy.store must be a mapping')
        store['version'] = VERSION
    return replace_plugins(original, plugins)


def public_projection(data):
    config, _ = validated_config(data)
    plugins = config.get('plugins') or {}
    item = (plugins.get('configs') or {}).get('fast-mode-policy') or {}
    # Do not print unknown values even under known keys: a manually misconfigured
    # field might contain a credential. This is a public settings diff only.
    defaults = {'policy_path': POLICY, 'status_path': str(STATUS)}
    public = {}
    for key in PUBLIC_KEYS:
        if key not in item:
            continue
        value = item[key]
        valid = type(value) is bool if key not in ('priority', *defaults) else (
            type(value) is int if key == 'priority' else value == defaults[key])
        public[key] = value if valid else '<custom value omitted>'
    version = (item.get('store') or {}).get('version') if isinstance(item.get('store', {}), dict) else None
    if version is not None:
        public['store.version'] = version if version == VERSION else '<other version>'
    directory = plugins.get('dir')
    return yaml.safe_dump({'plugins': {
        'enabled': plugins.get('enabled') if type(plugins.get('enabled')) is bool else '<unset>',
        'dir': directory if directory == str(PLUGIN_DIR) else '<other or unset>',
        'configs': {'fast-mode-policy': public}}}, sort_keys=False).splitlines(keepends=True)


def public_diff(original, candidate):
    return ''.join(difflib.unified_diff(public_projection(original), public_projection(candidate),
                                      fromfile='public-plugin-settings.before',
                                      tofile='public-plugin-settings.after'))


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout


class SandboxError(RuntimeError):
    """A public, credential-free deployment diagnostic."""


SANDBOX_PROPERTIES = (
    'ProtectSystem', 'ReadWritePaths', 'ReadOnlyPaths', 'InaccessiblePaths',
    'StateDirectory', 'User', 'Group', 'SupplementaryGroups', 'ProtectHome',
    'PrivateTmp', 'ProtectProc', 'ProcSubset', 'DynamicUser', 'RootDirectory',
    'RootImage', 'BindPaths', 'BindReadOnlyPaths', 'TemporaryFileSystem')


def unit_sandbox(unit):
    try:
        output = command('systemctl', 'show', unit,
                         '--property=' + ','.join(SANDBOX_PROPERTIES))
        facts = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if any(key not in facts for key in SANDBOX_PROPERTIES):
            raise ValueError('incomplete properties')
        return facts
    except (ValueError, subprocess.SubprocessError, OSError):
        raise SandboxError(f'Preflight: cannot inspect the effective {unit} sandbox; configuration unchanged.') from None


def sandbox_paths(facts, key):
    import shlex
    try:
        values = shlex.split(facts.get(key, ''))
    except ValueError:
        raise SandboxError(f'Preflight: cannot parse {key}; configuration unchanged.') from None
    paths = []
    for value in values:
        value = value.lstrip('-+')  # systemd optional and host-root prefixes
        if key == 'StateDirectory':
            # Read-only StateDirectory flags cannot grant a writable exception.
            parts = value.split(':')
            if any(parts[2:]):
                raise SandboxError('Preflight: unsupported StateDirectory flags; configuration unchanged.')
            value = '/var/lib/' + parts[0]
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts or '%' in value or '~' in value:
            raise SandboxError(f'Preflight: unsupported {key} path; configuration unchanged.')
        paths.append(path)
    return paths


def contains(parent, path):
    return path == parent or parent in path.parents


def sandbox_access(unit, facts, path, write=False):
    """Conservative mount-namespace check; filesystem permissions are separate."""
    if any(facts.get(key) for key in ('RootDirectory', 'RootImage', 'BindPaths',
                                     'BindReadOnlyPaths', 'TemporaryFileSystem')) or facts.get('DynamicUser') == 'yes':
        raise SandboxError(f'Preflight: unsupported filesystem remapping in {unit}; configuration unchanged.')
    hidden = sandbox_paths(facts, 'InaccessiblePaths')
    home = facts.get('ProtectHome', 'no')
    if home in ('yes', 'true', 'tmpfs'):
        hidden += [Path('/home'), Path('/root'), Path('/run/user')]
    if any(contains(parent, path) for parent in hidden):
        raise SandboxError(f'Preflight: {unit} hides required path {path}; configuration unchanged.')
    if facts.get('PrivateTmp', 'no') not in ('no', 'false') and any(
            contains(Path(parent), path) for parent in ('/tmp', '/var/tmp')):
        raise SandboxError(f'Preflight: {unit} has a private temporary directory for {path}; configuration unchanged.')
    if not write:
        return
    protect = facts.get('ProtectSystem', 'no')
    if protect not in ('no', 'false', 'yes', 'true', 'full', 'strict'):
        raise SandboxError(f'Preflight: unsupported ProtectSystem in {unit}; configuration unchanged.')
    readonly = []
    explicit_readonly = sandbox_paths(facts, 'ReadOnlyPaths')
    if protect == 'strict':
        readonly += [Path('/')]
    elif protect in ('yes', 'true', 'full'):
        readonly += [Path('/usr'), Path('/boot'), Path('/efi')]
        if protect == 'full':
            readonly += [Path('/etc')]
    if home == 'read-only':
        readonly += [Path('/home'), Path('/root'), Path('/run/user')]
    # Explicit restrictions override a StateDirectory exception at the same
    # depth. ReadWritePaths can make a nested directory writable.
    matches = [(len(parent.parts), rank, allowed)
               for rank, allowed, paths in ((0, False, readonly),
                   (1, True, sandbox_paths(facts, 'StateDirectory')),
                   (2, False, explicit_readonly),
                   (3, True, sandbox_paths(facts, 'ReadWritePaths')))
               for parent in paths if contains(parent, path)]
    if matches and not max(matches)[2]:
        raise SandboxError(f'Preflight: {unit} cannot write {path} in its effective sandbox; configuration unchanged.')


def service_identity(facts):
    import grp
    try:
        name = facts.get('User') or 'root'
        user = pwd.getpwuid(int(name)) if name.isdigit() else pwd.getpwnam(name)
        name = facts.get('Group')
        group = (grp.getgrgid(int(name)) if name.isdigit() else grp.getgrnam(name)) if name else None
        groups = set(os.getgrouplist(user.pw_name, group.gr_gid if group else user.pw_gid))
        for name in facts.get('SupplementaryGroups', '').split():
            groups.add(int(name) if name.isdigit() else grp.getgrnam(name).gr_gid)
        return user.pw_uid, groups, group.gr_gid if group else user.pw_gid
    except (KeyError, ValueError):
        raise SandboxError('Preflight: cannot resolve service User/Group; configuration unchanged.') from None


def mode_allows(info, identity, bits):
    uid, groups = identity[:2]
    if uid == 0:
        return True
    shift = 6 if info.st_uid == uid else 3 if info.st_gid in groups else 0
    return (stat.S_IMODE(info.st_mode) >> shift) & bits == bits


def filesystem_access(path, identity, *, write=False, future=None, socket=False, directory=False, stat_path=None):
    """Check ancestry and DAC without opening secrets or writing probe files.

    future=(owner identity, mode) describes a file created atomically by a service.
    Required paths must exist unless a future service-created file is specified.
    """
    stat_path = stat_path or Path.stat
    for parent in reversed(path.parents):
        if parent.is_symlink():
            raise SandboxError(f'Preflight: symlink in required path ancestry {parent}; configuration unchanged.')
        if parent.exists():
            if not parent.is_dir() or not mode_allows(stat_path(parent), identity, 1):
                raise SandboxError(f'Preflight: service cannot traverse {parent}; configuration unchanged.')
        else:
            raise SandboxError(f'Preflight: state directory {parent} must already exist; configuration unchanged.')
    if path.is_symlink():
        raise SandboxError(f'Preflight: symlink at required path {path}; configuration unchanged.')
    if write:
        if path.exists() and not path.is_file():
            raise SandboxError(f'Preflight: expected a state file at {path}; configuration unchanged.')
        if not mode_allows(stat_path(path.parent), identity, 3):
            raise SandboxError(f'Preflight: service cannot create/rename files in {path.parent}; configuration unchanged.')
    elif path.exists():
        info = stat_path(path)
        valid = (stat.S_ISDIR(info.st_mode) if directory else
                 stat.S_ISSOCK(info.st_mode) if socket else stat.S_ISREG(info.st_mode))
        bits = 5 if directory else 2 if socket else 4
        if not valid or not mode_allows(info, identity, bits):
            raise SandboxError(f'Preflight: service cannot read {path}; configuration unchanged.')
    elif socket:
        raise SandboxError('Preflight: dashboard system bus socket is missing; configuration unchanged.')
    elif future is not None:
        owner, mode = future
        owner_group = owner[2] if len(owner) > 2 else next(iter(owner[1]))
        shift = 6 if identity[0] == owner[0] else 3 if owner_group in identity[1] else 0
        if identity[0] != 0 and not (mode >> shift) & 4:
            raise SandboxError(f'Preflight: service cannot read future file {path}; configuration unchanged.')
    else:
        raise SandboxError(f'Preflight: required file is missing: {path}; configuration unchanged.')


def installer_access(path, *, protected=True, repair_root=None):
    # Installer writes as root outside service namespaces; reject links and
    # unsafe ownership before making backups, directories or library files.
    for parent in reversed((path.parent, *path.parent.parents)):
        if parent.is_symlink():
            raise SandboxError(f'Preflight: unsafe installer directory {parent}; configuration unchanged.')
        if parent.exists():
            info = parent.stat()
            repairable = repair_root is not None and contains(repair_root, parent)
            unsafe = info.st_uid != 0 or (info.st_mode & 0o022 and not repairable)
            if not stat.S_ISDIR(info.st_mode) or (protected and unsafe):
                raise SandboxError(f'Preflight: installer directory must be root-owned and protected: {parent}; configuration unchanged.')
    if path.is_symlink():
        raise SandboxError(f'Preflight: symlink at installer output {path}; configuration unchanged.')
    if path.exists() and not path.is_file():
        raise SandboxError(f'Preflight: expected an installer output file at {path}; configuration unchanged.')
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise SandboxError(f'Preflight: installer cannot write {parent}; configuration unchanged.')
    if path == CONFIG and (not path.is_file() or not os.access(path, os.R_OK | os.W_OK)):
        raise SandboxError('Preflight: installer cannot read/write the live config; configuration unchanged.')


def status_marker_paths(config, original=None):
    """Include both markers only when deactivation will publish to both."""
    enabled, item = status_settings(config)
    paths = {Path(item.get('status_path', str(STATUS)))}
    if not enabled and original is not None:
        _, old_item = status_settings(original)
        paths.add(Path(old_item.get('status_path', str(STATUS))))
    for path in paths:
        if not path.is_absolute() or contains(Path('/var/lib/cliproxyapi/auth'), path):
            raise SandboxError('Preflight: status path must be absolute and outside auth/; configuration unchanged.')
    return paths


def installer_preflight(config, original=None):
    """Reject unsafe output ancestry before preparing deployment artifacts."""
    installer_access(CONFIG, protected=False)
    installer_access(BACKUPS / 'deployment' / 'config.before.yaml')
    for status in status_marker_paths(config, original):
        installer_access(status, protected=False)
        if not status.parent.is_dir():
            raise SandboxError(f'Preflight: state directory {status.parent} must already exist; configuration unchanged.')
    if status_settings(config)[0]:
        directory, version = library_selection(config)
        installer_access(directory / 'linux/amd64' / f'fast-mode-policy-v{version}.so',
                         repair_root=PLUGIN_DIR)


def preflight(config, pid, original=None):
    enabled, item = status_settings(config)
    if not enabled:
        # Deactivation consumes no library or policy and root publishes the
        # markers. Dashboard and gateway namespace/DAC checks do not apply.
        installer_preflight(config, original)
        return
    gateway_unit, dashboard_unit = 'cliproxyapi', 'personal-codex-router-dashboard'
    gateway, dashboard = unit_sandbox(gateway_unit), unit_sandbox(dashboard_unit)
    gateway_id, dashboard_id = service_identity(gateway), service_identity(dashboard)
    policy = Path(item.get('policy_path', POLICY))
    status = Path(item.get('status_path', str(STATUS)))
    controls = Path('/var/lib/personal-codex-router/fast-mode-controls.json')
    directory, version = library_selection(config)
    library = directory / 'linux/amd64' / f'fast-mode-policy-v{version}.so'
    for path, writer, reader, mode in ((policy, dashboard_id, gateway_id, 0o640),
                                      (controls, dashboard_id, dashboard_id, 0o600),
                                      (status, gateway_id, dashboard_id, 0o640)):
        if not path.is_absolute() or contains(Path('/var/lib/cliproxyapi/auth'), path):
            raise SandboxError('Preflight: policy/status/control paths must be absolute and outside auth/; configuration unchanged.')
        writer_unit, writer_facts = (gateway_unit, gateway) if path == status else (dashboard_unit, dashboard)
        reader_unit, reader_facts = (dashboard_unit, dashboard) if path != policy else (gateway_unit, gateway)
        sandbox_access(writer_unit, writer_facts, path, write=True)
        sandbox_access(reader_unit, reader_facts, path)
        filesystem_access(path, writer, write=True, future=(writer, mode))
        # Policy and controls appear only after the dashboard sees Codex accounts.
        filesystem_access(path, reader, future=(writer, mode))
    sandbox_access(gateway_unit, gateway, library)
    filesystem_access(library, gateway_id)
    for parent in (directory, directory / 'linux', directory / 'linux/amd64'):
        sandbox_access(gateway_unit, gateway, parent)
        filesystem_access(parent, gateway_id, directory=True)
    installer_access(library)
    sandbox_access(dashboard_unit, dashboard, Path('/proc') / str(pid) / 'cmdline')
    if dashboard.get('ProcSubset') not in ('all', 'pid') or (dashboard.get('ProtectProc') != 'default' and gateway_id[0] != dashboard_id[0]):
        raise SandboxError('Preflight: dashboard cannot inspect the gateway process; configuration unchanged.')
    filesystem_access(Path('/proc') / str(pid) / 'cmdline', dashboard_id)
    # systemctl MainPID uses the system bus from inside the dashboard namespace.
    sandbox_access(dashboard_unit, dashboard, Path('/run/dbus/system_bus_socket'))
    filesystem_access(Path('/run/dbus/system_bus_socket'), dashboard_id, socket=True)
    installer_preflight(config, original)


def main_pid():
    pid = int(command('systemctl', 'show', 'cliproxyapi', '--property=MainPID', '--value').strip())
    if pid <= 0:
        raise RuntimeError('Gateway must already be running; this script never starts it')
    try:
        exe = os.readlink(f'/proc/{pid}/exe')
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        raise RuntimeError('Cannot verify running gateway identity') from None
    if 'cli-proxy-api' not in exe and b'cli-proxy-api' not in cmdline:
        raise RuntimeError('MainPID is not the expected gateway')
    return pid


def journal_cursor():
    lines = command('journalctl', '-u', 'cliproxyapi', '-n', '0', '--show-cursor', '--no-pager').splitlines()
    cursors = [line.removeprefix('-- cursor: ') for line in lines if line.startswith('-- cursor: ')]
    if len(cursors) != 1:
        raise RuntimeError('Cannot capture gateway journal cursor')
    return cursors[0]


def runtime_snapshot(config):
    # Inspect file identities only, never read credentials or consume usage queues.
    directory = Path(config['oauth']['auth-dir'])
    files = sorted((str(path.relative_to(directory)), path.stat().st_ino, path.stat().st_size,
                    path.stat().st_mtime_ns) for path in directory.rglob('*.json') if path.is_file())
    services = {}
    for unit in ('personal-codex-router-usage.timer', 'personal-codex-router-preference.timer'):
        services[unit] = command('systemctl', 'show', unit, '--property=ActiveState', '--value').strip()
    result = command('systemctl', 'show', 'personal-codex-router-usage.service',
                     '--property=Result', '--value').strip()
    return files, services, result


def metadata(path):
    info = path.stat()
    return info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


def status_settings(config):
    plugins = config.get('plugins') or {}
    item = (plugins.get('configs') or {}).get('fast-mode-policy') or {}
    enabled = plugins.get('enabled') is True and item.get('enabled') is True
    return enabled, item


def verify_status(pid, item, previous=None):
    path = Path(item.get('status_path', str(STATUS)))
    try:
        status = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    if not isinstance(status, dict):
        return False
    return (status.get('plugin') == 'fast-mode-policy'
            and type(status.get('pid')) is int and status.get('pid') == pid
            and not status.get('disabled', False)
            and status.get('version') == (item.get('store') or {}).get('version', VERSION)
            and status.get('policy_path') == item.get('policy_path', POLICY)
            and status.get('map_unsupported_ultrafast') is item.get('map_unsupported_ultrafast', True)
            and status.get('audit', False) is item.get('audit', False)
            and bool(status.get('loaded_at')) and status != previous)


class RuntimeConfirmationError(RuntimeError):
    """Only fixed, credential-free messages may cross the command-line boundary."""


def library_selection(config, *, historical=False):
    plugins = config.get('plugins') or {}
    enabled, item = status_settings(config)
    directory = plugins.get('dir')
    directory = Path(directory) if isinstance(directory, str) and directory else None
    if directory is not None and not directory.is_absolute():
        directory = None
    version = (item.get('store') or {}).get('version', VERSION)
    if enabled and not historical:
        import re
        if directory is None:
            raise SandboxError('Preflight: enabled plugin directory must be absolute; configuration unchanged.')
        if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
            raise SandboxError('Preflight: unsupported plugin version selector; configuration unchanged.')
    return directory, version


def verify_host_event(logs, config, replacement=False):
    directory, version = library_selection(config)
    filename = f'fast-mode-policy-v{version}.so'
    paths = {Path(directory) / filename, Path(directory) / 'linux/amd64' / filename}
    event = 'plugin hot reloaded' if replacement else 'plugin registered'
    version_key = 'active_version' if replacement else 'version'
    path_key = 'active_path' if replacement else 'path'
    for line in logs.splitlines():
        _, found, suffix = line.partition('pluginhost: ' + event + ' ')
        if not found:
            continue
        try:
            fields = dict(field.split('=', 1) for field in shlex.split(suffix) if '=' in field)
        except ValueError:
            continue
        if (fields.get('plugin_id') == 'fast-mode-policy'
                and fields.get(version_key) == version
                and Path(fields.get(path_key, '')) in paths):
            return True
    return False


def status_owner():
    identity = service_identity(unit_sandbox('cliproxyapi'))
    return identity[0], identity[2]


def set_permissions(path, mode, owner, *, chown=None):
    (chown or os.chown)(path, *owner, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)


def repair_status(path, *, owner=None, chown=None):
    """Repair a failed install's marker without changing its runtime evidence."""
    if path.is_symlink():
        raise SandboxError(f'Preflight: symlink at status marker {path}; configuration unchanged.')
    if path.exists():
        if not path.is_file():
            raise SandboxError(f'Preflight: expected a status file at {path}; configuration unchanged.')
        set_permissions(path, 0o640, owner or status_owner(), chown=chown)


def mark_disabled(path, *, owner=None, fchown=None):
    """Publish the same service-owned 0640 marker as the plugin itself."""
    if path.is_symlink():
        raise SandboxError(f'Preflight: symlink at status marker {path}; configuration unchanged.')
    owner = owner or status_owner()
    try:
        marker = json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        marker = {}
    if not isinstance(marker, dict):
        marker = {}
    marker.update(plugin='fast-mode-policy', disabled=True)
    fd, temporary = tempfile.mkstemp(prefix='.fast-mode-disabled-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            (fchown or os.fchown)(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), 0o640)
            stream.write((json.dumps(marker) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verified_reload(path, original, candidate, pid, before_runtime, timeout=30):
    before_meta = metadata(path)
    config = yaml.safe_load(candidate)
    enabled, item = status_settings(config)
    status_path = Path(item.get('status_path', str(STATUS)))
    try:
        previous = json.loads(status_path.read_bytes())
    except (OSError, ValueError):
        previous = None
    changed = candidate != original
    old_config = yaml.safe_load(original)
    _, old_item = status_settings(old_config)
    old_path = Path(old_item.get('status_path', str(STATUS)))
    if old_path == status_path:
        prior_library_status = previous
    else:
        try:
            prior_library_status = json.loads(old_path.read_bytes())
        except (OSError, ValueError):
            prior_library_status = None
    # A disabled same-process marker still proves this library was loaded before.
    # v8.0.7 re-registers retained libraries without a new 'registered' log.
    old_selection = library_selection(old_config, historical=True) if enabled else None
    known_loaded = (enabled and isinstance(prior_library_status, dict)
                    and prior_library_status.get('plugin') == 'fast-mode-policy'
                    and prior_library_status.get('pid') == pid and bool(prior_library_status.get('loaded_at'))
                    and prior_library_status.get('version') == old_selection[1])
    old_enabled, _ = status_settings(old_config)
    needs_swap = changed and enabled and (old_enabled or known_loaded) and old_selection != library_selection(config)
    needs_registration = changed and enabled and not known_loaded and not needs_swap
    cursor = journal_cursor() if changed else None
    if changed:
        gateway_config.write_same_inode(path, candidate, original)
    deadline = time.monotonic() + timeout
    reloaded = not changed
    host_confirmed = not (needs_swap or needs_registration)
    while time.monotonic() < deadline:
        if main_pid() != pid:
            raise RuntimeError('Gateway PID changed during hot reload')
        if changed and (not reloaded or not host_confirmed):
            logs = command('journalctl', '-u', 'cliproxyapi', '--after-cursor=' + cursor,
                           '_PID=' + str(pid), '--no-pager', '-o', 'cat')
            reloaded = reloaded or 'config successfully reloaded' in logs
            host_confirmed = host_confirmed or verify_host_event(logs, config, replacement=needs_swap)
        if (reloaded and host_confirmed
                and (not enabled or verify_status(pid, item, previous if changed else None))):
            break
        time.sleep(0.2)
    else:
        raise RuntimeConfirmationError('Timed out waiting for plugin runtime confirmation after hot reload; status marker retained.')
    if metadata(path) != before_meta or path.read_bytes() != candidate:
        raise RuntimeError('Config bytes or inode/owner/mode changed unexpectedly')
    after_runtime = runtime_snapshot(config)
    if after_runtime[0] != before_runtime[0] or after_runtime[1] != before_runtime[1]:
        raise RuntimeError('Account file identities or collection/routing timers changed')
    if before_runtime[2] == 'success' and after_runtime[2] != 'success':
        raise RuntimeError('Usage collector no longer reports success')
    if main_pid() != pid:
        raise RuntimeError('Gateway PID changed during runtime confirmation')
    if not enabled:
        # No host log or callback confirms the silent snapshot swap in v8.0.7.
        # Mark both locations conservatively when rollback changes/removes config.
        for marker in status_marker_paths(config, old_config):
            mark_disabled(marker)
        reload_evidence = 'the reload was logged' if changed else 'config was unchanged; no reload was needed'
        print('v8.0.7 gives no keyless runtime confirmation of deactivation: '
              'disable is applied in config, ' + reload_evidence +
              ', and runtime deactivation is unconfirmed; status marked disabled.', flush=True)
        return DEACTIVATION_UNCONFIRMED
    return 0


def secure_directory(path, *, mode=0o755, managed_root=None, chown=None, stat_path=None):
    """Set metadata on managed/new directories, leaving existing ancestors alone."""
    managed_root = managed_root or path
    stat_path = stat_path or Path.lstat
    for parent in reversed((path, *path.parents)):
        created = not parent.exists()
        if created:
            parent.mkdir(mode=mode)
        info = stat_path(parent)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('Install directory ancestry must contain only real directories')
        managed = created or contains(managed_root, parent)
        if managed:
            set_permissions(parent, mode, (0, 0), chown=chown)
        elif info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Install directory ancestry must be root-owned and not writable by group/others')


def install_library(source, directory=None, root_owned=True, *, managed_root=None,
                    chown=None, fchown=None, stat_path=None):
    directory = directory or PLUGIN_DIR / 'linux/amd64'
    data = source.read_bytes()
    # Check the architecture without executing the library as root.
    if len(data) < 20 or data[:6] != b'\x7fELF\x02\x01' or data[16:18] != b'\x03\x00' or data[18:20] != b'\x3e\x00':
        raise ValueError('Expected an ELF Linux amd64 shared library')
    if root_owned:
        secure_directory(directory, managed_root=managed_root or PLUGIN_DIR,
                         chown=chown, stat_path=stat_path)
    destination = directory / f'fast-mode-policy-v{VERSION}.so'
    if destination.is_symlink():
        raise ValueError('Refusing a symlinked versioned library')
    if destination.exists():
        if not destination.is_file():
            raise ValueError('Expected a regular versioned library')
        if destination.read_bytes() != data:
            raise ValueError('Versioned library already exists with different bytes; never overwrite a loaded .so')
        if root_owned:
            set_permissions(destination, 0o644, (0, 0), chown=chown)
        else:
            destination.chmod(0o644)
        return destination
    fd, temporary = tempfile.mkstemp(prefix='.fast-mode-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            owner = (0, 0) if root_owned else (os.geteuid(), os.getegid())
            (fchown or os.fchown)(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-clobber installation; no rename over a loaded library.
        os.link(temporary, destination)
        dir_fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        os.unlink(temporary)
    return destination


def run_build(builder, output):
    proc = subprocess.Popen(['runuser', '-u', builder.pw_name, '--',
                             str(ROOT / 'plugins/fast-mode-policy/build.sh'), output],
                            start_new_session=True)
    try:
        if proc.wait(timeout=600) != 0:
            raise RuntimeError('Plugin build failed')
    except BaseException:
        # A timed-out runuser must not leave a queued heavy/build child behind.
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        raise


def backup_config(original, candidate, path, *, chown=None, fchown=None, stat_path=None):
    secure_directory(BACKUPS, mode=0o700, chown=chown, stat_path=stat_path)
    backup = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=BACKUPS))
    set_permissions(backup, 0o700, (0, 0), chown=chown)
    for name, data in (
        ('config.before.yaml', original),
        ('manifest.json', (json.dumps({'before_sha256': digest(original), 'candidate_sha256': digest(candidate),
                                      'config_metadata': metadata(path)}, indent=2) + '\n').encode())):
        with (backup / name).open('xb') as stream:
            (fchown or os.fchown)(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, help='already-built fast-mode-policy-v0.2.0.so')
    parser.add_argument('--config', type=Path, default=CONFIG, help='alternate path allowed only with --dry-run')
    parser.add_argument('--dry-run', action='store_true')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--disable', action='store_true', help='apply disable and mark status disabled; v8.0.7 has no keyless runtime deactivation confirmation, so exits 3 after logged reload')
    actions.add_argument('--rollback', type=Path, help='backup directory from this installer; refuses config drift; rollback to disabled also exits 3 (runtime deactivation unconfirmed)')
    args = parser.parse_args(argv)
    if not args.dry_run and (os.geteuid() != 0 or args.config != CONFIG):
        raise ValueError('Apply requires sudo and the standard live config path')
    if args.config.is_symlink():
        raise ValueError('Refusing a symlinked config')
    original = args.config.read_bytes()
    if args.rollback:
        manifest = json.loads((args.rollback / 'manifest.json').read_bytes())
        candidate = (args.rollback / 'config.before.yaml').read_bytes()
        if digest(original) != manifest['candidate_sha256'] or digest(candidate) != manifest['before_sha256']:
            raise ValueError('Rollback backup mismatch or intervening config changes; reconcile manually')
        validated_config(candidate)
    else:
        candidate = configuration_candidate(original, args.disable)
    if args.dry_run:
        sys.stdout.write(public_diff(original, candidate))
        return
    os.umask(0o077)
    pid = main_pid()
    candidate_config = yaml.safe_load(candidate)
    original_config = yaml.safe_load(original)
    installer_preflight(candidate_config, original_config)
    before_runtime = runtime_snapshot(original_config)
    backup = backup_config(original, candidate, args.config)
    print(f'Private rollback backup: {backup}', flush=True)
    if not args.disable and not args.rollback:
        source = args.library
        if source is None:
            # Builds always go outside the repo. heavy is invoked by build.sh.
            with tempfile.TemporaryDirectory(prefix='fast-mode-build-', dir='/tmp') as output:
                builder = pwd.getpwuid(ROOT.stat().st_uid)
                if builder.pw_uid == 0:
                    raise ValueError('Root build needs --library; build separately as the checkout owner')
                set_permissions(Path(output), 0o700, (builder.pw_uid, builder.pw_gid))
                run_build(builder, output)
                install_library(Path(output) / f'fast-mode-policy-v{VERSION}.so')
        else:
            install_library(source)
    enabled, item = status_settings(candidate_config)
    if enabled:
        repair_status(Path(item.get('status_path', str(STATUS))))
    # Verify actual installed metadata, never a prediction based on root access.
    preflight(candidate_config, pid, original_config)
    try:
        result = verified_reload(args.config, original, candidate, pid, before_runtime)
    except Exception:
        # Revert only our candidate; refuse to overwrite a concurrent edit.
        if args.config.read_bytes() == candidate and candidate != original:
            try:
                installer_preflight(original_config, candidate_config)
                old_enabled, old_item = status_settings(original_config)
                if old_enabled:
                    repair_status(Path(old_item.get('status_path', str(STATUS))))
                preflight(original_config, pid, candidate_config)
                rollback_result = verified_reload(args.config, candidate, original, pid, before_runtime)
                if rollback_result == DEACTIVATION_UNCONFIRMED:
                    print(f'Deployment failed; original config restored, runtime deactivation unconfirmed. Backup: {backup}', flush=True)
                    return DEACTIVATION_UNCONFIRMED
            except Exception:
                raise RuntimeConfirmationError('Deployment and rollback runtime confirmation failed. Private backup: ' + str(backup)) from None
            raise RuntimeConfirmationError('Deployment runtime confirmation failed; original enabled config restored and verified. Backup: ' + str(backup)) from None
        raise RuntimeConfirmationError('Deployment runtime confirmation failed; config restoration was not verified. Backup: ' + str(backup)) from None
    if result == DEACTIVATION_UNCONFIRMED:
        return result
    print(f'Verified hot reload; gateway PID {pid}; backup {backup}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (SandboxError, RuntimeConfirmationError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (Exception, KeyboardInterrupt):
        # YAML errors and subprocess exceptions can include credential-bearing
        # source lines or journal output. Do not print exception text.
        print('Fast-mode deployment failed. No restart was attempted; inspect the config and private installer backup.', file=sys.stderr)
        sys.exit(1)
