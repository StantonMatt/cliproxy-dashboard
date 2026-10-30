from contextlib import ExitStack, redirect_stdout
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import site_config


def load_script(name):
    spec = importlib.util.spec_from_file_location('setup_' + name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_script('install-operations')


class SiteConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.user = pwd.getpwuid(os.getuid())

    def test_example_defaults_to_checkout_owner_without_legacy_features(self):
        with patch.dict(os.environ, {}, clear=True):
            site = site_config.load(ROOT / 'config/site.example.json')
        self.assertEqual(site, {'desktop_user': self.user.pw_name, 'devices': []})

    def test_invoking_user_precedes_checkout_owner(self):
        user = SimpleNamespace(pw_name='installer-user')
        with patch.dict(os.environ, {'SUDO_USER': 'installer-user'}), \
                patch.object(site_config.pwd, 'getpwnam', return_value=user) as lookup, \
                patch.object(site_config.pwd, 'getpwuid') as owner:
            self.assertIs(site_config.default_user(), user)
        lookup.assert_called_once_with('installer-user')
        owner.assert_not_called()

    def test_explicit_config_does_not_inherit_live_devices_or_watch(self):
        config = self.root / 'site.json'
        config.write_text('{}')
        with patch.object(site_config, 'LEGACY_PREVIEWS', self.root / 'must-not-read'):
            self.assertEqual(site_config.load(config)['devices'], [])
            self.assertNotIn('claude_b', site_config.load(config))

    def test_legacy_metadata_migrates_once_and_explicit_empty_sections_turn_off(self):
        home = self.root / 'home'
        unit = home / '.config/systemd/user/personal-codex-router-claude-b-usage.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('Environment=CLAUDE_B_EMAIL=account-b@example.com\nEnvironment="CLAUDE_B_PLAN=Max (20x)"\n')
        previews = self.root / 'previews.json'
        previews.write_text(json.dumps({'dns': 'gateway.tailnet-example.ts.net', 'devices': [
            {'name': 'Laptop', 'ips': ['100.64.0.11']}]}))
        canonical = self.root / 'site.json'
        with patch.object(site_config, 'SITE', canonical), patch.object(site_config, 'LEGACY_PREVIEWS', previews), \
                patch.object(site_config, 'default_user', return_value=SimpleNamespace(pw_name=self.user.pw_name, pw_dir=str(home))):
            site = site_config.load()
            self.assertEqual(site['devices'], [{'name': 'Laptop', 'tailscale_ip': '100.64.0.11'}])
            self.assertEqual(site['tailscale_hostname'], 'gateway.tailnet-example.ts.net')
            self.assertEqual(site['claude_b'], {'email': 'account-b@example.com', 'plan': 'Max (20x)'})
            canonical.write_text('{}')
            # Canonical ownership is independently tested below; isolate this metadata test.
            with patch.object(site_config.os, 'getuid', return_value=0), \
                    patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)):
                site = site_config.load()
            self.assertEqual(site['devices'], [])
            self.assertNotIn('claude_b', site)

    def test_canonical_requires_root_ownership_and_safe_permissions(self):
        canonical = self.root / 'site.json'
        canonical.write_text('{}')
        with patch.object(site_config, 'SITE', canonical):
            for uid, mode in ((1234, 0o100644), (0, 0o100666)):
                with self.subTest(uid=uid, mode=mode), \
                        patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=uid, st_mode=mode)):
                    with self.assertRaisesRegex(RuntimeError, 'root-owned'):
                        site_config.load()

    def test_incomplete_legacy_claude_metadata_stops_install_before_writes(self):
        unit = self.root / '.config/systemd/user/personal-codex-router-claude-b-usage.service'
        unit.parent.mkdir(parents=True)
        canonical = self.root / 'site.json'
        for metadata in ('Environment=CLAUDE_B_EMAIL=account-b@example.com\n',
                         'Environment="CLAUDE_B_PLAN=Max (20x)"\n', '',
                         'Environment=CLAUDE_B_EMAIL=\nEnvironment="CLAUDE_B_PLAN=Max (20x)"\n'):
            unit.write_text(metadata)
            with self.subTest(metadata=metadata), \
                    patch.object(site_config, 'SITE', canonical), \
                    patch.object(site_config, 'LEGACY_PREVIEWS', self.root / 'previews.json'), \
                    patch.object(site_config, 'default_user', return_value=SimpleNamespace(
                        pw_name=self.user.pw_name, pw_dir=str(self.root))), \
                    patch.object(installer, 'write_site') as write, \
                    patch.object(installer, 'install_file') as install, \
                    patch.object(installer, 'command') as command, \
                    patch.object(sys, 'argv', ['install-operations.py']), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'Legacy Claude B unit needs both CLAUDE_B_EMAIL and CLAUDE_B_PLAN'):
                    installer.main()
                write.assert_not_called()
                install.assert_not_called()
                command.assert_not_called()
                self.assertFalse(canonical.exists())
                self.assertEqual(unit.read_text(), metadata)

    def test_invalid_site_values_fail_closed(self):
        for value in ([], {'unknown': True}, {'devices': {}}, {'devices': [{'name': 'Bad', 'tailscale_ip': '127.0.0.1'}]},
                      {'devices': [{'name': 'Bad\nName', 'tailscale_ip': '100.64.0.11'}]},
                      {'tailscale_hostname': 'host\nExecStart=bad'}, {'claude_b': {}},
                      {'claude_b': {'email': 'account-b@example.com', 'plan': 'Max\nExecStart=bad'}}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                site_config.validate(value)

    def test_scratch_dry_run_is_independent_of_owner_settings_and_writes_nothing(self):
        site = self.root / 'site.json'
        site.write_text(json.dumps({'desktop_user': 'root', 'devices': []}))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install-operations.py'), '--site-config', str(site)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Loopback-only dashboard; device proxy stays off.', result.stdout)
        self.assertIn('device proxy: 0 devices; Claude B watch: False', result.stdout)
        self.assertNotIn('Install scripts/personal-codex-router-claude-b-usage', result.stdout)
        self.assertEqual(set(self.root.iterdir()), {site})


class HostInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.site = {'desktop_user': pwd.getpwuid(os.getuid()).pw_name, 'devices': [
            {'name': 'Laptop', 'tailscale_ip': '100.64.0.11'}]}
        self.old_site = installer.SITE_SETTINGS
        self.addCleanup(installer.configure_site, self.old_site)
        installer.configure_site(self.site)
        self.guard = self.root / 'guard.nft'
        self.guard.write_text('table inet host_guard {\n  chain input {\n    iifname != "tailscale0" return\n    counter drop\n  }\n  chain forward {}\n}\n')
        self.status = {'Self': {'UserID': 1, 'TailscaleIPs': ['100.64.0.10']}, 'Peer': {
            'laptop': {'UserID': 1, 'TailscaleIPs': ['100.64.0.11']}}}

    def command(self, *args):
        return json.dumps(self.status) if args[0] == 'tailscale' else 'active'

    def test_device_guard_is_validated_before_install_writes(self):
        for failure in ('missing-guard', 'wrong-peer', 'bad-layout', 'nft-rejection'):
            self.guard.write_text('bad layout' if failure == 'bad-layout' else 'unused')
            if failure == 'missing-guard':
                self.guard.unlink()
            self.status['Peer']['laptop']['UserID'] = 2 if failure == 'wrong-peer' else 1
            if failure == 'nft-rejection':
                self.guard.write_text('    iifname != "tailscale0" return\n    counter drop\n  }\n  chain forward')
            def command(*args):
                if args[0] == 'nft':
                    raise RuntimeError('nft check rejected')
                return self.command(*args)
            with self.subTest(failure=failure), \
                    patch.object(installer, 'GUARD', self.guard), patch.object(installer, 'command', side_effect=command), \
                    patch.object(installer.site_config, 'load', return_value=self.site), \
                    patch.object(installer, 'version_fingerprint'), \
                    patch.object(installer, 'overview', return_value={'gateway': {'online': True, 'accounts': []}}), \
                    patch.object(Path, 'mkdir') as mkdir, patch.object(installer, 'install_file') as install, \
                    patch.object(installer, 'pre_apply_backup') as backup, \
                    patch.object(installer.os, 'geteuid', return_value=0), \
                    patch.object(sys, 'argv', ['install-operations.py', '--apply']):
                with self.assertRaises(RuntimeError):
                    installer.main()
                for operation in (mkdir, install, backup):
                    operation.assert_not_called()

    def test_guard_updates_addresses_and_checks_candidate_without_changing_guard(self):
        old = self.guard.read_text()
        with patch.object(installer, 'GUARD', self.guard), patch.object(installer, 'command', side_effect=self.command) as command:
            rules = installer.device_preflight()
            self.guard.write_text(rules)
            self.site['devices'][0]['tailscale_ip'] = '100.64.0.12'
            self.status['Peer']['laptop']['TailscaleIPs'] = ['100.64.0.12']
            updated = installer.device_preflight()
        self.assertNotIn('100.64.0.11', updated)
        self.assertIn('100.64.0.12', updated)
        self.assertEqual(updated.count('Personal project previews IPv4'), 1)
        self.assertEqual(self.guard.read_text(), rules)
        self.assertNotEqual(rules, old)
        self.assertEqual(sum(call.args[:2] == ('nft', '--check') for call in command.call_args_list), 2)

    def test_no_devices_skip_every_device_prerequisite_and_omit_b_units(self):
        installer.configure_site({**self.site, 'devices': []})
        with patch.object(installer, 'command') as command:
            self.assertIsNone(installer.device_preflight())
        command.assert_not_called()
        names = {d.name for _, d, _ in installer.install_files()}
        self.assertNotIn('personal-codex-router-claude-b-usage.service', names)
        text = installer.render_unit((ROOT / 'scripts/personal-codex-router-preview.service').read_text())
        self.assertNotIn('tailscale-host-guard', text)
        self.assertNotIn('IPAddressAllow=100.', text)

    def test_units_are_rendered_with_only_site_values_and_escape_specifiers(self):
        self.site['claude_b'] = {'email': 'account-b@example.com', 'plan': 'Max (20x) 100% "quoted"'}
        with patch.object(installer, 'PREVIEW_BIND', '100.64.0.10'):
            preview = installer.render_unit((ROOT / 'scripts/personal-codex-router-preview.service').read_text())
            claude = installer.render_unit((ROOT / 'scripts/personal-codex-router-claude-b-usage.service').read_text())
        self.assertIn('IPAddressAllow=100.64.0.11/32', preview.split('[Install]')[0])
        self.assertIn('IPAddressAllow=100.64.0.10/32', preview)
        self.assertIn('CLAUDE_B_EMAIL=account-b@example.com', claude)
        self.assertIn('100%% \\"quoted\\"', claude)
        self.assertNotIn('@DEVICE_', preview)

    def test_site_persistence_and_preview_migration_preserve_registrations(self):
        canonical = self.root / 'site.json'
        preview = self.root / 'previews.json'
        old = {'base_port': 8400, 'previews': [{'name': 'app', 'listen': 8401}], 'aliases': ['gateway'], 'address': 'gateway'}
        preview.write_text(json.dumps(old))
        with patch.object(installer.site_config, 'SITE', canonical), \
                patch.object(installer, 'CONFIG', preview), patch.object(installer, 'PREVIEW_BIND', '100.64.0.10'), \
                patch.object(os, 'fchown') as chown:
            installer.write_site()
            installer.write_previews()
        self.assertEqual(json.loads(canonical.read_text()), {**self.site, 'source_checkout': str(ROOT)})
        self.assertEqual(canonical.stat().st_mode & 0o777, 0o644)
        self.assertEqual(chown.call_count, 2)
        self.assertEqual(chown.call_args.args[1:], (0, 0))
        updated = json.loads(preview.read_text())
        for key in ('previews', 'aliases', 'address'):
            self.assertEqual(updated[key], old[key])
        self.assertEqual(updated['devices'], [{'name': 'Laptop', 'ips': ['100.64.0.11']}])

    def test_absent_codex_daemon_is_supported(self):
        with patch.object(installer.site_config, 'desktop', return_value=SimpleNamespace(pw_dir=str(self.root))):
            self.assertIsNone(installer.version_fingerprint())

    def test_installed_backup_preserves_source_checkout_and_checkout_fallback(self):
        checkout = self.root / 'checkout'
        sources = (checkout / 'docs/recovery.md', checkout / 'plugins/policy/main.go')
        for source in sources:
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('source fixture\n')
        installed = self.root / 'installed'
        installed.mkdir()
        (installed / 'helper.py').write_text('# installed fixture\n')
        canonical = self.root / 'site.json'
        with patch.object(installer, 'ROOT', checkout), patch.object(site_config, 'SITE', canonical), \
                patch.object(os, 'fchown'):
            installer.write_site()
        self.assertEqual(json.loads(canonical.read_text()).get('source_checkout'), str(checkout))
        load = site_config.load
        with patch.object(site_config, 'load', side_effect=lambda: load(canonical)), \
                patch.object(site_config, 'REPO', installed):
            backup = load_script('router-backup')
        with patch.object(Path, 'exists', return_value=True), \
                patch.object(Path, 'is_symlink', return_value=False), patch.object(Path, 'glob', return_value=[]):
            selected = backup.sources()
        self.assertIn(checkout, selected)
        self.assertEqual(selected.count(Path('/opt/personal-codex-router')), 1)
        manifest = backup.snapshot([backup.PROJECT], self.root / 'snapshot')
        self.assertEqual({row['path'] for row in manifest['entries']},
                         {str(source.relative_to('/')) for source in sources})
        canonical.write_text('{}')
        with patch.object(site_config, 'load', side_effect=lambda: load(canonical)), \
                patch.object(site_config, 'REPO', checkout):
            backup = load_script('router-backup')
        self.assertEqual(backup.PROJECT, checkout)

    def test_fresh_loopback_install_needs_no_accounts_dashboard_or_device_tools(self):
        site = {**self.site, 'devices': []}
        (self.root / 'backup.json').write_text('{"snapshot":"fixture"}')
        requests = self.root / 'move-requests'
        requests.mkdir()
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.root))
        command = Mock(side_effect=lambda *args: 'active' if args[-1] == 'personal-codex-router-move.path' else '123')
        with ExitStack() as stack:
            for context in (patch.object(installer.site_config, 'load', return_value=site),
                            patch.object(installer, 'DEST', self.root / 'installed'),
                            patch.object(installer, 'OPS', self.root), patch.object(installer, 'MOVE_REQUESTS', requests),
                            patch.object(installer, 'write_site'),
                            patch.object(installer, 'write_previews'), patch.object(installer, 'install_file'),
                            patch.object(installer, 'pre_apply_backup'), patch.object(installer, 'prepare_watch_paths'),
                            patch.object(installer, 'version_fingerprint', return_value=None),
                            patch.object(installer.os, 'geteuid', return_value=0), patch.object(os, 'chown'),
                            patch.object(Path, 'mkdir'), patch.object(Path, 'chmod'),
                            patch.object(installer.pwd, 'getpwnam', return_value=user),
                            patch.object(installer.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=os.getgid())),
                            patch.object(installer, 'command', command),
                            patch.object(sys, 'argv', ['install-operations.py', '--apply']), redirect_stdout(io.StringIO())):
                stack.enter_context(context)
            overview = stack.enter_context(patch.object(installer, 'overview'))
            user_command = stack.enter_context(patch.object(installer, 'user_command'))
            installer.main()
        overview.assert_not_called()
        self.assertTrue(all(call.args[0] == 'systemctl' for call in command.call_args_list))
        self.assertFalse(any('enable' in call.args and 'personal-codex-router-preview.service' in call.args
                             for call in command.call_args_list))
        self.assertFalse(any('enable' in call.args and 'personal-codex-router-claude-b-usage.timer' in call.args
                             for call in user_command.call_args_list))
        self.assertTrue((self.root / 'deployment.json').exists())

    def test_runtime_helpers_use_configured_desktop_home(self):
        user = SimpleNamespace(pw_name='operator', pw_dir=str(self.root / 'desktop'))
        with patch.object(site_config, 'desktop', return_value=user):
            backup = load_script('router-backup')
            ops = load_script('router-ops')
        self.assertEqual(backup.HOME, Path(user.pw_dir))
        self.assertEqual(ops.HOME, Path(user.pw_dir))
        self.assertEqual(ops.USER, 'operator')

    def test_activation_uses_checkout_assets_and_configured_user(self):
        import gateway_config
        enable = load_script('enable-dashboard')
        self.assertEqual(enable.PROJECT, ROOT)
        repo = self.root / 'repo'
        (repo / 'references').mkdir(parents=True)
        (repo / 'docs').mkdir()
        panel = b'pinned test panel'
        (repo / 'references/management.html').write_bytes(panel)
        (repo / 'references/management-release.json').write_text(json.dumps({
            'tag_name': 'fixture', 'assets': [{'digest': 'sha256:' + hashlib.sha256(panel).hexdigest()}]}))
        config = self.root / 'config.yaml'
        original = (ROOT / 'tests/fixtures/config-v8-nested.yaml').read_bytes()
        config.write_bytes(gateway_config.validated_edit(original, {'secret-key': ''}))
        user = SimpleNamespace(pw_dir=str(self.root / 'desktop'), pw_uid=os.getuid(), pw_gid=os.getgid())
        with patch.object(enable, 'PROJECT', repo), patch.object(enable, 'CONFIG', config), \
                patch.object(enable, 'KEY', self.root / 'management-key'), \
                patch.object(enable, 'BACKUPS', self.root / 'activation-backups'), \
                patch.object(site_config, 'desktop', return_value=user), \
                patch.object(enable.pwd, 'getpwnam', return_value=user), \
                patch.object(os, 'geteuid', return_value=0), patch.object(os, 'chown'), patch.object(os, 'fchown'), \
                redirect_stdout(io.StringIO()):
            enable.main()
        key = Path(user.pw_dir) / '.config/personal-codex-router/management-key'
        self.assertTrue(key.exists())
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertTrue((repo / 'docs/activation.json').exists())

    def test_changed_operational_code_has_no_export_scanner_identifiers(self):
        scanner = load_script('export-public')
        paths = [ROOT / 'scripts' / name for name in (
            'site_config.py', 'install-operations.py', 'enable-dashboard.py', 'router-backup.py',
            'router-ops.py', 'verify-quota-failover.py', 'install-friendly-url.py', 'repair-friendly-url-log.py',
            'personal-codex-router-preview.service', 'personal-codex-router-claude-b-usage.service')]
        paths += list((ROOT / 'config').iterdir())
        paths += [ROOT / 'plugins/fast-mode-policy/build.sh', Path(__file__), ROOT / 'tests/test_install_operations.py']
        for path in paths:
            if path.is_file() and path.name != 'export-public.py':
                with self.subTest(path=path.name):
                    self.assertEqual(list(scanner.findings(path.read_text())), [])

    def test_plugin_build_uses_optional_heavy_from_path(self):
        for use_heavy in (False, True):
            with self.subTest(use_heavy=use_heavy):
                case = self.root / str(use_heavy)
                bin_dir = case / 'bin'
                bin_dir.mkdir(parents=True)
                go = bin_dir / 'go'
                go.write_text('#!/bin/bash\nwhile [[ $1 != -o ]]; do shift; done\nshift\nprintf fixture > "$1"\n')
                go.chmod(0o755)
                marker = case / 'used-heavy'
                if use_heavy:
                    heavy = bin_dir / 'heavy'
                    heavy.write_text('#!/bin/bash\nprintf yes > "$HEAVY_MARKER"\nexec "$@"\n')
                    heavy.chmod(0o755)
                result = subprocess.run(['bash', str(ROOT / 'plugins/fast-mode-policy/build.sh'), str(case / 'output')],
                                        env={**os.environ, 'PATH': str(bin_dir) + ':/usr/bin:/bin',
                                             'GO': str(go), 'HEAVY_MARKER': str(marker)},
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(marker.exists(), use_heavy)
                self.assertEqual((case / 'output/fast-mode-policy-v0.1.2.so').read_text(), 'fixture')

    def test_mac_log_repair_discovers_legacy_and_neutral_helper_labels(self):
        repair = load_script('repair-friendly-url-log')
        for label in ('com.example.personal-router-url', 'org.personal-codex-router.url-helper'):
            with self.subTest(label=label):
                root = self.root / label
                daemons, support, home = root / 'LaunchDaemons', root / 'Support', root / 'home'
                daemons.mkdir(parents=True)
                support.mkdir()
                (home / 'Library/Logs/PersonalCodexRouter').mkdir(parents=True)
                target = daemons / (label + '.plist')
                original = {'Label': label, 'UserName': 'operator', 'StandardErrorPath': '/var/log/old.log',
                            'Sockets': {'Listener': {'SockNodeName': '127.0.0.1', 'SockServiceName': '80'}}}
                target.write_bytes(plistlib.dumps(original))
                with patch.object(repair, 'DAEMONS', daemons), patch.object(repair, 'SUPPORT', support), \
                        patch.object(os, 'geteuid', return_value=0), patch.object(os, 'chown'), \
                        patch.object(repair.pwd, 'getpwnam', return_value=SimpleNamespace(pw_dir=str(home), pw_uid=1234)), \
                        patch.object(repair.subprocess, 'run') as run, redirect_stdout(io.StringIO()):
                    repair.main()
                run.assert_any_call(['/bin/launchctl', 'bootout', 'system/' + label], check=True)
                self.assertEqual(plistlib.loads(target.read_bytes()), {
                    **original, 'StandardErrorPath': str(home / 'Library/Logs/PersonalCodexRouter/url-helper.log')})
                self.assertEqual(plistlib.loads((support / 'launchdaemon-before-log-fix.plist').read_bytes()), original)


if __name__ == '__main__':
    unittest.main()
