import contextlib
import datetime as dt
import gzip
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_dashboard import app
from test_usage_collection import collector

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retention', ROOT / 'scripts/retain-usage.py')
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)
NOW = dt.datetime(2027, 1, 1, 12, tzinfo=dt.timezone.utc)
CUTOFF = '2026-10-03T00:00:00Z'


class RetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / 'usage.sqlite3'
        self.archives = self.root / 'archives'
        with contextlib.closing(collector.connect(self.db)) as con:
            spool = self.root / 'batch.json'
            spool.write_text(json.dumps([
                dict(timestamp=stamp, provider=provider, auth_index='a', model='m', failed=n % 2 == 0,
                     latency_ms=1.5, tokens=dict(input_tokens=n, total_tokens=20, cache_read_tokens=0))
                for provider in ('codex', 'claude') for n, stamp in enumerate((
                    '2026-09-22T01:23:45Z', '2026-10-02T23:59:59.9994Z',
                    '2026-10-03T00:00:00Z', '2026-12-31T12:00:00Z', 'invalid'))]))
            collector.replay_spool(con, spool)

    def snapshot(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            return {name: con.execute('SELECT * FROM ' + name).fetchall()
                    for name in ('usage', 'usage_hourly', 'usage_batches')}

    def retain(self, **kwargs):
        return retention.retain(self.db, self.archives, now=NOW, batch_size=1, **kwargs)

    def test_dry_run_is_read_only_and_counts_whole_utc_days(self):
        before = self.snapshot()
        result = self.retain(dry_run=True)
        self.assertEqual(result, dict(dry_run=True, before=CUTOFF, eligible=4, deleted=0, archives=0))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.archives.exists())
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            self.assertFalse(app.raw_history_boundary(con))

    def test_archives_reopen_and_match_every_deleted_row_with_private_modes(self):
        before = self.snapshot()
        result = self.retain()
        self.assertEqual((result['deleted'], result['archives']), (4, 4))
        after = self.snapshot()
        self.assertEqual(after['usage_hourly'], before['usage_hourly'])
        self.assertEqual(after['usage_batches'], before['usage_batches'])
        archived = []
        for path in self.archives.glob('*.jsonl.gz'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with gzip.open(path, 'rt') as stream:
                header, row, trailer = [json.loads(line) for line in stream]
            retention.verify_archive(path, header['columns'], [row], CUTOFF)
            self.assertEqual(trailer['rows'], 1)
            archived.append(tuple(row))
        self.assertEqual(set(archived), set(before['usage']) - set(after['usage']))
        self.assertEqual(self.archives.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(self.archives.glob('*.partial')))
        self.assertEqual(self.retain()['deleted'], 0)

    def test_corrupt_or_unverified_archive_never_deletes(self):
        before = self.snapshot()
        with patch.object(retention, 'verify_archive', side_effect=ValueError('bad checksum')):
            with self.assertRaisesRegex(ValueError, 'bad checksum'):
                self.retain()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.archives.glob('*.partial')))

    def test_corruption_is_detected_on_reread(self):
        self.retain()
        path = next(self.archives.glob('*.jsonl.gz'))
        with gzip.open(path, 'rt') as stream:
            header, row, _ = [json.loads(line) for line in stream]
        contents = bytearray(path.read_bytes())
        contents[-8] ^= 0xFF
        path.write_bytes(contents)
        with self.assertRaises((gzip.BadGzipFile, EOFError, ValueError)):
            retention.verify_archive(path, header['columns'], [row], CUTOFF)

    def test_delete_batches_release_writer_lock_and_preserve_concurrent_collection(self):
        original = retention.write_archive
        calls = []

        def archive(*args):
            # Acquiring this lock proves that compression never holds a write
            # transaction, and that previous deletions have committed.
            with contextlib.closing(collector.connect(self.db)) as writer:
                spool = self.root / ('concurrent-' + str(len(calls)) + '.json')
                spool.write_text(json.dumps([dict(timestamp='2026-12-31T13:00:00Z')]))
                collector.replay_spool(writer, spool)
                calls.append(writer.execute('SELECT count(*) FROM usage').fetchone()[0])
            return original(*args)

        with patch.object(retention, 'write_archive', side_effect=archive):
            self.assertEqual(self.retain()['deleted'], 4)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(self.snapshot()['usage']), 10)

    def test_changed_source_rows_abort_delete(self):
        original = retention.write_archive
        def archive(*args):
            path = original(*args)
            with contextlib.closing(sqlite3.connect(self.db)) as writer, writer:
                writer.execute('UPDATE usage SET total_tokens=999 WHERE id=1')
            return path
        with patch.object(retention, 'write_archive', side_effect=archive):
            with self.assertRaisesRegex(ValueError, 'Usage changed'):
                self.retain()
        self.assertEqual(len(self.snapshot()['usage']), 10)

    def test_missing_or_incomplete_hourly_history_refuses_retention(self):
        for operation in ('DELETE FROM usage_hourly', 'DROP TABLE usage_hourly'):
            with contextlib.closing(sqlite3.connect(self.db)) as con, con:
                con.execute(operation)
            with self.assertRaisesRegex(ValueError, 'Hourly history'):
                self.retain()
            with contextlib.closing(sqlite3.connect(self.db)) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM usage').fetchone()[0], 10)

    def test_backfill_after_pruning_is_blocked_but_normal_collection_still_works(self):
        self.retain()
        with contextlib.closing(collector.connect(self.db)) as con, con:
            con.execute('DROP TABLE usage_hourly')
        with self.assertRaisesRegex(RuntimeError, 'never backfill'):
            collector.connect(self.db)
        report = app.usage_summary('1970-01-01T00:00:00Z', self.db)
        self.assertFalse(report['available'])
        self.assertIn('missing after retention', report['error'])

    def test_rollup_totals_since_and_hours_survive_pruning(self):
        starts = ['1970-01-01T00:00:00Z', '2026-09-22T01:00:00Z', CUTOFF]
        before = [app.usage_summary(start, self.db) for start in starts]
        self.retain()
        for start, expected in zip(starts, before):
            actual = app.usage_summary(start, self.db)
            self.assertTrue(actual['available'])
            self.assertEqual(actual['since'], '2026-09-22T01:00:00Z')
            for field in ('since', 'accounts', 'models', 'hours', 'last_collected_at', 'providers', 'claude'):
                self.assertEqual(actual[field], expected[field], (start, field))

    def test_non_hour_archived_summaries_round_down_and_recent_ranges_stay_exact(self):
        self.retain()
        expected = app.usage_summary('2026-09-22T01:00:00Z', self.db)
        for start in ('2026-09-22T01:23:46Z', '2026-09-22T07:08:46+05:45',
                      '2026-09-21T21:53:46-03:30', '2026-09-22T01:23:46.123456789Z'):
            with self.subTest(start=start):
                report = app.usage_summary(start, self.db)
                self.assertTrue(report['available'], report['error'])
                self.assertEqual(report['effective_since'], '2026-09-22T01:00:00Z')
                for field in ('accounts', 'models', 'hours', 'providers', 'claude'):
                    self.assertEqual(report[field], expected[field])
        recent = app.usage_summary('2026-12-31T12:00:01Z', self.db)
        self.assertTrue(recent['available'])
        self.assertEqual(recent['accounts'], [])
        self.assertNotIn('effective_since', recent)
        self.assertEqual(len(app.usage_requests(CUTOFF, self.db)['rows']), 4)

    def test_normal_ui_summary_ranges_work_after_pruning_in_fractional_timezones(self):
        self.retain()
        for offset in (dt.timedelta(hours=5, minutes=45), dt.timedelta(hours=-3, minutes=-30)):
            # startFor uses browser-local midnight for Today and Last 7 days,
            # and the UTC epoch for All recorded.
            midnight = NOW.astimezone(dt.timezone(offset)).replace(hour=0, minute=0, second=0, microsecond=0)
            for start in (midnight.isoformat(), (midnight - dt.timedelta(days=6)).isoformat(),
                          '1970-01-01T00:00:00.000Z'):
                with self.subTest(offset=offset, start=start):
                    report = app.usage_summary(start, self.db)
                    self.assertTrue(report['available'], report['error'])
                    self.assertNotIn('effective_since', report)

    def test_requests_api_returns_retained_rows_and_archive_boundary(self):
        self.retain()
        handler = object.__new__(app.Handler)
        handler.path = '/api/requests'
        handler.allowed = Mock(return_value=True)
        handler.send = Mock()
        handler.server = SimpleNamespace(state=SimpleNamespace(
            fast_modes=None, request_history_snapshot=Mock(return_value={})))
        with patch.object(app, 'DB', self.db):
            handler.do_GET()
        status, body = handler.send.call_args.args
        self.assertEqual(status, 200)
        page = json.loads(body)
        self.assertEqual(page['archived_before'], CUTOFF)
        self.assertEqual(len(page['rows']), 4)
        self.assertIsNone(page['next'])

    def test_fractional_hour_boundary_corrections_are_retained_for_exact_totals(self):
        with contextlib.closing(collector.connect(self.db)) as con:
            spool = self.root / 'edges.json'
            spool.write_text(json.dumps([dict(timestamp=stamp, failed=True) for stamp in (
                '2026-09-22T01:59:59.9996Z', '2026-10-02T23:59:59.9996Z')]))
            collector.replay_spool(con, spool)
        starts = ('1970-01-01T00:00:00Z', '2026-09-22T02:00:00Z', CUTOFF)
        before = [app.usage_summary(start, self.db) for start in starts]
        self.assertEqual(self.retain()['deleted'], 4)
        for start, expected in zip(starts, before):
            actual = app.usage_summary(start, self.db)
            for field in ('accounts', 'models', 'hours', 'providers', 'claude'):
                self.assertEqual(actual[field], expected[field])
        rounded = app.usage_summary('2026-09-22T02:15:00Z', self.db)
        self.assertTrue(rounded['available'])
        self.assertEqual(rounded['effective_since'], '2026-09-22T02:00:00Z')
        for field in ('accounts', 'models', 'hours', 'providers', 'claude'):
            self.assertEqual(rounded[field], before[1][field])

    def test_newest_row_is_kept_so_ids_are_never_reused(self):
        db = self.root / 'old-only.sqlite3'
        with contextlib.closing(collector.connect(db)) as con:
            spool = self.root / 'old.json'
            spool.write_text(json.dumps([dict(timestamp='2026-09-22T01:00:0%dZ' % n) for n in range(3)]))
            collector.replay_spool(con, spool)
        result = retention.retain(db, self.archives, now=NOW, batch_size=1)
        self.assertEqual((result['eligible'], result['deleted']), (2, 2))
        with contextlib.closing(collector.connect(db)) as con:
            spool = self.root / 'new.json'
            spool.write_text(json.dumps([dict(timestamp='2026-12-31T12:00:00Z')]))
            collector.replay_spool(con, spool)
            self.assertEqual([r[0] for r in con.execute('SELECT id FROM usage ORDER BY id')], [3, 4])

    def test_archives_inside_checkout_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            retention.retain(self.db, ROOT / 'archives', now=NOW)


if __name__ == '__main__':
    unittest.main()
