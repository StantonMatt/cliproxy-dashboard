import configparser
import datetime as dt
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('move_conversations', ROOT / 'scripts/move-conversations.py')
move = importlib.util.module_from_spec(spec)
spec.loader.exec_module(move)
move.load_config_modules()

V8 = ROOT / 'tests/fixtures/config-v8-nested.yaml'
LEGACY = ROOT / 'tests/fixtures/config-legacy.yaml'
RESULT_KEYS = {'request_inode', 'outcome', 'code', 'value', 'inode_before', 'inode_after', 'sha256', 'at'}


class HelperCase(unittest.TestCase):
    fixture = V8

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.yaml'
        self.config.write_bytes(self.fixture.read_bytes())
        self.config.chmod(0o640)
        self.state = self.root / 'state'
        self.requests = self.root / 'requests'
        self.requests.mkdir(mode=0o770)
        self.request = self.requests / 'request'
        self.runtime = self.root / 'runtime'
        self.uid = os.stat(self.config).st_uid
        self.gid = os.stat(self.config).st_gid

    def write_request(self):
        """Create the request as the dashboard does: an empty 0640 file; return its inode."""
        fd = os.open(self.request, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        try:
            return os.fstat(fd).st_ino
        finally:
            os.close(fd)

    def handle(self, request=True, now=None, **kwargs):
        if request:
            self.write_request()
        return move.handle(self.request, self.config, self.state, self.runtime, now=now,
                           required_uid=self.uid, required_gid=self.gid, **kwargs)

    def forget_last_flip(self):
        (self.state / 'last-flip').unlink(missing_ok=True)

    def ttl(self):
        return move.gateway_config.get(move.yaml.safe_load(self.config.read_text()), 'session-affinity-ttl')

    def saved_result(self):
        return json.loads((self.runtime / 'result.json').read_text())


class FlipTests(HelperCase):
    def check_both_directions(self):
        original = self.config.read_bytes()
        before = self.config.stat()
        result = self.handle()
        self.assertEqual((result['outcome'], result['code'], result['value']), ('written', 'written', '1h0m1s'))
        first = self.config.read_bytes()
        # Byte-for-byte identical apart from the TTL scalar.
        self.assertEqual(first, original.replace(b'session-affinity-ttl: 1h\n',
                                                 b'session-affinity-ttl: "1h0m1s"\n', 1))
        self.forget_last_flip()
        result = self.handle()
        self.assertEqual((result['outcome'], result['value']), ('written', '1h0m0s'))
        second = self.config.read_bytes()
        self.assertEqual(second, first.replace(b'"1h0m1s"', b'"1h0m0s"', 1))
        self.assertEqual(len(second), len(first))
        self.forget_last_flip()
        self.assertEqual(self.handle()['value'], '1h0m1s')
        after = self.config.stat()
        self.assertEqual((after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)),
                         (before.st_ino, before.st_uid, before.st_gid, 0o640))
        self.assertEqual(result['inode_before'], before.st_ino)
        self.assertEqual(result['inode_after'], before.st_ino)

    def test_v8_flips_both_directions_in_place(self):
        self.check_both_directions()

    def test_legacy_layout_flips_both_directions_in_place(self):
        self.config.write_bytes(LEGACY.read_bytes())
        self.check_both_directions()

    def test_accepts_canonical_hour_and_writes_same_length_value(self):
        self.config.write_bytes(self.config.read_bytes().replace(b'session-affinity-ttl: 1h\n',
                                                                 b'session-affinity-ttl: 1h0m0s\n'))
        result = self.handle()
        self.assertEqual(result['value'], '1h0m1s')
        self.assertEqual(self.ttl(), '1h0m1s')

    def test_refuses_an_edit_that_would_shrink_the_file(self):
        self.config.write_bytes(self.config.read_bytes().replace(b'session-affinity-ttl: 1h\n',
                                                                 b'session-affinity-ttl: !!str 1h0m1s\n'))
        original = self.config.read_bytes()
        result = self.handle()
        self.assertEqual((result['outcome'], result['code']), ('refused', 'edit_shrinks'))
        self.assertEqual(self.config.read_bytes(), original)

    def test_result_file_is_atomic_private_and_names_the_request_inode(self):
        inode = self.write_request()
        result = self.handle(request=False)
        saved = self.saved_result()
        self.assertEqual(set(saved), RESULT_KEYS)
        self.assertEqual(saved, result)
        self.assertEqual((saved['request_inode'], saved['outcome']), (inode, 'written'))
        self.assertGreaterEqual(dt.datetime.fromisoformat(saved['at']).timestamp(),
                                os.stat(self.runtime / 'result.json').st_mtime - 1)
        self.assertEqual(saved['sha256'], move.hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE((self.runtime / 'result.json').stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o750)
        self.assertFalse((self.runtime / '.result.json.tmp').exists())


class ConfigRefusalTests(HelperCase):
    def assert_refused(self, code, **kwargs):
        regular = self.config.exists() and not self.config.is_symlink()
        original = self.config.read_bytes() if regular else None
        result = self.handle(**kwargs)
        self.assertEqual((result['outcome'], result['code'], result['value']), ('refused', code, None))
        self.assertFalse(self.request.exists())
        if original is not None:
            self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.state / 'last-flip').exists())

    def test_wrong_mode(self):
        self.config.chmod(0o644)
        self.assert_refused('config_unsafe')

    def test_wrong_owner_or_group(self):
        for uid, gid in ((self.uid + 1, self.gid), (self.uid, self.gid + 1)):
            with self.subTest(uid=uid, gid=gid):
                self.write_request()
                result = move.handle(self.request, self.config, self.state, self.runtime,
                                     required_uid=uid, required_gid=gid)
                self.assertEqual((result['outcome'], result['code']), ('refused', 'config_unsafe'))
                self.assertEqual(self.ttl(), '1h')

    def test_default_group_lookup_is_used(self):
        self.write_request()
        with patch.object(move, '_owner_gid', return_value=None):
            result = move.handle(self.request, self.config, self.state, self.runtime, required_uid=self.uid)
        self.assertEqual(result['code'], 'config_unsafe')

    def test_symlinked_config(self):
        real = self.root / 'real.yaml'
        self.config.rename(real)
        self.config.symlink_to(real)
        self.assert_refused('config_symlink')

    def test_missing_config(self):
        self.config.unlink()
        self.assert_refused('config_unavailable')

    def test_unsupported_ttl(self):
        for value in (b'60m', b'2h', b'"1h0m2s"'):
            with self.subTest(value=value):
                self.config.write_bytes(V8.read_bytes().replace(b'session-affinity-ttl: 1h',
                                                                b'session-affinity-ttl: ' + value))
                self.assert_refused('unsupported_ttl')

    def test_affinity_disabled(self):
        self.config.write_bytes(V8.read_bytes().replace(b'session-affinity: true', b'session-affinity: false'))
        self.assert_refused('affinity_disabled')

    def test_unparsable_config(self):
        self.config.write_bytes(b'routing: [unclosed\n')
        self.assert_refused('config_unparsed')


