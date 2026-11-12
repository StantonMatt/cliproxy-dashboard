"""Time-series behavior on synthetic temporary SQLite data, including real rollups."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from test_requests_api import collector
from test_dashboard import app
import series

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
HELPERS = dict(boundary=app.raw_history_boundary, lookup=app.usage_account_lookup,
               email=app.email_address, labels=app.USAGE_FAILURE_LABELS, retry=app.usage_retry_recovery, provider_filter=app.usage_provider_filter)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'usage.sqlite3'
        with contextlib.closing(collector.connect(self.db)):
            pass
        self.gateway = dict(inventory=[dict(type='codex', auth_index='a', name='codex.json', email='one@example.test'),
                                       dict(type='claude', auth_index='a', name='claude.json', email='two@example.test')])

    def insert(self, stamp, *, rollup=True, **kwargs):
        row = dict(collected_at=stamp, timestamp=stamp, request_time=app.timestamp_key(stamp), provider='codex',
                   auth_index='a', model='model-a', failed=0, latency_ms=100, input_tokens=100, output_tokens=20,
                   cached_tokens=30, total_tokens=120, cache_read_tokens=None, cache_creation_tokens=None)
        row.update(kwargs)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('INSERT INTO usage (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
            if rollup:
                con.execute('DELETE FROM usage_hourly')
                con.execute(collector.ROLLUP_INSERT + ' GROUP BY 1,2,3,4')

    def query(self, endpoint='series', **kwargs):
        kwargs.setdefault('since', '2026-11-01T00:00:00Z')
        kwargs.setdefault('until', '2026-11-02T00:00:00Z')
        return series.query('/api/' + endpoint, {k: [str(v)] for k, v in kwargs.items()}, self.db,
                            self.gateway, helpers=HELPERS, now=NOW)

    def get(self, endpoint='series', params=None, headers=None):
        handler = object.__new__(app.Handler)
        handler.path = '/api/' + endpoint + ('?' + urlencode(params, doseq=True) if params else '')
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1'} | (headers or {})
        handler.send = Mock()
        handler.server = SimpleNamespace(state=SimpleNamespace(request_history_snapshot=lambda: self.gateway))
        with patch.object(app, 'DB', self.db):
            handler.do_GET()
        status, body = handler.send.call_args.args
        return status, json.loads(body)

    def test_counts_groups_identity_filters_and_outcomes(self):
        self.insert('2026-11-01T01:10:00Z')
        self.insert('2026-11-01T01:20:00Z', provider='claude', failed=1, model='other')
        self.insert('2026-11-01T02:10:00Z', auth_index='b')
        for group, keys in [('none', {'total'}), ('provider', {'provider:codex', 'provider:claude'}),
                            ('account', {'account:codex:a', 'account:codex:b', 'account:claude:a'}),
                            ('model', {'model:codex:model-a', 'model:claude:other'}),
                            ('outcome', {'outcome:ok', 'outcome:failed'})]:
            with self.subTest(group=group):
                payload = self.query(group=group, bucket='hour')
                self.assertEqual({s['key'] for s in payload['series']}, keys)
                self.assertEqual(sum(s['total'] for s in payload['series']), 3)
                self.assertEqual(len(payload['buckets']), 24)
                self.assertTrue(all(len(s['values']) == 24 for s in payload['series']))
        account = next(s for s in self.query(group='account')['series'] if s['key'] == 'account:codex:a')
        self.assertEqual((account['provider'], account['auth_index'], account['email'], account['account_key']),
                         ('codex', 'a', 'one@example.test', 'codex.json'))
        self.assertEqual(self.query(provider='claude')['series'][0]['total'], 1)
        self.assertEqual(self.query(account='b')['series'][0]['total'], 1)
        self.assertEqual(self.query(provider='claude', account='b')['series'][0]['total'], 0)

    def test_api_key_provider_filters_reuse_request_history_attribution(self):
        self.gateway['api_key_accounts'] = [dict(provider='claude-api-key', auth_index='key-account', label='Synthetic key')]
        self.insert('2026-11-01T01:10:00Z', provider='claude')
        self.insert('2026-11-01T01:20:00Z', provider='claude', auth_index='key-account', failed=1, failure_kind='timeout')
        self.assertEqual(self.query(provider='claude')['series'][0]['total'], 1)
        self.assertEqual(self.query(provider='claude-api-key')['series'][0]['total'], 1)
        self.assertEqual(self.query('breakdown', provider='claude-api-key', by='failure_reason')['total'], 1)
        self.assertEqual(self.query(bucket='minute', provider='claude-api-key', since='2026-11-01T01:00:00Z', until='2026-11-01T02:00:00Z')['series'][0]['total'], 1)

    def test_rates_zero_denominators_legacy_cache_and_weighted_latency(self):
        self.insert('2026-11-01T00:10:00Z', provider='claude', input_tokens=10, output_tokens=5,
                    total_tokens=115, cached_tokens=100, latency_ms=100, cache_read_tokens=80, cache_creation_tokens=20)
        self.insert('2026-11-01T00:20:00Z', provider='claude', input_tokens=10, output_tokens=5,
                    total_tokens=215, cached_tokens=200, latency_ms=None, failed=1)
        self.insert('2026-11-01T01:10:00Z', provider='claude', input_tokens=0, output_tokens=0,
                    total_tokens=0, cached_tokens=None, latency_ms=900)
        payload = self.query(metric='cache_hit_rate', bucket='hour')
        self.assertAlmostEqual(payload['series'][0]['values'][0], 100 * 80 / 320)
        self.assertIsNone(payload['series'][0]['values'][1])
        self.assertIsNone(payload['series'][0]['values'][2])
        self.assertAlmostEqual(payload['series'][0]['total'], 25)
        # Execute the real client helpers against the existing summary's accounts.
        # This guards the legacy/partial coverage contract shared with the old UI.
        source = (Path(__file__).resolve().parents[1] / 'dashboard/static/app.js').read_text()
        functions = source[source.index('function totals('):source.index('function cachedShareLabel(')]
        script = functions + """
            const providers = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
            const rows = Object.entries(providers).flatMap(([p,s]) => s.accounts.map(r => ({...r,...promptUsage(p,r)})));
            console.log(JSON.stringify(cachedShare(totals(rows))));
        """
        summary = app.usage_summary('2026-11-01T00:00:00Z', self.db)
        expected = subprocess.run(['node', '-e', script], input=json.dumps(summary['providers']),
                                  text=True, capture_output=True, check=True, timeout=5)
        self.assertEqual(payload['series'][0]['total'], json.loads(expected.stdout))

        duration = self.query(metric='avg_duration', bucket='hour')['series'][0]
        self.assertEqual(duration['values'][:3], [100, 900, None])
        self.assertEqual(duration['total'], 500)
        success = self.query(metric='success_rate', bucket='hour')['series'][0]
        self.assertEqual(success['values'][:3], [50, 100, None])
        self.assertAlmostEqual(success['total'], 200 / 3)
        # Unknown Claude reads stay unknown despite a nonzero legacy cached total.
        unknown = self.query(metric='cache_hit_rate', bucket='minute', since='2026-11-01T00:20:00Z', until='2026-11-01T00:21:00Z')
        self.assertIsNone(unknown['series'][0]['total'])

    def test_cache_matches_client_when_combining_providers_and_partial_coverage(self):
        self.insert('2026-11-01T00:10:00Z', cached_tokens=500)  # clamp rates at 100
        self.assertEqual(self.query(metric='cache_hit_rate')['series'][0]['total'], 100)
        self.insert('2026-11-01T00:20:00Z', provider='claude', input_tokens=10, output_tokens=5,
                    total_tokens=215, cached_tokens=200, cache_read_tokens=None, cache_creation_tokens=None)
        self.assertEqual(self.query(metric='cache_hit_rate')['series'][0]['total'], 100)
        self.insert('2026-11-01T00:30:00Z', input_tokens=1000, total_tokens=1020, cached_tokens=0)
        self.assertAlmostEqual(self.query(metric='cache_hit_rate')['series'][0]['total'], 100 * 500 / 1310)

    def test_local_midnight_dst_fold_spring_gap_and_week_alignment(self):
        for stamp in ('2026-11-01T04:30:00Z', '2026-11-01T05:30:00Z', '2026-11-01T06:30:00Z', '2026-11-02T04:30:00Z'):
            self.insert(stamp)
        payload = self.query(bucket='hour', tz='America/New_York', since='2026-11-01T00:00:00-04:00', until='2026-11-02T00:00:00-05:00')
        self.assertEqual(len(payload['buckets']), 25)
        self.assertEqual(payload['buckets'][1:3], ['2026-11-01T01:00:00-04:00', '2026-11-01T01:00:00-05:00'])
        self.assertEqual(payload['series'][0]['values'][:3], [1, 1, 1])
        self.assertEqual(payload['series'][0]['values'][-1], 1)
        day = self.query(bucket='day', tz='America/New_York', since='2026-11-01T00:00:00-04:00', until='2026-11-02T00:00:00-05:00')
        self.assertEqual(day['series'][0]['values'], [4])
        self.insert('2026-03-08T05:30:00Z')
        spring = self.query(bucket='hour', tz='America/New_York', since='2026-03-08T00:00:00-05:00', until='2026-03-09T00:00:00-04:00')
        self.assertEqual(len(spring['buckets']), 23)
        self.assertFalse(any('T02:' in key for key in spring['buckets']))
        week = self.query(bucket='week', tz='America/New_York')['buckets']
        self.assertTrue(all(dt.datetime.fromisoformat(key).weekday() == 0 for key in week))

    def test_fractional_timezone_splits_rollup_hours_at_local_boundary(self):
        self.insert('2026-11-01T00:15:00Z')
        self.insert('2026-11-01T00:45:00Z')
        payload = self.query(bucket='hour', tz='Asia/Kolkata', since='2026-11-01T05:00:00+05:30', until='2026-11-01T07:00:00+05:30')
        self.assertEqual(payload['buckets'], ['2026-11-01T05:00:00+05:30', '2026-11-01T06:00:00+05:30'])
        self.assertEqual(payload['series'][0]['values'], [1, 1])
        day = self.query(bucket='day', tz='Asia/Kolkata', since='2026-11-01T00:00:00+05:30', until='2026-11-02T00:00:00+05:30')
        self.assertEqual(day['series'][0]['values'], [2])

    def test_santiago_skipped_midnight_and_lord_howe_half_hour_dst(self):
        self.insert('2026-09-06T04:15:00Z')
        payload = self.query(bucket='day', tz='America/Santiago', since='2026-09-06T00:00:00-04:00', until='2026-09-08T00:00:00-03:00')
        self.assertEqual(payload['buckets'], ['2026-09-06T01:00:00-03:00', '2026-09-07T00:00:00-03:00'])
        self.assertEqual(payload['series'][0]['values'], [1, 0])
        compared = self.query(bucket='hour', compare=1, tz='America/Santiago', since='2026-09-06T01:00:00-03:00', until='2026-09-06T04:00:00-03:00')
        self.assertEqual(compared['previous']['since'], '2026-09-05T04:00:00Z')
        self.assertEqual(compared['previous']['until'], '2026-09-05T08:00:00Z')
        self.insert('2026-10-03T15:45:00Z')  # 02:45 after Lord Howe's 30-minute gap.
        payload = self.query(bucket='hour', tz='Australia/Lord_Howe', since='2026-10-04T00:00:00+10:30', until='2026-10-04T05:00:00+11:00')
        self.assertEqual(payload['buckets'][-3:], ['2026-10-04T02:30:00+11:00', '2026-10-04T03:00:00+11:00', '2026-10-04T04:00:00+11:00'])
        self.assertEqual(payload['series'][0]['total'], 1)

    def test_all_history_compare_is_clamped_before_bucket_limit(self):
        self.insert('2026-11-01T01:00:00Z')
        payload = self.query(bucket='week', since='1970-01-01T00:00:00Z', compare=1)
        self.assertEqual(payload['since'], '2026-11-01T00:00:00Z')
        self.assertEqual(payload['previous']['since'], '2026-10-31T00:00:00Z')
        self.assertEqual(payload['previous']['series'][0]['total'], 0)

    def test_old_schema_without_rollup_or_split_cache_fields(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('DROP TABLE usage_hourly')
            con.execute('DROP TABLE usage')
            con.execute('CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT, timestamp TEXT, auth_index TEXT, model TEXT, failed INTEGER, latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, total_tokens INTEGER)')
            con.execute("INSERT INTO usage VALUES(1,'2026-11-01','2026-11-01T01:15:00Z','a','legacy',0,250,100,20,40,120)")
        payload = self.query(bucket='hour', metric='cache_hit_rate')
        self.assertEqual(payload['series'][0]['total'], 40)
        self.assertEqual(self.query(metric='avg_duration')['series'][0]['total'], 250)
        self.assertEqual(self.query(group='model')['series'][0]['key'], 'model:codex:legacy')
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE usage SET timestamp='2026-11-01 00:00:00'")
        first = self.query(since='1970-01-01T00:00:00Z', bucket='day')
        self.assertEqual(first['first_recorded'], '2026-11-01T00:00:00Z')
        self.assertEqual(first['buckets'], ['2026-11-01T00:00:00+00:00'])

    def test_indexed_schema_retains_parseable_rows_without_rfc3339_keys(self):
        self.insert('2026-11-01 01:10:00')
        self.assertEqual(self.query(bucket='minute', since='2026-11-01T01:00:00Z', until='2026-11-01T02:00:00Z')['series'][0]['total'], 1)

    def test_compare_today_same_hours_keys_and_regular_previous_period(self):
        self.insert('2026-10-31T03:00:00Z', auth_index='old')
        self.insert('2026-11-01T03:00:00Z')
        payload = self.query(bucket='hour', group='account', compare=1, until='2026-11-01T12:00:00Z')
        self.assertEqual(payload['previous']['since'], '2026-10-31T00:00:00Z')
        self.assertEqual(payload['previous']['until'], '2026-10-31T12:00:00Z')
        self.assertEqual(len(payload['previous']['buckets']), len(payload['buckets']))
        self.assertEqual([s['key'] for s in payload['series']], [s['key'] for s in payload['previous']['series']])
        totals = {s['key']: s['total'] for s in payload['series']}
        self.assertEqual(totals['account:codex:old'], 0)
        payload = self.query(bucket='hour', compare=1, since='2026-11-01T02:00:00Z', until='2026-11-01T08:00:00Z')
        self.assertEqual((payload['previous']['since'], payload['previous']['until']), ('2026-10-31T20:00:00Z', '2026-11-01T02:00:00Z'))

    def test_first_current_hour_is_read_when_rollup_has_not_caught_up(self):
        self.insert('2026-11-02T12:15:00Z', rollup=False)
        payload = self.query(bucket='hour', since='2026-11-02T12:00:00Z', until='2026-11-02T12:30:00Z')
        self.assertIsNotNone(payload['first_recorded'])
        self.assertEqual(payload['series'][0]['values'], [1])
        self.assertEqual(payload['series'][0]['total'], 1)
        self.assertEqual(payload['current'], 0)

    def test_partial_hours_current_raw_rollup_lag_and_boundary_exclusion(self):
        self.insert('2026-11-02T11:59:00Z')
        self.insert('2026-11-02T12:00:00Z')
        self.insert('2026-11-02T12:15:00Z', rollup=False)
        self.insert('2026-11-02T12:30:00Z', rollup=False)
        payload = self.query(bucket='hour', since='2026-11-02T11:59:30Z', until='2026-11-02T12:30:00Z')
        self.assertEqual(payload['series'][0]['total'], 2)
        self.assertEqual(payload['series'][0]['values'], [0, 2])
        self.assertEqual(payload['current'], 1)
        old = self.query(bucket='hour', since='2026-11-02T11:00:00Z', until='2026-11-02T12:00:00Z')
        self.assertIsNone(old['current'])
        self.assertEqual(old['series'][0]['total'], 1)

    def test_breakdown_failure_classification_top_other_and_zero_share(self):
        for n, kind in enumerate(('rate_limit', 'rate_limit', 'timeout', None, 'new-kind')):
            self.insert(f'2026-11-01T01:0{n}:00Z', failed=1, failure_kind=kind)
        self.insert('2026-11-01T01:30:00Z', failed=0)
        payload = self.query('breakdown', by='failure_reason', top=1)
        self.assertEqual(payload['total'], 5)
        self.assertEqual(payload['rows'][0]['value'], 2)
        self.assertEqual(payload['rows'][0]['share'], 40)
        self.assertEqual(payload['other'], dict(count=2, value=3, share=60))
        labels = {r['label'] for r in self.query('breakdown', by='failure_reason')['rows']}
        self.assertEqual(labels, {'Rate limited', 'Timed out', 'Reason unavailable'})
        zero = self.query('breakdown', metric='cached_tokens', provider='missing')
        self.assertEqual(zero['total'], 0)
        self.assertEqual(zero['rows'], [])
        for group in ('provider', 'account', 'model'):
            self.assertEqual(self.query('breakdown', by=group)['total'], 6)

    def archive(self, boundary):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE usage_retention (id INTEGER PRIMARY KEY, raw_before TEXT, pruned_rows INTEGER)')
            con.execute('INSERT INTO usage_retention VALUES (1,?,1)', (boundary,))
            con.execute('DELETE FROM usage WHERE request_time < ?', (app.timestamp_key(boundary),))

    def test_archive_rollups_unknown_failures_minute_clamp_and_calendar(self):
        self.insert('2026-10-01T00:15:00Z', failed=1, failure_kind='timeout')
        self.insert('2026-11-01T00:15:00Z', failed=1, failure_kind='rate_limit')
        self.archive('2026-11-01T00:00:00Z')
        payload = self.query(bucket='day', since='2026-10-01T00:30:00Z')
        self.assertEqual(payload['since'], '2026-10-01T00:00:00Z')
        self.assertEqual(payload['series'][0]['total'], 2)
        self.assertEqual(payload['archived_before'], '2026-11-01T00:00:00Z')
        failure = self.query('breakdown', by='failure_reason', since='2026-10-01T00:00:00Z')
        self.assertEqual({r['key']: r['value'] for r in failure['rows']}, {'failure_reason:unknown': 1, 'failure_reason:rate_limit': 1})
        minute = self.query(bucket='minute', since='2026-10-31T23:00:00Z', until='2026-11-01T01:00:00Z')
        self.assertEqual(minute['since'], '2026-11-01T00:00:00Z')
        self.assertEqual(minute['series'][0]['total'], 1)
        calendar = self.query('heatmap', mode='calendar', since='2026-09-30T00:00:00Z')
        self.assertEqual(calendar['days'][0]['value'], None)
        self.assertEqual(calendar['days'][1]['value'], 1)
        tokens = self.query('breakdown', by='failure_reason', metric='tokens', since='2026-10-01T00:00:00Z')
        self.assertEqual(tokens['since'], '2026-11-01T00:00:00Z')
        self.assertEqual(tokens['total'], 120)

    def test_heatmap_averages_include_recorded_zero_days_and_dst_day(self):
        self.insert('2026-10-25T05:30:00Z')  # Sunday 1:30 EDT
        self.insert('2026-11-01T05:30:00Z')  # Sunday 1:30 EDT
        self.insert('2026-11-01T06:30:00Z')  # Sunday 1:30 EST
        payload = self.query('heatmap', mode='weekhour', tz='America/New_York', since='2026-10-24T00:00:00-04:00', until='2026-11-02T00:00:00-05:00')
        self.assertEqual(payload['cells'][6][1], 1.5)
        self.assertEqual(payload['cells'][0][1], 0)
        self.assertEqual(payload['max'], 1.5)
        calendar = self.query('heatmap', mode='calendar', tz='America/New_York', since='2026-10-24T00:00:00-04:00', until='2026-11-02T00:00:00-05:00')
        self.assertIsNone(calendar['days'][0]['value'])
        self.assertEqual(calendar['days'][-1], dict(date='2026-11-01', value=2))

    def test_parameter_errors_http_auth_and_sqlite_unavailable(self):
        base = dict(since='2026-11-01T00:00:00Z', until='2026-11-02T00:00:00Z')
        invalid = [dict(metric='bad'), dict(group='bad'), dict(group='outcome', metric='tokens'), dict(bucket='month'),
                   dict(tz='Not/AZone'), dict(tz='x' * 300), dict(compare=2), dict(provider='A!'), dict(account='bad.thing'),
                   dict(since='2026-11-01'), dict(until='invalid'), dict(since=base['until']), dict(until=base['since']),
                   dict(since=['2026-11-01T00:00:00Z'] * 2), dict(bucket='minute'), dict(extra='x'),
                   dict(bucket='hour', since='2025-01-01T00:00:00Z')]
        for values in invalid:
            with self.subTest(values=values):
                self.assertEqual(self.get(params=base | values)[0], 400)
        for endpoint, values in [('breakdown', dict(by='bad')), ('breakdown', dict(top='21')),
                                 ('breakdown', dict(top='0')), ('breakdown', dict(metric='success_rate')),
                                 ('heatmap', dict(mode='bad')), ('heatmap', dict(metric='avg_duration')),
                                 ('retry', dict(tz='UTC'))]:
            self.assertEqual(self.get(endpoint, base | values)[0], 400)
        self.assertEqual(self.get(params=base, headers={'X-Router-Request': '0'})[0], 403)
        self.db.unlink()
        self.assertEqual(self.get(params=base)[0], 503)

    def test_failed_detail_queries_keep_indexed_range_seeks(self):
        self.insert('2026-11-01T01:00:00Z', failed=1, failure_kind='timeout')
        with contextlib.closing(sqlite3.connect(f'file:{self.db}?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            history = series.History(con, self.gateway, HELPERS, NOW)
            statements = []
            con.set_trace_callback(statements.append)
            history.raw(series.date('2026-11-01T00:00:00Z'), series.date('2026-11-02T00:00:00Z'), failures=True)
            con.set_trace_callback(None)
            for statement in statements:
                plan = [row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + statement)]
                self.assertTrue(any('SEARCH usage USING INDEX usage_failed_by_request_time' in row for row in plan), plan)
                self.assertFalse(any('SCAN usage' in row for row in plan), plan)

    def test_missing_archive_rollup_returns_503(self):
        self.insert('2026-10-01T00:00:00Z')
        self.archive('2026-11-01T00:00:00Z')
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('DROP TABLE usage_hourly')
        self.assertEqual(self.get(params=dict(since='2026-10-01T00:00:00Z'))[0], 503)

    def test_bucket_count_limit_and_empty_database(self):
        self.assertEqual(self.get()[0], 200)
        self.insert('2020-01-01T00:00:00Z')
        self.assertEqual(self.get(params=dict(since='2020-01-01T00:00:00Z', bucket='day'))[0], 400)

    def test_retry_reuses_summary_builder_with_upper_bound(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO usage_journal_state VALUES (1,'cursor','2026-10-01','2026-11-03','2026-11-03')")
            for i, stamp in enumerate(('2026-11-01T01:00:00Z', '2026-11-02T01:00:00Z')):
                con.execute('INSERT INTO usage_retry_events VALUES (?, ?, ?, ?, ?, ?, ?)', (i*2+1, f'c{i}a', 'inv', f'req{i}', stamp, 'upstream_429', 429))
                con.execute('INSERT INTO usage_retry_events VALUES (?, ?, ?, ?, ?, ?, ?)', (i*2+2, f'c{i}b', 'inv', f'req{i}', stamp, 'response', 200))
        result = self.query('retry')
        self.assertEqual(result['recovered_requests'], 1)
        self.assertEqual(result['recovered_upstream_429s'], 1)
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(set(result), set(app.usage_retry_recovery(con, '2026-11-01')))
