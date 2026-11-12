"""Synthetic queue/journal evidence only; never open the live usage database."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_dashboard import app
from test_usage_collection import collector

spec = importlib.util.spec_from_file_location(
    'request_outcomes', Path(__file__).resolve().parents[1] / 'scripts/collect-request-outcomes.py')
outcomes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(outcomes)

START = '2026-10-03T03:00:00Z'
INVOCATION = 'a' * 32


def entry(n, message, *, invocation=INVOCATION, seconds=None):
    stamp = dt.datetime.fromisoformat(START.replace('Z', '+00:00')).timestamp()
    return dict(__CURSOR=f's=fixture;i={n}', __REALTIME_TIMESTAMP=str(int((stamp + (n if seconds is None else seconds)) * 1e6)),
                _SYSTEMD_UNIT='cliproxyapi.service', _SYSTEMD_INVOCATION_ID=invocation, MESSAGE=message)


def upstream(request_id='1234abcd', status=429):
    return (f'[2026-10-03 00:00:01] [{request_id}] [warn ] [conductor_execution.go:2034] '
            f'{status} | 123ms | upstream execution failed: provider=claude model=test '
            'auth=PRIVATE err=PRIVATE Authorization: Bearer PRIVATE')


def response(request_id='1234abcd', status=200, *, path='/v1/messages'):
    return (f'[2026-10-03 00:00:02] [{request_id}] [info ] [gin_logger.go:113] '
            f'{status} | 500ms | 127.0.0.1 | POST "{path}"')


class UsageFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.db = self.directory / 'usage.sqlite3'

    def replay(self, con, records, name='batch'):
        path = self.directory / (name + '.json')
        path.write_text(json.dumps(records))
        return collector.replay_spool(con, path)


class FailureTests(UsageFixture):
    def test_classifies_status_body_and_midstream_failures(self):
        cases = [
            ({'status_code': 429}, 'rate_limit'),
            ({'body': 'Rate limit exceeded'}, 'rate_limit'),
            ({'status_code': 200, 'body': 'Selected model is at capacity'}, 'capacity'),
            ({'status_code': 502, 'body': '{"error":{"code":"server_is_overloaded"}}'}, 'capacity'),
            ({'status_code': 529, 'body': '{"error":{"type":"overloaded_error"}}'}, 'capacity'),
            ({'status_code': 401}, 'auth'), ({'status_code': 403}, 'auth'),
            ({'status_code': 500}, 'server_error'), ({'status_code': 503}, 'server_error'),
            ({'status_code': 408}, 'timeout'), ({'status_code': 504}, 'timeout'),
            ({'status_code': 500, 'body': 'context deadline exceeded'}, 'timeout'),
            ({'body': 'read tcp PRIVATE: i/o timeout'}, 'timeout'),
            ({'body': 'upstream timed out'}, 'timeout'),
            ({'status_code': 499}, 'cancelled'),
            ({'status_code': 500, 'body': 'context canceled'}, 'cancelled'),
            ({'body': 'request cancelled'}, 'cancelled'),
            ({'status_code': 400, 'body': '{"error":{"message":"PRIVATE"}}'}, 'other'),
            ({'body': 'unexpected EOF'}, 'other'),
            ({'status_code': 200}, 'unknown'), ({}, 'unknown'),
            ({'code': 'stream_truncated', 'status_code': 502}, 'other'),
        ]
        for fail, kind in cases:
            with self.subTest(fail=fail):
                raw = dict(failed=True, fail=fail)
                reduced = collector.reduce_record(raw)
                self.assertEqual(reduced['failure_kind'], kind)
                self.assertEqual(collector.reduce_record(reduced), reduced)
                self.assertEqual(collector.normalize(reduced)['failure_kind'], kind)
                self.assertNotIn('PRIVATE', json.dumps(reduced))
                self.assertNotIn('fail', reduced)
        self.assertIsNone(collector.normalize(dict(failed=False, fail={'status_code': 429}))['failure_kind'])

    def test_malformed_details_never_abort_reduction_or_persist_private_codes(self):
        for fail in (None, [], 'PRIVATE', {'code': {'private': 'PRIVATE'}, 'body': []},
                     {'status_code': True}, {'status_code': 'PRIVATE'}, {'status_code': 999},
                     {'body': '{"response":[],"error":null}'},
                     {'body': '{"error":{"type":[],"message":{}}}', 'code': ['PRIVATE']}):
            with self.subTest(fail=fail):
                reduced = collector.reduce_record(dict(failed=True, fail=fail))
                self.assertIn(reduced['failure_kind'], collector.FAILURE_KINDS)
                self.assertNotIn('PRIVATE', json.dumps(reduced))
        record = dict(failed=True, fail={'body': '{"echo":"Rate limit exceeded"}'},
                      request_id='PRIVATE', execution_id='PRIVATE')
        self.assertEqual(collector.reduce_record(record)['failure_kind'], 'unknown')

    def test_sensitive_details_are_removed_before_fsync_and_quarantine(self):
        record = dict(timestamp=START, failed=True, request_id='1234abcd',
                      execution_id='12345678-1234-1234-1234-123456789abc',
                      fail={'status_code': 200, 'body': '{"error":{"code":"server_is_overloaded","message":"PRIVATE"}}'},
                      api_key='PRIVATE', response_headers={'Authorization': 'PRIVATE'})
        with contextlib.closing(collector.connect(self.db)) as con:
            with patch.object(collector.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps([record]).encode())):
                spool = collector.spool_response(self.directory, None)
            self.assertNotIn('PRIVATE', spool.read_text())
            self.assertNotIn('body', spool.read_text())
            with patch.object(Path, 'unlink', side_effect=OSError('after commit')):
                with self.assertRaises(OSError):
                    collector.replay_spool(con, spool)
            self.assertEqual(collector.replay_spool(con, spool), (0, 0, 0))
            self.assertEqual(con.execute('SELECT failure_kind,status_code,failure_code,request_id,execution_id FROM usage').fetchone(),
                ('capacity', 200, 'server_is_overloaded', '1234abcd', '12345678-1234-1234-1234-123456789abc'))
            record['tokens'] = ['PRIVATE']
            self.assertEqual(self.replay(con, [record], 'reject'), (0, 1, 0))
            self.assertNotIn('PRIVATE', '\n'.join(con.iterdump()))
            raw = json.loads(con.execute('SELECT raw_json FROM usage_quarantine').fetchone()[0])
            self.assertEqual(raw['failure_kind'], 'capacity')

    def legacy_db(self):
        with contextlib.closing(collector.connect(self.db)) as con:
            self.replay(con, [dict(timestamp=START, failed=True), dict(timestamp=START, failed=False)])
            with con:
                for column in ('failure_kind', 'status_code', 'failure_code', 'request_id', 'execution_id'):
                    con.execute('ALTER TABLE usage DROP COLUMN ' + column)
                for table in ('usage_journal_state', 'usage_retry_events'):
                    con.execute('DROP TABLE ' + table)
            return (con.execute('SELECT id,timestamp,failed FROM usage ORDER BY id').fetchall(),
                    con.execute('SELECT * FROM usage_hourly').fetchall())

    def test_existing_database_migration_is_additive_idempotent_and_preserves_rollup(self):
        original, hourly = self.legacy_db()
        for _ in range(2):
            with contextlib.closing(collector.connect(self.db)) as con:
                self.assertEqual(con.execute('SELECT id,timestamp,failed FROM usage ORDER BY id').fetchall(), original)
                self.assertEqual(con.execute('SELECT * FROM usage_hourly').fetchall(), hourly)
                self.assertEqual(con.execute('SELECT failure_kind,status_code,failure_code FROM usage ORDER BY id').fetchall(),
                                 [('unknown', None, None), (None, None, None)])
        self.assertEqual(app.usage_summary(START, self.db)['failure_counts']['unknown'], 1)

    def test_interrupted_migration_rolls_back_all_new_columns_and_unknown_backfill(self):
        self.legacy_db()
        real_connect = sqlite3.connect

        class Interrupted(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith('CREATE TABLE IF NOT EXISTS usage_retry_events'):
                    raise sqlite3.OperationalError('interrupted migration')
                return super().execute(sql, parameters)

        with patch.object(collector.sqlite3, 'connect', side_effect=lambda *a, **kw: real_connect(*a, **kw, factory=Interrupted)):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted migration'):
                collector.connect(self.db)
        with contextlib.closing(real_connect(self.db)) as con:
            self.assertNotIn('failure_kind', {r[1] for r in con.execute('PRAGMA table_info(usage)')})
        with contextlib.closing(collector.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT failure_kind FROM usage WHERE failed').fetchone(), ('unknown',))

    def test_request_and_summary_contract_range_provider_legacy_and_recent(self):
        records = [dict(timestamp=START, provider='codex', failed=True, fail={'status_code':429}),
                   dict(timestamp=START, provider='claude', failed=True, fail={'status_code':401}),
                   dict(timestamp=START, provider='claude', failed=True, fail={'status_code':200, 'body':'Selected model is at capacity'}),
                   dict(timestamp=START, provider='claude', failed=True),
                   dict(timestamp=START, provider='codex', failed=False),
                   dict(timestamp='2026-10-03T02:59:59Z', failed=True, fail={'status_code':503})]
        with contextlib.closing(collector.connect(self.db)) as con:
            self.replay(con, records)
        expected = dict(rate_limit=1, capacity=1, auth=1, server_error=0, timeout=0, cancelled=0, other=0, unknown=1)
        report = app.usage_summary(START, self.db)
        self.assertEqual(report['failure_counts'], expected)
        self.assertEqual(report['providers']['claude']['failure_counts']['auth'], 1)
        self.assertEqual(report['providers']['codex']['failure_counts']['rate_limit'], 1)
        rows = app.usage_requests(START, self.db)['rows']
        self.assertEqual([r['failure'] for r in rows], [r['failure'] for r in report['recent']])
        self.assertIsNone(rows[0]['failure'])
        self.assertEqual(rows[-1]['failure'], dict(kind='rate_limit', label='Rate limited', code=None,
                                                 status_code=429, source='usage_queue'))
        self.assertEqual(rows[1]['failure']['kind'], 'unknown')
        self.assertIsNone(rows[1]['failure']['source'])
        self.assertFalse(report['retry_recovery']['available'])
        self.assertIsNone(report['retry_recovery']['recovered_requests'])
        self.assertEqual(app.usage_summary('2026-10-04T00:00:00Z', self.db)['failure_counts'], app.usage_failure_counts())
        self.legacy_db()
        old_report = app.usage_summary(START, self.db)
        self.assertGreater(old_report['failure_counts']['unknown'], 0)
        self.assertTrue(all(r['failure']['kind'] == 'unknown' for r in app.usage_requests(START, self.db, failed=True)['rows']))


class JournalTests(UsageFixture):
    def test_reader_resumes_by_cursor_without_gateway_calls_and_preserves_checkpoint_on_error(self):
        class Process:
            def __init__(self, entries, status=0):
                self.stdout = io.StringIO(''.join(json.dumps(value) + '\n' for value in entries))
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stdout.close()

            def wait(self):
                return self.status

            def poll(self):
                return self.status

            def kill(self):
                self.status = -9

        reachable = patch.object(outcomes, 'cursor_reachable', return_value=True)
        reachable.start()
        self.addCleanup(reachable.stop)
        with patch.object(outcomes.subprocess, 'Popen', return_value=Process([entry(1, upstream()), entry(2, response())])) as popen, \
                patch.object(collector.urllib.request, 'urlopen', side_effect=AssertionError('No gateway calls')):
            result = outcomes.collect(self.db, since='2026-10-03')
            self.assertEqual(result['captured_events'], 2)
            self.assertIn('--since=2026-10-03', popen.call_args.args[0])
            self.assertEqual(popen.call_args.args[0], ['journalctl', '_SYSTEMD_UNIT=cliproxyapi.service',
                             '--no-pager', '--output=json', '--since=2026-10-03'])
        with patch.object(outcomes.subprocess, 'Popen', return_value=Process([])) as popen:
            self.assertEqual(outcomes.collect(self.db)['scanned_entries'], 0)
            self.assertIn('--after-cursor=s=fixture;i=2', popen.call_args.args[0])
            self.assertFalse(any(arg.startswith('--since') or arg == '--follow' for arg in popen.call_args.args[0]))
        with patch.object(outcomes.subprocess, 'Popen', return_value=Process([entry(3, upstream())], status=1)):
            with self.assertRaisesRegex(RuntimeError, 'cursor was not reset'):
                outcomes.collect(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT cursor FROM usage_journal_state').fetchone()[0], 's=fixture;i=2')
            self.assertEqual(con.execute('SELECT count(*) FROM usage_retry_events').fetchone()[0], 2)

    def test_vacuumed_checkpoint_restarts_coverage_instead_of_hiding_the_gap(self):
        class Process:
            def __init__(self, entries):
                self.stdout = io.StringIO(''.join(json.dumps(value) + '\n' for value in entries))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stdout.close()

            def wait(self):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        def state():
            with contextlib.closing(sqlite3.connect(self.db)) as con:
                return con.execute('SELECT cursor,since FROM usage_journal_state').fetchone()

        with patch.object(outcomes.subprocess, 'Popen', return_value=Process([entry(1, upstream())])):
            outcomes.collect(self.db, since='2026-10-03')
        cursor, first_since = state()
        # The saved entry still exists: reachable, coverage unchanged.
        with patch.object(outcomes.subprocess, 'Popen', return_value=Process([entry(1, upstream())])):
            self.assertTrue(outcomes.cursor_reachable(cursor))
        # Vacuumed: journalctl silently seeks to a later entry.
        with patch.object(outcomes.subprocess, 'Popen', return_value=Process([entry(5, response())])):
            self.assertFalse(outcomes.cursor_reachable(cursor))
        later = entry(6, response())
        with patch.object(outcomes, 'cursor_reachable', return_value=False), \
                patch.object(outcomes.subprocess, 'Popen', return_value=Process([later])):
            self.assertTrue(outcomes.collect(self.db)['coverage_gap'])
        self.assertEqual(state(), (later['__CURSOR'], outcomes.journal_position(later)[1]))
        self.assertNotEqual(state()[1], first_since)
        with patch.object(outcomes, 'cursor_reachable', return_value=False), \
                patch.object(outcomes.subprocess, 'Popen', return_value=Process([])):
            self.assertTrue(outcomes.collect(self.db)['coverage_gap'])
        self.assertGreater(state()[1], outcomes.journal_position(later)[1])

    def test_parser_reduces_only_allowlisted_request_evidence(self):
        event = outcomes.reduce_entry(entry(1, upstream()))
        self.assertEqual(event[-2:], ('upstream_429', 429))
        self.assertNotIn('PRIVATE', json.dumps(event))
        self.assertEqual(outcomes.reduce_entry(entry(2, response()))[-2:], ('response', 200))
        for unit in (None, 'other.service', ['cliproxyapi.service', 'other.service']):
            self.assertIsNone(outcomes.reduce_entry(entry(2, response()) | {'_SYSTEMD_UNIT': unit}))
        for message, invocation in ((upstream('--------'), INVOCATION),
                                    (upstream(status=503), INVOCATION),
                                    (response(path='/v0/management/auth-files'), INVOCATION),
                                    (upstream(), 'invalid'), ('PRIVATE', INVOCATION)):
            self.assertIsNone(outcomes.reduce_entry(entry(3, message, invocation=invocation)))

    def test_retry_recovery_counts_attempts_and_distinct_completions_conservatively(self):
        events = [entry(1, upstream()), entry(2, upstream()), entry(3, response()),
                  entry(4, upstream('11111111')), entry(5, response('11111111', 429)),
                  entry(6, upstream('22222222')), entry(7, response('22222222', 101)),
                  entry(8, response('33333333')), entry(9, upstream('33333333')),
                  entry(10, upstream('44444444')), entry(11, response('44444444'), invocation='b'*32),
                  entry(12, upstream('55555555')), entry(13, response('55555555')),
                  entry(14, response('55555555')),  # reused short ID: exclude the whole group
                  entry(15, upstream('66666666')), entry(16, response('66666666', 500)),
                  entry(17, upstream('77777777'))]  # no completion yet
        with contextlib.closing(collector.connect(self.db)) as con:
            self.assertEqual(outcomes.ingest_entries(con, events), dict(scanned_entries=17, captured_events=17))
            self.assertEqual(outcomes.ingest_entries(con, events)['captured_events'], 0)
            con.row_factory = sqlite3.Row
            view = app.usage_retry_recovery(con, START)
            self.assertTrue(view['available'])
            self.assertEqual((view['recovered_requests'], view['recovered_upstream_429s']), (1, 2))
            later = app.usage_retry_recovery(con, '2026-10-03T03:00:04Z')
            self.assertEqual((later['recovered_requests'], later['recovered_upstream_429s']), (0, 0))
            # Recovery becomes visible once a later batch supplies the completion.
            outcomes.ingest_entries(con, [entry(18, response('77777777'))])
            self.assertEqual(app.usage_retry_recovery(con, START)['recovered_requests'], 2)
            self.assertNotIn('PRIVATE', '\n'.join(con.iterdump()))
        self.assertEqual(app.usage_summary(START, self.db)['retry_recovery']['recovered_requests'], 2)

    def test_cursor_and_events_commit_together_and_stream_failure_keeps_previous_checkpoint(self):
        with contextlib.closing(collector.connect(self.db)) as con:
            with con:
                con.execute('''CREATE TRIGGER reject_event BEFORE INSERT ON usage_retry_events
                    BEGIN SELECT RAISE(FAIL, 'event failure'); END''')
            with self.assertRaises(sqlite3.IntegrityError):
                outcomes.ingest_entries(con, [entry(1, response()), entry(2, upstream())])
            self.assertEqual(con.execute('SELECT count(*) FROM usage_journal_state').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM usage_retry_events').fetchone()[0], 0)
            con.execute('DROP TRIGGER reject_event')
            con.commit()

            def interrupted():
                yield entry(1, upstream())
                yield entry(2, response())
                yield entry(3, upstream('11111111'))
                raise RuntimeError('journal failure')

            with self.assertRaises(RuntimeError):
                outcomes.ingest_entries(con, interrupted(), batch_size=2)
            self.assertEqual(con.execute('SELECT cursor FROM usage_journal_state').fetchone()[0], 's=fixture;i=2')
            self.assertEqual(con.execute('SELECT count(*) FROM usage_retry_events').fetchone()[0], 2)
            outcomes.ingest_entries(con, [entry(3, upstream('11111111')), entry(4, response('11111111'))])
            self.assertEqual(app.usage_retry_recovery(con, START)['recovered_requests'], 2)

    def test_missing_journal_identity_is_not_guessed_and_position_must_be_valid(self):
        value = entry(1, upstream())
        del value['_SYSTEMD_INVOCATION_ID']
        self.assertIsNone(outcomes.reduce_entry(value))
        for field, bad in (('__CURSOR', 'PRIVATE with spaces'), ('__REALTIME_TIMESTAMP', 'PRIVATE')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                outcomes.reduce_entry(entry(1, upstream()) | {field:bad})


if __name__ == '__main__':
    unittest.main()
