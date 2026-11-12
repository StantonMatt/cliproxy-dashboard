from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install-operations.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
spec = importlib.util.spec_from_file_location('backup', ROOT / 'scripts/router-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


PREPARE_DISCONNECTED = installer.prepare_disconnected_directory
PREPARE_PROVIDER_STATUS_USER = installer.prepare_provider_status_user


class InstallOperationsTests(unittest.TestCase):
    def setUp(self):
        user = SimpleNamespace(pw_name='operator', pw_dir='/home/operator', pw_uid=1234, pw_gid=1235)
        self.site = {'desktop_user': 'operator', 'devices': [
            {'name': 'Laptop', 'tailscale_ip': '100.64.0.11'}],
            'claude_b': {'email': 'account-b@example.com', 'plan': 'Max (20x)'}}
        for context in (patch.object(installer.site_config, 'load', return_value=self.site),
                        patch.object(installer.site_config, 'desktop', return_value=user),
                        patch.object(installer.site_config, 'SITE', ROOT / 'README.md'),
                        patch.object(backup, 'HOME', Path(user.pw_dir))):
            self.addCleanup(context.stop)
            context.start()
        installer.configure_site(self.site)
        disconnected = patch.object(installer, 'prepare_disconnected_directory')
        self.mock_disconnected = disconnected.start()
        self.addCleanup(disconnected.stop)
        provider_user = patch.object(installer, 'prepare_provider_status_user')
        self.mock_provider_user = provider_user.start()
        self.addCleanup(provider_user.stop)

    def test_disconnect_backup_directory_matches_dashboard_sandbox_and_is_private(self):
        # Invoke the original helper in an isolated path while all ownership writes are mocked.
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'disconnected'
            proxy = SimpleNamespace(pw_uid=1234, pw_gid=1235)
            with patch.object(installer, 'DISCONNECTED', target), \
                    patch.object(installer.pwd, 'getpwnam', return_value=proxy), \
                    patch.object(installer.os, 'chown') as chown:
                helper = PREPARE_DISCONNECTED
                helper()
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)
                chown.assert_called_once_with(target, 1234, 1235)
                target.rmdir()
                target.symlink_to(Path(directory))
                with self.assertRaises(RuntimeError):
                    helper()
        unit = (ROOT / 'scripts/personal-codex-router-dashboard.service').read_text()
        writable = [Path(path.lstrip('-')) for line in unit.splitlines() if line.startswith('ReadWritePaths=')
                    for path in line.partition('=')[2].split()]
        self.assertTrue(any(installer.DISCONNECTED.is_relative_to(path) for path in writable))

    def test_provider_status_collector_unit_sandbox_and_timer_install(self):
        files = installer.install_files()
        self.assertIn((ROOT / 'scripts/collect-provider-status.py',
                       installer.DEST / 'collect-provider-status.py', 0o755), files)
        self.assertIn((ROOT / 'dashboard/provider_status.py',
                       installer.DEST / 'dashboard/provider_status.py', 0o644), files)
        unit = (ROOT / 'scripts/personal-codex-router-provider-status.service').read_text().splitlines()
        for line in ('Type=oneshot', 'User=' + installer.PROVIDER_STATUS_USER,
                     'Group=' + installer.PROVIDER_STATUS_USER,
                     'RuntimeDirectory=personal-codex-router-provider-status', 'RuntimeDirectoryMode=0755',
                     'RuntimeDirectoryPreserve=yes',
                     'UMask=0077', 'ProtectSystem=strict', 'ProtectHome=true', 'NoNewPrivileges=true',
                     'CapabilityBoundingSet=', 'SocketBindDeny=any',
                     'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX',
                     'SystemCallFilter=landlock_create_ruleset landlock_add_rule landlock_restrict_self',
                     'InaccessiblePaths=-/etc/cliproxyapi -/var/lib/cliproxyapi',
                     'InaccessiblePaths=-/var/lib/personal-codex-router/disconnected'):
            self.assertIn(line, unit)
        self.assertNotIn('User=cliproxyapi', unit)
        self.assertFalse(any(line.startswith('StateDirectory=') for line in unit))
        self.assertFalse(any(line.startswith('ReadWritePaths=') for line in unit))
        self.assertNotIn('IPAddressDeny=any', unit)  # The separate collector needs public HTTPS/DNS.
        dashboard = (ROOT / 'scripts/personal-codex-router-dashboard.service').read_text().splitlines()
        self.assertIn('IPAddressDeny=any', dashboard)
        self.assertIn('IPAddressAllow=localhost', dashboard)
        timer = (ROOT / 'scripts/personal-codex-router-provider-status.timer').read_text().splitlines()
        for line in ('OnBootSec=30', 'OnUnitActiveSec=5min', 'RandomizedDelaySec=30',
                     'Unit=personal-codex-router-provider-status.service', 'WantedBy=timers.target'):
            self.assertIn(line, timer)
        for suffix in ('service', 'timer'):
            name = 'personal-codex-router-provider-status.' + suffix
            self.assertIn((ROOT / 'scripts' / name, Path('/etc/systemd/system') / name, 0o644), files)

    def test_provider_status_user_creation_reuse_and_unsafe_identity_rejection(self):
        user = SimpleNamespace(pw_uid=987, pw_gid=987, pw_dir='/nonexistent', pw_shell='/usr/sbin/nologin')
        gateway = SimpleNamespace(pw_uid=988, pw_gid=988)
        with patch.object(installer.pwd, 'getpwnam', side_effect=[KeyError(), user, gateway]), \
                patch.object(installer.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=987)), \
                patch.object(installer.os, 'getgrouplist', return_value=[987]), \
                patch.object(installer, 'command') as command:
            PREPARE_PROVIDER_STATUS_USER()
            command.assert_called_once_with('useradd', '--system', '--user-group', '--no-create-home',
                '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', installer.PROVIDER_STATUS_USER)
        for groups, uid in (([987], 987), ([987, 988], 987), ([987], 988), ([987, 0], 987)):
            with self.subTest(groups=groups, uid=uid):
                user.pw_uid = uid
                with patch.object(installer.pwd, 'getpwnam', side_effect=[user, gateway]), \
                        patch.object(installer.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=987)), \
                        patch.object(installer.os, 'getgrouplist', return_value=groups), \
                        patch.object(installer, 'command') as command:
                    if groups == [987] and uid == 987:
                        PREPARE_PROVIDER_STATUS_USER()
                    else:
                        with self.assertRaises(RuntimeError):
                            PREPARE_PROVIDER_STATUS_USER()
                    command.assert_not_called()

    def test_request_outcomes_reader_manifest_sandbox_and_timer(self):
        files = installer.install_files()
        self.assertIn((ROOT / 'scripts/collect-request-outcomes.py',
                       installer.DEST / 'collect-request-outcomes.py', 0o755), files)
        for suffix in ('service', 'timer'):
            name = 'personal-codex-router-request-outcomes.' + suffix
            self.assertIn((ROOT / 'scripts' / name, Path('/etc/systemd/system') / name, 0o644), files)
        service = installer.render_unit((ROOT / 'scripts/personal-codex-router-request-outcomes.service').read_text())
        unit = service.splitlines()
        for line in ('Type=oneshot', 'User=cliproxyapi', 'Group=cliproxyapi',
                     'SupplementaryGroups=systemd-journal',
                     'ExecStart=/usr/bin/python3 /opt/personal-codex-router/collect-request-outcomes.py',
                     'StateDirectory=personal-codex-router', 'StateDirectoryMode=0700',
                     'ReadWritePaths=/var/lib/personal-codex-router',
                     'UMask=0077', 'TimeoutStartSec=240', 'NoNewPrivileges=true',
                     'PrivateTmp=true', 'ProtectSystem=strict', 'ProtectHome=true',
                     'ProtectKernelTunables=true', 'ProtectKernelModules=true',
                     'ProtectControlGroups=true', 'RestrictSUIDSGID=true',
                     'LockPersonality=true', 'CapabilityBoundingSet=',
                     'RestrictAddressFamilies=AF_UNIX', 'IPAddressDeny=any'):
            self.assertIn(line, unit)
        self.assertEqual([line for line in unit if line.startswith('ReadWritePaths=')],
                         ['ReadWritePaths=/var/lib/personal-codex-router'])
        self.assertNotIn('@', service)
        timer = (ROOT / 'scripts/personal-codex-router-request-outcomes.timer').read_text().splitlines()
        for line in ('OnBootSec=30', 'OnUnitActiveSec=60', 'AccuracySec=2',
                     'Unit=personal-codex-router-request-outcomes.service', 'WantedBy=timers.target'):
            self.assertIn(line, timer)
        for scope in ('account_access_only', 'claude_watch_only', 't3_usage_only'):
            names = {dest.name for _, dest, _ in installer.install_files(**{scope: True})}
            self.assertFalse({'collect-request-outcomes.py', 'personal-codex-router-request-outcomes.service',
                              'personal-codex-router-request-outcomes.timer'} & names)

    def test_t3_usage_backend_starts_without_older_dashboard_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for source,dest,_ in installer.install_files(t3_usage_only=True):
                installed=root / dest.relative_to(installer.DEST)
                installed.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,installed)
            # These existing scripts are loaded from outside dashboard/.
            for name in ('claude-redeemer.py','collect-claude-usage.py'):
                shutil.copyfile(ROOT / 'scripts' / name,root / name)
            result=subprocess.run([sys.executable,'-B','-c',
                                   "import server; assert server.routing_policy.identities('codex', []) == ()"],
                                  cwd=root / 'dashboard',capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_t3_usage_scope_dry_run_and_manifest_are_backend_only(self):
        files = installer.install_files(t3_usage_only=True)
        backend = {path.name for path in (ROOT / 'dashboard').glob('*.py')}
        self.assertEqual(set(files), {(ROOT / 'dashboard' / name, installer.DEST / 'dashboard' / name, 0o644)
                                     for name in backend})
        self.assertTrue(set(files) <= set(installer.install_files()))
        self.assertTrue({item for item in installer.install_files(account_access_only=True)
                         if item[0].parent == ROOT / 'dashboard'} <= set(files))
        self.assertEqual(installer.backup_paths(files, t3_usage_only=True), [dest for _, dest, _ in files])
        output = io.StringIO()
        with patch('sys.argv', ['install-operations.py', '--t3-usage-only']), redirect_stdout(output), \
                patch.object(installer, 'command') as command, patch.object(installer, 'overview') as overview, \
                patch.object(installer, 'install_file') as install, patch.object(installer, 'pre_apply_backup') as backup_call, \
                patch.object(installer, 'version_fingerprint') as fingerprint:
            installer.main()
        for operation in (command, overview, install, backup_call, fingerprint):
            operation.assert_not_called()
        self.assertIn('restart personal-codex-router-dashboard.service only', output.getvalue())
        self.assertIn('No keys provisioned', output.getvalue())
        self.assertEqual(output.getvalue().count('Install dashboard/'), len(backend))
        self.assertNotIn('Back up if present: /etc', output.getvalue())
        with patch('sys.argv', ['install-operations.py', '--t3-usage-only', '--account-access-only']), \
                redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                installer.main()

    def test_t3_usage_backup_copies_only_replaced_backend_and_never_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed, keys = root / 'server.py', root / 'keys.json'
            installed.write_text('old server')
            keys.write_text('{"private":"never copy"}')
            module = SimpleNamespace(sources=Mock(return_value=[keys]))
            original = module.sources
            module.run = Mock(side_effect=lambda: backup.snapshot(module.sources(), root / 'snapshot'))
            installer.pre_apply_backup(module, [(installed, installed, 0o644)], t3_usage_only=True)
            original.assert_not_called()
            self.assertIs(module.sources, original)
            entries = backup.verify(root / 'snapshot')['entries']
            self.assertEqual({row['path'] for row in entries}, {str(installed.relative_to('/'))})

    def test_t3_usage_apply_only_restarts_dashboard_and_has_no_runtime_api_calls(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / 'status.json').write_text(json.dumps({'snapshot': 'fixture'}))
            command = Mock(return_value='123')
            backup_call, install = Mock(), Mock()
            overview, prepare_watch, user_command = Mock(), Mock(), Mock()
            output = io.StringIO()
            for context in [patch('sys.argv', ['install-operations.py', '--t3-usage-only', '--apply']),
                    patch.object(installer, 'T3_USAGE_BACKUPS', root),
                    patch.object(installer.os, 'geteuid', return_value=0),
                    patch.object(installer, 'version_fingerprint', return_value='stable'),
                    patch.object(installer, 'overview', overview),
                    patch.object(installer, 'pre_apply_backup', backup_call),
                    patch.object(installer, 'prepare_watch_paths', prepare_watch),
                    patch.object(installer, 'command', command), patch.object(installer, 'user_command', user_command),
                    patch.object(installer, 'install_file', install), redirect_stdout(output)]:
                stack.enter_context(context)
            installer.main()
            self.assertEqual(backup_call.call_args.kwargs, {'t3_usage_only': True})
            self.assertEqual([c.args for c in command.call_args_list], [
                ('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi'),
                ('systemctl', 'restart', 'personal-codex-router-dashboard.service'),
                ('systemctl', 'show', '-p', 'MainPID', '--value', 'cliproxyapi')])
            for operation in (overview, prepare_watch, user_command):
                operation.assert_not_called()
            self.mock_provider_user.assert_not_called()
            self.assertEqual(install.call_args_list, [unittest.mock.call(*item) for item in installer.install_files(t3_usage_only=True)])
            self.assertEqual(json.loads(output.getvalue())['scope'], 't3-usage')

    def test_account_access_manifest_is_exact_and_preview_is_read_only(self):
        expected = {'series.py', 't3_usage.py', 'providers.py', 'usage_adapters.py', 'quota_headers.py', 'subscriptions.py', 'account_access.py', 'account_add.py', 'account_mutations.py', 'api_keys.py', 'signin.py', 'credential_replace.py', 'request_deadline.py', 'server.py',
                    'availability.py', 'operations.py', 'claude_warnings.py', 'routing_policy.py', 'static/index.html', 'static/app.js',
                    'static/billing.js', 'static/style.css', 'static/boards.js', 'static/boards.css',
                    'static/charts.js', 'static/widgets.js', 'static/charts.css', 'provider_status.py',
                    'fast_mode.py', 'codex_catalog.py'}
        units = ('personal-codex-router-preference.service', 'personal-codex-router-dashboard.service')
        files = installer.install_files(account_access_only=True)
        self.assertEqual({dest for _, dest, _ in files},
                         {installer.DEST / 'dashboard' / name for name in expected}
                         | {installer.DEST / name for name in ('router-preview.mjs', 'router-backup.py', 'site_config.py')}
                         | {Path('/etc/systemd/system') / unit for unit in units})
        self.assertEqual({source for source, _, _ in files},
                         {ROOT / 'dashboard' / name for name in expected}
                         | {ROOT / 'scripts' / name for name in ('router-preview.mjs', 'router-backup.py', 'site_config.py')}
                         | {ROOT / 'scripts' / unit for unit in units})
        self.assertEqual(len(files), len(expected) + 5)
        self.assertTrue(all(mode == (0o755 if source.name in ('router-preview.mjs', 'router-backup.py', 'site_config.py') else 0o644)
                            for source, _, mode in files))
        self.assertEqual(installer.backup_paths(files, account_access_only=True), [d for _, d, _ in files])
        output = io.StringIO()
        with patch('sys.argv', ['install-operations.py', '--account-access-only']), redirect_stdout(output), \
                patch.object(installer, 'command') as command, patch.object(installer, 'overview') as overview:
            installer.main()
        self.assertIn('restart personal-codex-router-dashboard.service and personal-codex-router-preview.service only', output.getvalue())
        self.assertIn('does not confirm account status', output.getvalue())
        command.assert_not_called()
        overview.assert_not_called()

    def test_scoped_installs_ship_signin_dependencies_with_full_install_destinations_and_modes(self):
        required = {'dashboard/' + name for name in (
            'account_access.py', 'credential_replace.py', 'signin.py', 'operations.py', 'server.py',
            'request_deadline.py', 'availability.py', 'routing_policy.py',
            'static/index.html', 'static/style.css', 'static/app.js', 'static/billing.js')}
        required.update('scripts/' + name for name in (
            'router-backup.py', 'router-preview.mjs', 'personal-codex-router-dashboard.service'))
        full = {source.relative_to(ROOT).as_posix(): (source, dest, mode)
                for source, dest, mode in installer.install_files()}
        for scope in ('account_access_only', 'claude_watch_only'):
            with self.subTest(scope=scope):
                files = installer.install_files(**{scope: True})
                self.assertTrue({full[name] for name in required}.issubset(set(files)))
                self.assertEqual(len(files), len({dest for _, dest, _ in files}))
                self.assertIn(installer.DEST / 'router-backup.py', installer.backup_paths(files, **{scope: True}))

    def test_account_access_backup_copies_only_replaced_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed, private = root / 'installed.py', root / 'auth.json'
            installed.write_text('old script')
            private.write_text('{"secret":"never copy"}')
            module = SimpleNamespace(sources=Mock(return_value=[private]))
            original = module.sources
            module.run = Mock(side_effect=lambda: backup.snapshot(module.sources(), root / 'snapshot'))
            installer.pre_apply_backup(module, [(installed, installed, 0o644)], account_access_only=True)
            original.assert_not_called()
            self.assertIs(module.sources, original)
            entries = backup.verify(root / 'snapshot')['entries']
            self.assertEqual({row['path'] for row in entries}, {str(installed.relative_to('/'))})

    def test_account_access_apply_restarts_dashboard_and_preview_only_and_leaves_watch_and_firewall_alone(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / 'account-access-deployment-backup.json').write_text(json.dumps({'snapshot': 'fixture'}))
            command, user_command, prepare_watch = Mock(return_value='123'), Mock(), Mock()
            backup_call, install = Mock(), Mock()
            output = io.StringIO()
            # Source checks are isolated from concurrently authored frontend assets.
            source_is_file = Path.is_file
            board_sources = {ROOT / 'dashboard/static' / name for name in
                             ('boards.js', 'boards.css', 'charts.js', 'widgets.js', 'charts.css')}
            for context in [patch('sys.argv', ['install-operations.py', '--account-access-only', '--apply']),
                    patch.object(installer, 'OPS', root), patch.object(installer.os, 'geteuid', return_value=0),
                    patch.object(installer.os, 'chown'), patch.object(Path, 'mkdir'), patch.object(Path, 'chmod'),
                    patch.object(installer.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=2345)),
                    patch.object(installer, 'version_fingerprint', return_value='stable'),
                    patch.object(installer, 'require_preview_tls'),
                    patch.object(installer, 'overview', return_value={'gateway': {'online': True, 'accounts': [{'email': 'account-a@example.com'}]}}),
                    patch.object(installer, 'pre_apply_backup', backup_call),
                    patch.object(installer, 'prepare_watch_paths', prepare_watch),
                    patch.object(installer, 'command', command), patch.object(installer, 'user_command', user_command),
                    patch.object(installer, 'install_file', install),
                    patch.object(Path, 'is_file', autospec=True, side_effect=lambda path: path in board_sources or source_is_file(path)), redirect_stdout(output)]:
                stack.enter_context(context)
            installer.main()
            self.assertEqual(backup_call.call_args.kwargs, {'account_access_only': True})
            self.assertEqual([c.args for c in command.call_args_list if 'restart' in c.args],
                             [('systemctl', 'restart', 'personal-codex-router-dashboard.service',
                               'personal-codex-router-preview.service')])
            self.assertTrue(all(c.args[0] == 'systemctl' for c in command.call_args_list))
            command.assert_any_call('systemctl', 'daemon-reload')
            user_command.assert_not_called()
            prepare_watch.assert_not_called()
            self.mock_provider_user.assert_not_called()
            self.assertEqual(install.call_count, len(installer.install_files(account_access_only=True)))
            self.assertFalse(json.loads(output.getvalue())['account_status_verified'])

    def test_install_set_covers_dashboard_units_and_unit_referenced_scripts(self):
        files = installer.install_files()
        sources = {source for source, _, _ in files}
        destinations = {dest for _, dest, _ in files}
        for source in (ROOT / 'dashboard').rglob('*'):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix not in ('.pyc', '.pyo'):
                with self.subTest(source=source):
                    self.assertIn(source, sources)
                    self.assertIn(installer.DEST / source.relative_to(ROOT), destinations)
        units = [source for source in (ROOT / 'scripts').iterdir()
                 if source.suffix in ('.service', '.timer', '.socket', '.path')]
        self.assertIn(ROOT / 'scripts/personal-codex-router-move.path', units)
        for unit in units:
            with self.subTest(unit=unit):
                self.assertIn(unit, sources)
                unit_dir = installer.USER_UNIT_DIR if unit.name in {
                    'personal-codex-router-' + suffix for suffix in installer.USER_UNITS
                } else Path('/etc/systemd/system')
                self.assertIn(unit_dir / unit.name, destinations)
                for name in re.findall(r'/opt/personal-codex-router/([^\s]+)', unit.read_text()):
                    if Path(name).suffix:
                        self.assertIn(installer.DEST / name, destinations)
        self.assertTrue(all(source.is_file() for source in sources))
        self.assertEqual(len(files), len(destinations))
        self.assertIn(Path('/usr/local/bin/router-preview'), destinations)

    def test_dashboard_discovery_includes_new_assets_and_excludes_caches(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            included = ('new_module.py', 'static/new-font.ttf', 'static/licenses/LICENSE.txt')
            excluded = ('__pycache__/module.pyc', 'static/.pytest_cache/state', 'static/old.pyc')
            for name in included + excluded:
                source = root / 'dashboard' / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('fixture')
            with patch.object(installer, 'ROOT', root):
                sources = {source for source, _, _ in installer.install_files()}
            for name in included:
                self.assertIn(root / 'dashboard' / name, sources)
            for name in excluded:
                self.assertNotIn(root / 'dashboard' / name, sources)

    def test_preview_lists_files_without_runtime_access(self):
        output = io.StringIO()
        with patch('sys.argv', ['install-operations.py']), redirect_stdout(output), \
                patch.object(installer, 'command') as command, \
                patch.object(installer, 'overview') as overview, \
                patch.object(installer, 'install_file') as install_file, \
                patch.object(installer.os, 'geteuid') as geteuid, \
                patch.object(installer, 'pre_apply_backup') as pre_apply_backup:
            installer.main()
        for source, dest, _ in installer.install_files():
            self.assertIn(f'Install {source.relative_to(ROOT)} -> {dest} ', output.getvalue())
        for operation in (command, overview, install_file, geteuid, pre_apply_backup):
            operation.assert_not_called()
        self.assertIn('Restart personal-codex-router-dashboard.service and personal-codex-router-preview.service',
                      output.getvalue())
        self.assertIn('Restart personal-codex-router-claude-redeemer.service for operator', output.getvalue())
        self.assertNotIn('restart only dashboard', output.getvalue())

    def test_apply_restarts_services_after_replacing_scripts_even_when_already_running(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            config, guard = root / 'previews.json', root / 'guard.nft'
            config.write_text('{}')
            guard.write_text('Personal project previews IPv4')
            (root / 'backup.json').write_text(json.dumps({'snapshot': 'fixture'}))
            user = SimpleNamespace(pw_uid=1234, pw_gid=1235)
            ts = {'Self': {'UserID': 1}, 'Peer': {'personal': {
                'UserID': 1, 'TailscaleIPs': ['100.64.0.11']}}}
            calls = Mock()
            command = Mock(side_effect=lambda *args, **kwargs: json.dumps(ts) if args[0] == 'tailscale' else
                           'active' if args[-1] == 'personal-codex-router-move.path' else '123')
            calls.attach_mock(command, 'command')
            requests = root / 'move-requests'
            requests.mkdir()
            user_command = Mock()
            calls.attach_mock(user_command, 'user_command')
            install_file = Mock()
            calls.attach_mock(install_file, 'install_file')
            files = installer.install_files()
            patches = [patch('sys.argv', ['install-operations.py', '--apply']),
                       patch.object(installer, 'OPS', root), patch.object(installer, 'CONFIG', config),
                       patch.object(installer, 'GUARD', guard), patch.object(installer, 'MOVE_REQUESTS', requests),
                       patch.object(installer.os, 'geteuid', return_value=0),
                       patch.object(installer.os, 'chown'), patch.object(Path, 'mkdir'), patch.object(Path, 'chmod'),
                       patch.object(installer.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=2345)),
                       patch.object(installer.pwd, 'getpwnam', return_value=user),
                       patch.object(installer, 'version_fingerprint', return_value='stable'),
                       patch.object(installer, 'overview', return_value={
                           'gateway': {'online': True, 'accounts': [{'email': 'account-a@example.com'}]}}),
                       patch.object(installer, 'pre_apply_backup'), patch.object(installer, 'prepare_watch_paths'),
                       patch.object(installer, 'device_preflight', return_value=None),
                       patch.object(installer, 'write_site'), patch.object(installer, 'write_previews'),
                       patch.object(installer, 'command', command),
                       patch.object(installer, 'user_command', user_command),
                       patch.object(installer, 'install_file', install_file), redirect_stdout(io.StringIO())]
            for context in patches:
                stack.enter_context(context)
            installer.main()
            self.assertEqual(install_file.call_count, len(files))
            self.mock_provider_user.assert_called_once_with()
            self.assertTrue(any(call.args[:3] == ('systemctl', 'enable', '--now') and
                                'personal-codex-router-provider-status.timer' in call.args
                                for call in command.call_args_list))
            self.assertTrue(any(call.args[:3] == ('systemctl', 'enable', '--now') and
                                'personal-codex-router-request-outcomes.timer' in call.args
                                for call in command.call_args_list))
            for timer in ('retention', 'release-check'):
                self.assertTrue(any(call.args[:3] == ('systemctl', 'enable', '--now') and
                                    'personal-codex-router-' + timer + '.timer' in call.args
                                    for call in command.call_args_list))
            command.assert_any_call('systemctl', 'restart', 'personal-codex-router-dashboard.service',
                                    'personal-codex-router-preview.service')
            user_command.assert_any_call(user, 'restart', 'personal-codex-router-claude-redeemer.service')
            user_command.assert_any_call(user, 'enable', '--now', 'personal-codex-router-claude-keepalive.timer')
            user_command.assert_any_call(user, 'enable', '--now', 'personal-codex-router-nightly-tests.timer')
            last_install = max(i for i, call in enumerate(calls.mock_calls) if call[0] == 'install_file')
            restarts = [i for i, call in enumerate(calls.mock_calls) if 'restart' in call.args]
            self.assertEqual(len(restarts), 2)
            self.assertTrue(all(i > last_install for i in restarts))
            enable = next(i for i, call in enumerate(calls.mock_calls)
                          if call[0] == 'command' and call.args[:3] == ('systemctl', 'enable', '--now')
                          and 'personal-codex-router-move.path' in call.args)
            ready = calls.mock_calls.index(unittest.mock.call.command(
                'systemctl', 'show', '-p', 'ActiveState', '--value', 'personal-codex-router-move.path'))
            self.assertTrue(enable < ready and all(i < ready for i in restarts))

    def test_move_readiness_requires_active_path_unit_and_request_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            requests = Path(directory) / 'requests'
            with patch.object(installer, 'MOVE_REQUESTS', requests), \
                    patch.object(installer, 'command', return_value='failed'):
                with self.assertRaisesRegex(RuntimeError, 'move.path is not active'):
                    installer.check_move_ready()
            clock = iter(range(0, 100, 5))
            with patch.object(installer, 'MOVE_REQUESTS', requests), \
                    patch.object(installer, 'command', return_value='active'), \
                    patch.object(installer.time, 'monotonic', side_effect=lambda: next(clock)), \
                    patch.object(installer.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'missing'):
                    installer.check_move_ready()
            # The directory appearing during the wait is accepted.
            with patch.object(installer, 'MOVE_REQUESTS', requests), \
                    patch.object(installer, 'command', return_value='active'), \
                    patch.object(installer.time, 'sleep', side_effect=lambda _: requests.mkdir()):
                installer.check_move_ready()

    def test_only_the_full_install_ships_and_enables_conversation_moves(self):
        full = {dest.name for _, dest, _ in installer.install_files()}
        self.assertTrue(set(installer.MOVE_FILES).issubset(full))
        self.assertIn('personal-codex-router-dashboard.service', full)
        dashboard = (ROOT / 'scripts/personal-codex-router-dashboard.service').read_text().splitlines()
        self.assertIn('RuntimeDirectory=' + installer.MOVE_REQUESTS.name, dashboard)
        for scope in ('account_access_only', 'claude_watch_only'):
            with self.subTest(scope=scope):
                names = {dest.name for _, dest, _ in installer.install_files(**{scope: True})}
                self.assertFalse(set(installer.MOVE_FILES) & names)
                output = io.StringIO()
                flag = '--' + scope.replace('_', '-')
                with patch('sys.argv', ['install-operations.py', flag]), redirect_stdout(output), \
                        patch.object(installer, 'command') as command:
                    installer.main()
                self.assertIn(installer.SCOPED_MOVE_NOTE, output.getvalue())
                self.assertIn('last full install', installer.SCOPED_MOVE_NOTE)
                self.assertNotIn('unavailable', output.getvalue())
                command.assert_not_called()

    def test_backup_scope_covers_routing_and_all_install_destinations(self):
        files = installer.install_files()
        paths = installer.backup_paths(files)
        for _, dest, _ in files:
            self.assertIn(dest, paths)
        for path in (installer.ROUTING, installer.OPS, installer.WATCH, installer.CONFIG.parent, installer.GUARD):
            self.assertIn(path, paths)

    def test_claude_redeemer_installed_and_private_state_backed_up_without_socket(self):
        files = installer.install_files()
        self.assertIn((ROOT / 'scripts/claude-redeemer.py', installer.DEST / 'claude-redeemer.py', 0o755), files)
        self.assertNotIn(installer.CLAUDE_STATE, installer.backup_paths(files))
        with patch.object(Path, 'exists', return_value=True), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'glob', return_value=[]):
            self.assertIn(installer.CLAUDE_STATE, backup.sources())
        self.assertNotIn(installer.WATCH / 'claude-redeem.sock', installer.backup_paths(files))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'claude-resets.json').write_text('{}')
            # A UNIX socket is a directory entry but not a regular file.
            socket_path = root / 'claude-redeem.sock'
            socket_path.touch()
            original = Path.is_file
            with patch.object(Path, 'is_file', lambda p: False if p == socket_path else original(p)):
                self.assertEqual(backup.regular_files([root]), [root / 'claude-resets.json'])

    def test_claude_watch_installation_and_preview_are_user_scoped(self):
        files = installer.install_files()
        self.assertIn((ROOT / 'scripts/collect-claude-usage.py',
                       installer.DEST / 'collect-claude-usage.py', 0o755), files)
        for suffix in installer.USER_UNITS:
            name = 'personal-codex-router-' + suffix
            self.assertIn((ROOT / 'scripts' / name, installer.USER_UNIT_DIR / name, 0o644), files)
            self.assertNotIn(Path('/etc/systemd/system') / name, {dest for _, dest, _ in files})
        output = io.StringIO()
        with patch('sys.argv', ['install-operations.py']), redirect_stdout(output):
            installer.main()
        self.assertIn('operator:cliproxyapi, 2750', output.getvalue())
        self.assertIn('Enable/start personal-codex-router-claude-usage.timer for operator', output.getvalue())
        self.assertIn(str(installer.WATCH), output.getvalue())

    def test_keepalive_script_units_preview_and_backups(self):
        files = installer.install_files()
        self.assertIn((ROOT / 'scripts/claude-token-keepalive.py',
                       installer.DEST / 'claude-token-keepalive.py', 0o755), files)
        output = io.StringIO()
        with patch('sys.argv', ['install-operations.py']), redirect_stdout(output):
            installer.main()
        self.assertIn('Enable/start personal-codex-router-claude-keepalive.timer for operator', output.getvalue())
        for name in ('claude-token-keepalive.py', 'personal-codex-router-claude-keepalive.service',
                     'personal-codex-router-claude-keepalive.timer'):
            dest = (installer.DEST if name.endswith('.py') else installer.USER_UNIT_DIR) / name
            self.assertIn(dest, installer.backup_paths(files))
            self.assertIn(f'Back up if present: {dest}', output.getvalue())
            self.assertIn(f'-> {dest}', output.getvalue())

    def test_watch_directory_and_new_user_unit_ancestors_have_correct_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            watch = Path(directory) / 'watch'
            units = Path(directory) / 'home/.config/systemd/user'
            units.parent.parent.parent.mkdir()
            user = SimpleNamespace(pw_uid=1234, pw_gid=1235)
            with patch.object(installer, 'WATCH', watch), patch.object(installer, 'USER_UNIT_DIR', units), \
                    patch.object(installer, 'CLAUDE_STATE', Path(directory) / 'private-state'), \
                    patch.object(installer.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=2345)), \
                    patch.object(installer.os, 'chown') as chown:
                installer.prepare_watch_paths(user)
            self.assertEqual(watch.stat().st_mode & 0o7777, 0o2750)
            self.assertIn(unittest.mock.call(watch, 1234, 2345), chown.call_args_list)
            for path in (units.parent.parent, units.parent, units):
                self.assertIn(unittest.mock.call(path, 1234, 1235), chown.call_args_list)

    def test_user_systemctl_uses_lingering_users_bus(self):
        user = SimpleNamespace(pw_uid=1234)
        with patch.object(installer, 'command') as command:
            installer.user_command(user, 'daemon-reload')
            installer.user_command(user, 'enable', '--now', 'personal-codex-router-claude-usage.timer')
        self.assertEqual(command.call_args_list[0].args,
                         ('/usr/sbin/runuser', '-u', 'operator', '--', '/usr/bin/env',
                          'XDG_RUNTIME_DIR=/run/user/1234',
                          'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1234/bus',
                          '/usr/bin/systemctl', '--user', 'daemon-reload'))
        self.assertEqual(command.call_args.args[-3:],
                         ('enable', '--now', 'personal-codex-router-claude-usage.timer'))

    def test_install_file_can_own_user_units_without_changing_root_default(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory) / 'source', Path(directory) / 'unit'
            source.write_text('fixture')
            with patch.object(installer.os, 'chown') as chown:
                installer.install_file(source, dest, 0o644, 1234, 1235)
                self.assertEqual(chown.call_args.args[1:], (1234, 1235))
                installer.install_file(source, dest)
                self.assertEqual(chown.call_args.args[1:], (0, 0))
            self.assertEqual(dest.read_text(), 'fixture')

    def test_pre_apply_backup_preserves_state_helper_and_settings(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            routing, ops, watch = root / 'routing', root / 'ops', root / 'watch'
            config, guard, helper = root / 'config/previews.json', root / 'guard.nft', root / 'router-preview'
            paths = [routing / name for name in ('baseline.json', 'selection.json', 'status.json')]
            paths += [ops / name for name in ('backup.json', 'deployment.json')]
            paths += [watch / 'claude.json']
            paths += [config, guard, helper]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'fixture': path.name}) if path.suffix == '.json' else 'fixture')
            original_sources = lambda: []
            module = SimpleNamespace(sources=original_sources)

            def run():
                backup.snapshot(module.sources(), root / 'snapshot')
                backup.verify(root / 'snapshot')

            module.run = Mock(side_effect=run)
            files = [(ROOT / 'scripts/router-preview.py', helper, 0o755)]
            with patch.object(installer, 'ROUTING', routing), patch.object(installer, 'OPS', ops), \
                    patch.object(installer, 'WATCH', watch), patch.object(installer, 'CONFIG', config), \
                    patch.object(installer, 'GUARD', guard):
                installer.pre_apply_backup(module, files)
            module.run.assert_called_once_with()
            self.assertIs(module.sources, original_sources)
            manifest = backup.verify(root / 'snapshot')
            self.assertEqual({row['path'] for row in manifest['entries']},
                             {str(path.relative_to('/')) for path in paths})

    def test_pre_apply_backup_restores_sources_when_backup_fails(self):
        original_sources = lambda: []
        module = SimpleNamespace(sources=original_sources, run=Mock(side_effect=RuntimeError('failed')))
        with self.assertRaises(RuntimeError):
            installer.pre_apply_backup(module, [])
        self.assertIs(module.sources, original_sources)

    def test_watch_only_preview_and_backup_exclude_private_config_and_other_services(self):
        files = installer.install_files(True)
        destinations = {dest for _, dest, _ in files}
        self.assertIn(installer.DEST / 'dashboard/server.py', destinations)
        self.assertIn(installer.DEST / 'collect-claude-usage.py', destinations)
        for suffix in ('claude-b-usage.service', 'claude-b-usage.timer'):
            self.assertIn(installer.USER_UNIT_DIR / ('personal-codex-router-' + suffix), destinations)
        self.assertNotIn(installer.USER_UNIT_DIR / 'personal-codex-router-claude-redeemer.service', destinations)
        self.assertNotIn(Path('/etc/systemd/system/personal-codex-router-preview.service'), destinations)
        self.assertIn(installer.DEST / 'router-preview.mjs', destinations)
        self.assertIn(Path('/etc/systemd/system/personal-codex-router-preference.service'), destinations)
        self.assertNotIn(Path('/etc/systemd/system/personal-codex-router-preference.timer'), destinations)
        # Its dashboard code writes credentials, so the unit granting that access ships with it.
        self.assertIn((ROOT / 'scripts/personal-codex-router-dashboard.service',
                       Path('/etc/systemd/system/personal-codex-router-dashboard.service'), 0o644), files)
        output = io.StringIO()
        with patch('sys.argv', ['install-operations.py', '--claude-watch-only']), redirect_stdout(output), \
                patch.object(installer, 'command') as command:
            installer.main()
        self.assertIn('do not read or copy the setup-token', output.getvalue())
        self.assertNotIn('Restart personal-codex-router-claude-redeemer', output.getvalue())
        command.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watch, private, installed = root / 'watch', root / 'private-config', root / 'installed.py'
            watch.mkdir()
            (watch / 'claude-b.json').write_text('{}')
            private.mkdir()
            (private / 'claude-b.token').write_text('never-copy')
            installed.write_text('old script')
            module = SimpleNamespace(sources=Mock(return_value=[private]))
            sources = module.sources
            module.run = Mock(side_effect=lambda: backup.snapshot(module.sources(), root / 'snapshot'))
            with patch.object(installer, 'WATCH', watch):
                installer.pre_apply_backup(module, [(installed, installed, 0o755)], True)
            sources.assert_not_called()
            entries = backup.verify(root / 'snapshot')['entries']
            self.assertEqual({row['path'] for row in entries},
                             {str(p.relative_to('/')) for p in (watch / 'claude-b.json', installed)})

    def test_dashboard_unit_grants_write_access_to_the_auth_dir(self):
        unit = (ROOT / 'scripts/personal-codex-router-dashboard.service').read_text()
        paths = [path for line in unit.splitlines() if line.startswith('ReadWritePaths=')
                 for path in line.split('=', 1)[1].split()]
        # Optional: a missing auth dir must not stop the dashboard; sign-in then fails closed at start.
        self.assertIn('-/var/lib/cliproxyapi/auth', paths)
        self.assertNotIn('/var/lib/cliproxyapi/auth', paths)
        self.assertIn('/var/lib/personal-codex-router', paths)
        self.assertIn('ProtectSystem=strict', unit.splitlines())
        self.assertIn('User=cliproxyapi', unit.splitlines())

    def test_watch_only_apply_restarts_dashboard_and_preview_and_enables_b_timer(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / 'claude-watch-deployment-backup.json').write_text(json.dumps({'snapshot': 'fixture'}))
            user = SimpleNamespace(pw_uid=1234, pw_gid=1235)
            command = Mock(return_value='123')
            user_command = Mock()
            backup_call = Mock()
            for context in [patch('sys.argv', ['install-operations.py', '--claude-watch-only', '--apply']),
                    patch.object(installer, 'OPS', root), patch.object(installer.os, 'geteuid', return_value=0),
                    patch.object(installer.os, 'chown'), patch.object(Path, 'mkdir'), patch.object(Path, 'chmod'),
                    patch.object(installer.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=2345)),
                    patch.object(installer.pwd, 'getpwnam', return_value=user),
                    patch.object(installer, 'version_fingerprint', return_value='stable'),
                    patch.object(installer, 'require_preview_tls'),
                    patch.object(installer, 'overview', return_value={'gateway': {'online': True, 'accounts': [{'email': 'account-a@example.com'}]}}),
                    patch.object(installer, 'pre_apply_backup', backup_call),
                    patch.object(installer, 'prepare_watch_paths'), patch.object(installer, 'command', command),
                    patch.object(installer, 'user_command', user_command), patch.object(installer, 'install_file'),
                    redirect_stdout(io.StringIO())]:
                stack.enter_context(context)
            installer.main()
            self.assertEqual(backup_call.call_args.args[2], True)
            self.assertEqual([c.args for c in command.call_args_list if 'restart' in c.args],
                             [('systemctl', 'restart', 'personal-codex-router-dashboard.service',
                               'personal-codex-router-preview.service')])
            self.assertIn(('systemctl', 'daemon-reload'), [c.args for c in command.call_args_list])
            self.assertEqual([c.args for c in user_command.call_args_list], [
                (user, 'daemon-reload'), (user, 'enable', '--now', 'personal-codex-router-claude-b-usage.timer')])

    def test_pre_apply_backup_rejects_symlinked_targets(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            target = root / 'target'
            target.write_text('fixture')
            link = root / 'link'
            link.symlink_to(target)
            module = SimpleNamespace(sources=lambda: [])
            module.run = Mock(side_effect=lambda: module.sources())
            with patch.object(installer, 'backup_paths', return_value=[link]), self.assertRaises(RuntimeError):
                installer.pre_apply_backup(module, [])


if __name__ == '__main__':
    unittest.main()
