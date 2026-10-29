"""Locked credential replacement and sign-in convergence; temp dirs and synthetic tokens only."""
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import credential_replace as store
import routing_policy
import signin
from signin_fixture import NAME, FakeGateway, jwt
from test_signin import ClockEvent

OVERLAY = {'access_token': 'FAKE_A2', 'refresh_token': 'FAKE_R2', 'id_token': 'FAKE_I2',
           'expired': '2030-01-01T00:00:00Z', 'last_refresh': '2026-10-16T00:00:00Z', 'plan_type': 'free'}
HOLD = ('import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); '
        'fcntl.flock(fd,fcntl.LOCK_EX); print("locked",flush=True); time.sleep(float(sys.argv[2]))')


class WriterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.path = os.path.join(self.dir, NAME)
        self.original = {'type': 'codex', 'email': 'one@example.com', 'account_id': 'workspace-a',
            'access_token': 'FAKE_A1', 'refresh_token': 'FAKE_R1', 'id_token': 'FAKE_I1',
            'expired': '2026-01-01T00:00:00Z', 'last_refresh': '2025-12-31T00:00:00Z', 'plan_type': 'pro',
            'priority': '7', 'weight': 2.5, 'prefix': 'acct-a', 'disabled': True, 'note': None,
            'unknown': {'nested': [1, 2.0, 'x', {'deep': False}]}}
        self.write(self.original)

    def write(self, value, raw=None):
        with open(self.path, 'w') as file:
            file.write(raw if raw is not None else json.dumps(value))
        os.chmod(self.path, 0o600)

    def read(self):
        with open(self.path) as file:
            return json.load(file)

    def replace(self, **kwargs):
        kwargs.setdefault('check', lambda current: None)
        name = kwargs.pop('name', NAME)
        listed = kwargs.pop('listed', os.path.join(self.dir, NAME))
        overlay = kwargs.pop('overlay', OVERLAY)
        return store.replace(name, listed, overlay, auth_dir=self.dir, **kwargs)

    def leftovers(self):
        return sorted(n for n in os.listdir(self.dir) if n not in (NAME, NAME + '.lock'))

    def test_only_token_fields_change_and_other_keys_keep_values_and_types(self):
        self.replace()
        saved = self.read()
        for key, value in self.original.items():
            expected = OVERLAY.get(key, value)
            self.assertEqual(saved[key], expected, key)
            self.assertIs(type(saved[key]), type(expected), key)
        self.assertEqual(list(saved), list(self.original))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_refusals_write_nothing(self):
        bad_names = ['other.json', 'codex-a/../b.json', '../codex-a.json', 'codex-a..json',
                     'codex-a/b.json', 'codex-a.json.tmp', 'codex-\x00.json', 'codex-a.json\n', '/abs/codex-a.json']
        for name in bad_names:
            with self.subTest(name=name), self.assertRaises(store.ReplaceError):
                self.replace(name=name, listed=None)
        for listed in (os.path.join(self.dir, 'codex-other.json'), self.dir + '//' + NAME, os.path.join('/var/lib/cliproxyapi/auth', NAME)):
            with self.subTest(listed=listed), self.assertRaisesRegex(store.ReplaceError, 'path_mismatch'):
                self.replace(listed=listed)
        with self.assertRaisesRegex(store.ReplaceError, 'wrong_owner'):
            self.replace(uid=os.geteuid() + 1)
        for raw in ('not json', '[]', '{"type":"codex","type":"codex"}', '{"type":"codex","x":NaN}', '\xff'):
            with self.subTest(raw=raw), self.assertRaisesRegex(store.ReplaceError, 'invalid_json'):
                self.write(None, raw)
                self.replace()
        for extra in ({'siwc': {}}, {'use_siwc': False}, {'type': 'claude'}):
            with self.subTest(extra=extra), self.assertRaises(store.ReplaceError):
                self.write(dict(self.original, **extra))
                self.replace()
        self.write(self.original)
        with self.assertRaises(signin.IdentityError):
            self.replace(check=Mock(side_effect=signin.IdentityError('wrong_account')))
        for overlay in (dict(OVERLAY, extra='x'), dict(OVERLAY, access_token='')):
            with self.subTest(overlay=overlay), self.assertRaisesRegex(store.ReplaceError, 'invalid_tokens'):
                self.replace(overlay=overlay)
        self.assertEqual(self.read(), self.original)
        self.assertEqual(self.leftovers(), [])

    def test_symlinks_are_refused(self):
        real = os.path.join(self.dir, 'codex-real.json')
        os.rename(self.path, real)
        os.symlink(real, self.path)
        # O_NOFOLLOW's ELOOP is a lasting refusal, not a transient unreadable file.
        with self.assertRaisesRegex(store.ReplaceError, 'not_regular'):
            self.replace()
        os.unlink(self.path)
        os.rename(real, self.path)
        os.unlink(self.path + '.lock')
        os.symlink(os.path.join(self.dir, 'elsewhere'), self.path + '.lock')
        with self.assertRaisesRegex(store.ReplaceError, 'lock_untrusted'):
            self.replace()
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'elsewhere')))
        self.assertEqual(self.read(), self.original)
        os.unlink(self.path + '.lock')
        os.mkdir(os.path.join(self.dir, 'codex-dir.json'))
        with self.assertRaisesRegex(store.ReplaceError, 'not_regular'):
            self.replace(name='codex-dir.json', listed=os.path.join(self.dir, 'codex-dir.json'))

    def test_failure_before_rename_leaves_original_and_no_temp_file(self):
        for target in ('rename', 'fsync', 'write'):
            with self.subTest(target=target), patch.object(store.os, target, side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    self.replace()
            self.assertEqual(self.read(), self.original)
            self.assertEqual(self.leftovers(), [])
        with patch.object(store.os, 'stat', return_value=Mock(st_dev=0, st_ino=0)):
            with self.assertRaisesRegex(store.ReplaceError, 'changed'):
                self.replace()
        self.assertEqual(self.leftovers(), [])

    def test_temp_name_and_mode(self):
        seen, real = [], os.rename
        def rename(source, target, **kwargs):
            info = os.stat(source, dir_fd=kwargs['src_dir_fd'])
            seen.append((source, target, stat.S_IMODE(info.st_mode), info.st_uid))
            return real(source, target, **kwargs)
        old = os.umask(0)
        try:
            with patch.object(store.os, 'rename', side_effect=rename):
                self.replace()
        finally:
            os.umask(old)
        (source, target, mode, uid), = seen
        self.assertRegex(source, r'^' + NAME.replace('.', r'\.') + r'\.tmp\.[0-9a-f]{24}$')
        self.assertFalse(source.endswith('.json'))
        self.assertEqual((target, mode, uid), (NAME, 0o600, os.geteuid()))

    def test_check_and_write_run_under_the_gateway_sidecar_lock(self):
        observed = []
        def check(current):
            observed.append(store.holding())
            probe = subprocess.run([sys.executable, '-c',
                'import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); '
                'sys.exit(0 if fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB) is None else 1)',
                self.path + '.lock'], capture_output=True)
            observed.append(probe.returncode != 0)
        self.replace(check=check)
        self.assertEqual(observed, [True, True])
        self.assertFalse(store.holding())
        self.assertEqual(stat.S_IMODE(os.stat(self.path + '.lock').st_mode), 0o600)

    def hold(self, seconds):
        child = subprocess.Popen([sys.executable, '-c', HOLD, self.path + '.lock', str(seconds)],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.kill)
        self.addCleanup(child.stdout.close)
        self.assertEqual(child.stdout.readline().strip(), 'locked')
        return child

    def test_lock_contention_waits_then_proceeds(self):
        self.hold(0.6)
        started = time.monotonic()
        self.replace(wait=10)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(self.read()['access_token'], 'FAKE_A2')

    def test_lock_timeout_writes_nothing_and_leaves_no_temp_file(self):
        self.hold(30)
        check = Mock()
        with self.assertRaisesRegex(store.ReplaceError, 'lock_timeout'):
            self.replace(wait=0.3, check=check)
        check.assert_not_called()
        self.assertEqual(self.read(), self.original)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(store.holding())

    def owned_by_other(self, suffix):
        """fstat reports another owner for the fd whose path ends with suffix."""
        real = os.fstat
        def fstat(fd):
            info = real(fd)
            if os.readlink('/proc/self/fd/%d' % fd).endswith(suffix):
                fields = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                return SimpleNamespace(**dict(fields, st_uid=info.st_uid + 1))
            return info
        return patch.object(store.os, 'fstat', side_effect=fstat)

    def assert_unchanged(self):
        self.assertEqual(self.read(), self.original)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(store.holding())

    def test_untrusted_directory_or_lock_writes_nothing(self):
        lock = self.path + '.lock'
        with self.owned_by_other(os.path.basename(self.dir)), \
                self.assertRaisesRegex(store.ReplaceError, 'wrong_owner'):
            self.replace()
        self.assertFalse(os.path.exists(lock))
        self.assert_unchanged()
        with self.owned_by_other('.lock'), self.assertRaisesRegex(store.ReplaceError, 'lock_untrusted'):
            self.replace()
        self.assert_unchanged()
        os.unlink(lock)
        os.mkfifo(lock, 0o600)
        with self.assertRaisesRegex(store.ReplaceError, 'lock_untrusted'):
            self.replace()
        self.assertEqual(self.read(), self.original)
        self.assertEqual(self.leftovers(), [])

    def test_preflight_refuses_an_untrusted_lock_file_without_touching_it(self):
        lock = self.path + '.lock'
        flags, real_open = [], os.open
        def tracked_open(path, flag, *args, **kwargs):
            flags.append(flag)
            return real_open(path, flag, *args, **kwargs)
        def refused():
            with patch.object(store.os, 'open', side_effect=tracked_open), \
                    self.assertRaisesRegex(store.ReplaceError, 'lock_untrusted'):
                store.preflight(self.record(), auth_dir=self.dir)
        os.symlink(os.path.join(self.dir, 'elsewhere'), lock)
        refused()
        self.assertTrue(os.path.islink(lock))
        self.assertFalse(os.path.lexists(os.path.join(self.dir, 'elsewhere')))
        os.unlink(lock)
        os.mkdir(lock)
        refused()
        os.rmdir(lock)
        os.mkfifo(lock, 0o600)
        refused()
        self.assertTrue(stat.S_ISFIFO(os.lstat(lock).st_mode))
        os.unlink(lock)
        with open(lock, 'w'):
            pass
        os.chmod(lock, 0o600)
        self.assertIsNone(store.preflight(self.record(), auth_dir=self.dir))
        real_stat = os.stat
        def other_owner(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path).endswith('.lock'):
                fields = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                return SimpleNamespace(**dict(fields, st_uid=info.st_uid + 1))
            return info
        before = store.fingerprint(os.stat(lock))
        with patch.object(store.os, 'stat', side_effect=other_owner):
            refused()
        self.assertEqual(store.fingerprint(os.stat(lock)), before)
        self.assertFalse([f for f in flags if f & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)])
        self.assertEqual(self.read(), self.original)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(store.holding())

    def test_open_errors_are_classified_by_errno(self):
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        if os.access(self.path, os.R_OK):
            self.skipTest('Running with privileges that ignore file modes')
        for action in (self.replace, lambda: store.preflight(self.record(), auth_dir=self.dir)):
            with self.assertRaisesRegex(store.ReplaceError, 'no_access'):
                action()
        self.assertIn('no_access', store.LASTING)
        self.assertNotIn('unreadable', store.LASTING)

    def test_lock_needs_owner_read_and_write_and_an_unopenable_trusted_lock_is_lasting(self):
        lock = self.path + '.lock'
        with open(lock, 'w'):
            pass
        for mode in (0o400, 0o200, 0o000):
            with self.subTest(mode=oct(mode)):
                os.chmod(lock, mode)
                for action in (self.replace, lambda: store.preflight(self.record(), auth_dir=self.dir)):
                    with self.assertRaisesRegex(store.ReplaceError, 'lock_untrusted'):
                        action()
                self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), mode)
        os.chmod(lock, 0o600)
        real_open = os.open
        def refuse_lock(path, flags, *args, **kwargs):
            if str(path).endswith('.lock'):
                raise PermissionError(13, 'synthetic')
            return real_open(path, flags, *args, **kwargs)
        with patch.object(store.os, 'open', side_effect=refuse_lock), \
                self.assertRaisesRegex(store.ReplaceError, 'lock_untrusted'):
            self.replace()
        # No lock yet and it cannot be created: the open error itself, not a verdict on a lock.
        os.unlink(lock)
        with patch.object(store.os, 'open', side_effect=refuse_lock), self.assertRaises(PermissionError):
            self.replace()
        self.assertEqual(self.read(), self.original)
        self.assertEqual(self.leftovers(), [])

    def test_listed_path_is_required(self):
        for listed in (None, '', NAME, b'x'):
            with self.subTest(listed=listed), self.assertRaisesRegex(store.ReplaceError, 'path_mismatch'):
                self.replace(listed=listed)
        self.assert_unchanged()

    def test_oversize_fifo_and_non_finite_credentials_are_refused(self):
        self.write(None, json.dumps(dict(self.original, pad='x' * store.MAX_BYTES)))
        with self.assertRaisesRegex(store.ReplaceError, 'too_large'):
            self.replace()
        self.assertIn('too_large', store.LASTING)
        for number in ('1e999', '-1e999'):
            with self.subTest(number=number), self.assertRaisesRegex(store.ReplaceError, 'invalid_json'):
                self.write(None, json.dumps(self.original)[:-1] + ',"weight":' + number + '}')
                self.replace()
        with self.assertRaisesRegex(store.ReplaceError, 'invalid_json'):
            store.parse(b'{"x":1e999}')
        os.unlink(self.path)
        os.mkfifo(self.path, 0o600)
        with self.assertRaisesRegex(store.ReplaceError, 'not_regular'):
            self.replace()
        self.assertTrue(stat.S_ISFIFO(os.stat(self.path).st_mode))
        self.assertEqual(self.leftovers(), [])

    def record(self, **changes):
        return dict({'name': NAME, 'path': os.path.join(self.dir, NAME), 'source': 'file', 'runtime_only': False},
                    **changes)

    def test_preflight_refuses_what_replace_refuses_without_writing_anything(self):
        """Every lasting refusal is found read-only: no lock file, no temp file, no write flags."""
        flags, real_open = [], os.open
        def tracked_open(path, flag, *args, **kwargs):
            flags.append(flag)
            return real_open(path, flag, *args, **kwargs)
        def refusal(code, record=None, **kwargs):
            with patch.object(store.os, 'open', side_effect=tracked_open), \
                    self.assertRaisesRegex(store.ReplaceError, code):
                store.preflight(record or self.record(), auth_dir=self.dir, **kwargs)
        other = os.path.join(self.dir, 'codex-other.json')
        cases = [('invalid_name', self.record(name='other.json', path=os.path.join(self.dir, 'other.json'))),
                 ('path_mismatch', self.record(path=other)),
                 ('path_mismatch', self.record(path=None)),
                 ('not_file', self.record(source='memory')),
                 ('not_file', self.record(runtime_only=True))]
        for code, record in cases:
            with self.subTest(code=code, record=record):
                refusal(code, record)
        refusal('wrong_owner', uid=os.geteuid() + 1)
        with self.owned_by_other(os.path.basename(self.dir)):
            refusal('wrong_owner')
        with self.owned_by_other(NAME):
            refusal('wrong_owner')
        for extra, code in (({'siwc': {}}, 'siwc_present'), ({'use_siwc': False}, 'siwc_present'),
                            ({'type': 'claude'}, 'not_codex')):
            with self.subTest(extra=extra):
                self.write(dict(self.original, **extra))
                refusal(code)
                with self.assertRaisesRegex(store.ReplaceError, code):
                    self.replace()
                os.unlink(self.path + '.lock')
        self.write(self.original)
        real = os.path.join(self.dir, 'codex-real.json')
        os.rename(self.path, real)
        os.symlink(real, self.path)
        refusal('not_regular')
        os.unlink(self.path)
        os.mkfifo(self.path, 0o600)
        refusal('not_regular')
        os.unlink(self.path)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(self.path)
            refusal('not_regular')  # ENXIO
        os.unlink(self.path)
        refusal('unreadable')  # ENOENT: deleted since the listing, transient.
        with self.assertRaisesRegex(store.ReplaceError, 'unreadable'):
            store.preflight(self.record(path=os.path.join(self.dir, 'missing', NAME)),
                            auth_dir=os.path.join(self.dir, 'missing'))
        os.rename(real, self.path)
        before = os.stat(self.path)
        with patch.object(store.os, 'open', side_effect=tracked_open):
            self.assertIsNone(store.preflight(self.record(), auth_dir=self.dir))
        self.assertEqual(sorted(os.listdir(self.dir)), [NAME])
        self.assertEqual(store.fingerprint(os.stat(self.path)), store.fingerprint(before))
        self.assertEqual(self.read(), self.original)
        self.assertTrue(flags)
        self.assertFalse([f for f in flags if f & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)])
        self.assertTrue(all(f & os.O_NOFOLLOW for f in flags))
        self.assertFalse(store.holding())

    def test_holding_is_false_after_check_raises(self):
        with self.assertRaisesRegex(ValueError, 'refused'):
            self.replace(check=Mock(side_effect=ValueError('refused')))
        self.assert_unchanged()

    def test_in_place_write_between_read_and_rename_refuses(self):
        def same_size(current):
            time.sleep(0.05)
            raw = json.dumps(dict(self.original, note='xy'))
            self.assertEqual(len(raw), os.stat(self.path).st_size)
            fd = os.open(self.path, os.O_WRONLY)
            os.write(fd, raw.encode())
            os.close(fd)
        for check in (same_size, lambda current: self.write(dict(self.original, note='other upload'))):
            with self.subTest(check=check), self.assertRaisesRegex(store.ReplaceError, 'changed'):
                self.write(self.original)
                self.replace(check=check)
            self.assertEqual(self.read()['access_token'], 'FAKE_A1')
            self.assertEqual(self.leftovers(), [])

    def test_delete_or_replace_between_read_and_rename_refuses(self):
        def delete(current):
            os.unlink(self.path)
        def recreate(current):
            os.unlink(self.path)
            self.write(dict(self.original, note='new upload'))
        with self.assertRaisesRegex(store.ReplaceError, 'changed'):
            self.replace(check=delete)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.leftovers(), [])
        self.write(self.original)
        with self.assertRaisesRegex(store.ReplaceError, 'changed'):
            self.replace(check=recreate)
        self.assertEqual(self.read()['note'], 'new upload')
        self.assertEqual(self.leftovers(), [])

    def test_failures_after_the_rename_still_report_the_write(self):
        real_flock, real_close, real_fsync = fcntl.flock, os.close, os.fsync
        def flock(fd, operation):
            if operation == fcntl.LOCK_UN:
                raise OSError('injected')
            return real_flock(fd, operation)
        def close(fd):
            target = os.readlink('/proc/self/fd/%d' % fd)
            real_close(fd)
            if target == os.path.realpath(self.dir):
                raise OSError('injected')
        def fsync(fd):
            if os.path.isdir('/proc/self/fd/%d' % fd):
                raise OSError('injected')
            return real_fsync(fd)
        for injected in (patch.object(store.fcntl, 'flock', side_effect=flock),
                         patch.object(store.os, 'close', side_effect=close),
                         patch.object(store.os, 'fsync', side_effect=fsync)):
            with self.subTest(injected=injected):
                self.write(self.original)
                with injected:
                    self.assertIsNone(self.replace())
                self.assertEqual(self.read()['access_token'], 'FAKE_A2')
                self.assertEqual(self.leftovers(), [])
                self.assertFalse(store.holding())

    def test_stale_temp_files_of_every_codex_credential_are_swept(self):
        old = time.time() - store.STALE_TEMP_SECONDS - 60
        def make(name, age=old, kind='file'):
            path = os.path.join(self.dir, name)
            if kind == 'link':
                os.symlink(self.path, path)
                return name
            with open(path, 'w') as file:
                file.write('{}')
            os.utime(path, (age, age))
            return name
        hexed = 'ab' * 12
        def files():
            swept = [make(NAME + '.tmp.' + hexed), make('codex-other.json.tmp.' + hexed)]
            kept = [make(NAME + '.tmp.' + 'cd' * 12, age=time.time()),
                    make(NAME + '.tmp.' + 'AB' * 12),
                    make(NAME + '.tmp.' + hexed + 'x'),
                    make(NAME + '.tmp.' + 'ef' * 11),
                    make('claude-other.json.tmp.' + hexed),
                    make('notes.tmp.' + hexed),
                    make(NAME + '.tmp.' + '01' * 12, kind='link')]
            if not os.path.isdir(os.path.join(self.dir, NAME + '.tmp.' + '23' * 12)):
                os.mkdir(os.path.join(self.dir, NAME + '.tmp.' + '23' * 12))
                os.utime(os.path.join(self.dir, NAME + '.tmp.' + '23' * 12), (old, old))
            kept.append(NAME + '.tmp.' + '23' * 12)
            return swept, kept
        swept, kept = files()
        seen = []
        self.replace(check=lambda current: seen.append([os.path.exists(os.path.join(self.dir, n)) for n in swept]))
        self.assertEqual(seen, [[False, False]])
        self.assertEqual(self.leftovers(), sorted(kept))
        self.assertEqual(self.read()['access_token'], 'FAKE_A2')
        # Start's preflight sweeps too, before any sign-in reaches replace().
        for name in kept:
            if not os.path.isdir(os.path.join(self.dir, name)) or os.path.islink(os.path.join(self.dir, name)):
                os.unlink(os.path.join(self.dir, name))
        swept, kept = files()
        self.assertIsNone(store.preflight(self.record(), auth_dir=self.dir))
        self.assertEqual(self.leftovers(), sorted(kept))

    def test_stamp_uses_gateway_rfc3339_utc(self):
        self.assertEqual(store.stamp(1800000000), '2027-01-15T08:00:00Z')
        dt.datetime.strptime(store.stamp(time.time()), '%Y-%m-%dT%H:%M:%SZ')


