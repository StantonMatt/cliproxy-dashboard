import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gateway_config as config


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


enable = load('enable-dashboard')
rollback = load('rollback-dashboard')
account = load('verify-account-failover')
reset = load('verify-reset-routing')
quota = load('verify-quota-failover')
FIXTURES = ROOT / 'tests/fixtures'
SOURCES = [(name, (FIXTURES / filename).read_bytes()) for name, filename in (
    ('legacy', 'config-legacy.yaml'), ('v8', 'config-v8-nested.yaml'))]
EXPECTED = {
    'allow-remote': False, 'disable-control-panel': True, 'disable-auto-update-panel': True,
    'usage-statistics-enabled': False, 'request-retry': 0, 'max-retry-credentials': 2,
    'max-retry-interval': 0, 'disable-cooling': True, 'save-cooldown-status': True,
    'force-model-prefix': True, 'antigravity-credits': True, 'strategy': 'round-robin',
    'session-affinity': True, 'session-affinity-ttl': '1h', 'session-affinity-subagents': True,
    'secret-key': yaml.safe_load(SOURCES[0][1])['remote-management']['secret-key'],
}


def set_setting(mapping, setting, value):
    path = config.setting_path(mapping, setting)
    node = mapping
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def unconfigured(raw):
    return config.validated_edit(raw, {'secret-key': ''})