class RateLimitTests(HelperCase):
    def test_minimum_interval_is_sixty_seconds(self):
        self.assertEqual(move.MIN_INTERVAL, 60)
        move.write_last_flip(self.state, now=1000)
        with self.assertRaises(move.Refused) as caught:
            move.preflight(self.config, self.state, now=1059, required_uid=self.uid, required_gid=self.gid)
        self.assertEqual(caught.exception.code, 'too_soon')
        move.preflight(self.config, self.state, now=1060, required_uid=self.uid, required_gid=self.gid)

    def test_future_last_flip_is_stale_after_a_clock_step(self):
        move.write_last_flip(self.state, now=time.time() + 3600)
        self.assertEqual(self.handle()['outcome'], 'written')

    def test_written_and_uncertain_record_last_flip(self):
        self.assertEqual(self.handle()['outcome'], 'written')
        self.assertEqual(self.handle()['code'], 'too_soon')
        self.assertEqual(stat.S_IMODE((self.state / 'last-flip').stat().st_mode), 0o600)

    def test_unrecorded_last_flip_is_reported_on_a_write(self):
        with patch.object(move, 'write_last_flip', side_effect=OSError('read-only state')):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code'], result['value']),
                         ('written', 'last_flip_not_recorded', '1h0m1s'))
        self.assertEqual(self.ttl(), '1h0m1s')
        self.assertEqual(self.saved_result()['code'], 'last_flip_not_recorded')
        self.assertFalse((self.state / 'last-flip').exists())

    def test_unrecorded_last_flip_keeps_the_uncertain_code(self):
        with patch.object(move, '_read_bytes', return_value=b'changed by someone else'), \
                patch.object(move, 'write_last_flip', side_effect=OSError('read-only state')):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code']), ('uncertain', 'readback_mismatch'))


