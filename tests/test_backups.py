import datetime as dt
from contextlib import closing
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('backup', Path(__file__).resolve().parents[1] / 'scripts/router-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_online_database_snapshot_includes_wal_but_not_uncommitted_transaction(self):
        source = self.root / 'live.sqlite3'
        con = sqlite3.connect(source)
        self.addCleanup(con.close)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE events (value TEXT)')
        con.execute("INSERT INTO events VALUES ('committed')")
        con.commit()
        con.execute("INSERT INTO events VALUES ('uncommitted')")
        destination = self.root / 'snapshot'
        result = backup.snapshot([source], destination)
        self.assertEqual(result['entries'][0]['method'], 'sqlite-online-backup')
        copy = destination / 'files' / source.relative_to('/')
        with closing(sqlite3.connect(copy)) as verify:
            self.assertEqual(verify.execute('SELECT value FROM events').fetchall(), [('committed',)])
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(backup.verify(destination)['entries']), 1)
        self.assertEqual(backup.rehearse(destination, self.root / 'rehearsal'), 1)
        self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0], 2)

    def test_secret_permissions_checksums_rehearsal_and_symlink_rejection(self):
        source = self.root / 'config.toml'
        source.write_text('key = "private-fixture"\n')
        dest = self.root / 'backup'
        backup.snapshot([source], dest)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o700)
        # Rehearsal never overwrites an existing directory.
        with self.assertRaises(ValueError): backup.rehearse(dest, self.root)
        with self.assertRaises(ValueError): backup.rehearse(dest, Path('/etc/forbidden-rehearsal'))
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): backup.rehearse(dest, link / 'rehearsal')
        copy = dest / 'files' / source.relative_to('/')
        copy.write_text('changed')
        with self.assertRaises(ValueError): backup.verify(dest)

    def test_sources_include_optional_usage_spool_and_reject_its_symlink(self):
        with patch.object(Path, 'exists', return_value=True), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'glob', return_value=[]):
            self.assertIn(backup.USAGE_SPOOL, backup.sources())
        with patch.object(Path, 'exists', return_value=True), \
                patch.object(Path, 'is_symlink', autospec=True,
                             side_effect=lambda path: path == backup.USAGE_SPOOL), \
                patch.object(Path, 'glob', return_value=[]):
            self.assertNotIn(backup.USAGE_SPOOL, backup.sources())

    def test_fast_mode_controls_are_optional_and_derived_files_are_skipped(self):
        controls = Path('/var/lib/personal-codex-router/fast-mode-controls.json')
        policy = controls.with_name('fast-mode-policy.json')
        status = Path('/var/lib/cliproxyapi/fast-mode-plugin-status.json')
        for present, symlinked in ((True, False), (False, False), (True, True)):
            with self.subTest(present=present, symlinked=symlinked), \
                    patch.object(Path, 'exists', autospec=True,
                                 side_effect=lambda path: present if path == controls else True), \
                    patch.object(Path, 'is_symlink', autospec=True,
                                 side_effect=lambda path: symlinked if path == controls else False), \
                    patch.object(Path, 'glob', return_value=[]):
                sources = backup.sources()
                self.assertEqual(controls in sources, present and not symlinked)
                self.assertNotIn(policy, sources)
                self.assertNotIn(status, sources)
        source = self.root / 'fast-mode-controls.json'
        value = {'accounts': {'fixture@example.test': {'allow_fast': False,
                 'changed_at': '2026-10-02T12:00:00Z', 'history': []}}}
        source.write_text(json.dumps(value))
        destination = self.root / 'backup'
        backup.snapshot([source], destination)
        restored = destination / 'files' / source.relative_to('/')
        self.assertEqual(json.loads(restored.read_text()), value)
        self.assertEqual(restored.stat().st_mode & 0o777, 0o600)

    def test_daily_sources_include_optional_claude_state_unless_missing_or_symlinked(self):
        state = backup.HOME / '.local/state/personal-codex-router'
        for present, symlinked in ((True, False), (False, False), (True, True)):
            with self.subTest(present=present, symlinked=symlinked), \
                    patch.object(Path, 'exists', autospec=True,
                                 side_effect=lambda path: present if path == state else True), \
                    patch.object(Path, 'is_symlink', autospec=True,
                                 side_effect=lambda path: symlinked if path == state else False), \
                    patch.object(Path, 'glob', return_value=[]):
                self.assertEqual(state in backup.sources(), present and not symlinked)

    def test_claude_journal_backed_up_privately_without_socket_or_symlinks(self):
        state = self.root / '.local/state/personal-codex-router'
        state.mkdir(parents=True, mode=0o700)
        journal = state / 'claude-resets.json'
        journal.write_text(json.dumps({'journal': [], 'automatic': True}))
        (state / 'linked.json').symlink_to(journal)
        socket = state / 'claude-redeemer.sock'
        socket.touch()
        original_stat = os.stat

        def source_stat(path, *args, **kwargs):
            # Represent a UNIX socket without binding one in restricted sandboxes.
            if not isinstance(path, int) and Path(path) == socket:
                return os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return original_stat(path, *args, **kwargs)

        destination = self.root / 'backup'
        with patch.object(os, 'stat', side_effect=source_stat):
            manifest = backup.snapshot([state], destination)
        self.assertEqual([row['path'] for row in manifest['entries']], [str(journal.relative_to('/'))])
        copy = destination / 'files' / journal.relative_to('/')
        self.assertEqual(copy.read_text(), journal.read_text())
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(copy.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(backup.verify(destination)['entries']), 1)

    def test_snapshot_includes_pending_temporary_and_failed_spools_privately(self):
        spool = self.root / 'usage.sqlite3.spool'
        spool.mkdir(mode=0o700)
        failed = spool / 'failed'
        failed.mkdir(mode=0o700)
        files = [spool / 'pending.json', spool / 'orphan.tmp', failed / 'corrupt.json', failed / 'partial.tmp']
        for path in files:
            path.write_text('[' if path.parent == failed else '[{"model":"test"}]')
        (spool / 'collector.lock').touch()
        (spool / 'link.json').symlink_to(files[0])
        (spool / 'linked-directory').symlink_to(failed, target_is_directory=True)
        dest = self.root / 'backup'
        previous_umask = os.umask(0o077)
        try:
            with patch.object(backup, 'USAGE_SPOOL', spool):
                self.assertEqual(backup.regular_files([spool]), sorted(files))
                manifest = backup.snapshot([spool], dest)
        finally:
            os.umask(previous_umask)
        self.assertEqual({row['path'] for row in manifest['entries']},
                         {str(path.relative_to('/')) for path in files})
        for path in files:
            copy = dest / 'files' / path.relative_to('/')
            self.assertEqual(copy.read_bytes(), path.read_bytes())
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            self.assertEqual(copy.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(backup.verify(dest)['entries']), len(files))

    def test_unfinished_atomic_writes_are_not_backed_up(self):
        auth = self.root / 'auth'
        auth.mkdir()
        names = ['codex-a.json', 'codex-a.json.tmp.0123456789abcdef01234567', 'codex-a.json.tmp',
                 'notes.tmp.abc123', 'codex-a.json.tmp.XYZ', 'codex-a.json.lock']
        for name in names:
            (auth / name).write_text('{}')
        found = sorted(p.name for p in backup.regular_files([auth]))
        self.assertEqual(found, ['codex-a.json', 'codex-a.json.lock', 'codex-a.json.tmp.XYZ'])

    def test_retention_preserves_two_latest_and_unmanaged_directories(self):
        now = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)
        names = ['20260801T040000Z', '20260802T040000Z', '20260803T040000Z', '20260923T040000Z']
        for name in names:
            path = self.root / name
            path.mkdir()
            stamp = dt.datetime.strptime(name, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
            (path / 'manifest.json').write_text(json.dumps({'completed_at': stamp.isoformat()}))
        manual = self.root / 'manual-backup'
        manual.mkdir()
        backup.prune(self.root, now)
        self.assertFalse((self.root / names[0]).exists())
        self.assertFalse((self.root / names[1]).exists())
        self.assertTrue((self.root / names[2]).exists())
        self.assertTrue((self.root / names[3]).exists())
        self.assertTrue(manual.exists())

    def test_snapshot_blocks_collector_unlink_during_usage_enumeration_and_copy(self):
        source = self.root / 'usage.sqlite3'
        with closing(sqlite3.connect(source)) as con:
            con.execute('CREATE TABLE events (value TEXT)')
            con.commit()
        spool = self.root / 'usage.sqlite3.spool'
        spool.mkdir()
        pending = spool / 'pending.json'
        pending.write_text('[]')
        lock_path = spool / 'collector.lock'
        lock_path.touch()
        setting = self.root / 'settings.json'
        setting.write_text('{}')
        regular_files = backup.regular_files
        stable_copy = backup.stable_copy
        sqlite_copy = backup.sqlite_copy
        attempted = []
        def collector_unlink():
            with lock_path.open('rb') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                attempted.append(True)
                # The collector cannot unlink pending until it owns the lock.
        def enumerate_files(paths, exclude=()):
            files = regular_files(paths, exclude)
            if pending in files:
                collector_unlink()
            return files
        def copy_file(path, target, validate=True):
            if path == pending:
                collector_unlink()
            else:
                with lock_path.open('rb') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    pending.unlink()
            return stable_copy(path, target, validate)
        def copy_db(path, target):
            collector_unlink()
            return sqlite_copy(path, target)
        with patch.object(backup, 'USAGE_SPOOL', spool), \
                patch.object(backup, 'regular_files', side_effect=enumerate_files), \
                patch.object(backup, 'stable_copy', side_effect=copy_file), \
                patch.object(backup, 'sqlite_copy', side_effect=copy_db):
            manifest = backup.snapshot([self.root], self.root / 'backup')
        self.assertEqual(len(attempted), 3)
        self.assertFalse(pending.exists())
        copied = self.root / 'backup/files' / pending.relative_to('/')
        self.assertEqual(copied.read_text(), '[]')
        self.assertIn(str(source.relative_to('/')), {r['path'] for r in manifest['entries']})

    def test_snapshot_waits_for_collector_before_listing_vanished_spool(self):
        spool = self.root / 'usage.sqlite3.spool'
        spool.mkdir()
        pending = spool / 'pending.json'
        pending.write_text('[]')
        remaining = spool / 'remaining.json'
        remaining.write_text('[]')
        lock_path = spool / 'collector.lock'
        lock_path.touch()
        with lock_path.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def finish_collection(_):
                pending.unlink()
                fcntl.flock(lock, fcntl.LOCK_UN)
            with patch.object(backup, 'USAGE_SPOOL', spool), \
                    patch.object(backup.time, 'sleep', side_effect=finish_collection) as sleep:
                manifest = backup.snapshot([spool], self.root / 'backup')
        sleep.assert_called_once()
        self.assertEqual([r['path'] for r in manifest['entries']], [str(remaining.relative_to('/'))])

    def test_collector_lock_wait_is_bounded(self):
        spool = self.root / 'usage.sqlite3.spool'
        spool.mkdir()
        with (spool / 'collector.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(backup, 'USAGE_SPOOL', spool), \
                    patch.object(backup.time, 'monotonic', side_effect=[0, 11]):
                with self.assertRaises(TimeoutError):
                    with backup.collector_lock():
                        self.fail('Must not enter while the collector owns the lock')

    def test_snapshot_spool_without_collector_lock(self):
        spool = self.root / 'usage.sqlite3.spool'
        spool.mkdir()
        pending = spool / 'pending.json'
        pending.write_text('[]')
        with patch.object(backup, 'USAGE_SPOOL', spool):
            manifest = backup.snapshot([spool], self.root / 'backup')
        self.assertEqual([r['path'] for r in manifest['entries']], [str(pending.relative_to('/'))])
        self.assertFalse((spool / 'collector.lock').exists())