class GatewayConfigTests(unittest.TestCase):
    def test_fixture_provenance_and_every_accessor(self):
        provenance = json.loads((FIXTURES / 'config-provenance.json').read_text())
        self.assertEqual(provenance['patch_count'], 1)
        for label, raw in SOURCES:
            with self.subTest(layout=label):
                key = 'before_sha256' if label == 'legacy' else 'after_sha256'
                self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance[key])
                mapping = yaml.safe_load(raw)
                self.assertEqual(config.layout(mapping), label)
                self.assertEqual(set(EXPECTED), set(config.PATHS))
                for setting, expected in EXPECTED.items():
                    with self.subTest(setting=setting):
                        self.assertEqual(config.get(mapping, setting), expected)

    def test_every_scalar_edit_preserves_everything_else_and_comments(self):
        for label, raw in SOURCES:
            raw = b'# unchanged header\n' + raw + b'# unchanged footer\n'
            for setting, before in EXPECTED.items():
                with self.subTest(layout=label, setting=setting):
                    value = not before if isinstance(before, bool) else (
                        before + 1 if isinstance(before, int) else 'dummy-edited-value')
                    expected = yaml.safe_load(raw)
                    set_setting(expected, setting, value)
                    edited = config.validated_edit(raw, {setting: value})
                    self.assertEqual(yaml.safe_load(edited), expected)
                    self.assertEqual(config.get(yaml.safe_load(edited), setting), value)
                    self.assertTrue(edited.startswith(b'# unchanged header\n'))
                    self.assertTrue(edited.endswith(b'# unchanged footer\n'))

    def test_layout_validation_and_missing_values(self):
        for invalid in ({'config-version': 9}, {'config-version': '8'},
                        {'config-version': 8, 'usage-statistics-enabled': True},
                        {'management': {}, 'remote-management': {}},
                        {'config-version': 8, 'quota-exceeded': {'antigravity-credits': False}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                config.layout(invalid)
        self.assertEqual(config.layout({'management': {}}), 'v8')
        for mapping in ({}, {'config-version': 8}):
            for setting in config.PATHS:
                with self.subTest(mapping=mapping, setting=setting):
                    self.assertIsNone(config.get(mapping, setting, None))
                    with self.assertRaisesRegex(ValueError, 'Missing configuration setting'):
                        config.get(mapping, setting)
            self.assertEqual(config.isolated_policy(mapping), {})

    def test_unsafe_edits_refused(self):
        for raw, updates in (
            (b'remote-management: {secret-key: "", secret-key: ""}\n', {'secret-key': 'dummy'}),
            (b'remote-management: {secret-key: ""}\n', {'disable-control-panel': False}),
            (b'remote-management: {secret-key: []}\n', {'secret-key': 'dummy'}),
            (b'remote-management:\n  secret-key: &key ""\nother: *key\n', {'secret-key': 'dummy'}),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.validated_edit(raw, updates)

    def test_same_inode_fsync_and_concurrent_change_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'config.yaml'
            target.write_bytes(SOURCES[0][1]); target.chmod(0o600)
            original = target.read_bytes()
            candidate = config.validated_edit(original, {'usage-statistics-enabled': True})
            stat = target.stat()
            with patch.object(config.os, 'fsync', wraps=os.fsync) as sync:
                config.write_same_inode(target, candidate, original)
            sync.assert_called_once()
            after = target.stat()
            self.assertEqual((after.st_ino, after.st_mode, after.st_uid, after.st_gid),
                             (stat.st_ino, stat.st_mode, stat.st_uid, stat.st_gid))
            self.assertEqual(target.read_bytes(), candidate)
            with self.assertRaisesRegex(ValueError, 'changed during preparation'):
                config.write_same_inode(target, original, original)
            self.assertEqual(target.read_bytes(), candidate)

    def test_activation_both_layouts_and_refuse_reactivation(self):
        for label, raw in SOURCES:
            with self.subTest(layout=label):
                with self.assertRaisesRegex(AssertionError, 'already configured'):
                    enable.activation_candidate(raw, 'dummy-new-hash', False)
                original = unconfigured(raw)
                with self.assertRaisesRegex(AssertionError, 'key already exists'):
                    enable.activation_candidate(original, 'dummy-new-hash', True)
                unsafe = config.validated_edit(original, {'allow-remote': True})
                with self.assertRaises(AssertionError):
                    enable.activation_candidate(unsafe, 'dummy-new-hash', False)
                expected = yaml.safe_load(original)
                for setting, value in {'secret-key': 'dummy-new-hash', 'disable-control-panel': False,
                    'disable-auto-update-panel': True, 'usage-statistics-enabled': True}.items():
                    set_setting(expected, setting, value)
                self.assertEqual(yaml.safe_load(enable.activation_candidate(original, 'dummy-new-hash', False)),
                                 expected)

    def test_activation_requires_existing_keys_except_auto_update(self):
        for label, raw in SOURCES:
            for setting in ('secret-key', 'allow-remote', 'disable-control-panel',
                            'usage-statistics-enabled', 'disable-auto-update-panel'):
                with self.subTest(layout=label, setting=setting):
                    mapping = yaml.safe_load(unconfigured(raw))
                    node = mapping
                    path = config.setting_path(mapping, setting)
                    for key in path[:-1]: node = node[key]
                    del node[path[-1]]
                    original = yaml.safe_dump(mapping, sort_keys=False).encode()
                    if setting == 'disable-auto-update-panel':
                        candidate = enable.activation_candidate(original, 'dummy-new-hash', False)
                        self.assertTrue(config.get(yaml.safe_load(candidate), setting))
                    else:
                        with self.assertRaisesRegex(ValueError, 'Missing configuration setting'):
                            enable.activation_candidate(original, 'dummy-new-hash', False)

    def test_activation_accepts_existing_implicit_null_key_and_preserves_comments(self):
        for label, raw in SOURCES:
            for suffix in (b'', b' # empty management key'):
                with self.subTest(layout=label, suffix=suffix):
                    original = unconfigured(raw).replace(b'secret-key: ""', b'secret-key:' + suffix)
                    self.assertIsNone(config.get(yaml.safe_load(original), 'secret-key'))
                    candidate = enable.activation_candidate(original, 'dummy-new-hash', False)
                    self.assertEqual(config.get(yaml.safe_load(candidate), 'secret-key'), 'dummy-new-hash')
                    if suffix:
                        self.assertIn(suffix, candidate)

    def test_policy_normalization_preserves_all_settings_without_source_mutation(self):
        legacy = yaml.safe_load(SOURCES[0][1])
        expected = {key: copy.deepcopy(legacy[key]) for key in (
            'routing', *config.RETRY, *config.COOLDOWN, 'force-model-prefix', 'quota-exceeded')}
        for label, raw in SOURCES:
            with self.subTest(layout=label):
                mapping = yaml.safe_load(raw)
                before = copy.deepcopy(mapping)
                policy = config.isolated_policy(mapping)
                self.assertEqual(policy, expected)
                for module in (account, reset.support):
                    result = module.isolated_config(mapping, 12345, Path('/tmp/dummy-auth'), 'dummy-api', 'dummy-management')
                    for key, value in expected.items():
                        self.assertEqual(result[key], value)
                    self.assertEqual(result['remote-management']['secret-key'], 'dummy-management')
                    self.assertEqual(result['auth-dir'], '/tmp/dummy-auth')
                    self.assertEqual(result['host'], '127.0.0.1')
                    self.assertNotIn('config-version', result)
                    self.assertNotIn('oauth', result)
                policy['routing']['strategy'] = 'changed-test-copy'
                self.assertEqual(mapping, before)

    def test_nested_rollback_refused_even_with_matching_hash(self):
        for matching in (True, False):
            with self.subTest(matching=matching), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / 'docs').mkdir()
                target = root / 'config.yaml'; target.write_bytes(SOURCES[1][1])
                manifest = {'backup': str(root), 'enabled_sha256':
                    hashlib.sha256(target.read_bytes()).hexdigest() if matching else 'does-not-match'}
                (root / 'docs/activation.json').write_text(json.dumps(manifest))
                with patch.object(rollback, 'PROJECT', root), patch.object(rollback, 'CONFIG', target), \
                        patch.object(rollback.os, 'geteuid', return_value=0), \
                        patch.object(rollback.subprocess, 'run') as run:
                    with self.assertRaisesRegex(RuntimeError, 'manual, reviewed.*gateway-config-v8-migration.md'):
                        rollback.main(['--apply'])
                run.assert_not_called()
                self.assertEqual(target.read_bytes(), SOURCES[1][1])

    def test_legacy_rollback_exact_hash_guard_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'docs').mkdir()
            target = root / 'config.yaml'; target.write_bytes(SOURCES[0][1])
            (root / 'docs/activation.json').write_text(json.dumps({'backup': str(root), 'enabled_sha256': 'mismatch'}))
            with patch.object(rollback, 'PROJECT', root), patch.object(rollback, 'CONFIG', target), \
                    patch.object(rollback.os, 'geteuid', return_value=0), patch.object(rollback.subprocess, 'run') as run:
                with self.assertRaisesRegex(AssertionError, 'Config has changed since activation; merge the rollback manually.'):
                    rollback.main(['--apply'])
            run.assert_not_called()


EXEC_START = ('{ path=/opt/cliproxyapi/8.0.7/cli-proxy-api ; '
              'argv[]=/opt/cliproxyapi/8.0.7/cli-proxy-api -config /etc/cliproxyapi/config.yaml ; '
              'ignore_errors=no ; start_time=[Wed 2026-09-30 21:57:47 -03] ; '
              'stop_time=[n/a] ; pid=1220732 ; code=(null) ; status=0/0 }')


class BinaryResolverTests(unittest.TestCase):
    def test_effective_execstart_and_fallback_table(self):
        for value, expected in (
            (EXEC_START, '/opt/cliproxyapi/8.0.7/cli-proxy-api'),
            (EXEC_START.replace('/opt/cliproxyapi/8.0.7/cli-proxy-api', '/opt/cliproxyapi/custom/cli-proxy-api'),
             '/opt/cliproxyapi/custom/cli-proxy-api'),
            ('', str(config.FALLBACK_BINARY)), ('{ path=relative ; }', str(config.FALLBACK_BINARY)),
            (EXEC_START + EXEC_START, str(config.FALLBACK_BINARY)),
            ('{ path=/opt/unknown binary ; }', str(config.FALLBACK_BINARY)),
        ):
            with self.subTest(value=value), patch.object(config.subprocess, 'run', return_value=SimpleNamespace(stdout=value)) as run:
                self.assertEqual(config.resolve_gateway_binary(), Path(expected))
                run.assert_called_once_with(['systemctl', 'show', 'cliproxyapi', '--property=ExecStart', '--value'],
                    capture_output=True, text=True, check=True, timeout=5)
        for error in (FileNotFoundError(), subprocess.CalledProcessError(1, ['systemctl']),
                      subprocess.TimeoutExpired(['systemctl'], 5)):
            with self.subTest(error=error), patch.object(config.subprocess, 'run', side_effect=error):
                self.assertEqual(config.resolve_gateway_binary(), config.FALLBACK_BINARY)

    def test_verification_help_never_queries_service(self):
        for module in (account, reset, quota):
            with self.subTest(module=module.__name__), \
                    patch.object(config, 'resolve_gateway_binary') as resolve, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    module.main(['--help'])
                self.assertEqual(stopped.exception.code, 0)
                resolve.assert_not_called()

    def test_account_and_reset_binary_reaches_launch_with_both_layouts(self):
        class StopAtLaunch(Exception): pass
        temp_factory = tempfile.TemporaryDirectory
        for module in (account, reset):
            support = account if module is account else reset.support
            for label, raw in SOURCES:
                for explicit in (True, False):
                    with self.subTest(module=module.__name__, layout=label, explicit=explicit), temp_factory() as directory:
                        root = Path(directory); source = root / 'config.yaml'; source.write_bytes(raw)
                        auth = root / 'auth'; auth.mkdir()
                        emails = ['a@example.invalid', 'b@example.invalid']
                        for index, email in enumerate(emails):
                            (auth / f'{index}.json').write_text(json.dumps({'type': 'codex', 'email': email, 'access_token': 'dummy', 'refresh_token': ''}))
                        chosen = Path('/tmp/dummy-explicit-binary' if explicit else '/tmp/dummy-running-binary')
                        captured = []
                        def launch(command, **kwargs):
                            captured.append(json.loads(Path(command[2]).read_text()))
                            self.assertEqual(command[0], str(chosen))
                            raise StopAtLaunch()
                        def temporary(**kwargs):
                            return temp_factory(dir=root)
                        with contextlib.ExitStack() as stack:
                            for mock in (
                                patch.object(support, 'CONFIG', source), patch.object(support, 'AUTH', auth),
                                patch.object(support, 'EMAILS', emails),
                                patch.object(reset.policy, 'EMAILS', emails),
                                patch.object(support, 'snapshot', return_value={}),
                                patch.object(module.os, 'geteuid', return_value=0),
                                patch.object(module.os, 'chown'),
                                patch.object(module.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())),
                                patch.object(module.tempfile, 'TemporaryDirectory', side_effect=temporary),
                                patch.object(module.subprocess, 'Popen', side_effect=launch),
                                contextlib.redirect_stdout(io.StringIO()),
                            ): stack.enter_context(mock)
                            resolve = stack.enter_context(patch.object(config, 'resolve_gateway_binary', return_value=chosen))
                            with self.assertRaises(StopAtLaunch):
                                module.main(['--binary', str(chosen)] if explicit else [])
                            self.assertEqual(resolve.call_count, 0 if explicit else 1)
                        self.assertEqual(len(captured), 1)
                        policy = config.isolated_policy(yaml.safe_load(raw))
                        self.assertEqual({key: captured[0][key] for key in policy}, policy)

    def test_quota_binary_override_and_default(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                chosen = root / 'dummy-binary'; chosen.write_bytes(b'dummy')
                output = root / 'report.json'
                with patch.object(config, 'resolve_gateway_binary', return_value=chosen) as resolve, \
                        patch.object(quota, 'TEMP', root), patch.object(quota.signal, 'signal'), \
                        patch.object(quota.subprocess, 'run', return_value=SimpleNamespace(
                            stdout='CLIProxyAPI Version: 8.0.7, Commit: dummy,', stderr='')) as banner, \
                        patch.object(quota, 'run', return_value=([], [])) as run, \
                        contextlib.redirect_stdout(io.StringIO()):
                    quota.main(['--output', str(output)] + (['--binary', str(chosen)] if explicit else []))
                self.assertEqual(resolve.call_count, 0 if explicit else 1)
                self.assertEqual(banner.call_args.args[0], [str(chosen), '-h'])
                self.assertEqual(run.call_args.args[1], chosen)
                self.assertEqual(json.loads(output.read_text())['binary'], str(chosen))


if __name__ == '__main__':
    unittest.main()