class RequestTests(HelperCase):
    def handle_existing(self, **kwargs):
        return move.handle(self.request, self.config, self.state, self.runtime,
                           required_uid=self.uid, required_gid=self.gid, **kwargs)

    def test_request_is_never_read_and_any_entry_type_is_taken(self):
        # Only the cliproxyapi user and group and root can create entries in the 0770 directory; the content is irrelevant.
        for name, create in (('empty file', lambda: self.write_request()),
                             ('file with content', lambda: self.request.write_bytes(b'x' * 4096)),
                             ('symlink', lambda: self.request.symlink_to(self.config)),
                             ('directory', lambda: self.request.mkdir()),
                             ('fifo', lambda: os.mkfifo(self.request))):
            with self.subTest(name):
                self.forget_last_flip()
                create()
                inode = os.lstat(self.request).st_ino
                with patch.object(move.os, 'open', wraps=os.open) as opened:
                    result = self.handle_existing()
                self.assertEqual((result['outcome'], result['request_inode']), ('written', inode))
                self.assertFalse(os.path.lexists(self.request))
                self.assertNotIn(str(self.request), [str(call.args[0]) for call in opened.call_args_list])
        self.assertTrue(self.config.exists())

    def test_result_time_is_later_than_a_request_created_after_the_helper_started(self):
        # A run started for a withdrawn request can take a new one created just before its lstat.
        real_read = move.read_request
        created = {}
        def late_request(path, *args, **kwargs):
            time.sleep(0.05)
            created['inode'] = self.write_request()
            created['ns'] = self.request.stat().st_mtime_ns
            return real_read(path, *args, **kwargs)
        with patch.object(move, 'read_request', late_request):
            result = self.handle_existing()
        self.assertEqual((result['outcome'], result['request_inode']), ('written', created['inode']))
        # The dashboard's matching rule: `at` is no earlier than the request's creation.
        at = dt.datetime.fromisoformat(self.saved_result()['at'])
        self.assertIsNotNone(at.tzinfo)
        self.assertGreaterEqual(int(at.timestamp() * 1_000_000), created['ns'] // 1000)

    def test_missing_request_writes_no_result(self):
        self.assertIsNone(self.handle_existing())
        self.assertFalse((self.runtime / 'result.json').exists())
        self.assertEqual(self.ttl(), '1h')

    def test_request_withdrawn_before_removal_is_never_acted_on(self):
        # The dashboard withdrew its request between the helper's lstat and unlink.
        self.write_request()
        real_unlink = os.unlink
        def withdrawn(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            raise FileNotFoundError(path)
        with patch.object(move.os, 'unlink', withdrawn):
            self.assertIsNone(self.handle_existing())
        self.assertFalse((self.runtime / 'result.json').exists())
        self.assertEqual(self.ttl(), '1h')

    def test_stale_and_future_requests_are_refused_and_removed(self):
        for offset in (move.MAX_REQUEST_AGE + 1, -move.MAX_REQUEST_AGE - 1):
            with self.subTest(offset=offset):
                inode = self.write_request()
                now = self.request.stat().st_ctime + offset
                result = self.handle_existing(now=now)
                self.assertEqual((result['outcome'], result['code'], result['request_inode']),
                                 ('refused', 'stale_request', inode))
                self.assertFalse(self.request.exists())
                self.assertEqual(self.ttl(), '1h')

    def test_request_is_taken_before_the_yaml_modules_load(self):
        # Importing the helper loads neither module, so a loaded machine still takes the request promptly.
        code = ('import importlib.util, sys\n'
                'spec = importlib.util.spec_from_file_location("m", sys.argv[1])\n'
                'm = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n'
                'print("yaml" in sys.modules, m.yaml is None, m.gateway_config is None)\n'
                'm.load_config_modules()\n'
                'print("yaml" in sys.modules, m.yaml is None, m.gateway_config is None)\n')
        out = subprocess.run([sys.executable, '-I', '-c', code, str(ROOT / 'scripts/move-conversations.py')],
                             capture_output=True, text=True, timeout=30, cwd='/')
        self.assertEqual(out.stdout.split('\n')[:2], ['False True True', 'True False False'], out.stderr)
        # handle() takes the request first, and every load happens after it is gone.
        seen = []
        real = move.load_config_modules
        with patch.object(move, 'load_config_modules', side_effect=lambda: (seen.append(os.path.lexists(self.request)),
                                                                            real())):
            self.assertEqual(self.handle()['outcome'], 'written')
        self.assertTrue(seen)
        self.assertNotIn(True, seen)
        # An import failure after the request is taken is reported, never silent.
        self.forget_last_flip()
        with patch.object(move, 'load_config_modules', side_effect=ImportError('yaml')):
            self.assertEqual(self.handle()['outcome'], 'failed')
            self.assertEqual(self.saved_result()['code'], 'error')
        self.assertFalse(os.path.lexists(self.request))

    def test_read_request_returns_the_inode(self):
        inode = self.write_request()
        self.assertEqual(move.read_request(self.request), inode)
        self.assertFalse(self.request.exists())
        with self.assertRaises(move.NoRequest):
            move.read_request(self.request)

    def test_unremovable_request_is_consume_failed(self):
        inode = self.write_request()
        with patch.object(move.os, 'unlink', side_effect=PermissionError('denied')):
            result = self.handle_existing()
        self.assertEqual((result['outcome'], result['code'], result['request_inode']),
                         ('failed', 'consume_failed', inode))
        self.assertEqual(self.saved_result(), result)
        self.assertEqual(self.ttl(), '1h')
        self.assertFalse((self.state / 'last-flip').exists())
        # A removal failure replaces a refusal, so the path unit's retriggers are reported.
        with patch.object(move.os, 'unlink', side_effect=PermissionError('denied')):
            result = self.handle_existing(now=self.request.stat().st_ctime + move.MAX_REQUEST_AGE + 1)
        self.assertEqual((result['outcome'], result['code']), ('failed', 'consume_failed'))

    def test_untraversable_request_directory_is_consume_failed(self):
        with patch.object(move.os, 'lstat', side_effect=PermissionError('denied')):
            result = self.handle_existing()
        self.assertEqual((result['outcome'], result['code'], result['request_inode']),
                         ('failed', 'consume_failed', None))
        self.assertEqual(self.saved_result(), result)

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses directory permissions')
    def test_read_only_request_directory_is_consume_failed(self):
        self.write_request()
        self.requests.chmod(0o550)
        self.addCleanup(self.requests.chmod, 0o770)
        result = self.handle_existing()
        self.assertEqual((result['outcome'], result['code']), ('failed', 'consume_failed'))
        self.assertEqual(self.ttl(), '1h')

    def test_directory_that_cannot_be_removed_is_consume_failed(self):
        self.request.mkdir()
        (self.request / 'child').write_text('x')
        result = self.handle_existing()
        self.assertEqual((result['outcome'], result['code']), ('failed', 'consume_failed'))
        self.assertEqual(self.ttl(), '1h')


class PostWriteTests(HelperCase):
    def test_readback_mismatch_is_uncertain_with_value_and_records_last_flip(self):
        with patch.object(move, '_read_bytes', return_value=b'changed by someone else'):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code'], result['value']),
                         ('uncertain', 'readback_mismatch', '1h0m1s'))
        self.assertEqual(result['inode_before'], self.config.stat().st_ino)
        self.assertTrue((self.state / 'last-flip').exists())
        self.assertEqual(self.saved_result()['outcome'], 'uncertain')

    def test_metadata_change_is_uncertain(self):
        real_stat = os.stat
        with patch.object(move.os, 'stat', lambda path, *a, **k: os.stat_result(
                (0o100644,) + tuple(real_stat(path, *a, **k))[1:])):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code']), ('uncertain', 'config_metadata_changed'))

    def test_partial_write_is_restored_and_uncertain(self):
        original = self.config.read_bytes()

        def partial(path, candidate, expected):
            with path.open('r+b') as stream:
                stream.write(candidate[:len(candidate) // 2] + b'\0garbage')
            raise OSError('disk full')

        with patch.object(move.gateway_config, 'write_same_inode', partial):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code'], result['value']),
                         ('uncertain', 'write_interrupted_restored', '1h0m1s'))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertTrue((self.state / 'last-flip').exists())

    def test_partial_write_that_cannot_be_restored_is_write_interrupted(self):
        original = self.config.read_bytes()

        def partial(path, candidate, expected):
            with path.open('r+b') as stream:
                stream.write(candidate[:len(candidate) // 2] + b'\0garbage')
            raise OSError('disk full')

        def no_restore(path, mode='r', *args, **kwargs):
            if mode == 'r+b':
                raise OSError('read-only filesystem')
            return open(path, mode, *args, **kwargs)

        with patch.object(move.gateway_config, 'write_same_inode', partial), \
                patch.object(move, 'open', no_restore, create=True):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code'], result['value']),
                         ('uncertain', 'write_interrupted', '1h0m1s'))
        self.assertNotEqual(self.config.read_bytes(), original)
        self.assertTrue((self.state / 'last-flip').exists())
        self.assertEqual(self.saved_result()['code'], 'write_interrupted')

    def test_failure_before_any_write_is_failed(self):
        with patch.object(move.gateway_config, 'write_same_inode', side_effect=PermissionError()):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code'], result['value']), ('failed', 'write_failed', None))
        self.assertFalse((self.state / 'last-flip').exists())

    def test_config_changed_during_preparation_is_refused(self):
        with patch.object(move.gateway_config, 'write_same_inode', side_effect=ValueError('changed')):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code']), ('refused', 'config_changed'))
        self.assertFalse((self.state / 'last-flip').exists())


class ResultWriteTests(HelperCase):
    def test_unwritable_runtime_directory_raises_after_the_flip(self):
        self.runtime.write_text('not a directory')
        with self.assertRaises(OSError):
            self.handle()
        # The flip happened and the interval is recorded; the dashboard, seeing no result, reports helper_timeout.
        self.assertFalse(self.request.exists())
        self.assertEqual(self.ttl(), '1h0m1s')
        self.assertTrue((self.state / 'last-flip').exists())
        self.assertEqual(self.runtime.read_text(), 'not a directory')

    def test_unwritable_temp_result_leaves_no_result(self):
        (self.runtime / '.result.json.tmp').mkdir(parents=True)
        with self.assertRaises(OSError):
            self.handle()
        self.assertFalse((self.runtime / 'result.json').exists())
        self.assertEqual(self.ttl(), '1h0m1s')
        self.assertTrue((self.state / 'last-flip').exists())


class BackupTests(HelperCase):
    def test_backup_content_mode_and_pruning(self):
        originals = []
        for index in range(7):
            originals.append(self.config.read_bytes())
            self.forget_last_flip()
            self.assertEqual(self.handle()['outcome'], 'written')
        backups = self.state / 'backups'
        self.assertEqual(stat.S_IMODE(backups.stat().st_mode), 0o700)
        kept = sorted(backups.glob('config-*.yaml'))
        self.assertEqual(len(kept), move.BACKUPS_KEPT)
        self.assertEqual([path.read_bytes() for path in kept], originals[-move.BACKUPS_KEPT:])
        for path in kept:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_new_backup_survives_a_backwards_clock_step(self):
        backups = self.state / 'backups'
        backups.mkdir(parents=True)
        for index in range(6):
            old = backups / f'config-9{index:019d}-1.yaml'
            old.write_bytes(b'old')
            os.utime(old, ns=(4_000_000_000_000_000_000 + index, 4_000_000_000_000_000_000 + index))
        created = move.write_backup(self.state, b'newest', 7)
        kept = list(backups.glob('config-*.yaml'))
        self.assertIn(created, kept)
        self.assertEqual(len(kept), move.BACKUPS_KEPT)

    def test_backup_failure_writes_nothing(self):
        with patch.object(move, 'write_backup', side_effect=OSError()):
            result = self.handle()
        self.assertEqual((result['outcome'], result['code']), ('failed', 'backup_failed'))
        self.assertEqual(self.ttl(), '1h')


class MainTests(unittest.TestCase):
    def test_main_blocks_sigterm_before_handling(self):
        calls = []
        with patch.object(move.signal, 'pthread_sigmask', side_effect=lambda *a: calls.append('mask')), \
                patch.object(move, 'handle', side_effect=lambda *a: calls.append('handle') or
                             {'outcome': 'refused', 'code': 'too_soon'}), \
                patch('sys.argv', ['move-conversations.py']), patch('builtins.print'):
            move.main()
        self.assertEqual(calls, ['mask', 'handle'])

    def test_main_reports_no_request_without_a_result(self):
        with patch.object(move.signal, 'pthread_sigmask'), patch.object(move, 'handle', return_value=None), \
                patch('sys.argv', ['move-conversations.py']), patch('builtins.print') as printed:
            move.main()
        self.assertEqual(json.loads(printed.call_args.args[0]), {'outcome': None, 'code': 'no_request'})


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    text = (ROOT / 'scripts' / name).read_text()
    # Repeated keys are merged here; collect them separately where it matters.
    parser.read_string(text)
    return parser, text


class UnitTests(unittest.TestCase):
    def test_path_unit_watches_the_contract_request_without_a_boot_cycle(self):
        parser, _ = unit('personal-codex-router-move.path')
        self.assertEqual(parser['Path']['PathExists'], str(move.REQUEST))
        self.assertNotIn('After', parser['Unit'])

    def test_request_directory_is_the_dashboards_group_runtime_directory(self):
        parser, _ = unit('personal-codex-router-dashboard.service')
        service = parser['Service']
        self.assertEqual(Path('/run') / service['RuntimeDirectory'], move.REQUEST.parent)
        self.assertEqual(service['RuntimeDirectoryMode'], '0770')
        self.assertEqual((service['User'], service['Group']), ('cliproxyapi', 'cliproxyapi'))

    def test_helper_unit_is_hardened_and_can_reach_only_its_paths(self):
        parser, _ = unit('personal-codex-router-move.service')
        service = parser['Service']
        expected = {
            'User': 'root', 'Group': 'cliproxyapi', 'CapabilityBoundingSet': '',
            'PrivateDevices': 'yes', 'PrivateNetwork': 'yes', 'PrivateTmp': 'yes',
            'ProtectSystem': 'strict', 'ProtectHome': 'yes', 'ProtectKernelTunables': 'yes',
            'ProtectKernelModules': 'yes', 'ProtectKernelLogs': 'yes', 'ProtectControlGroups': 'yes',
            'ProtectClock': 'yes', 'ProtectProc': 'invisible', 'RestrictNamespaces': 'yes',
            'RestrictRealtime': 'yes', 'RestrictAddressFamilies': 'AF_UNIX',
            'SystemCallFilter': '@system-service', 'SystemCallArchitectures': 'native',
            'MemoryDenyWriteExecute': 'yes', 'IPAddressDeny': 'any', 'NoNewPrivileges': 'yes',
            # The result directory must survive the oneshot's exit, or systemd deletes result.json with it.
            # The sandbox can't create systemd's directories; the deploy checks them live.
            'RuntimeDirectory': move.RUNTIME.name, 'RuntimeDirectoryMode': '0750',
            'RuntimeDirectoryPreserve': 'yes', 'StateDirectory': move.STATE.name, 'StateDirectoryMode': '0700',
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(service[key], value)
        self.assertEqual(service['ReadWritePaths'].split(),
                         ['-' + str(move.CONFIG), '-' + str(move.REQUEST.parent)])
        self.assertLessEqual(int(service['TimeoutStartSec']), 15)
        self.assertGreater(int(service['TimeoutStartSec']), 0)
        self.assertIn('TimeoutStopSec', service)
        # Every request starts the helper once, even one it refuses. The dashboard creates at most one
        # request per MOVE_START_INTERVAL, so even a scripted burst of POSTs uses at most half the limit.
        sys.path.insert(0, str(ROOT / 'dashboard'))
        self.addCleanup(sys.path.remove, str(ROOT / 'dashboard'))
        import server as dashboard
        limits = parser['Unit']
        window, burst = int(limits['StartLimitIntervalSec']), int(limits['StartLimitBurst'])
        most_starts = math.floor(window / dashboard.MOVE_START_INTERVAL) + 1
        self.assertLessEqual(most_starts * 2, burst, (most_starts, burst))


if __name__ == '__main__':
    unittest.main()