class EchoTests(unittest.TestCase):
    def call(self, token, deliver=True, before=None):
        def call(path, body=None, method=None, timeout=None):
            self.assertEqual((path, method, body['proxy_url']), ('/api-call', 'POST', 'direct'))
            if before:
                before(body['url'])
            url = body['url'] if deliver else body['url'] + 'x'
            header = body['header']['Authorization'].replace('$TOKEN$', token)
            with urlopen(Request(url, headers={'Authorization': header}), timeout=2):
                return {'status_code': 204}
        return call

    def refused(self, url, wait=3):
        ends = time.monotonic() + wait
        while time.monotonic() < ends:
            try:
                urlopen(url, timeout=1).close()
            except OSError:
                return True
            time.sleep(0.05)
        return False

    def test_matches_only_the_expected_runtime_token(self):
        self.assertTrue(signin.runtime_echo(self.call('FAKE_NEW'), 'account-a', 'FAKE_NEW'))
        self.assertFalse(signin.runtime_echo(self.call('FAKE_OLD'), 'account-a', 'FAKE_NEW'))
        self.assertFalse(signin.runtime_echo(self.call('FAKE_NEW', deliver=False), 'account-a', 'FAKE_NEW'))
        self.assertFalse(signin.runtime_echo(Mock(side_effect=OSError()), 'account-a', 'FAKE_NEW', linger=0.2))

    def test_returns_as_soon_as_call_returns_without_a_delivery(self):
        urls = []
        def call(path, body=None, method=None, timeout=None):
            urls.append(body['url'])
            return {'status_code': 502}
        started = time.monotonic()
        self.assertFalse(signin.runtime_echo(call, 'account-a', 'FAKE_NEW'))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(self.refused(urls[0], wait=0.5))

    def test_stray_idle_connection_does_not_stall_the_listener(self):
        strays = []
        def stray(url):
            host, port = urlsplit(url).hostname, urlsplit(url).port
            for partial in (b'', b'GET /'):
                connection = socket.create_connection((host, port), timeout=2)
                self.addCleanup(connection.close)
                connection.sendall(partial)
                strays.append(connection)
        started = time.monotonic()
        self.assertTrue(signin.runtime_echo(self.call('FAKE_NEW', before=stray), 'account-a', 'FAKE_NEW'))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(strays), 2)

    def test_failed_call_keeps_the_port_bound_until_the_gateway_window_ends(self):
        urls = []
        def call(path, body=None, method=None, timeout=None):
            urls.append(body['url'])
            raise TimeoutError('timed out')
        started = time.monotonic()
        self.assertFalse(signin.runtime_echo(call, 'account-a', 'FAKE_NEW', linger=0.6))
        # A late delivery reaches this listener, not a process that rebinds the port.
        with urlopen(Request(urls[0], headers={'Authorization': 'Bearer FAKE_NEW'}), timeout=1) as response:
            self.assertEqual(response.status, 204)
        self.assertTrue(self.refused(urls[0]))
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertEqual(signin.ECHO_LINGER_SECONDS, 65)

    def test_request_deadline_timeout_propagates_after_arranging_the_linger(self):
        from request_deadline import RequestDeadline
        now = [0.0]
        deadline = RequestDeadline(45, clock=lambda: now[0])
        urls = []
        def call(path, body=None, method=None, timeout=None):
            urls.append(body['url'])
            now[0] = 100  # The deadline runs out while the gateway works on the call.
            return {'status_code': 204}
        with self.assertRaises(TimeoutError):
            signin.runtime_echo(deadline.wrap(call), 'account-a', 'FAKE_NEW', linger=0.6, deadline=deadline)
        with urlopen(Request(urls[0], headers={'Authorization': 'Bearer FAKE_NEW'}), timeout=1) as response:
            self.assertEqual(response.status, 204)
        self.assertTrue(self.refused(urls[0]))
        # Without a request deadline, a failed call still reads as no echo.
        self.assertFalse(signin.runtime_echo(Mock(side_effect=TimeoutError()), 'account-a', 'FAKE_NEW', linger=0.2))

    def test_header_is_never_logged(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            self.assertTrue(signin.runtime_echo(self.call('FAKE_NEW'), 'account-a', 'FAKE_NEW'))
            self.assertFalse(signin.runtime_echo(self.call('FAKE_NEW', deliver=False), 'account-a', 'FAKE_NEW'))
        self.assertEqual(output.getvalue(), '')

    def test_listener_is_loopback_one_shot_and_path_is_256_bit(self):
        urls = []
        def call(path, body=None, method=None, timeout=None):
            urls.append(body['url'])
            return {}
        signin.runtime_echo(call, 'account-a', 'FAKE_NEW')
        host, nonce = urls[0][len('http://'):].split('/', 1)
        self.assertTrue(host.startswith('127.0.0.1:'))
        self.assertGreaterEqual(len(nonce), 43)
        with self.assertRaises(OSError):
            urlopen(urls[0], timeout=1)


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        isolation = patch.object(routing_policy, 'STATE', Path(directory.name))
        isolation.start()
        self.addCleanup(isolation.stop)
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.gateway.tokens['id_token'] = jwt(plan='free')
        self.clock = [1800000000.0]
        self.changed = Mock()
        self.manager = signin.SigninManager(self.gateway.call, threading.RLock(), self.changed,
                                            clock=lambda: self.clock[0])
        self.addCleanup(self.manager.close)

    def run_flow(self):
        with patch.object(signin.threading, 'Thread'):
            self.manager.start('account-a', self.gateway.identity)
        session = self.manager.sessions['account-a']
        session['stop'] = ClockEvent(self.clock)
        self.manager.run(session)
        return self.view()

    def view(self):
        return self.manager.view('account-a', self.gateway.identity)

    def unconfirmed(self):
        """Saved but never confirmed; the gateway no longer reports the old token's rejection."""
        self.gateway.stale_patches = 2
        view = self.run_flow()
        self.gateway.row['status_message'] = ''
        return view

    def device_calls(self, since=0):
        return [c for c in self.gateway.calls[since:] if c[0] == '/api-call'
                and c[1]['url'].startswith('https://auth.openai.com')]

    def test_runtime_matches_immediately_then_enables(self):
        view = self.run_flow()
        self.assertEqual(view['last_outcome'], {'kind': 'signed_in', 'at': ANY, 'enabled': True})
        self.assertEqual(len(self.gateway.field_patches), 1)
        self.assertEqual(self.gateway.echoes, ['FAKE_NEW_ACCESS'])
        self.assertEqual(self.gateway.saved['plan_type'], 'free')
        self.assertEqual(self.gateway.row['id_token']['plan_type'], 'free')
        self.assertEqual([p['disabled'] for p in self.gateway.patches], [False])

    def test_8_0_43_replacement_confirms_and_enables(self):
        self.gateway.version = '8.0.43'
        view = self.run_flow()
        self.assertEqual(view['last_outcome'], {'kind': 'signed_in', 'at': ANY, 'enabled': True})
        self.assertEqual(self.gateway.echoes, ['FAKE_NEW_ACCESS'])
        self.assertEqual(self.gateway.row['id_token']['plan_type'], 'free')
        self.assertEqual([p['disabled'] for p in self.gateway.patches], [False])

    def test_runtime_matches_only_after_one_repatch(self):
        self.gateway.stale_patches = 1
        self.assertEqual(self.run_flow()['last_outcome']['kind'], 'signed_in')
        self.assertEqual(len(self.gateway.field_patches), 2)
        self.assertEqual(self.gateway.echoes, ['FAKE_OLD_ACCESS', 'FAKE_NEW_ACCESS'])

    def test_never_matches_reports_saved_unconfirmed_without_rollback_or_enable(self):
        view = self.unconfirmed()
        self.assertEqual(view['last_outcome'], {'kind': 'saved_unconfirmed', 'at': self.clock[0]})
        self.assertFalse(view['retry_allowed'])
        self.assertEqual(self.gateway.saved['refresh_token'], 'FAKE_NEW_REFRESH')
        self.assertEqual(self.gateway.runtime['refresh_token'], 'FAKE_OLD_REFRESH')
        self.assertEqual(len(self.gateway.field_patches), 2)
        self.assertFalse(self.gateway.patches)
        self.changed.assert_called_once_with('account-a')
        # The new refresh token exists only on disk; nothing may write the old one back.
        self.assertFalse(any(p.get('refresh_token') == 'FAKE_OLD_REFRESH' for p in self.gateway.field_patches))

        saved_at, calls = self.clock[0], len(self.gateway.calls)
        self.clock[0] += 300
        self.manager.prune([self.gateway.row])
        self.assertEqual(self.view()['last_outcome'], {'kind': 'saved_unconfirmed', 'at': saved_at})
        self.assertFalse(self.view()['retry_allowed'])
        self.assertEqual(len(self.gateway.calls), calls)
        self.assertFalse(self.gateway.patches)

    def test_no_management_call_while_the_credential_lock_is_held(self):
        original, held = self.gateway.call, []
        def call(*args, **kwargs):
            held.append(store.holding())
            return original(*args, **kwargs)
        self.manager.call = call
        real = store.replace
        def replace(name, path, overlay, check):
            def guarded(current):
                held.append(('check', store.holding()))
                return check(current)
            return real(name, path, overlay, guarded)
        with patch.object(store, 'replace', side_effect=replace):
            self.assertEqual(self.run_flow()['last_outcome']['kind'], 'signed_in')
        self.assertIn(('check', True), held)
        self.assertNotIn(True, held)

    def test_unknown_gateway_version_fails_closed_before_flow_and_before_write(self):
        for version in ('8.0.31', None, '8.0.30-dirty', '8.0.43-dirty'):
            with self.subTest(version=version):
                self.setUp()
                self.gateway.version = version
                with patch.object(signin.threading, 'Thread'):
                    view = self.manager.start('account-a', self.gateway.identity)
                self.assertEqual(view['last_outcome']['kind'], 'gateway_untested')
                self.assertEqual(self.device_calls(), [])
        self.setUp()
        before = dict(self.gateway.saved)
        with patch.object(signin.threading, 'Thread'):
            self.manager.start('account-a', self.gateway.identity)
        session = self.manager.sessions['account-a']
        session['stop'] = ClockEvent(self.clock)
        self.gateway.version = '8.0.31'
        self.manager.run(session)
        self.assertEqual(self.view()['last_outcome']['kind'], 'gateway_untested')
        self.assertEqual(self.gateway.saved, before)
        self.assertFalse(self.gateway.field_patches or self.gateway.patches)

    def test_listed_path_outside_the_auth_dir_writes_nothing(self):
        for changes in ({'path': '/tmp/' + NAME}, {'name': 'existing-paid-name.json'}, {'runtime_only': True},
                        {'source': 'memory'}):
            with self.subTest(changes=changes):
                self.setUp()
                before = dict(self.gateway.saved)
                with patch.object(signin.threading, 'Thread'):
                    self.manager.start('account-a', self.gateway.identity)
                session = self.manager.sessions['account-a']
                session['stop'] = ClockEvent(self.clock)
                self.gateway.row.update(changes)
                session['identity'] = self.gateway.identity
                self.manager.run(session)
                self.assertEqual(self.manager.outcomes['account-a']['kind'], 'save_unavailable')
                self.assertEqual(self.gateway.saved, before)
                self.assertFalse(self.gateway.field_patches)

    def test_outcomes_views_and_logs_never_contain_secrets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            views = [self.unconfirmed()]
            views.append(self.view())
            self.manager.close()
            self.setUp()
            self.gateway.tokens['id_token'] = jwt(email='intruder@example.com')
            views.append(self.run_flow())
        text = output.getvalue() + json.dumps(views) + json.dumps(self.manager.outcomes, default=str)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([l['kind'] for l in lines], ['saved_unconfirmed', 'wrong_account'])
        self.assertEqual(lines[0]['checks'], {'disk': True, 'listing': False, 'runtime': False, 'repatched': True})
        self.assertEqual(lines[0]['email'], 'one@example.com')
        secrets = ['FAKE_NEW_ACCESS', 'FAKE_NEW_REFRESH', 'FAKE_OLD', 'FAKE_CODE', 'FAKE_VERIFIER',
                   'FAKE_DEVICE_SECRET', 'Bearer', 'synthetic.', 'ABCD-EFGH']
        for secret in secrets:
            self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
