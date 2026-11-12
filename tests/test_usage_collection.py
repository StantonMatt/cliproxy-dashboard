import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'usage_collector', Path(__file__).resolve().parents[1] / 'scripts/collect-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
ROOT = Path(__file__).resolve().parents[1]


class UsageCollectionTests(unittest.TestCase):
    def test_hourly_migration_preserves_null_groups_sums_and_legacy_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(collector.connect(db)) as con, con:
                con.execute('DROP TABLE usage_hourly')
                for provider, account, model, stamp, latency, read in (
                        (None, None, None, '2026-10-03 03:00:00', None, None),
                        ('', None, None, '2026-10-03T03:05:00Z', 1.5, 0),
                        ('codex', '', '', '2026-10-03T03:00:00Z', None, None),
                        ('claude', '0', '0', 'invalid', None, None)):
                    con.execute('''INSERT INTO usage
                        (provider,auth_index,model,timestamp,collected_at,latency_ms,cache_read_tokens)
                        VALUES (?,?,?,?,'collected',?,?)''', (provider, account, model, stamp, latency, read))
                original = con.execute('SELECT * FROM usage ORDER BY id').fetchall()
            for _ in range(2):
                with contextlib.closing(collector.connect(db)) as con:
                    self.assertEqual(con.execute('SELECT * FROM usage ORDER BY id').fetchall(), original)
                    self.assertEqual(con.execute('''SELECT provider,hour,auth_index,model,requests,
                        failed,total_tokens,latency_ms,latency_ms_count,cache_read_tokens,
                        cache_read_tokens_count,cache_creation_tokens,cache_creation_tokens_count
                        FROM usage_hourly ORDER BY provider,auth_index''').fetchall(), [
                            ('claude', None, '0', '0', 1, None, None, None, 0, None, 0, None, 0),
                            ('codex', '2026-10-03T03:00:00Z', None, None, 2, None, None, 1.5, 1, 0, 1, None, 0),
                            ('codex', '2026-10-03T03:00:00Z', '', '', 1, None, None, None, 0, None, 0, None, 0)])

    def test_interrupted_rollup_backfill_is_atomic_and_retryable(self):
        real_connect = sqlite3.connect

        class Interrupted(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith('INSERT INTO usage_hourly'):
                    raise sqlite3.OperationalError('interrupted backfill')
                return super().execute(sql, parameters)

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(collector.connect(db)) as con, con:
                con.execute('DROP TABLE usage_hourly')
                con.execute("INSERT INTO usage (timestamp,collected_at) VALUES ('invalid','old')")
            with patch.object(collector.sqlite3, 'connect',
                              side_effect=lambda *a, **kw: real_connect(*a, **kw, factory=Interrupted)):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted backfill'):
                    collector.connect(db)
            with contextlib.closing(real_connect(db)) as con:
                self.assertFalse(con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_hourly'").fetchall())
            with contextlib.closing(collector.connect(db)) as con:
                self.assertEqual(con.execute('SELECT requests,hour FROM usage_hourly').fetchall(), [(1, None)])

    def test_replay_upserts_atomically_excludes_quarantine_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db, spool = Path(directory) / 'usage.sqlite3', Path(directory) / 'batch.json'
            records = [dict(timestamp='2026-10-03T03:00:00Z', model='m', auth_index='a'),
                       dict(timestamp='2026-10-03T03:01:00Z', model='m', auth_index='a', provider='',
                            failed=True, latency_ms=2.5, tokens={'input_tokens':4, 'cache_read_tokens':0}),
                       dict(timestamp='invalid', provider='claude'), 'quarantined']
            spool.write_text(json.dumps(records))
            with contextlib.closing(collector.connect(db)) as con:
                with patch.object(Path, 'unlink', side_effect=OSError('after commit')):
                    with self.assertRaises(OSError):
                        collector.replay_spool(con, spool)
                self.assertEqual(collector.replay_spool(con, spool), (0, 0, 0))
                self.assertEqual(con.execute('SELECT count(*) FROM usage_quarantine').fetchone()[0], 1)
                self.assertEqual(con.execute('''SELECT provider,hour,requests,failed,latency_ms,
                    latency_ms_count,input_tokens,total_tokens,cache_read_tokens,
                    cache_read_tokens_count,cache_creation_tokens_count
                    FROM usage_hourly ORDER BY provider''').fetchall(), [
                        ('claude', None, 1, 0, None, 0, None, None, None, 0, 0),
                        ('codex', '2026-10-03T03:00:00Z', 2, 1, 2.5, 1, 4, None, 0, 1, 0)])
                # A subsequent NULL observation must not erase an earlier sum.
                spool.write_text(json.dumps([records[0], records[2]]))
                renamed = spool.with_name('second.json')
                spool.rename(renamed)
                self.assertEqual(collector.replay_spool(con, renamed), (2, 0, 0))
                self.assertEqual(con.execute("SELECT requests,latency_ms,latency_ms_count FROM usage_hourly WHERE provider='codex'").fetchone(), (3, 2.5, 1))
                self.assertEqual(con.execute("SELECT count(*),sum(requests) FROM usage_hourly WHERE hour IS NULL").fetchone(), (1, 2))

    def test_rollup_failure_rolls_back_usage_and_preserves_spool(self):
        with tempfile.TemporaryDirectory() as directory:
            db, spool = Path(directory) / 'usage.sqlite3', Path(directory) / 'batch.json'
            spool.write_text('[{"timestamp":"2026-10-03T03:00:00Z"}]')
            with contextlib.closing(collector.connect(db)) as con:
                con.execute('''CREATE TRIGGER reject_rollup BEFORE INSERT ON usage_hourly
                    BEGIN SELECT RAISE(FAIL, 'rollup failure'); END''')
                con.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'rollup failure'):
                    collector.replay_spool(con, spool)
                for table in ('usage', 'usage_hourly', 'usage_quarantine', 'usage_batches'):
                    self.assertEqual(con.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
                self.assertTrue(spool.exists())
                con.execute('DROP TRIGGER reject_rollup')
                self.assertEqual(collector.replay_spool(con, spool), (1, 0, 0))

    def test_failed_request_index_migration_is_additive_repeatable_and_maintained(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(collector.connect(db)) as con, con:
                for name in ('usage_failed_by_request_time', 'usage_failed_by_provider_request_time'):
                    con.execute('DROP INDEX ' + name)
                con.executemany('INSERT INTO usage (collected_at,timestamp,request_time,provider,failed) '
                                'VALUES (?,?,?,?,?)',
                                [('old', '2026-10-03T00:00:00Z', '2026-10-03T00:00:00.000000000Z', provider, failed)
                                 for provider, failed in [('claude', 1), ('codex', 0), ('', 2), (None, -1), ('codex', None)]])
                old_rows = con.execute('SELECT * FROM usage ORDER BY id').fetchall()
            for _ in range(2):
                with contextlib.closing(collector.connect(db)) as con:
                    self.assertEqual(con.execute('SELECT * FROM usage ORDER BY id').fetchall(), old_rows)
                    indexes = {r[1]: r for r in con.execute('PRAGMA index_list(usage)')}
                    for name in ('usage_failed_by_request_time', 'usage_failed_by_provider_request_time'):
                        self.assertEqual(indexes[name][4], 1, 'failed-only index must be partial')
                        sql = con.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
                        self.assertTrue(sql.endswith('WHERE failed'))
                        self.assertEqual(con.execute('SELECT id FROM usage INDEXED BY ' + name +
                                                     ' WHERE failed ORDER BY id').fetchall(), [(1,), (3,), (4,)])
            spool = Path(directory) / 'failed-batch.json'
            spool.write_text(json.dumps([dict(timestamp='2026-10-04T00:00:00Z', provider='claude', failed=True),
                                         dict(timestamp='2026-10-04T00:00:01Z', provider='codex', failed=False)]))
            with contextlib.closing(collector.connect(db)) as con:
                self.assertEqual(collector.replay_spool(con, spool), (2, 0, 0))
                for name in ('usage_failed_by_request_time', 'usage_failed_by_provider_request_time'):
                    self.assertEqual(con.execute('SELECT id FROM usage INDEXED BY ' + name +
                                                 ' WHERE failed ORDER BY id').fetchall(), [(1,), (3,), (4,), (6,)])

    def test_unreported_served_tiers_remain_unknown_through_spool_replay(self):
        records = [{}, {'service_tier':'priority'},
                   {'service_tier':'ultrafast', 'response_service_tier':None, 'failed':True},
                   {'service_tier':None, 'response_service_tier':None},
                   {'service_tier':'priority', 'response_service_tier':''},
                   {'service_tier':'priority', 'response_service_tier':'default'},
                   {'service_tier':'priority', 'response_service_tier':'priority'}]
        expected = [('normal','unknown'), ('priority','unknown'), ('ultrafast','unknown'),
                    ('normal','unknown'), ('priority','unknown'), ('priority','normal'),
                    ('priority','priority')]
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory)/'usage.sqlite3'
            spool = Path(directory)/'batch.json'
            # The queue redacts and normalizes before replay, which must be idempotent.
            reduced = [collector.reduce_record(record) for record in records]
            self.assertEqual([collector.reduce_record(record) for record in reduced], reduced)
            spool.write_text(json.dumps(reduced))
            with contextlib.closing(collector.connect(db)) as con:
                self.assertEqual(collector.replay_spool(con, spool), (7,0,0))
                self.assertEqual(con.execute('SELECT service_tier,response_service_tier FROM usage ORDER BY id').fetchall(), expected)

    def test_status_publishes_aggregate_counts_timestamps_and_private_permissions(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            spool = db.with_name(db.name + '.spool')
            spool.mkdir(mode=0o700)
            (spool / 'corrupt.json').write_bytes(b'[{')
            events = [{'model': 'test'}, 'SENSITIVE', {'tokens': ['SENSITIVE']}]
            with patch.object(collector.urllib.request, 'urlopen',
                              return_value=io.BytesIO(json.dumps(events).encode())), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            path = db.with_name('collector-status.json')
            status = json.loads(path.read_text())
            self.assertEqual(set(status), {'quarantined_total', 'failed_spools',
                                           'last_success_at', 'last_run_at'})
            self.assertEqual(status['quarantined_total'], 2)
            self.assertEqual(status['failed_spools'], 1)
            self.assertEqual(status['last_success_at'], status['last_run_at'])
            self.assertEqual(dt.datetime.fromisoformat(status['last_run_at']).utcoffset(), dt.timedelta(0))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertNotIn('SENSITIVE', path.read_text())
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            repeated = json.loads(path.read_text())
            self.assertEqual(repeated['quarantined_total'], 2)
            self.assertEqual(repeated['failed_spools'], 1)
            self.assertGreaterEqual(repeated['last_success_at'], status['last_success_at'])

    def test_failed_run_updates_status_without_erasing_last_success(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            path = db.with_name('collector-status.json')
            previous = json.loads(path.read_text())
            spool = db.with_name(db.name + '.spool')
            (spool / 'pending.json').write_text('["private record"]')
            with patch.object(collector.urllib.request, 'urlopen', side_effect=OSError('network failure')):
                with self.assertRaises(OSError):
                    collector.collect(db, key)
            status = json.loads(path.read_text())
            self.assertEqual(status['last_success_at'], previous['last_success_at'])
            self.assertGreater(status['last_run_at'], previous['last_run_at'])
            self.assertEqual(status['quarantined_total'], 1)
            self.assertEqual(status['failed_spools'], 0)

    def test_status_syncs_before_atomic_rename_and_syncs_directory_after(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db = Path(directory) / 'usage.sqlite3'
            spool = db.with_name(db.name + '.spool')
            spool.mkdir()
            path = db.with_name('collector-status.json')
            previous = {'quarantined_total': 0, 'failed_spools': 0,
                        'last_success_at': '2026-09-29T10:00:00+00:00',
                        'last_run_at': '2026-09-29T10:00:00+00:00'}
            path.write_text(json.dumps(previous))
            original_fsync, original_replace = os.fsync, Path.replace
            events = []

            def synced(fd):
                events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
                original_fsync(fd)

            def replaced(source, destination):
                self.assertEqual(events, ['file'])
                self.assertEqual(json.loads(path.read_text()), previous)
                self.assertEqual(json.loads(source.read_text())['quarantined_total'], 0)
                self.assertEqual(source.stat().st_mode & 0o777, 0o640)
                events.append('rename')
                return original_replace(source, destination)

            with contextlib.closing(collector.connect(db)) as con, \
                    patch.object(collector.os, 'fsync', side_effect=synced), \
                    patch.object(Path, 'replace', replaced):
                collector.write_status(db, con, spool, True)
            self.assertEqual(events, ['file', 'rename', 'directory'])
            self.assertEqual(json.loads(path.read_text())['last_success_at'],
                             json.loads(path.read_text())['last_run_at'])
            self.assertFalse(list(Path(directory).glob('collector-status.json.*.tmp')))

    def test_status_sync_or_rename_failure_keeps_previous_status_and_cleans_temp(self):
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                db = Path(directory) / 'usage.sqlite3'
                spool = db.with_name(db.name + '.spool')
                spool.mkdir()
                path = db.with_name('collector-status.json')
                original = '{"last_success_at":"2026-09-29T10:00:00+00:00"}'
                path.write_text(original)
                target = collector.os if operation == 'fsync' else Path
                with contextlib.closing(collector.connect(db)) as con, \
                        patch.object(target, operation, side_effect=OSError('status write failure')):
                    with self.assertRaises(OSError):
                        collector.write_status(db, con, spool, True)
                self.assertEqual(path.read_text(), original)
                self.assertFalse(list(Path(directory).glob('collector-status.json.*.tmp')))

    def test_default_lock_wait_leaves_service_time_for_collection(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db = Path(directory) / 'usage.sqlite3'
            with patch.object(collector.fcntl, 'flock', side_effect=BlockingIOError), \
                    patch.object(collector.time, 'monotonic', side_effect=[100, 114.9, 115]), \
                    patch.object(collector.time, 'sleep') as sleep, \
                    patch.object(collector.urllib.request, 'urlopen') as fetch:
                with self.assertRaises(BlockingIOError):
                    collector.collect(db)
            sleep.assert_called_once_with(0.1)
            fetch.assert_not_called()
            self.assertFalse(db.exists())
        service = (ROOT / 'scripts/personal-codex-router-usage.service').read_text()
        self.assertIn('TimeoutStartSec=90\n', service)

    def test_recorded_effort_is_preserved_without_inference(self):
        for effort in ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']:
            self.assertEqual(collector.normalize({'reasoning_effort': effort})['reasoning_effort'], effort)
        for value in [None, '', '  ', 123, {}, [], '<script>', 'x' * 33]:
            self.assertIsNone(collector.normalize({'reasoning_effort': value})['reasoning_effort'])
        self.assertEqual(collector.normalize({'reasoning_effort': ' XHIGH '})['reasoning_effort'], 'xhigh')
        record = collector.normalize({'model': 'gpt-6-astra', 'tokens': {'reasoning_tokens': 9999}})
        self.assertIsNone(record['reasoning_effort'])

    def test_legacy_database_migration_is_additive_and_repeatable(self):
        for current_schema in (False, True):
            with self.subTest(current_schema=current_schema), tempfile.TemporaryDirectory() as directory:
                db = Path(directory) / 'usage.sqlite3'
                with contextlib.closing(sqlite3.connect(db)) as con, con:
                    con.execute('CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT NOT NULL, '
                                'timestamp TEXT, provider TEXT, model TEXT, auth_index TEXT, failed INTEGER, '
                                'latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER, '
                                'reasoning_tokens INTEGER, cached_tokens INTEGER, total_tokens INTEGER)')
                    if current_schema:
                        for column in ('reasoning_effort', *collector.TIER_FIELDS):
                            con.execute(f'ALTER TABLE usage ADD COLUMN {column} TEXT')
                    con.execute("INSERT INTO usage (collected_at, model, cached_tokens, total_tokens) "
                                "VALUES ('old', 'test', 13, 42)")
                    old_columns = [row[1] for row in con.execute('PRAGMA table_info(usage)')]
                    old_row = con.execute('SELECT * FROM usage').fetchone()
                for _ in range(2):
                    with contextlib.closing(collector.connect(db)) as con:
                        self.assertEqual(con.execute('SELECT ' + ','.join(old_columns) + ' FROM usage').fetchone(),
                                         old_row)
                        self.assertEqual(con.execute('SELECT cache_read_tokens,cache_creation_tokens FROM usage').fetchone(),
                                         (None, None))
                        columns = {row[1]: row for row in con.execute('PRAGMA table_info(usage)')}
                        for field in collector.CACHE_TOKEN_FIELDS:
                            self.assertEqual(columns[field][2:5], ('INTEGER', 0, None))

    def test_summary_reads_split_counts_and_unmigrated_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,
                    timestamp TEXT, provider TEXT, model TEXT, auth_index TEXT, failed INTEGER,
                    latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
                    reasoning_tokens INTEGER, cached_tokens INTEGER, total_tokens INTEGER)''')
                con.execute("INSERT INTO usage (model, auth_index, cached_tokens) VALUES ('test','one',13)")
            for migrated in (False, True):
                if migrated:
                    with contextlib.closing(collector.connect(db)) as con, con:
                        con.execute("INSERT INTO usage (collected_at, model, auth_index, cached_tokens, "
                                    "cache_read_tokens, cache_creation_tokens) VALUES ('new','test','one',5,0,5)")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    collector.summary(db)
                row, = json.loads(output.getvalue())
                self.assertEqual(row['requests'], 2 if migrated else 1)
                self.assertEqual(row['cached_tokens'], 18 if migrated else 13)
                self.assertEqual(row['cache_read_tokens'], 0 if migrated else None)
                self.assertEqual(row['cache_creation_tokens'], 5 if migrated else None)
                self.assertEqual(row['cache_read_requests'], int(migrated))
                self.assertEqual(row['cache_creation_requests'], int(migrated))

    def test_queue_preserves_cache_split_without_inferring_from_cached_tokens(self):
        tokens = [{'cached_tokens': 17, 'cache_read_tokens_present': True},
                  {'cached_tokens': 3868, 'cache_read_tokens': 3868, 'cache_creation_tokens': 194},
                  {'cached_tokens': 5344, 'cache_read_tokens': 0, 'cache_creation_tokens': 5344},
                  {'cached_tokens': 0, 'cache_read_tokens': 0, 'cache_creation_tokens': 0},
                  {}, {'cache_read_tokens': 0}, {'cache_creation_tokens': 7}]
        expected = [(17, None, None), (3868, 3868, 194), (5344, 0, 5344), (0, 0, 0),
                    (None, None, None), (None, 0, None), (None, None, 7)]
        for value in (None, -1, 2**63, 2**80, True, False, 1.5, 'SENSITIVE', {}, [],
                      float('inf'), float('-inf'), float('nan')):
            tokens.append({field: value for field in collector.CACHE_TOKEN_FIELDS})
            expected.append((None, None, None))
        events = [{'tokens': value} for value in tokens]
        events.append({})
        expected.append((None, None, None))
        with tempfile.TemporaryDirectory() as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            with patch.object(collector.urllib.request, 'urlopen',
                              return_value=io.BytesIO(json.dumps(events).encode())), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                collector.collect(db, key)
            self.assertEqual(json.loads(output.getvalue())['saved_records'], len(events))
            with contextlib.closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute('SELECT cached_tokens,cache_read_tokens,cache_creation_tokens '
                                             'FROM usage ORDER BY id').fetchall(), expected)
                self.assertNotIn('SENSITIVE', '\n'.join(con.iterdump()))

    def test_queue_to_database_preserves_per_request_effort_and_drops_other_data(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            events = [{'timestamp': '2026-09-26T22:00:00Z', 'model': 'test', 'auth_index': 'fixture',
                       'reasoning_effort': 'xhigh', 'tokens': {'total_tokens': 42},
                       'prompt': 'SENSITIVE_PROMPT', 'api_key': 'SENSITIVE_KEY'},
                      {'model': 'test', 'reasoning_effort': 'none', 'failed': True},
                      {'model': 'test'}]
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            with sqlite3.connect(db) as con:
                rows = con.execute('SELECT reasoning_effort, total_tokens, failed FROM usage ORDER BY id').fetchall()
                self.assertEqual(rows, [('xhigh', 42, 0), ('none', None, 1), (None, None, 0)])
                self.assertNotIn('SENSITIVE', '\n'.join(con.iterdump()))

    def test_process_exit_after_read_replays_private_spool_before_fetching(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            events = [{'model': 'recovered', 'api_key': 'SENSITIVE', 'source': 'SENSITIVE',
                       'prompt': 'SENSITIVE', 'tokens': {'total_tokens': 42, 'secret': 'SENSITIVE'}},
                      'SENSITIVE', {'tokens': ['SENSITIVE'], 'api_key': 'SENSITIVE'},
                      {'model': {'prompt': 'SENSITIVE'}}]
            code = f'''
import importlib.util, io, os
from pathlib import Path
spec = importlib.util.spec_from_file_location('collector', {str(ROOT / 'scripts/collect-usage.py')!r})
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
collector.urllib.request.urlopen = lambda *args, **kwargs: io.BytesIO({json.dumps(events).encode()!r})
collector.replay_spool = lambda *args: os._exit(23)
collector.collect(Path({str(db)!r}), Path({str(key)!r}))
'''
            result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 23, result.stderr.decode())
            spool = db.with_name(db.name + '.spool')
            paths = list(spool.glob('*.json'))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_bytes()),
                             [{'model': 'recovered', 'tokens': {'total_tokens': 42}},
                              {'_invalid_type': 'str'}, {'tokens': {'_invalid_type': 'list'}},
                              {'model': {'_invalid_type': 'dict'}}])
            self.assertNotIn('SENSITIVE', paths[0].read_text())
            self.assertEqual(spool.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)

            def next_response(*args, **kwargs):
                with sqlite3.connect(db) as con:
                    self.assertEqual(con.execute("SELECT model FROM usage WHERE model = 'recovered'").fetchall(),
                                     [('recovered',)])
                    self.assertNotIn('SENSITIVE', '\n'.join(con.iterdump()))
                self.assertFalse(list(spool.glob('*.json')))
                return io.BytesIO(b'[]')

            with patch.object(collector.urllib.request, 'urlopen', side_effect=next_response), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            self.assertFalse(list(spool.glob('*.json')))

    def test_bad_records_are_quarantined_without_aborting_good_records(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            events = [{'model': 'first', 'api_key': 'SENSITIVE'}, 'SENSITIVE',
                      {'tokens': ['SENSITIVE'], 'source': 'SENSITIVE'}, {'model': 'last'}]
            output = io.StringIO()
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())), \
                    contextlib.redirect_stdout(output):
                collector.collect(db, key)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage ORDER BY id').fetchall(),
                                 [('first',), ('last',)])
                rejected = con.execute('SELECT record_index,raw_json,error FROM usage_quarantine ORDER BY id').fetchall()
                self.assertEqual([(index, json.loads(raw), error) for index, raw, error in rejected],
                                 [(1, {'_invalid_type': 'str'}, 'ValueError'),
                                  (2, {'tokens': {'_invalid_type': 'list'}}, 'ValueError')])
                self.assertNotIn('SENSITIVE', '\n'.join(con.iterdump()))
                self.assertEqual(con.execute('SELECT count(*) FROM usage_batches').fetchone()[0], 1)
            self.assertEqual(json.loads(output.getvalue()),
                             {'saved_records': 2, 'quarantined_records': 2, 'failed_spools': 0})
            self.assertFalse(list(db.with_name(db.name + '.spool').glob('*.json')))

    def test_scalar_values_are_sanitized_before_spooling_and_quarantine(self):
        events = [{'failed': 'SENSITIVE'}, {'latency_ms': 'SENSITIVE'},
                  {'reasoning_effort': 'SENSITIVE private text'}]
        events.extend({'tokens': {field: 'SENSITIVE'}} for field in collector.TOKEN_FIELDS)
        for field in collector.TEXT_FIELDS:
            events.extend([{field: {'private': 'SENSITIVE'}},
                           {field: 'x' * 256 + 'SENSITIVE'}])
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            spool = Path(directory) / 'spool'
            spool.mkdir()
            con = collector.connect(Path(directory) / 'usage.sqlite3')
            try:
                con.execute('CREATE TRIGGER reject_usage BEFORE INSERT ON usage '
                            "BEGIN SELECT RAISE(FAIL, 'bad fixture data'); END")
                con.commit()
                with patch.object(collector.urllib.request, 'urlopen',
                                  return_value=io.BytesIO(json.dumps(events).encode())):
                    path = collector.spool_response(spool, None)
                self.assertNotIn('SENSITIVE', path.read_text())
                reduced = json.loads(path.read_text())
                self.assertEqual([collector.reduce_record(record) for record in reduced], reduced)
                self.assertEqual(reduced[:3], [{'failed': {'_invalid_type': 'str'}},
                                              {'latency_ms': {'_invalid_type': 'str'}},
                                              {'reasoning_effort': None}])
                for index, field in enumerate(collector.TOKEN_FIELDS, start=3):
                    self.assertEqual(reduced[index], {'tokens': {field: {'_invalid_type': 'str'}}})
                self.assertEqual(collector.replay_spool(con, path), (0, len(events), 0))
                rows = con.execute('SELECT raw_json FROM usage_quarantine ORDER BY id').fetchall()
                self.assertEqual([json.loads(raw) for raw, in rows], reduced)
                self.assertNotIn('SENSITIVE', '\n'.join(con.iterdump()))
                self.assertFalse(path.exists())
            finally:
                con.close()

    def test_normalize_rejects_unbindable_or_nonfinite_numeric_values(self):
        for value in (2**63, 2**80, -1, float('inf'), float('-inf'), float('nan'),
                      float(2**63), True, 'SENSITIVE'):
            with self.subTest(value=value):
                self.assertIsNone(collector.normalize({'latency_ms': value})['latency_ms'])
        for value in (0, 2**63 - 1, 1.5):
            with self.subTest(value=value):
                self.assertEqual(collector.normalize({'latency_ms': value})['latency_ms'], value)
        for field in collector.TOKEN_FIELDS:
            for value in (-1, 2**63, 2**80, True, 1.5, 'SENSITIVE'):
                with self.subTest(field=field, value=value):
                    self.assertIsNone(collector.normalize({'tokens': {field: value}})[field])
            for value in (0, 2**63 - 1):
                with self.subTest(field=field, value=value):
                    record = collector.reduce_record({'tokens': {field: value}})
                    self.assertEqual(collector.normalize(record)[field], value)

    def test_huge_integer_latency_is_quarantined_and_batch_continues(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            events = [{'model': 'first'}, {'latency_ms': 2**80}, {'model': 'last'}]
            output = io.StringIO()
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())), \
                    contextlib.redirect_stdout(output):
                collector.collect(db, key)
            with contextlib.closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute('SELECT model FROM usage ORDER BY id').fetchall(),
                                 [('first',), ('last',)])
                self.assertEqual(con.execute('SELECT record_index,raw_json,error FROM usage_quarantine').fetchall(),
                                 [(1, json.dumps({'_invalid_type': 'dict'}), 'ValueError')])
            self.assertEqual(json.loads(output.getvalue()),
                             {'saved_records': 2, 'quarantined_records': 1, 'failed_spools': 0})
            self.assertFalse(list(db.with_name(db.name + '.spool').glob('*.json')))

    def test_record_binding_errors_quarantine_only_the_rejected_record(self):
        for error in (OverflowError, ValueError, sqlite3.InterfaceError, sqlite3.IntegrityError):
            with self.subTest(error=error.__name__), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                class RejectRecord(sqlite3.Connection):
                    def execute(self, sql, parameters=()):
                        if sql.startswith('INSERT INTO usage (') and parameters['model'] == 'rejected':
                            raise error('simulated bad data')
                        return super().execute(sql, parameters)

                db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
                key.write_text('test-fixture-key')
                collector.connect(db).close()
                con = sqlite3.connect(db, factory=RejectRecord)
                events = [{'model': 'first'}, {'model': 'rejected'}, {'model': 'last'}]
                with patch.object(collector, 'connect', return_value=con), \
                        patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())), \
                        contextlib.redirect_stdout(io.StringIO()):
                    collector.collect(db, key)
                with contextlib.closing(sqlite3.connect(db)) as con:
                    self.assertEqual(con.execute('SELECT model FROM usage ORDER BY id').fetchall(),
                                     [('first',), ('last',)])
                    self.assertEqual(con.execute('SELECT record_index,error FROM usage_quarantine').fetchall(),
                                     [(1, error.__name__)])
                self.assertFalse(list(db.with_name(db.name + '.spool').glob('*.json')))

    def test_commit_failure_keeps_spool_and_rolls_back_entire_batch(self):
        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError('simulated commit failure')

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            collector.connect(db).close()
            con = sqlite3.connect(db, factory=FailingCommit)
            events = [{'model': 'recovered'}, 'invalid']
            with patch.object(collector, 'connect', return_value=con), \
                    patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())):
                with self.assertRaises(sqlite3.OperationalError):
                    collector.collect(db, key)
            with sqlite3.connect(db) as con:
                for table in ('usage', 'usage_quarantine', 'usage_batches'):
                    self.assertEqual(con.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
            spool = db.with_name(db.name + '.spool')
            self.assertEqual(len(list(spool.glob('*.json'))), 1)
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage').fetchall(), [('recovered',)])
                self.assertEqual(con.execute('SELECT count(*) FROM usage_quarantine').fetchone()[0], 1)
            self.assertFalse(list(spool.glob('*.json')))

    def test_insert_failure_keeps_all_records_for_retry(self):
        class FailingInsert(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith('INSERT INTO usage (') and parameters['model'] == 'rejected':
                    raise sqlite3.OperationalError('simulated insert failure')
                return super().execute(sql, parameters)

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            collector.connect(db).close()
            con = sqlite3.connect(db, factory=FailingInsert)
            events = [{'model': 'first'}, {'model': 'rejected'}, {'model': 'last'}]
            with patch.object(collector, 'connect', return_value=con), \
                    patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())):
                with self.assertRaises(sqlite3.OperationalError):
                    collector.collect(db, key)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT count(*) FROM usage_quarantine').fetchone()[0], 0)
                self.assertEqual(con.execute('SELECT count(*) FROM usage_batches').fetchone()[0], 0)
            self.assertEqual(len(list(db.with_name(db.name + '.spool').glob('*.json'))), 1)
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage ORDER BY id').fetchall(),
                                 [('first',), ('rejected',), ('last',)])

    def test_replay_after_commit_before_spool_deletion_does_not_duplicate(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            events = [{'model': 'once'}, 'invalid']
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(events).encode())), \
                    patch.object(Path, 'unlink', side_effect=OSError('simulated deletion failure')):
                with self.assertRaises(OSError):
                    collector.collect(db, key)
            self.assertEqual(len(list(db.with_name(db.name + '.spool').glob('*.json'))), 1)
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage').fetchall(), [('once',)])
                self.assertEqual(con.execute('SELECT count(*) FROM usage_quarantine').fetchone()[0], 1)
            self.assertFalse(list(db.with_name(db.name + '.spool').glob('*.json')))

    def test_complete_temporary_spool_replays_before_key_is_read(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'missing-key'
            spool = db.with_name(db.name + '.spool')
            spool.mkdir(mode=0o700)
            path = spool / 'interrupted.tmp'
            path.write_text('[{"model": "recovered"}]')
            path.chmod(0o600)
            with patch.object(collector.urllib.request, 'urlopen') as request:
                with self.assertRaises(FileNotFoundError):
                    collector.collect(db, key)
                request.assert_not_called()
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage').fetchall(), [('recovered',)])
            self.assertFalse(path.exists())
            key.write_text('test-fixture-key')
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage').fetchall(), [('recovered',)])

    def test_invalid_response_raises_without_spooling_and_allows_next_fetch(self):
        for body in (b'{"unexpected": true}', b'[{"truncated":'):
            with self.subTest(body=body), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
                key.write_text('test-fixture-key')
                with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(body)):
                    with self.assertRaises(ValueError):
                        collector.collect(db, key)
                spool = db.with_name(db.name + '.spool')
                self.assertFalse(list(spool.glob('*.json')))
                self.assertFalse(list(spool.glob('*.tmp')))
                with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')) as request, \
                        contextlib.redirect_stdout(io.StringIO()):
                    collector.collect(db, key)
                    request.assert_called_once()

    def test_corrupt_spools_move_to_failed_and_collection_continues(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            spool = db.with_name(db.name + '.spool')
            spool.mkdir(mode=0o700)
            for name in ('partial.tmp', 'corrupt.json'):
                (spool / name).write_bytes(b'[{"truncated":')
            output = io.StringIO()
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[{"model":"new"}]')) as request, \
                    contextlib.redirect_stdout(output):
                collector.collect(db, key)
                request.assert_called_once()
            self.assertEqual(json.loads(output.getvalue()),
                             {'saved_records': 1, 'quarantined_records': 0, 'failed_spools': 2})
            failed = spool / 'failed'
            self.assertEqual(failed.stat().st_mode & 0o777, 0o700)
            self.assertEqual({p.name for p in failed.iterdir()}, {'partial.tmp', 'corrupt.json'})
            for path in failed.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            output = io.StringIO()
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')) as request, \
                    contextlib.redirect_stdout(output):
                collector.collect(db, key)
                request.assert_called_once()
            self.assertEqual(json.loads(output.getvalue())['failed_spools'], 0)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage').fetchall(), [('new',)])

    def test_network_failure_before_response_does_not_leave_an_empty_spool(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            with patch.object(collector.urllib.request, 'urlopen', side_effect=OSError('simulated network failure')):
                with self.assertRaises(OSError):
                    collector.collect(db, key)
            spool = db.with_name(db.name + '.spool')
            self.assertFalse(list(spool.glob('*.tmp')))
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)

    def test_spool_sync_failure_preserves_reduced_response_for_retry(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            original_fsync = os.fsync

            def fail_file_sync(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError('simulated spool sync failure')
                original_fsync(fd)

            body = b'[{"model": "recovered", "api_key": "SENSITIVE"}]'
            with patch.object(collector.os, 'fsync', side_effect=fail_file_sync), \
                    patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(body)):
                with self.assertRaises(OSError):
                    collector.collect(db, key)
            spool = db.with_name(db.name + '.spool')
            paths = list(spool.glob('*.tmp'))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_bytes()), [{'model': 'recovered'}])
            self.assertNotIn('SENSITIVE', paths[0].read_text())
            self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT model FROM usage').fetchall(), [('recovered',)])
            self.assertFalse(list(spool.glob('*.tmp')))

    def test_second_collector_cannot_dequeue_while_lock_is_held(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            db, key = Path(directory) / 'usage.sqlite3', Path(directory) / 'key'
            key.write_text('test-fixture-key')
            spool = db.with_name(db.name + '.spool')
            spool.mkdir(mode=0o700)
            with (spool / 'collector.lock').open('wb') as lock:
                collector.fcntl.flock(lock, collector.fcntl.LOCK_EX | collector.fcntl.LOCK_NB)
                with patch.object(collector.urllib.request, 'urlopen') as request:
                    with self.assertRaises(BlockingIOError):
                        collector.collect(db, key, lock_timeout=0)
                    request.assert_not_called()
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')), \
                    contextlib.redirect_stdout(io.StringIO()):
                collector.collect(db, key)


if __name__ == '__main__':
    unittest.main()
