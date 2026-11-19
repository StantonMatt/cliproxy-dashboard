import contextlib
import importlib.util
import io
import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('install_fast_mode', ROOT / 'scripts/install-fast-mode-plugin.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

# Only synthetic credentials; secret markers also detect unsafe dry-run output.
CONFIG = b'''# gateway config
config-version: 8
server:
  host: 127.0.0.1
  port: 32123
management:
  secret-key: TEST-SECRET-MUST-NOT-PRINT
oauth:
  auth-dir: /tmp/dummy-auth
routing:
  strategy: round-robin
  session-affinity: true
  retry:
    request-retry: 0
observability:
  usage:
    usage-statistics-enabled: true
'''


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(installer, 'status_owner', return_value=(os.getuid(), os.getgid())))
        # Every installer test refuses both live credential opens and networking.
        import builtins
        import urllib.request
        for module, name in ((builtins, 'open'), (io, 'open'), (os, 'open')):
            original = getattr(module, name)
            def guarded(path, *args, _open=original, **kwargs):
                if not isinstance(path, int):
                    self.assertNotEqual(os.fsdecode(path), '/etc/cliproxyapi/dashboard-management-key')
                return _open(path, *args, **kwargs)
            self.enterContext(patch.object(module, name, side_effect=guarded))
        self.enterContext(patch.object(urllib.request, 'build_opener', side_effect=AssertionError('no API access')))
        self.enterContext(patch.object(urllib.request, 'urlopen', side_effect=AssertionError('no API access')))

    def test_full_install_action_preserves_disabled_and_reuses_only_live_current_version(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yaml'
            status = Path(directory) / 'status.json'
            config.write_bytes(CONFIG)
            with patch.object(installer, 'CONFIG', config), patch.object(installer, 'STATUS', status):
                self.assertEqual(installer.full_install_action(123)[0], 'install')
                for marker, action in (({'pid': 123, 'version': '0.2.0'}, 'loaded'),
                                       ({'pid': 123, 'version': '0.1.2'}, 'install'),
                                       ({'pid': 124, 'version': '0.2.0'}, 'install'),
                                       ({'pid': 0, 'version': '0.2.0'}, 'install'),
                                       ({'pid': 123, 'version': '0.2.0', 'disabled': True}, 'disabled'),
                                       ({'pid': 124, 'version': '0.1.2', 'disabled': True}, 'disabled'),
                                       ([], 'install')):
                    with self.subTest(marker=marker):
                        status.write_text(json.dumps(marker))
                        self.assertEqual(installer.full_install_action(123)[0], action)
                status.write_text('invalid JSON')
                self.assertEqual(installer.full_install_action(123)[0], 'install')
                status.write_text(json.dumps({'pid': 123, 'version': '0.2.0'}))
                self.assertIn('already loaded 0.2.0', installer.full_install_action('123')[1])
                for block in (b'plugins:\n  enabled: false\n',
                              b'plugins:\n  enabled: true\n  configs:\n    fast-mode-policy:\n      enabled: false\n'):
                    config.write_bytes(CONFIG + block)
                    self.assertEqual(installer.full_install_action(123)[0], 'disabled')
                self.assertEqual(config.read_bytes(), CONFIG + block)
                self.assertEqual(json.loads(status.read_bytes()), {'pid': 123, 'version': '0.2.0'})

    def test_full_install_action_uses_configured_status_path_and_rejects_ambiguous_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yaml'
            status = Path(directory) / 'custom-status.json'
            config.write_bytes(CONFIG + ('plugins:\n  configs:\n    fast-mode-policy:\n      status_path: ' + str(status) + '\n').encode())
            status.write_text('{"disabled": true}')
            with patch.object(installer, 'CONFIG', config):
                self.assertEqual(installer.full_install_action(123)[0], 'disabled')
                config.write_bytes(CONFIG + b'plugins: []\n')
                with self.assertRaises(ValueError):
                    installer.full_install_action(123)

    def test_enable_preserves_every_other_setting_and_bytes(self):
        candidate = installer.configuration_candidate(CONFIG)
        self.assertTrue(candidate.startswith(CONFIG))
        before, after = yaml.safe_load(CONFIG), yaml.safe_load(candidate)
        plugins = after.pop('plugins')
        self.assertEqual(after, before)
        self.assertTrue(plugins['enabled'])
        self.assertEqual(plugins['dir'], str(installer.PLUGIN_DIR))
        item = plugins['configs']['fast-mode-policy']
        self.assertEqual(item, {'enabled': True, 'priority': 100,
                               'policy_path': installer.POLICY, 'status_path': str(installer.STATUS),
                               'map_unsupported_ultrafast': True, 'audit': False,
                               'store': {'version': installer.VERSION}})

    def test_existing_plugins_disable_and_idempotence(self):
        existing = CONFIG + b'''plugins:
  enabled: true
  dir: /opt/personal-codex-router/plugins
  configs:
    other:
      enabled: true
      secret: TEST-PLUGIN-SECRET
      custom: [1, 2]
# outside plugin block
other-setting: true
'''
        enabled = installer.configuration_candidate(existing)
        self.assertIn(b'# outside plugin block\nother-setting: true', enabled)
        before, after = yaml.safe_load(existing), yaml.safe_load(enabled)
        self.assertEqual(after['plugins']['configs']['other'], before['plugins']['configs']['other'])
        self.assertEqual(installer.configuration_candidate(enabled), enabled)
        disabled = installer.configuration_candidate(enabled, disable=True)
        expected = yaml.safe_load(enabled)
        expected['plugins']['configs']['fast-mode-policy']['enabled'] = False
        self.assertEqual(yaml.safe_load(disabled), expected)
        self.assertNotIn('TEST-PLUGIN-SECRET', installer.public_diff(existing, enabled))

    def test_dry_run_temp_config_only_prints_public_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_bytes(CONFIG)
            before = path.stat()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(installer, 'main_pid', side_effect=AssertionError('no systemd')), patch.object(installer, 'install_library', side_effect=AssertionError('no install')):
                installer.main(['--dry-run', '--config', str(path)])
            self.assertEqual(path.read_bytes(), CONFIG)
            self.assertEqual(path.stat().st_ino, before.st_ino)
            self.assertIn('+', output.getvalue())
            self.assertIn('map_unsupported_ultrafast: true', output.getvalue())
            self.assertNotIn('TEST-SECRET', output.getvalue())
            enabled = installer.configuration_candidate(CONFIG)
            path.write_bytes(enabled)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                installer.main(['--dry-run', '--disable', '--config', str(path)])
            self.assertIn('+      enabled: false', output.getvalue())
            self.assertEqual(path.read_bytes(), enabled)

    def test_rejects_duplicates_aliases_bad_layout_and_directory_move(self):
        for data in (CONFIG + b'plugins: {}\nplugins: {}\n',
                     CONFIG + b'x: &a {secret: hidden}\ny: *a\n',
                     b'host: 127.0.0.1\nport: 32123\n',
                     CONFIG + b'plugins: []\n',
                     CONFIG + b'plugins: {dir: /tmp/other, configs: {other: {enabled: true}}}\n'):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    installer.configuration_candidate(data)
        with self.assertRaises(ValueError):
            installer.configuration_candidate(CONFIG, disable=True)

    def test_same_inode_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_bytes(CONFIG)
            path.chmod(0o640)
            identity = installer.metadata(path)
            candidate = installer.configuration_candidate(CONFIG)
            installer.gateway_config.write_same_inode(path, candidate, CONFIG)
            self.assertEqual(installer.metadata(path), identity)
            with self.assertRaises(ValueError):
                installer.gateway_config.write_same_inode(path, CONFIG, CONFIG)
            self.assertEqual(path.read_bytes(), candidate)

    def test_installer_preflight_resolves_real_library_path(self):
        cfg = yaml.safe_load(installer.configuration_candidate(CONFIG))
        seen = []
        with patch.object(installer, 'installer_access', side_effect=lambda path, **_: seen.append(path)):
            installer.installer_preflight(cfg)
        self.assertIn(installer.PLUGIN_DIR / 'linux/amd64' / f'fast-mode-policy-v{installer.VERSION}.so', seen)

    def runtime_fixture(self, directory):
        path, status = Path(directory) / 'config.yaml', Path(directory) / 'status.json'
        cfg = yaml.safe_load(installer.configuration_candidate(CONFIG))
        cfg['plugins']['configs']['fast-mode-policy']['status_path'] = str(status)
        candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
        marker = {'plugin': 'fast-mode-policy', 'pid': 999, 'version': installer.VERSION,
                  'loaded_at': 'old', 'policy_path': installer.POLICY,
                  'map_unsupported_ultrafast': True, 'audit': False}
        return path, status, cfg, candidate, marker

    def reload_offline(self, path, original, candidate, journal, timeout=.05):
        snapshot = ([], {}, 'success')
        with patch.object(installer, 'main_pid', return_value=999), \
                patch.object(installer, 'journal_cursor', return_value='cursor'), \
                patch.object(installer, 'command', side_effect=journal), \
                patch.object(installer, 'runtime_snapshot', return_value=snapshot), \
                patch.object(installer.time, 'sleep'):
            return installer.verified_reload(path, original, candidate, 999, snapshot, timeout=timeout)

    def host_log(self, cfg, replacement=False):
        directory, version = installer.library_selection(cfg)
        if replacement:
            return ('[host.go:408] pluginhost: plugin hot reloaded plugin_id=fast-mode-policy '
                    f'active_version={version} active_path={directory}/linux/amd64/fast-mode-policy-v{version}.so')
        return ('[host.go:381] pluginhost: plugin registered plugin_id=fast-mode-policy '
                f'version={version} path={directory}/linux/amd64/fast-mode-policy-v{version}.so')

    def test_first_load_waits_for_matching_registration_and_fresh_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status, cfg, candidate, marker = self.runtime_fixture(directory)
            path.write_bytes(CONFIG)
            identity = installer.metadata(path)
            polls = []
            def journal(*args):
                self.assertIn('--after-cursor=cursor', args)
                self.assertIn('_PID=999', args)
                polls.append(args)
                status.write_text(json.dumps(marker))
                log = self.host_log(cfg)
                if len(polls) == 1:
                    log = log.replace('plugin_id=fast-mode-policy', 'plugin_id=other')
                if len(polls) == 2:
                    log = log.replace(f'version={installer.VERSION}', 'version=9.9.9')
                return 'config successfully reloaded\n' + log
            self.assertEqual(self.reload_offline(path, CONFIG, candidate, journal, timeout=1), 0)
            self.assertEqual(len(polls), 3)
            self.assertEqual(installer.metadata(path), identity)
            self.assertEqual(path.read_bytes(), candidate)

    def test_first_load_status_alone_cannot_confirm_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status, cfg, candidate, marker = self.runtime_fixture(directory)
            path.write_bytes(CONFIG)
            def journal(*args):
                status.write_text(json.dumps(marker))
                return 'config successfully reloaded'
            with self.assertRaises(installer.RuntimeConfirmationError):
                self.reload_offline(path, CONFIG, candidate, journal)

    def test_enabled_config_change_requires_fresh_current_pid_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status, cfg, original, marker = self.runtime_fixture(directory)
            path.write_bytes(original)
            status.write_text(json.dumps(marker))
            item = cfg['plugins']['configs']['fast-mode-policy']
            item['audit'] = True
            candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
            polls = []
            def journal(*args):
                polls.append(args)
                status.write_text(json.dumps({**marker, 'audit': True, 'loaded_at': 'new'}))
                return 'config successfully reloaded'
            self.assertEqual(self.reload_offline(path, original, candidate, journal), 0)
            self.assertEqual(len(polls), 1)
            for change in ({'pid': 123}, {'version': '9.9.9'}, {'disabled': True},
                           {'policy_path': '/tmp/wrong'}, {'map_unsupported_ultrafast': False},
                           {'audit': False}, {'loaded_at': ''}):
                status.write_text(json.dumps({**marker, 'audit': True, **change}))
                self.assertFalse(installer.verify_status(999, item))
            status.write_text(json.dumps(marker))
            self.assertFalse(installer.verify_status(999, item, marker))

    def test_same_config_change_never_accepts_unchanged_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status, cfg, original, marker = self.runtime_fixture(directory)
            path.write_bytes(original)
            status.write_text(json.dumps(marker))
            cfg['plugins']['configs']['fast-mode-policy']['priority'] += 1
            candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
            with self.assertRaises(installer.RuntimeConfirmationError):
                self.reload_offline(path, original, candidate, lambda *args: 'config successfully reloaded')

    def test_status_path_change_uses_old_marker_for_library_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status, cfg, original, marker = self.runtime_fixture(directory)
            path.write_bytes(original); status.write_text(json.dumps(marker))
            new_status = Path(directory) / 'new-status.json'
            cfg['plugins']['configs']['fast-mode-policy']['status_path'] = str(new_status)
            candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
            def journal(*args):
                new_status.write_text(json.dumps({**marker, 'loaded_at': 'new'}))
                return 'config successfully reloaded'
            self.assertEqual(self.reload_offline(path, original, candidate, journal), 0)

    def test_version_update_and_enabled_rollback_require_post_swap_log(self):
        for before, after in (('0.1.1', '0.1.2'), ('0.1.2', '0.1.1')):
            with self.subTest(before=before, after=after), tempfile.TemporaryDirectory() as directory:
                path, status, cfg, _, marker = self.runtime_fixture(directory)
                cfg['plugins']['configs']['fast-mode-policy']['store']['version'] = before
                original = installer.replace_plugins(CONFIG, cfg['plugins'])
                path.write_bytes(original)
                status.write_text(json.dumps({**marker, 'version': before}))
                cfg['plugins']['configs']['fast-mode-policy']['store']['version'] = after
                candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
                polls = []
                def journal(*args):
                    polls.append(args)
                    status.write_text(json.dumps({**marker, 'version': after, 'loaded_at': 'new'}))
                    # A pre-swap registration must not confirm version replacement.
                    log = self.host_log(cfg, replacement=len(polls) > 1)
                    return 'config successfully reloaded\n' + log
                self.assertEqual(self.reload_offline(path, original, candidate, journal, timeout=1), 0)
                self.assertEqual(len(polls), 2)

    def test_disable_marks_status_and_returns_unconfirmed(self):
        for scope in ('individual', 'global', 'unchanged', 'removed'):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as directory:
                path, status, cfg, original, marker = self.runtime_fixture(directory)
                if scope == 'global':
                    cfg['plugins']['enabled'] = False
                else:
                    cfg['plugins']['configs']['fast-mode-policy']['enabled'] = False
                candidate = installer.replace_plugins(CONFIG, None if scope == 'removed' else cfg['plugins'])
                if scope == 'unchanged':
                    original = candidate
                path.write_bytes(original)
                status.write_text(json.dumps(marker)); status.chmod(0o640)
                output = io.StringIO()
                with patch.object(installer, 'STATUS', status), contextlib.redirect_stdout(output):
                    result = self.reload_offline(path, original, candidate, lambda *args: 'config successfully reloaded')
                self.assertEqual(result, 3)
                self.assertTrue(json.loads(status.read_text())['disabled'])
                self.assertEqual(status.stat().st_mode & 0o777, 0o640)
                self.assertIn('no keyless runtime confirmation', output.getvalue())
                self.assertIn('runtime deactivation is unconfirmed', output.getvalue())
                self.assertIn('config was unchanged' if scope == 'unchanged' else 'reload was logged', output.getvalue())

    def test_disable_missing_or_malformed_marker_is_marked_disabled(self):
        for existing in (None, 'invalid', '[]'):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                path, status, cfg, original, marker = self.runtime_fixture(directory)
                path.write_bytes(original)
                if existing is not None:
                    status.write_text(existing)
                candidate = installer.configuration_candidate(original, disable=True)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.reload_offline(path, original, candidate, lambda *args: 'config successfully reloaded'), 3)
                self.assertTrue(json.loads(status.read_text())['disabled'])

    def test_disable_ignores_obsolete_directory_and_version_in_runtime_history(self):
        for directory in (None, 'plugins'):
            for version in (installer.VERSION, 'unused'):
                with self.subTest(directory=directory, version=version), tempfile.TemporaryDirectory() as root:
                    path, status, cfg, _, marker = self.runtime_fixture(root)
                    if directory is None:
                        cfg['plugins'].pop('dir')
                    else:
                        cfg['plugins']['dir'] = directory
                    cfg['plugins']['configs']['fast-mode-policy']['store']['version'] = version
                    original = installer.replace_plugins(CONFIG, cfg['plugins'])
                    candidate = installer.configuration_candidate(original, disable=True)
                    path.write_bytes(original)
                    status.write_text(json.dumps({**marker, 'version': version}))
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(self.reload_offline(path, original, candidate,
                            lambda *args: 'config successfully reloaded'), 3)
                    self.assertEqual(path.read_bytes(), candidate)
                    self.assertTrue(json.loads(status.read_text())['disabled'])

    def test_enabled_path_switch_does_not_resolve_relative_history_against_cwd(self):
        with tempfile.TemporaryDirectory() as root:
            path, status, cfg, _, marker = self.runtime_fixture(root)
            cfg['plugins']['dir'] = 'plugins'
            original = installer.replace_plugins(CONFIG, cfg['plugins'])
            path.write_bytes(original)
            status.write_text(json.dumps(marker))
            cfg['plugins']['dir'] = str(installer.PLUGIN_DIR)
            candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
            def journal(*args):
                status.write_text(json.dumps({**marker, 'loaded_at': 'new'}))
                return 'config successfully reloaded\n' + self.host_log(cfg, replacement=True)
            self.assertEqual(self.reload_offline(path, original, candidate, journal), 0)

    def test_disable_reload_timeout_keeps_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status, cfg, original, marker = self.runtime_fixture(directory)
            path.write_bytes(original)
            status.write_text(json.dumps(marker))
            before = status.read_bytes()
            candidate = installer.configuration_candidate(original, disable=True)
            with self.assertRaises(installer.RuntimeConfirmationError):
                self.reload_offline(path, original, candidate, lambda *args: '')
            self.assertEqual(status.read_bytes(), before)

    def test_reenable_retained_library_requires_status_without_new_load_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status, cfg, candidate, marker = self.runtime_fixture(directory)
            original = installer.configuration_candidate(candidate, disable=True)
            path.write_bytes(original)
            status.write_text(json.dumps({**marker, 'disabled': True}))
            def journal(*args):
                status.write_text(json.dumps({**marker, 'loaded_at': 'new'}))
                return 'config successfully reloaded'
            self.assertEqual(self.reload_offline(path, original, candidate, journal), 0)

    def test_main_disable_and_explicit_rollback_return_three_without_success_claim(self):
        for rollback in (False, True):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory() as directory:
                path, status, cfg, original, marker = self.runtime_fixture(directory)
                path.write_bytes(original); status.write_text(json.dumps(marker))
                disabled = installer.configuration_candidate(original, disable=True)
                backup = Path(directory) / 'backup'; backup.mkdir()
                (backup / 'config.before.yaml').write_bytes(disabled)
                (backup / 'manifest.json').write_text(json.dumps({'before_sha256': installer.digest(disabled), 'candidate_sha256': installer.digest(original)}))
                output = io.StringIO()
                with patch.object(installer, 'CONFIG', path), patch.object(installer, 'STATUS', status), \
                        patch.object(installer.os, 'geteuid', return_value=0), patch.object(installer.os, 'umask'), \
                        patch.object(installer, 'main_pid', return_value=999), patch.object(installer, 'preflight'), \
                        patch.object(installer, 'installer_preflight'), \
                        patch.object(installer, 'backup_config', return_value=backup), \
                        patch.object(installer, 'runtime_snapshot', return_value=([], {}, 'success')), \
                        patch.object(installer, 'journal_cursor', return_value='cursor'), \
                        patch.object(installer, 'command', return_value='config successfully reloaded'), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(installer.main(['--rollback', str(backup)] if rollback else ['--disable']), 3)
                self.assertNotIn('Verified hot reload', output.getvalue())
                self.assertTrue(json.loads(status.read_text())['disabled'])

    def test_automatic_rollback_to_disabled_is_unconfirmed_and_reload_failure_retains_marker(self):
        for rollback_reloads in (True, False):
            with self.subTest(rollback_reloads=rollback_reloads), tempfile.TemporaryDirectory() as directory:
                path, status, cfg, _, marker = self.runtime_fixture(directory)
                path.write_bytes(CONFIG); status.write_text(json.dumps(marker))
                before = status.read_bytes()
                actual_reload = installer.verified_reload
                def reload(*args):
                    return actual_reload(*args, timeout=.01)
                def journal(*args):
                    return 'config successfully reloaded' if rollback_reloads else ''
                output = io.StringIO()
                with patch.object(installer, 'CONFIG', path), patch.object(installer, 'STATUS', status), \
                        patch.object(installer.os, 'geteuid', return_value=0), patch.object(installer.os, 'umask'), \
                        patch.object(installer, 'main_pid', return_value=999), patch.object(installer, 'preflight'), \
                        patch.object(installer, 'installer_preflight'), \
                        patch.object(installer, 'backup_config', return_value=Path(directory) / 'backup'), \
                        patch.object(installer, 'install_library'), \
                        patch.object(installer, 'runtime_snapshot', return_value=([], {}, 'success')), \
                        patch.object(installer, 'journal_cursor', return_value='cursor'), \
                        patch.object(installer, 'command', side_effect=journal), \
                        patch.object(installer, 'verified_reload', side_effect=reload), \
                        patch.object(installer.time, 'sleep'), contextlib.redirect_stdout(output):
                    if rollback_reloads:
                        self.assertEqual(installer.main(['--library', '/tmp/not-used.so']), 3)
                    else:
                        with self.assertRaisesRegex(installer.RuntimeConfirmationError, 'rollback runtime confirmation failed'):
                            installer.main(['--library', '/tmp/not-used.so'])
                self.assertEqual(path.read_bytes(), CONFIG)
                if rollback_reloads:
                    self.assertTrue(json.loads(status.read_text())['disabled'])
                    self.assertIn('Deployment failed', output.getvalue())
                else:
                    self.assertEqual(status.read_bytes(), before)

    def test_help_documents_unconfirmed_disable_and_rollback(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error:
            installer.main(['--help'])
        self.assertEqual(error.exception.code, 0)
        self.assertIn('exits 3', output.getvalue())
        self.assertIn('runtime deactivation unconfirmed', ' '.join(output.getvalue().split()))

    def test_same_version_path_switch_waits_for_matching_post_swap_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path, status = Path(directory) / 'config.yaml', Path(directory) / 'status.json'
            cfg = yaml.safe_load(installer.configuration_candidate(CONFIG))
            item = cfg['plugins']['configs']['fast-mode-policy']
            item['status_path'] = str(status)
            cfg['plugins']['dir'] = '/tmp/old-plugin'
            original = installer.replace_plugins(CONFIG, cfg['plugins'])
            cfg['plugins']['dir'] = '/tmp/new-plugin'
            candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
            path.write_bytes(original)
            marker = {'plugin': 'fast-mode-policy', 'pid': 999, 'version': installer.VERSION,
                      'loaded_at': 'old', 'policy_path': installer.POLICY,
                      'map_unsupported_ultrafast': True, 'audit': False}
            status.write_text(json.dumps(marker))
            prefix = '[host.go:408] pluginhost: plugin hot reloaded '
            suffix = (f'plugin_id=fast-mode-policy active_version={installer.VERSION} '
                      f'active_path=/tmp/new-plugin/linux/amd64/fast-mode-policy-v{installer.VERSION}.so')
            polls = []

            def journal(*args):
                polls.append(args)
                status.write_text(json.dumps({**marker, 'loaded_at': 'new'}))
                if len(polls) == 1:
                    # Other plugin/version/path logs cannot confirm our switch.
                    return 'config successfully reloaded\n' + prefix + suffix.replace('plugin_id=fast-mode-policy', 'plugin_id=other')
                if len(polls) == 2:
                    return 'config successfully reloaded\n' + prefix + suffix.replace('/tmp/new-plugin/', '/tmp/old-plugin/')
                return 'config successfully reloaded\n' + prefix + suffix

            snapshot = ([], {}, 'success')
            with patch.object(installer, 'main_pid', return_value=999), \
                    patch.object(installer, 'journal_cursor', return_value='cursor'), \
                    patch.object(installer, 'command', side_effect=journal), \
                    patch.object(installer, 'runtime_snapshot', return_value=snapshot), \
                    patch.object(installer.time, 'sleep'):
                installer.verified_reload(path, original, candidate, 999, snapshot, timeout=1)
            self.assertEqual(len(polls), 3)
            for log in (prefix + suffix.replace(f'active_version={installer.VERSION}', 'active_version=0.1.1'),
                        prefix.replace('plugin hot reloaded', 'plugin unloaded') + suffix, prefix + 'active_path="unfinished'):
                self.assertFalse(installer.verify_host_event(log, cfg, replacement=True))


    def test_host_event_matches_message_across_gateway_source_locations(self):
        cfg = yaml.safe_load(installer.configuration_candidate(CONFIG))
        cfg['plugins']['dir'] = '/opt/test-plugins'
        for replacement, event, version_key, path_key, lines in (
                (False, 'plugin registered', 'version', 'path', (381, 383, 999)),
                (True, 'plugin hot reloaded', 'active_version', 'active_path', (408, 410, 999))):
            suffix = (f'plugin_id=fast-mode-policy {version_key}={installer.VERSION} '
                      f'{path_key}=/opt/test-plugins/linux/amd64/fast-mode-policy-v{installer.VERSION}.so')
            for line in lines:
                with self.subTest(replacement=replacement, line=line):
                    log = f'[host.go:{line}] pluginhost: {event} {suffix}'
                    self.assertTrue(installer.verify_host_event(log, cfg, replacement=replacement))
                    self.assertFalse(installer.verify_host_event(log, cfg, replacement=not replacement))
                    self.assertFalse(installer.verify_host_event(
                        log.replace('plugin_id=fast-mode-policy', 'plugin_id=other'), cfg, replacement=replacement))
                    self.assertFalse(installer.verify_host_event(
                        log.replace('/opt/test-plugins/', '/opt/other/'), cfg, replacement=replacement))

    def test_library_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / 'input.so'
            elf = b'\x7fELF\x02\x01' + b'\0' * 10 + b'\x03\x00\x3e\x00' + b'first'
            source.write_bytes(elf)
            destination = installer.install_library(source, directory, root_owned=False)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            identity = destination.stat().st_ino
            installer.install_library(source, directory, root_owned=False)
            self.assertEqual(destination.stat().st_ino, identity)
            source.write_bytes(elf + b'different')
            with self.assertRaises(ValueError):
                installer.install_library(source, directory, root_owned=False)
            self.assertEqual(destination.read_bytes(), elf)
            source.write_bytes(b'wrong architecture')
            with self.assertRaises(ValueError):
                installer.install_library(source, directory, root_owned=False)

    def test_rollback_drift_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            path = directory / 'config.yaml'
            candidate = installer.configuration_candidate(CONFIG)
            path.write_bytes(candidate)
            backup = directory / 'backup'
            backup.mkdir()
            (backup / 'config.before.yaml').write_bytes(CONFIG)
            (backup / 'manifest.json').write_text(json.dumps({'before_sha256': installer.digest(CONFIG), 'candidate_sha256': installer.digest(candidate)}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                installer.main(['--dry-run', '--config', str(path), '--rollback', str(backup)])
            self.assertNotIn('TEST-SECRET', output.getvalue())
            path.write_bytes(candidate + b'# concurrent edit\n')
            with self.assertRaises(ValueError):
                installer.main(['--dry-run', '--config', str(path), '--rollback', str(backup)])



class SandboxPreflightTests(unittest.TestCase):
    def setUp(self):
        # Effective properties from the deployed units, with no live systemd use.
        self.gateway = dict.fromkeys(installer.SANDBOX_PROPERTIES, '')
        self.gateway.update(ProtectSystem='strict', StateDirectory='cliproxyapi',
                            User='cliproxyapi', Group='cliproxyapi', ProtectHome='yes',
                            PrivateTmp='yes', ProtectProc='default', ProcSubset='all', DynamicUser='no')
        self.dashboard = {**self.gateway, 'StateDirectory': '',
                          'ReadOnlyPaths': '/etc/cliproxyapi',
                          'ReadWritePaths': '/var/lib/personal-codex-router /var/lib/personal-codex-router-routing'}

    def test_effective_properties_are_required_and_parsed_offline(self):
        output = '\n'.join(f'{key}={value}' for key, value in self.gateway.items())
        with patch.object(installer, 'command', return_value=output) as command:
            self.assertEqual(installer.unit_sandbox('cliproxyapi'), self.gateway)
        self.assertIn('ProtectSystem', command.call_args.args[-1])
        self.assertIn('User', command.call_args.args[-1])
        with patch.object(installer, 'command', return_value='ProtectSystem=strict'):
            with self.assertRaisesRegex(installer.SandboxError, 'cannot inspect'):
                installer.unit_sandbox('cliproxyapi')

    def test_service_identity_resolves_effective_user_group_and_supplementary_groups(self):
        import grp
        user = SimpleNamespace(pw_name='cliproxyapi', pw_uid=4242, pw_gid=4242)
        group = SimpleNamespace(gr_gid=4243)
        facts = {**self.gateway, 'Group': 'shared', 'SupplementaryGroups': '4244 extra'}
        with patch.object(installer.pwd, 'getpwnam', return_value=user) as lookup, \
                patch.object(grp, 'getgrnam', side_effect=[group, SimpleNamespace(gr_gid=4245)]), \
                patch.object(installer.os, 'getgrouplist', return_value=[4243, 4246]) as groups:
            self.assertEqual(installer.service_identity(facts), (4242, {4243, 4244, 4245, 4246}, 4243))
        lookup.assert_called_once_with('cliproxyapi')
        groups.assert_called_once_with('cliproxyapi', 4243)

    def test_real_sandboxes_allow_new_status_and_reject_old_status(self):
        installer.sandbox_access('gateway', self.gateway, installer.STATUS, write=True)
        installer.sandbox_access('dashboard', self.dashboard, installer.STATUS)
        installer.sandbox_access('gateway', self.gateway, Path(installer.POLICY))
        installer.sandbox_access('dashboard', self.dashboard, Path(installer.POLICY), write=True)
        installer.sandbox_access('gateway', self.gateway, installer.PLUGIN_DIR / 'linux/amd64/plugin.so')
        with self.assertRaisesRegex(installer.SandboxError, 'cannot write'):
            installer.sandbox_access('gateway', self.gateway,
                                     Path('/var/lib/personal-codex-router/fast-mode-plugin-status.json'), write=True)
        with self.assertRaisesRegex(installer.SandboxError, 'cannot write'):
            installer.sandbox_access('dashboard', self.dashboard, installer.STATUS, write=True)

    def test_sibling_namespace_restrictions_and_overrides(self):
        cases = [
            (self.gateway, 'InaccessiblePaths', '-/opt/personal-codex-router', installer.PLUGIN_DIR, False),
            (self.gateway, 'InaccessiblePaths', '/var/lib/personal-codex-router', Path(installer.POLICY), False),
            (self.dashboard, 'InaccessiblePaths', '/var/lib/cliproxyapi', installer.STATUS, False),
            (self.gateway, 'ReadOnlyPaths', str(installer.STATUS.parent), installer.STATUS, True),
            (self.dashboard, 'ReadOnlyPaths', str(Path(installer.POLICY).parent / 'fast-mode-policy.json'), Path(installer.POLICY), True),
            (self.gateway, 'StateDirectory', '', installer.STATUS, True),
            (self.gateway, 'StateDirectory', 'cliproxyapi::ro', installer.STATUS, True),
            (self.dashboard, 'ReadWritePaths', '', Path(installer.POLICY), True),
            (self.dashboard, 'InaccessiblePaths', '/proc', Path('/proc/999/cmdline'), False),
            (self.dashboard, 'InaccessiblePaths', '/run/dbus', Path('/run/dbus/system_bus_socket'), False),
            (self.gateway, 'ProtectHome', 'yes', Path('/home/user/plugin.so'), False),
            (self.dashboard, 'PrivateTmp', 'yes', Path('/tmp/policy.json'), False),
            (self.gateway, 'RootDirectory', '/alternate', installer.STATUS, False),
        ]
        for base, key, value, path, write in cases:
            with self.subTest(key=key, value=value, path=path), self.assertRaises(installer.SandboxError):
                installer.sandbox_access('test-unit', {**base, key:value}, path, write=write)
        facts = {**self.gateway, 'ReadOnlyPaths':'/var/lib', 'ReadWritePaths':'/var/lib/shared'}
        installer.sandbox_access('gateway', facts, Path('/var/lib/shared/status.json'), write=True)
        self.assertEqual(installer.sandbox_paths({'StateDirectory':'cliproxyapi:alias'}, 'StateDirectory'),
                         [Path('/var/lib/cliproxyapi')])

    def test_preflight_checks_all_feature_paths_and_proc_visibility(self):
        config = yaml.safe_load(installer.configuration_candidate(CONFIG))
        def facts(unit):
            return self.gateway if unit == 'cliproxyapi' else self.dashboard
        with patch.object(installer, 'unit_sandbox', side_effect=facts), \
                patch.object(installer, 'service_identity', return_value=(1001, {1001})), \
                patch.object(installer, 'filesystem_access') as access, \
                patch.object(installer, 'installer_access') as install:
            installer.preflight(config, 999)
            paths = {call.args[0] for call in access.call_args_list}
            self.assertTrue({installer.STATUS, Path(installer.POLICY),
                Path('/var/lib/personal-codex-router/fast-mode-controls.json'),
                installer.PLUGIN_DIR / f'linux/amd64/fast-mode-policy-v{installer.VERSION}.so',
                Path('/proc/999/cmdline'), Path('/run/dbus/system_bus_socket')} <= paths)
            self.assertIn(installer.CONFIG, {call.args[0] for call in install.call_args_list})
            self.dashboard['ProcSubset'] = 'unsupported'
            with self.assertRaisesRegex(installer.SandboxError, 'cannot inspect the gateway'):
                installer.preflight(config, 999)
            self.dashboard['ProcSubset'] = 'all'
            self.dashboard['ProtectProc'] = 'invisible'
            with patch.object(installer, 'service_identity', side_effect=[(1001,{1001}), (1002,{1002})]):
                with self.assertRaisesRegex(installer.SandboxError, 'cannot inspect the gateway'):
                    installer.preflight(config, 999)

    def test_disabled_marker_does_not_require_gateway_write_access(self):
        config = yaml.safe_load(installer.configuration_candidate(
            installer.configuration_candidate(CONFIG), disable=True))
        config['plugins']['configs']['fast-mode-policy']['status_path'] = '/var/lib/personal-codex-router/fast-mode-plugin-status.json'
        with patch.object(installer, 'unit_sandbox', side_effect=[self.gateway,self.dashboard]), \
                patch.object(installer, 'service_identity', return_value=(1001,{1001})), \
                patch.object(installer, 'filesystem_access'), \
                patch.object(installer, 'installer_access'):
            installer.preflight(config, 999)

    def test_preflight_leaves_existing_policy_bytes_and_metadata_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / 'policy.json'
            contents = b'{"version":2,"accounts":{},"files":{},"updated_at":"2026-01-01T00:00:00Z"}\n'
            policy.write_bytes(contents)
            policy.chmod(0o640)
            before = policy.stat()
            config = yaml.safe_load(installer.configuration_candidate(CONFIG))
            config['plugins']['configs']['fast-mode-policy']['policy_path'] = str(policy)
            facts = {**self.gateway, 'PrivateTmp': 'no', 'ProtectSystem': 'no'}
            identity = (os.getuid(), set(os.getgroups()) | {os.getgid()}, os.getgid())
            access = installer.filesystem_access
            with patch.object(installer, 'unit_sandbox', return_value=facts), \
                    patch.object(installer, 'service_identity', return_value=identity), \
                    patch.object(installer, 'installer_access'), \
                    patch.object(installer, 'filesystem_access', side_effect=lambda path, *args, **kwargs:
                        access(path, *args, **kwargs) if path == policy else None):
                installer.preflight(config, 999)
            self.assertEqual(policy.read_bytes(), contents)
            after = policy.stat()
            for field in ('st_ino', 'st_mtime_ns', 'st_mode', 'st_uid', 'st_gid'):
                self.assertEqual(getattr(after, field), getattr(before, field))

    def test_modes_missing_state_and_links_are_checked_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            path = root / 'state.json'
            identity = (os.getuid(), set(os.getgroups()) | {os.getgid()})
            installer.filesystem_access(path, identity, write=True, future=(identity,0o600))
            installer.filesystem_access(path, identity, future=(identity,0o600))
            other = (identity[0]+10000, {10000})
            with self.assertRaisesRegex(installer.SandboxError, 'future file'):
                installer.filesystem_access(path, other, future=(identity,0o600))
            path.write_text('fixture')
            path.chmod(0o600)
            with self.assertRaisesRegex(installer.SandboxError, 'cannot read'):
                installer.filesystem_access(path, other)
            with self.assertRaisesRegex(installer.SandboxError, 'system bus socket is missing'):
                installer.filesystem_access(root/'missing-bus', identity, socket=True)
            with self.assertRaisesRegex(installer.SandboxError, 'must already exist'):
                installer.filesystem_access(root/'missing'/'policy.json', identity, write=True, future=(identity,0o640))
            with self.assertRaisesRegex(installer.SandboxError, 'expected a state file'):
                installer.filesystem_access(root, identity, write=True)
            link = root / 'link'
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(installer.SandboxError, 'symlink'):
                installer.filesystem_access(link/'state.json', identity)
            with self.assertRaisesRegex(installer.SandboxError, 'unsafe installer'):
                installer.installer_access(link/'library.so', protected=False)

    def test_access_rejection_follows_install_but_precedes_config_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yaml'
            path.write_bytes(CONFIG)
            with patch.object(installer, 'CONFIG', path), patch.object(installer.os, 'geteuid', return_value=0), \
                    patch.object(installer, 'main_pid', return_value=999), \
                    patch.object(installer, 'installer_preflight'), \
                    patch.object(installer, 'runtime_snapshot'), \
                    patch.object(installer, 'repair_status'), \
                    patch.object(installer, 'preflight', side_effect=installer.SandboxError('Preflight: unusable status path')),  \
                    patch.object(installer, 'backup_config') as backup, \
                    patch.object(installer, 'install_library') as install, \
                    patch.object(installer, 'run_build') as build, \
                    patch.object(installer, 'verified_reload') as reload:
                with self.assertRaisesRegex(installer.SandboxError, 'unusable status path'):
                    installer.main(['--library', '/tmp/not-used.so'])
                backup.assert_called_once()
                install.assert_called_once()
                build.assert_not_called()
                reload.assert_not_called()
            self.assertEqual(path.read_bytes(), CONFIG)



class CandidatePreflightTests(unittest.TestCase):
    """Real preflights, namespace parsing and DAC against an offline filesystem."""

    def setUp(self):
        self.files = {}
        self.accessed = []
        self.denied = set()
        self.facts = {}
        for unit, uid in (('cliproxyapi', 4242), ('personal-codex-router-dashboard', 4243)):
            facts = dict.fromkeys(installer.SANDBOX_PROPERTIES, '')
            facts.update(User=str(uid), Group=str(uid), ProtectSystem='strict',
                         ProtectHome='yes', PrivateTmp='yes', ProtectProc='default',
                         ProcSubset='all', DynamicUser='no')
            facts['StateDirectory'] = 'cliproxyapi' if uid == 4242 else ''
            facts['ReadWritePaths'] = '' if uid == 4242 else '/var/lib/personal-codex-router'
            self.facts[unit] = facts
        # Atomic dashboard publications must use a gateway-readable primary group.
        self.facts['personal-codex-router-dashboard']['Group'] = '4242'
        self.add_file(installer.CONFIG)
        self.add_file(Path(installer.POLICY), uid=4243, gid=4242)
        self.add_file(installer.STATUS, uid=4242, gid=4242)
        self.add_file(Path('/var/lib/personal-codex-router/fast-mode-controls.json'),
                      uid=4243, gid=4243, mode=0o600)
        self.files[installer.STATUS.parent] = self.info(stat.S_IFDIR | 0o750, 4242, 4242)
        self.files[Path(installer.POLICY).parent] = self.info(stat.S_IFDIR | 0o770, 4243, 4242)
        self.add_file(Path('/proc/999/cmdline'), mode=0o444)
        self.add_file(Path('/run/dbus/system_bus_socket'), mode=0o666, kind=stat.S_IFSOCK)
        for version in ('0.1.1', installer.VERSION):
            self.add_file(installer.PLUGIN_DIR / 'linux/amd64' / f'fast-mode-policy-v{version}.so', mode=0o644)
        # A real sudo invocation starts in a user-owned checkout. No relative
        # plugin path may traverse it, even if an unrelated plugins dir exists.
        self.files[Path('.')] = self.info(stat.S_IFDIR | 0o777, 1000, 1000)
        self.add_file(Path('plugins/linux/amd64') / f'fast-mode-policy-v{installer.VERSION}.so')
        self.raw_stat, self.raw_lstat = os.stat, os.lstat
        self.real_root = None
        def stat_path(path, **kwargs):
            path = Path(path)
            self.accessed.append(path)
            if self.real_root is not None and installer.contains(self.real_root, path):
                return self.raw_stat(path, **kwargs)
            if path not in self.files:
                raise FileNotFoundError(str(path))
            return self.files[path]
        def lstat_path(path, **kwargs):
            if self.real_root is not None and installer.contains(self.real_root, Path(path)):
                return self.raw_lstat(path, **kwargs)
            return stat_path(path, **kwargs)
        self.enterContext(patch.object(os, 'stat', stat_path))
        self.enterContext(patch.object(os, 'lstat', lstat_path))
        self.enterContext(patch.object(installer.os, 'access', side_effect=lambda path, mode: Path(path) not in self.denied))
        # Only systemd output and account lookup hooks are synthetic; the real
        # unit_sandbox, service_identity, sandbox_access and filesystem_access run.
        def command(*args):
            self.assertEqual(args[:2], ('systemctl', 'show'))
            return '\n'.join(f'{key}={value}' for key, value in self.facts[args[2]].items())
        self.enterContext(patch.object(installer, 'command', side_effect=command))
        self.enterContext(patch.object(installer.pwd, 'getpwuid', side_effect=lambda uid:
            SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_name=str(uid))))
        import grp
        self.enterContext(patch.object(grp, 'getgrgid', side_effect=lambda gid: SimpleNamespace(gr_gid=gid)))
        self.enterContext(patch.object(installer.os, 'getgrouplist', side_effect=lambda name, gid: [gid, 4242]))

    def info(self, mode, uid=0, gid=0):
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)

    def add_file(self, path, uid=0, gid=0, mode=0o640, kind=stat.S_IFREG):
        for parent in path.parents:
            self.files.setdefault(parent, self.info(stat.S_IFDIR | 0o755))
        self.files[path] = self.info(kind | mode, uid, gid)

    def enabled(self):
        return installer.configuration_candidate(CONFIG)

    def disabled(self, present=True):
        if not present:
            return CONFIG
        cfg = yaml.safe_load(installer.configuration_candidate(self.enabled(), disable=True))
        # Deactivation must ignore obsolete library and policy selectors.
        cfg['plugins']['dir'] = 'plugins'
        cfg['plugins']['configs']['fast-mode-policy']['store']['version'] = 'unused'
        return installer.replace_plugins(CONFIG, cfg['plugins'])

    def run_preflights(self, candidate, original):
        cfg, old = yaml.safe_load(candidate), yaml.safe_load(original)
        installer.installer_preflight(cfg, old)
        installer.preflight(cfg, 999, old)

    def test_account_free_install_leaves_policy_and_controls_absent(self):
        path, _, install = self.main_fixture()
        policy = Path(installer.POLICY)
        controls = policy.with_name('fast-mode-controls.json')
        self.files.pop(policy)
        self.files.pop(controls)
        before = self.files.copy()
        path.write_bytes(CONFIG)
        with contextlib.redirect_stdout(io.StringIO()):
            installer.main(['--dry-run', '--config', str(path)])
        self.assertEqual(self.files, before)
        install.assert_not_called()
        with patch.object(installer, 'verified_reload', return_value=0) as reload, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(['--library', '/tmp/offline-input.so']), 0)
        install.assert_called_once()
        reload.assert_called_once()
        self.assertEqual(self.files, before)
        # Model the dashboard's first atomic publication with the checked mode
        # and effective primary group; the gateway must then read that file.
        self.add_file(policy, uid=4243, gid=4242)
        self.run_preflights(self.enabled(), CONFIG)

    def test_missing_policy_requires_writable_directory_and_future_reader_access(self):
        policy = Path(installer.POLICY)
        self.files.pop(policy)
        parent_info = self.files[policy.parent]
        self.facts['personal-codex-router-dashboard']['Group'] = '4243'
        with patch.object(installer.os, 'getgrouplist', side_effect=lambda name, gid: [gid]):
            with self.assertRaisesRegex(installer.SandboxError, 'cannot read future file'):
                self.run_preflights(self.enabled(), CONFIG)
        self.facts['personal-codex-router-dashboard']['Group'] = '4242'
        for mode, uid, gid, error in ((0o750, 0, 4242, 'cannot create/rename'),
                                      (0o700, 4243, 4243, 'cannot traverse')):
            with self.subTest(mode=oct(mode)):
                self.files[policy.parent] = self.info(stat.S_IFDIR | mode, uid, gid)
                with self.assertRaisesRegex(installer.SandboxError, error):
                    self.run_preflights(self.enabled(), CONFIG)
        self.files[policy.parent] = parent_info

    def test_candidate_matrix_uses_only_required_paths(self):
        modes = ('enable', 'config-change', 'version-switch', 'disable',
                 'explicit-rollback-enabled', 'explicit-rollback-disabled',
                 'automatic-rollback-enabled', 'automatic-rollback-disabled')
        policy = Path(installer.POLICY)
        policy_info = self.files[policy]
        for mode in modes:
            for policy_present in (True, False):
                for plugins_present in (True, False):
                    with self.subTest(mode=mode, policy=policy_present, plugins=plugins_present):
                        self.files.pop(policy, None)
                        if policy_present:
                            self.files[policy] = policy_info
                        old = self.disabled(plugins_present)
                        if mode == 'enable':
                            candidate = installer.configuration_candidate(old)
                        elif mode == 'disable':
                            if not plugins_present:
                                with self.assertRaises(ValueError):
                                    installer.configuration_candidate(CONFIG, disable=True)
                                continue
                            old = self.enabled()
                            candidate = installer.configuration_candidate(old, disable=True)
                        elif mode in ('config-change', 'version-switch') or mode.endswith('-enabled'):
                            # An enabled config cannot lack the plugins mapping.
                            if not plugins_present:
                                continue
                            old = self.enabled()
                            cfg = yaml.safe_load(old)
                            item = cfg['plugins']['configs']['fast-mode-policy']
                            if mode == 'config-change':
                                item['audit'] = True
                            else:
                                item['store']['version'] = '0.1.1'
                            candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
                        else:
                            old, candidate = self.enabled(), self.disabled(plugins_present)
                        self.accessed.clear()
                        self.run_preflights(candidate, old)
                        self.assertTrue(all(path.is_absolute() for path in self.accessed))
                        if not installer.status_settings(yaml.safe_load(candidate))[0]:
                            self.assertNotIn(policy, self.accessed)
                            self.assertFalse(any(installer.contains(installer.PLUGIN_DIR, path)
                                                 for path in self.accessed))
                            self.assertNotIn(Path('/var/lib/personal-codex-router/fast-mode-controls.json'), self.accessed)
                            self.assertNotIn(Path('/proc/999/cmdline'), self.accessed)
                            self.assertNotIn(Path('/run/dbus/system_bus_socket'), self.accessed)
                            self.assertIn(installer.CONFIG, self.accessed)
                            self.assertIn(installer.BACKUPS.parent, self.accessed)
                            self.assertIn(installer.STATUS, self.accessed)

    def main_fixture(self):
        self.real_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config_path = self.real_root / 'config.yaml'
        backup = self.real_root / 'backup'
        backup.mkdir()
        self.enterContext(patch.object(installer, 'CONFIG', config_path))
        self.enterContext(patch.object(installer.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(installer.os, 'umask'))
        self.enterContext(patch.object(installer, 'main_pid', return_value=999))
        self.enterContext(patch.object(installer, 'runtime_snapshot', return_value=([], {}, 'success')))
        self.enterContext(patch.object(installer, 'backup_config', return_value=backup))
        install = self.enterContext(patch.object(installer, 'install_library'))
        # A missing marker exercises the real repair_status without metadata writes.
        self.files.pop(installer.STATUS)
        return config_path, backup, install

    def write_rollback(self, backup, original, candidate):
        (backup / 'config.before.yaml').write_bytes(candidate)
        (backup / 'manifest.json').write_text(json.dumps({
            'before_sha256': installer.digest(candidate),
            'candidate_sha256': installer.digest(original)}))

    def test_main_mode_matrix_runs_real_preflights_before_reload(self):
        path, backup, install = self.main_fixture()
        policy = Path(installer.POLICY)
        policy_info = self.files[policy]
        for mode in ('enable', 'disable', 'rollback-enabled', 'rollback-disabled'):
            for policy_present in (True, False):
                for plugins_present in (True, False):
                    if mode in ('disable', 'rollback-enabled') and not plugins_present:
                        continue  # No plugin to disable or enabled selector to restore.
                    with self.subTest(mode=mode, policy=policy_present, plugins=plugins_present):
                        self.files.pop(policy, None)
                        if policy_present:
                            self.files[policy] = policy_info
                        if mode == 'enable':
                            original = self.disabled(plugins_present)
                            candidate = installer.configuration_candidate(original)
                            args = ['--library', '/tmp/offline-input.so']
                        elif mode == 'disable':
                            original = self.enabled()
                            candidate = installer.configuration_candidate(original, disable=True)
                            args = ['--disable']
                        else:
                            original = self.enabled()
                            if mode == 'rollback-enabled':
                                cfg = yaml.safe_load(original)
                                cfg['plugins']['configs']['fast-mode-policy']['store']['version'] = '0.1.1'
                                candidate = installer.replace_plugins(CONFIG, cfg['plugins'])
                            else:
                                candidate = self.disabled(plugins_present)
                            self.write_rollback(backup, original, candidate)
                            args = ['--rollback', str(backup)]
                        path.write_bytes(original)
                        enabled = installer.status_settings(yaml.safe_load(candidate))[0]
                        install.reset_mock()
                        self.accessed.clear()
                        with patch.object(installer, 'verified_reload', return_value=0 if enabled else 3) as reload, \
                                contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(installer.main(args), 0 if enabled else 3)
                            self.assertEqual(reload.call_args.args[1:3], (original, candidate))
                        self.assertEqual(install.call_count, int(mode == 'enable'))
                        if not enabled:
                            self.assertNotIn(policy, self.accessed)
                            self.assertFalse(any(installer.contains(installer.PLUGIN_DIR, p) for p in self.accessed))
                        self.assertEqual(path.read_bytes(), original)

    def test_automatic_rollback_rechecks_the_restored_candidate_with_real_preflights(self):
        path, backup, install = self.main_fixture()
        policy = Path(installer.POLICY)
        policy_info = self.files[policy]
        for target in ('enabled', 'disabled', 'plugin-free'):
            for policy_present in (True, False):
                with self.subTest(target=target, policy=policy_present):
                    # Simulate deletion during failure; rollback must also
                    # tolerate a policy the dashboard has not published yet.
                    self.files[policy] = policy_info
                    if target == 'enabled':
                        cfg = yaml.safe_load(self.enabled())
                        cfg['plugins']['configs']['fast-mode-policy']['store']['version'] = '0.1.1'
                        original = installer.replace_plugins(CONFIG, cfg['plugins'])
                    else:
                        original = self.disabled(target == 'disabled')
                    candidate = installer.configuration_candidate(original)
                    path.write_bytes(original)
                    rollback_access = []
                    def reload(config_path, before, after, *args):
                        if before == original:
                            config_path.write_bytes(after)
                            if not policy_present:
                                self.files.pop(policy)
                            self.accessed.clear()
                            raise installer.RuntimeConfirmationError('synthetic deployment failure')
                        rollback_access.extend(self.accessed)
                        self.assertEqual((before, after), (candidate, original))
                        config_path.write_bytes(after)
                        return 0 if target == 'enabled' else 3
                    with patch.object(installer, 'verified_reload', side_effect=reload) as reload_call, \
                            contextlib.redirect_stdout(io.StringIO()):
                        if target == 'enabled':
                            with self.assertRaisesRegex(installer.RuntimeConfirmationError,
                                                        'original enabled config restored and verified'):
                                installer.main(['--library', '/tmp/offline-input.so'])
                            self.assertEqual(path.read_bytes(), original)
                            self.assertEqual(reload_call.call_count, 2)
                        else:
                            self.assertEqual(installer.main(['--library', '/tmp/offline-input.so']), 3)
                            self.assertEqual(path.read_bytes(), original)
                            self.assertEqual(reload_call.call_count, 2)
                            self.assertNotIn(policy, rollback_access)
                            self.assertFalse(any(installer.contains(installer.PLUGIN_DIR, p) for p in rollback_access))
                        self.assertIn(installer.CONFIG, self.accessed)
                        self.assertTrue(all(p.is_absolute() for p in self.accessed))

    def test_directory_and_version_requirements_follow_candidate_activation(self):
        for enabled in (True, False):
            for directory in (None, '', 'plugins', '/opt/personal-codex-router/plugins'):
                for version in (installer.VERSION, 'invalid'):
                    with self.subTest(enabled=enabled, directory=directory, version=version):
                        cfg = yaml.safe_load(self.enabled())
                        cfg['plugins']['configs']['fast-mode-policy'].update(enabled=enabled, store={'version': version})
                        if directory is None:
                            cfg['plugins'].pop('dir')
                        else:
                            cfg['plugins']['dir'] = directory
                        self.accessed.clear()
                        if enabled and (not directory or not directory.startswith('/') or version == 'invalid'):
                            for check in (lambda: installer.installer_preflight(cfg), lambda: installer.preflight(cfg, 999)):
                                with self.assertRaises(installer.SandboxError):
                                    check()
                        else:
                            installer.installer_preflight(cfg)
                            installer.preflight(cfg, 999)
                        self.assertTrue(all(path.is_absolute() for path in self.accessed))
                        if not enabled and (not directory or not directory.startswith('/')):
                            self.assertIsNone(installer.library_selection(cfg)[0])

    def test_global_deactivation_ignores_policy_and_library_requirements(self):
        cfg = yaml.safe_load(self.enabled())
        cfg['plugins']['enabled'] = False
        cfg['plugins'].pop('dir')
        item = cfg['plugins']['configs']['fast-mode-policy']
        item.update(policy_path='missing-relative-policy.json', store={'version': 'unused'})
        self.files.pop(Path(installer.POLICY))
        self.facts['cliproxyapi']['InaccessiblePaths'] = '/var/lib /opt'
        self.facts['personal-codex-router-dashboard']['RootDirectory'] = '/alternate'
        installer.installer_preflight(cfg)
        installer.preflight(cfg, 999)

    def test_service_dac_and_sandbox_restrictions_only_block_enabled_candidates(self):
        policy = Path(installer.POLICY)
        library = installer.PLUGIN_DIR / 'linux/amd64' / f'fast-mode-policy-v{installer.VERSION}.so'
        controls = Path('/var/lib/personal-codex-router/fast-mode-controls.json')
        cases = (
            ('unreadable-policy', lambda: self.files.update({policy: self.info(stat.S_IFREG | 0o600, 4243, 4243)})),
            ('missing-library', lambda: self.files.pop(library)),
            ('unreadable-library', lambda: self.files.update({library: self.info(stat.S_IFREG | 0o600)})),
            ('untraversable-library', lambda: self.files.update({installer.PLUGIN_DIR: self.info(stat.S_IFDIR | 0o700)})),
            ('status-gateway-dac', lambda: self.files.update({installer.STATUS.parent: self.info(stat.S_IFDIR | 0o755)})),
            ('status-gateway-sandbox', lambda: self.facts['cliproxyapi'].update(ReadOnlyPaths=str(installer.STATUS.parent))),
            ('policy-hidden', lambda: self.facts['cliproxyapi'].update(InaccessiblePaths=str(policy))),
            ('library-hidden', lambda: self.facts['cliproxyapi'].update(InaccessiblePaths=str(installer.PLUGIN_DIR))),
            ('controls-dac', lambda: self.files.update({controls.parent: self.info(stat.S_IFDIR | 0o755)})),
            ('dashboard-proc', lambda: self.facts['personal-codex-router-dashboard'].update(ProcSubset='unsupported')),
            ('dashboard-bus', lambda: self.files.pop(Path('/run/dbus/system_bus_socket'))),
            ('dashboard-status-hidden', lambda: self.facts['personal-codex-router-dashboard'].update(InaccessiblePaths=str(installer.STATUS))),
        )
        baseline_files = self.files.copy()
        baseline_facts = {unit: facts.copy() for unit, facts in self.facts.items()}
        self.run_preflights(self.enabled(), CONFIG)
        for name, mutate in cases:
            with self.subTest(check=name):
                self.files = baseline_files.copy()
                self.facts = {unit: facts.copy() for unit, facts in baseline_facts.items()}
                mutate()
                with self.assertRaises(installer.SandboxError):
                    self.run_preflights(self.enabled(), CONFIG)
                for present in (True, False):
                    self.run_preflights(self.disabled(present), self.enabled())

    def test_disabled_candidates_still_check_every_installer_output(self):
        cases = (
            ('config-access', lambda: self.denied.add(installer.CONFIG)),
            ('backup-ancestry', lambda: self.files.update({installer.BACKUPS.parent: self.info(stat.S_IFDIR | 0o777, 1000, 1000)})),
            ('status-parent-missing', lambda: self.files.pop(installer.STATUS.parent)),
            ('status-is-directory', lambda: self.files.update({installer.STATUS: self.info(stat.S_IFDIR | 0o755)})),
            ('status-is-link', lambda: self.files.update({installer.STATUS: self.info(stat.S_IFLNK | 0o777)})),
            ('status-not-writable', lambda: self.denied.add(installer.STATUS.parent)),
        )
        # BACKUPS need not exist, but their existing ancestor must be protected.
        for parent in installer.BACKUPS.parents:
            self.files.setdefault(parent, self.info(stat.S_IFDIR | 0o755))
        baseline = self.files.copy()
        for name, mutate in cases:
            with self.subTest(check=name):
                self.files = baseline.copy()
                self.denied.clear()
                mutate()
                for candidate in (self.enabled(), self.disabled(), CONFIG):
                    with self.assertRaises(installer.SandboxError):
                        self.run_preflights(candidate, self.enabled())

    def test_deactivation_checks_old_and_new_marker_outputs(self):
        old = yaml.safe_load(self.enabled())
        previous_marker = Path('/var/lib/cliproxyapi/previous/status.json')
        old['plugins']['configs']['fast-mode-policy']['status_path'] = str(previous_marker)
        self.add_file(previous_marker, uid=4242, gid=4242)
        for candidate in (yaml.safe_load(self.disabled()), yaml.safe_load(CONFIG)):
            with self.subTest(plugins=bool(candidate.get('plugins'))):
                self.accessed.clear()
                installer.installer_preflight(candidate, old)
                installer.preflight(candidate, 999, old)
                self.assertIn(previous_marker, self.accessed)
                self.assertIn(installer.STATUS, self.accessed)
                self.denied.add(previous_marker.parent)
                with self.assertRaises(installer.SandboxError):
                    installer.preflight(candidate, 999, old)
                self.denied.clear()
        old['plugins']['configs']['fast-mode-policy']['status_path'] = 'relative-status.json'
        with self.assertRaisesRegex(installer.SandboxError, 'status path must be absolute'):
            installer.installer_preflight(yaml.safe_load(CONFIG), old)


class OutputPermissionsTests(unittest.TestCase):
    ELF = b'\x7fELF\x02\x01' + b'\0' * 10 + b'\x03\x00\x3e\x00' + b'fixture'
    SERVICE = (4242, {4242, 4243}, 4242)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root.chmod(0o755)
        self.owners = {}
        self.source = self.root / 'input.so'
        self.source.write_bytes(self.ELF)
        self.plugins = self.root / 'plugins'
        self.library_dir = self.plugins / 'linux/amd64'
        self.status_dir = self.root / 'state'
        self.status_dir.mkdir()
        self.status_dir.chmod(0o750)
        self.chown(self.status_dir, 4242, 4242)
        self.status = self.status_dir / 'status.json'
        old_mask = os.umask(0o077)
        self.addCleanup(os.umask, old_mask)

    def chown(self, path, uid, gid, **kwargs):
        self.owners[Path(path).lstat().st_ino] = (uid, gid)

    def fchown(self, fd, uid, gid):
        self.owners[os.fstat(fd).st_ino] = (uid, gid)

    def stat_path(self, path):
        info = Path(path).lstat()
        uid, gid = self.owners.get(info.st_ino, (0, 0))
        # Simulate trusted installation ancestry outside the temporary fixture.
        mode = info.st_mode if installer.contains(self.root, Path(path)) else stat.S_IFDIR | 0o755
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)

    def assert_metadata(self, path, mode, owner):
        info = self.stat_path(path)
        self.assertEqual(stat.S_IMODE(info.st_mode), mode)
        self.assertEqual((info.st_uid, info.st_gid), owner)

    def install(self):
        return installer.install_library(self.source, self.library_dir,
            managed_root=self.plugins, chown=self.chown, fchown=self.fchown,
            stat_path=self.stat_path)

    def check_service_access(self, library):
        for path in (self.plugins, self.plugins / 'linux', self.library_dir):
            installer.filesystem_access(path, self.SERVICE, directory=True, stat_path=self.stat_path)
        installer.filesystem_access(library, self.SERVICE, stat_path=self.stat_path)
        installer.filesystem_access(self.status, self.SERVICE, write=True,
                                   future=(self.SERVICE, 0o640), stat_path=self.stat_path)
        installer.filesystem_access(self.status, self.SERVICE,
                                   future=(self.SERVICE, 0o640), stat_path=self.stat_path)

    def test_install_under_private_umask_sets_dirs_library_and_atomic_temp_metadata(self):
        link = os.link
        observed = []
        def checked_link(source, target):
            self.assert_metadata(Path(source), 0o644, (0, 0))
            observed.append(source)
            link(source, target)
        with patch.object(installer.os, 'link', side_effect=checked_link):
            library = self.install()
        self.assertEqual(len(observed), 1)
        for path in (self.plugins, self.plugins / 'linux', self.library_dir):
            self.assert_metadata(path, 0o755, (0, 0))
        self.assert_metadata(library, 0o644, (0, 0))
        self.check_service_access(library)
        self.assertFalse(list(self.library_dir.glob('.fast-mode-*')))

    def test_repair_old_dirs_library_and_root_marker_preserves_runtime_evidence(self):
        self.library_dir.mkdir(parents=True)
        for path, mode in ((self.plugins, 0o700), (self.plugins / 'linux', 0o777), (self.library_dir, 0o700)):
            path.chmod(mode)
        library = self.library_dir / f'fast-mode-policy-v{installer.VERSION}.so'
        library.write_bytes(self.ELF)
        library.chmod(0o600)
        inode = library.stat().st_ino
        marker = b'{"plugin":"fast-mode-policy","disabled":true,"pid":999}\n'
        self.status.write_bytes(marker)
        self.status.chmod(0o600)
        self.chown(self.status, 0, 0)
        self.assertEqual(self.install().stat().st_ino, inode)
        installer.repair_status(self.status, owner=(4242, 4242), chown=self.chown)
        self.assertEqual(self.status.read_bytes(), marker)
        self.assert_metadata(self.status, 0o640, (4242, 4242))
        for path in (self.plugins, self.plugins / 'linux', self.library_dir):
            self.assert_metadata(path, 0o755, (0, 0))
        self.assert_metadata(library, 0o644, (0, 0))
        self.check_service_access(library)

    def test_disabled_marker_and_atomic_temp_get_service_owner_and_exact_mode(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.status.write_text('{"pid":999}')
                    self.status.chmod(0o777)
                    self.chown(self.status, 0, 0)
                replace = os.replace
                def checked_replace(source, target):
                    self.assert_metadata(Path(source), 0o640, (4242, 4242))
                    replace(source, target)
                with patch.object(installer.os, 'replace', side_effect=checked_replace):
                    installer.mark_disabled(self.status, owner=(4242, 4242), fchown=self.fchown)
                self.assert_metadata(self.status, 0o640, (4242, 4242))
                self.assertTrue(json.loads(self.status.read_text())['disabled'])
                if existing:
                    self.assertEqual(json.loads(self.status.read_text())['pid'], 999)
                self.assertFalse(list(self.status_dir.glob('.fast-mode-disabled-*')))

    def test_backups_remain_private_even_with_permissive_umask(self):
        config = self.root / 'config.yaml'
        config.write_bytes(CONFIG)
        backups = self.root / 'private/backups'
        os.umask(0)
        with patch.object(installer, 'BACKUPS', backups):
            backup = installer.backup_config(CONFIG, CONFIG, config,
                chown=self.chown, fchown=self.fchown, stat_path=self.stat_path)
        for path in (backups.parent, backups, backup):
            self.assert_metadata(path, 0o700, (0, 0))
        for path in backup.iterdir():
            self.assert_metadata(path, 0o600, (0, 0))

    def test_dac_rejects_plugin_policy_and_status_ancestors_and_unreadable_library(self):
        library = self.install()
        policy = self.root / 'policy/policy.json'
        policy.parent.mkdir()
        policy.parent.chmod(0o755)
        policy.write_text('{}')
        policy.chmod(0o640)
        self.chown(policy, 1000, 4243)  # Access through a supplementary group.
        installer.filesystem_access(policy, self.SERVICE, stat_path=self.stat_path)
        for ancestor, path in ((self.root, library), (self.plugins, library),
                               (policy.parent, policy), (self.status_dir, self.status)):
            with self.subTest(ancestor=ancestor):
                old_mode = stat.S_IMODE(ancestor.stat().st_mode)
                old_owner = self.stat_path(ancestor)
                self.chown(ancestor, 0, 0)
                ancestor.chmod(0o700)
                try:
                    with self.assertRaisesRegex(installer.SandboxError, 'cannot traverse'):
                        installer.filesystem_access(path, self.SERVICE,
                            future=(self.SERVICE, 0o640) if path == self.status else None,
                            stat_path=self.stat_path)
                finally:
                    ancestor.chmod(old_mode)
                    self.chown(ancestor, old_owner.st_uid, old_owner.st_gid)
        library.chmod(0o600)
        with self.assertRaisesRegex(installer.SandboxError, 'cannot read'):
            installer.filesystem_access(library, self.SERVICE, stat_path=self.stat_path)
        missing = self.library_dir / 'missing.so'
        with self.assertRaisesRegex(installer.SandboxError, 'required file is missing'):
            installer.filesystem_access(missing, self.SERVICE, stat_path=self.stat_path)

    def test_main_checks_real_dac_after_install_and_before_any_config_write(self):
        config_path = self.root / 'config.yaml'
        config_path.write_bytes(CONFIG)
        policy = self.root / 'policy.json'
        policy.write_text('{}')
        policy.chmod(0o640)
        self.chown(policy, 4242, 4242)
        filesystem_access = installer.filesystem_access
        install_library = installer.install_library
        events = []
        def install(source):
            events.append('install')
            result = install_library(source, self.library_dir, managed_root=self.plugins,
                chown=self.chown, fchown=self.fchown, stat_path=self.stat_path)
            # The installer must also check the existing ancestor above plugins.
            self.root.chmod(0o700)
            return result
        def access(path, identity, **kwargs):
            events.append('access')
            filesystem_access(path, identity, stat_path=self.stat_path, **kwargs)
        facts = dict.fromkeys(installer.SANDBOX_PROPERTIES, '')
        facts.update(User='cliproxyapi', Group='cliproxyapi', ProtectSystem='no',
                     ProcSubset='all', ProtectProc='default', PrivateTmp='no')
        with patch.object(installer, 'CONFIG', config_path), \
                patch.object(installer, 'PLUGIN_DIR', self.plugins), \
                patch.object(installer, 'POLICY', str(policy)), \
                patch.object(installer, 'STATUS', self.status), \
                patch.object(installer.os, 'geteuid', return_value=0), \
                patch.object(installer, 'main_pid', return_value=999), \
                patch.object(installer, 'installer_preflight'), \
                patch.object(installer, 'runtime_snapshot'), \
                patch.object(installer, 'backup_config', return_value=self.root / 'backup'), \
                patch.object(installer, 'install_library', side_effect=install), \
                patch.object(installer, 'unit_sandbox', return_value=facts), \
                patch.object(installer, 'service_identity', return_value=self.SERVICE), \
                patch.object(installer, 'filesystem_access', side_effect=access), \
                patch.object(installer, 'verified_reload') as reload, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(installer.SandboxError, 'cannot traverse'):
                installer.main(['--library', str(self.source)])
        self.assertEqual(events[0], 'install')
        self.assertIn('access', events)
        reload.assert_not_called()
        self.assertEqual(config_path.read_bytes(), CONFIG)


if __name__ == '__main__':
    unittest.main()
