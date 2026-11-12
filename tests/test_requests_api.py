"""Request history uses temporary SQLite state and synthetic account metadata."""
import base64
import contextlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from test_dashboard import app

spec = importlib.util.spec_from_file_location(
    'requests_collector', Path(__file__).resolve().parents[1] / 'scripts/collect-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

START = '1970-01-01T00:00:00Z'
FIELDS = {'id', 'timestamp', 'auth_index', 'model', 'failed', 'latency_ms', 'total_tokens', 'provider',
          'reasoning_effort', 'service_tier', 'response_service_tier', 'cache_read_tokens',
          'cache_creation_tokens', 'key', 'email', 'fast_mode_allowed', 'fast_mode_level', 'forwarded_service_tier', 'failure'}


class RequestsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'usage.sqlite3'
        with contextlib.closing(collector.connect(self.db)):
            pass
        self.gateway = {'inventory': [dict(type='codex', auth_index='shared', name='codex.json',
                                          email='codex@example.test')],
                        'accounts': [dict(id='shared', key='fallback.json', email='fallback@example.test')],
                        'claude_accounts': [dict(auth_index='shared', key='claude.json', email='claude@example.test')],
                        'api_key_accounts': []}

    def insert(self, stamps, providers=None, failures=None):
        providers = providers or ['codex'] * len(stamps)
        failures = failures if failures is not None else [0] * len(stamps)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            for n, (stamp, provider, failed) in enumerate(zip(stamps, providers, failures)):
                con.execute('''INSERT INTO usage (collected_at,timestamp,request_time,provider,model,
                    auth_index,failed,latency_ms,total_tokens,service_tier,response_service_tier,
                    reasoning_effort,cache_read_tokens,cache_creation_tokens)
                    VALUES (?,?,?,?,?,'shared',?,125,42,'priority','normal','high',11,7)''',
                            (stamp, stamp, app.timestamp_key(stamp), provider, str(n), failed))

    def get(self, params=None, headers=None, path=None):
        handler = object.__new__(app.Handler)
        handler.path = path or '/api/requests' + ('?' + urlencode(params) if params else '')
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1'} | (headers or {})
        handler.send = Mock()
        state = SimpleNamespace(request_history_snapshot=Mock(return_value=self.gateway), fast_modes=None)
        handler.server = SimpleNamespace(state=state)
        with patch.object(app, 'DB', self.db):
            handler.do_GET()
        status, body = handler.send.call_args.args
        return status, json.loads(body)

    def all_pages(self, params=None):
        params = dict(params or {})
        rows, cursors = [], set()
        while True:
            status, page = self.get(params)
            self.assertEqual(status, 200)
            self.assertEqual(set(page), {'rows', 'next', 'archived_before'})
            rows.extend(page['rows'])
            if page['next'] is None:
                return rows
            self.assertNotIn(page['next'], cursors)
            cursors.add(page['next'])
            params['before'] = page['next']

    def test_account_filter_combines_provider_failed_kind_until_and_paging(self):
        self.insert(['2026-11-01T01:00:00Z', '2026-11-01T02:00:00Z', '2026-11-01T03:00:00Z'],
                    providers=['codex', 'claude', 'codex'], failures=[1, 1, 0])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE usage SET auth_index='different' WHERE model='2'")
        params = dict(account='shared', provider='codex', failed='1', kind='unknown', until='2026-11-01T02:30:00Z')
        self.assertEqual([r['model'] for r in self.all_pages(params | {'limit': 1})], ['0'])
        self.assertEqual(len(self.all_pages({'account': 'shared', 'limit': 1})), 2)
        self.assertEqual(len(self.all_pages({'account': 'different'})), 1)
        self.assertEqual(self.get({'account': 'missing'})[1]['rows'], [])
        for account in ('', 'has space', 'bad.', 'x' * 256):
            self.assertEqual(self.get({'account': account}), (400, {'error': 'Invalid account'}))
        self.assertEqual(self.get(path='/api/requests?account=a&account=b'), (400, {'error': 'Invalid account'}))

    def test_pagination_preserves_nanoseconds_offsets_ties_and_has_no_gaps(self):
        stamps = ['2026-10-03T03:49:40.216142001Z',
                  '2026-10-03T05:49:40.216142009+02:00',
                  '2026-10-03T00:49:40.216142008-03:00',
                  '2026-10-03T03:49:40.216142009Z',
                  '2026-10-03T03:49:40.216142010Z',
                  '2026-10-03T03:49:41Z',
                  '2026-10-03T03:49:40.216142009z']
        self.insert(stamps)
        expected = sorted(range(len(stamps)), key=lambda n: (app.timestamp_key(stamps[n]), n), reverse=True)
        for limit in (1, 2, 3, 50):
            with self.subTest(limit=limit):
                rows = self.all_pages({'limit': limit})
                self.assertEqual([r['model'] for r in rows], list(map(str, expected)))
        self.assertTrue(all(set(r) == FIELDS for r in rows))

    def test_http_failure_reason_is_safe_and_uses_the_existing_failed_filter(self):
        spool = self.db.with_name('failure.json')
        spool.write_text(json.dumps([
            dict(timestamp='2026-10-03T03:00:00Z', failed=True,
                 fail={'status_code':200, 'body':'Selected model is at capacity; PRIVATE'}),
            dict(timestamp='2026-10-03T03:00:01Z', failed=False)]))
        with contextlib.closing(collector.connect(self.db)) as con:
            collector.replay_spool(con, spool)
        status, payload = self.get({'failed':'1'})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload['rows']), 1)
        self.assertEqual(payload['rows'][0]['failure'], dict(kind='capacity', label='Model at capacity',
                         code=None, status_code=200, source='usage_queue'))
        self.assertNotIn('PRIVATE', json.dumps(payload))
    def test_archive_boundary_clamps_pages_and_remains_on_empty_filtered_pages(self):
        boundary = '2026-10-03T00:00:00Z'
        # Include surviving correction/late rows before the boundary: clamping
        # must exclude older detail even when such rows remain in the database.
        self.insert(['2026-10-02T23:00:00Z', boundary, '2026-10-03T01:00:00Z'],
                    ['claude', 'codex', 'claude'], [1, 0, 1])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE usage_retention (id INTEGER PRIMARY KEY, raw_before TEXT, pruned_rows INTEGER)')
            con.execute('INSERT INTO usage_retention VALUES (1, ?, 1)', (boundary,))
        status, first = self.get({'limit': 1, 'since': '1970-01-01T05:30:00+05:30'})
        self.assertEqual(status, 200)
        self.assertEqual(first['archived_before'], boundary)
        self.assertEqual([r['model'] for r in first['rows']], ['2'])
        status, last = self.get({'limit': 1, 'before': first['next']})
        self.assertEqual(status, 200)
        self.assertEqual([r['model'] for r in last['rows']], ['1'])
        self.assertEqual(last['archived_before'], boundary)
        self.assertIsNone(last['next'])
        for params in ({'provider': 'unknown'}, {'until': '2026-10-03T05:29:59+05:30'},
                       {'since': boundary, 'until': '2026-10-02T23:00:00Z'},
                       {'before': app.request_cursor(app.timestamp_key(boundary), 1)}):
            with self.subTest(params=params):
                self.assertEqual(self.get(params),
                                 (200, dict(rows=[], next=None, archived_before=boundary)))
        self.assertEqual([r['model'] for r in self.all_pages({'provider': 'claude', 'failed': '1'})], ['2'])
        self.assertEqual([r['model'] for r in self.all_pages({'since': '2026-10-03T00:30:00Z'})], ['2'])

    def test_archive_boundary_is_null_until_rows_are_actually_pruned(self):
        self.assertEqual(self.get()[1]['archived_before'], None)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('CREATE TABLE usage_retention (id INTEGER PRIMARY KEY, raw_before TEXT, pruned_rows INTEGER)')
            con.execute("INSERT INTO usage_retention VALUES (1, '2026-10-03T00:00:00Z', 0)")
        self.assertEqual(self.get()[1]['archived_before'], None)

    def test_cursor_round_trip_and_boundary_survives_newer_insert(self):
        self.insert(['2026-10-03T03:00:00Z'] * 5)
        _, first = self.get({'limit': 2})
        key, row_id = app.parse_request_cursor(first['next'])
        self.assertEqual((key, row_id), ('2026-10-03T03:00:00.000000000Z', 4))
        self.assertEqual(app.request_cursor(key, row_id), first['next'])
        self.insert(['2026-10-04T00:00:00Z'])
        rest = self.all_pages({'limit': 2, 'before': first['next']})
        self.assertEqual([r['model'] for r in first['rows'] + rest], ['4', '3', '2', '1', '0'])

    def test_kind_filter_accepts_all_reasons_and_implies_failed_only(self):
        kinds = list(app.USAGE_FAILURE_LABELS)
        self.insert(['2026-10-03T03:00:00Z'] * (len(kinds) + 1),
                    failures=[1] * len(kinds) + [0])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.executemany('UPDATE usage SET failure_kind=? WHERE id=?',
                            [(kind, n + 1) for n, kind in enumerate(kinds)] + [('capacity', len(kinds) + 1)])
        for n, kind in enumerate(kinds):
            for failed in (False, True):
                with self.subTest(kind=kind, failed=failed):
                    params = {'kind': kind} | ({'failed': '1'} if failed else {})
                    rows = self.all_pages(params)
                    self.assertEqual([r['model'] for r in rows], [str(n)])
                    self.assertEqual(rows[0]['failure']['kind'], kind)

    def test_kind_combines_with_provider_groups_dates_and_cursor(self):
        self.gateway['api_key_accounts'] = [dict(provider='codex-api-key', auth_index='key-a')]
        self.insert(['2026-10-03T03:00:00Z'] * 2 + ['2026-10-03T03:00:01Z'] * 10
                    + ['2026-10-03T03:00:02Z'] * 2,
                    ['codex', 'claude'] * 7, [1] * 12 + [0, 1])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE usage SET failure_kind='capacity'")
            con.execute("UPDATE usage SET failure_kind='timeout' WHERE id=7")
            con.execute("UPDATE usage SET auth_index='key-a' WHERE id IN (3,5,9,11)")
        params = {'kind': 'capacity', 'since': '2026-10-03T03:00:01Z',
                  'until': '2026-10-03T03:00:01Z', 'limit': 1}
        for provider, expected in [(None, ['11', '10', '9', '8', '7', '5', '4', '3', '2']),
                                   ('codex', []), ('codex-api-key', ['10', '8', '4', '2']),
                                   ('claude', ['11', '9', '7', '5', '3'])]:
            with self.subTest(provider=provider):
                filters = params | ({'provider': provider} if provider else {})
                self.assertEqual([r['model'] for r in self.all_pages(filters)], expected)

    def test_unknown_kind_matches_null_and_unmigrated_rows(self):
        self.insert(['2026-10-03T03:00:00Z'] * 4, failures=[1, 1, 1, 0])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE usage SET failure_kind='unknown' WHERE id=2")
            con.execute("UPDATE usage SET failure_kind='capacity' WHERE id=3")
        self.assertEqual([r['model'] for r in self.all_pages({'kind': 'unknown', 'limit': 1})], ['1', '0'])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('ALTER TABLE usage DROP COLUMN failure_kind')
        self.assertEqual([r['model'] for r in self.all_pages({'kind': 'unknown', 'limit': 1})], ['2', '1', '0'])
        self.assertEqual(self.get({'kind': 'capacity'})[1], {'rows': [], 'next': None, 'archived_before': None})
        # The oldest schema also has neither exact time keys nor a provider column.
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            for index in ('usage_by_request_time', 'usage_by_provider_request_time',
                          'usage_failed_by_request_time', 'usage_failed_by_provider_request_time'):
                con.execute('DROP INDEX ' + index)
            con.execute('ALTER TABLE usage DROP COLUMN request_time')
            con.execute('ALTER TABLE usage DROP COLUMN provider')
        self.assertEqual([r['model'] for r in self.all_pages({'kind': 'unknown', 'provider': 'codex', 'limit': 1})],
                         ['2', '1', '0'])
        self.assertEqual(self.get({'kind': 'rate_limit'})[1], {'rows': [], 'next': None, 'archived_before': None})

    def test_provider_filter_uses_default_codex_and_unknown_is_empty(self):
        self.insert(['2026-10-03T03:00:00Z'] * 7,
                    ['codex', None, '', 'claude', 'claude', 'xai', 'other-provider'])
        for provider, models in [('codex', ['2', '1', '0']), ('claude', ['4', '3']),
                                 ('xai', ['5']), ('other-provider', ['6']), ('unknown', [])]:
            rows = self.all_pages({'provider': provider, 'limit': 1})
            self.assertEqual([r['model'] for r in rows], models)
            self.assertTrue(all(r['provider'] == provider for r in rows))

    def test_provider_groups_filter_by_upstream_channel_and_auth_index(self):
        self.gateway['api_key_accounts'] = [
            dict(provider='codex-api-key', auth_index='key-a'),
            dict(provider='codex-api-key', auth_index='key-b'),
            dict(provider='claude-api-key', auth_index='claude-key'),
            dict(provider='openai-compatibility', auth_index='compat-a', label=' First '),
            dict(provider='openai-compatibility', auth_index='compat-b', label='openai-compatible-second'),
            dict(provider='openai-compatibility', auth_index=None, label='unindexed')]
        identities = [('codex', 'shared'), ('codex', 'key-a'), ('codex', 'key-b'),
                      ('codex', 'removed'), ('claude', 'key-a'), ('claude', 'claude-key'),
                      ('openai-compatible-first', 'compat-a'), ('openai-compatible-second', 'compat-b'),
                      ('openai-compatible-first', 'compat-b'), ('retired-provider', 'key-a'), ('codex', None)]
        self.insert(['2026-10-03T03:00:00Z'] * len(identities), [p for p, _ in identities])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.executemany('UPDATE usage SET auth_index=? WHERE model=?',
                            [(auth_index, str(n)) for n, (_, auth_index) in enumerate(identities)])
            con.execute('UPDATE usage SET failed = (CAST(model AS INTEGER) % 2 = 0)')
        for provider, models in [('codex-api-key', ['2', '1']), ('codex', ['10', '3', '0']),
                                 ('claude-api-key', ['5']), ('claude', ['4']),
                                 ('openai-compatibility', ['7', '6']), ('retired-provider', ['9']),
                                 ('openai-compatible-first', ['8', '6'])]:
            with self.subTest(provider=provider):
                rows = self.all_pages({'provider': provider, 'limit': 1})
                self.assertEqual([r['model'] for r in rows], models)
                filtered = self.all_pages({'provider': provider, 'limit': 1, 'failed': '1',
                                           'since': '2026-10-03T03:00:00Z',
                                           'until': '2026-10-03T03:00:00Z'})
                self.assertEqual([r['model'] for r in filtered], [m for m in models if int(m) % 2 == 0])
        self.assertEqual(len(self.all_pages()), len(identities))
        # Handler snapshots must carry a detached copy of the group membership.
        state = object.__new__(app.GatewayState)
        state.lock, state.data = threading.Lock(), self.gateway
        snapshot = state.request_history_snapshot()
        self.assertEqual(snapshot['api_key_accounts'], self.gateway['api_key_accounts'])
        snapshot['api_key_accounts'][0]['auth_index'] = 'modified'
        self.assertEqual(self.gateway['api_key_accounts'][0]['auth_index'], 'key-a')

    def test_since_filter_matches_overview_sql_rounding_and_offsets(self):
        stamps = ['2026-10-02T23:59:59-03:00', '2026-10-03T03:00:00Z',
                  '2026-10-03T05:00:00.000000009+02:00', '2026-10-03T03:00:01Z']
        self.insert(stamps)
        for start in ('2026-10-03T03:00:00Z', '2026-10-03 00:00:00-03:00',
                      '2026-10-03T03:00:00.000000010Z', '2027-01-01T00:00:00Z'):
            with contextlib.closing(sqlite3.connect(self.db)) as con:
                expected = {r[0] for r in con.execute(
                    'SELECT model FROM usage WHERE julianday(timestamp) >= julianday(?)', (start,))}
            rows = self.all_pages({'since': start, 'limit': 1})
            self.assertEqual({r['model'] for r in rows}, expected)

    def test_failed_filter_uses_truthy_values_and_preserves_unfiltered_rows(self):
        self.insert(['2026-10-03T03:00:00Z'] * 7, failures=[0, 1, None, 2, -1, 0, 1])
        rows = self.all_pages({'failed': '1', 'limit': 1})
        self.assertEqual([r['model'] for r in rows], ['6', '4', '3', '1'])
        self.assertTrue(all(r['failed'] for r in rows))
        self.assertEqual(len(self.all_pages()), 7)

    def test_until_includes_exact_nanosecond_boundary_and_timezone_offsets(self):
        self.insert(['2026-10-03T03:00:00.123456788Z',
                     '2026-10-03T00:00:00.123456789-03:00',
                     '2026-10-03T05:00:00.123456789+02:00',
                     '2026-10-03T03:00:00.123456790Z'])
        for until in ('2026-10-03T03:00:00.123456789Z',
                      '2026-10-03 00:00:00.123456789-03:00',
                      '2026-10-03t05:00:00.123456789+02:00',
                      '20261003T050000.123456789+0200',
                      '2026-10-03T05:00:00,123456789+02',
                      '2026-10-03T03:00:00.123456789z'):
            with self.subTest(until=until):
                rows = self.all_pages({'until': until, 'limit': 1})
                self.assertEqual([r['model'] for r in rows], ['2', '1', '0'])
        self.assertEqual(self.get({'since': '2026-10-03T03:00:00.123456790Z',
                                   'until': '2026-10-03T03:00:00.123456789Z'})[1],
                         {'rows': [], 'next': None, 'archived_before': None})
        self.assertEqual(self.get({'since': '20261003T030000.123456790+0000',
                                   'until': '2026-10-03T03:00:00.123456789Z'})[1],
                         {'rows': [], 'next': None, 'archived_before': None})

    def test_filter_combinations_paginate_without_duplicates_or_gaps(self):
        stamps = ['2026-10-03T03:00:00Z', '2026-10-03T03:00:01Z',
                  '2026-10-03T00:00:01.000000001-03:00',
                  '2026-10-03T05:00:01.000000001+02:00',
                  '2026-10-03T03:00:02Z', '2026-10-03T03:00:03Z'] * 4
        providers = ['codex', 'claude', None, '', 'claude', 'codex'] * 4
        failures = [1, 0, 2, None, -1, 1] * 4
        self.insert(stamps, providers, failures)
        until = '2026-10-03T03:00:02Z'
        for failed in (False, True):
            for provider in (None, 'codex', 'claude'):
                for upper in (None, until):
                    for since in (START, '2026-10-03T03:00:01Z'):
                        params = {'since': since}
                        if failed:
                            params['failed'] = '1'
                        if provider:
                            params['provider'] = provider
                        if upper:
                            params['until'] = upper
                        expected = [n for n in range(len(stamps))
                                    if app.timestamp_key(stamps[n]) >= app.timestamp_key(since)
                                    and (not upper or app.timestamp_key(stamps[n]) <= app.timestamp_key(upper))
                                    and (not provider or (providers[n] or 'codex') == provider)
                                    and (not failed or failures[n])]
                        expected.sort(key=lambda n: (app.timestamp_key(stamps[n]), n), reverse=True)
                        for limit in (1, 3, 7):
                            with self.subTest(params=params, limit=limit):
                                rows = self.all_pages(params | {'limit': limit})
                                self.assertEqual([r['model'] for r in rows], list(map(str, expected)))

    def test_until_and_before_both_apply_even_with_cursor_outside_upper_bound(self):
        self.insert([f'2026-10-03T03:00:0{n}Z' for n in range(6)])
        for cursor_second, expected in [(5, ['3', '2', '1', '0']), (2, ['1', '0'])]:
            before = app.request_cursor(f'2026-10-03T03:00:0{cursor_second}.000000000Z', cursor_second + 1)
            rows = self.all_pages({'until': '2026-10-03T03:00:03Z', 'before': before, 'limit': 1})
            self.assertEqual([r['model'] for r in rows], expected)

    def test_limit_defaults_clamps_and_last_page_has_null_cursor(self):
        self.insert(['2026-10-03T03:00:00Z'] * 240)
        for params, count in [({}, 50), ({'limit': 0}, 1), ({'limit': -20}, 1),
                              ({'limit': 1}, 1), ({'limit': 99999}, 200)]:
            status, page = self.get(params)
            self.assertEqual(status, 200)
            self.assertEqual(len(page['rows']), count)
            self.assertIsInstance(page['next'], str)
        _, page = self.get({'limit': 200})
        _, last = self.get({'limit': 200, 'before': page['next']})
        self.assertEqual(len(last['rows']), 40)
        self.assertIsNone(last['next'])

    def test_invalid_dates_providers_and_limits(self):
        for name, values, error in [
                ('since', ['', 'bad', '2026-10-03T00:00:00', '2026-02-30T00:00:00Z'], 'Invalid date'),
                ('until', ['', 'bad', '2026-10-03T00:00:00', '2026-02-30T00:00:00Z',
                           '2026-10-03T00:00:00Z\n', '2026-10-03T00:00:00.1234567890Z',
                           '0001-01-01T00:00:00+03:00', '9999-12-31T23:59:59-03:00'], 'Invalid date'),
                ('failed', ['', '0', 'true', 'False', '-1', '01', ' 1 ', '1\n'], 'Invalid failed'),
                ('kind', ['', 'bogus', 'Capacity', 'capacity,auth', ' capacity', 'auth\n'], 'Invalid kind'),
                ('provider', ['', 'Codex', 'has space', '../codex', 'a' * 41, 'codex\n'], 'Invalid provider'),
                ('limit', ['', '1.0', 'abc', '1e2', ' 2 ', '2\n'], 'Invalid limit')]:
            for value in values:
                with self.subTest(name=name, value=value):
                    self.assertEqual(self.get({name: value}), (400, {'error': error}))
            self.assertEqual(self.get([(name, '1'), (name, '2')]), (400, {'error': error}))
        for name, value, error in [('failed', '1', 'Invalid failed'),
                                   ('kind', 'capacity', 'Invalid kind'),
                                   ('until', '2026-10-03T00:00:00Z', 'Invalid date')]:
            self.assertEqual(self.get([(name, value), (name, value)]), (400, {'error': error}))

    def test_malformed_cursors_are_400(self):
        key = '2026-10-03T03:00:00.000000000Z'
        invalid = ['', 'not-base64', 'a' * 201, '@', app.request_cursor(key, 1) + '=',
                   *[base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
                     for data in [None, {}, [1], [2, key, 1], [True, key, 1], [1, key, True],
                                  [1, key, 0], [1, key, 2**63], [1, 'bad', 1], [1, 5, 1]]]]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.get({'before': value}), (400, {'error': 'Invalid cursor'}))

    def test_request_guards_and_read_only_views(self):
        for headers, status in [({'Host': 'evil.test'}, 421), ({'X-Router-Request': '0'}, 403),
                                ({'Origin': 'http://evil.test'}, 403), ({'Sec-Fetch-Site': 'cross-site'}, 403),
                                ({'X-Router-Read-Only': '1'}, 200)]:
            self.assertEqual(self.get(headers=headers)[0], status)

    def test_identity_snapshot_is_detached_and_does_not_enrich_or_reconcile(self):
        state = object.__new__(app.GatewayState)
        state.lock = threading.Lock()
        state.data = self.gateway | {'unrelated': 'excluded'}
        state.snapshot = Mock(side_effect=AssertionError('full snapshot may reconcile settings'))
        snapshot = state.request_history_snapshot()
        self.assertEqual(snapshot, self.gateway)
        snapshot['inventory'][0]['name'] = 'modified'
        self.assertEqual(state.data['inventory'][0]['name'], 'codex.json')

    def test_database_unavailable_returns_safe_503_and_does_not_create_it(self):
        self.db.unlink()
        self.assertEqual(self.get(), (503, {'error': 'Usage history is temporarily unavailable'}))
        self.assertFalse(self.db.exists())
        self.db.write_bytes(b'not sqlite')
        self.assertEqual(self.get(), (503, {'error': 'Usage history is temporarily unavailable'}))

    def test_shared_decoration_matches_recent_with_fast_mode_and_cache_fields(self):
        self.insert(['2026-10-03T03:00:00Z'] * 18,
                    ['codex', 'claude', 'xai', None, '', 'codex'] * 3)
        fast = Mock()
        fast.request_view.return_value = dict(fast_mode_allowed=False, fast_mode_level='normal', forwarded_service_tier='normal')
        recent = app.usage_summary(START, self.db, fast, self.gateway)['recent']
        page = app.usage_requests(START, self.db, fast, self.gateway)
        self.assertEqual(page['rows'][:12], recent)
        self.assertEqual(len(recent), 12)
        for row in page['rows']:
            self.assertEqual(set(row), FIELDS)
            self.assertEqual((row['cache_read_tokens'], row['cache_creation_tokens']), (11, 7))
            if row['provider'] == 'codex':
                self.assertEqual((row['key'], row['email'], row['fast_mode_allowed']),
                                 ('codex.json', 'codex@example.test', False))
            elif row['provider'] == 'claude':
                self.assertEqual((row['key'], row['email']), ('claude.json', 'claude@example.test'))
                self.assertIsNone(row['fast_mode_allowed'])
            else:
                self.assertIsNone(row['key'])
                self.assertIsNone(row['email'])
                self.assertIsNone(row['forwarded_service_tier'])

    def test_recent_still_uses_only_last_200_candidates_and_twelve_rows(self):
        self.insert(['2026-10-04T00:00:00Z'] + ['2026-10-03T03:00:00Z'] * 230)
        recent = app.usage_summary(START, self.db)['recent']
        self.assertEqual([r['model'] for r in recent], list(map(str, range(230, 218, -1))))
        self.assertEqual(self.get({'limit': 1})[1]['rows'][0]['model'], '0')

    def test_old_schema_without_provider_or_optional_columns(self):
        self.db.unlink()
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,
                timestamp TEXT, auth_index TEXT, model TEXT, failed INTEGER, latency_ms REAL,
                total_tokens INTEGER, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER)''')
            con.executemany('INSERT INTO usage (timestamp,model) VALUES (?,?)',
                            [('2026-10-03T03:00:00Z', 'one'), ('invalid', 'invalid'),
                             ('2026-10-03T00:00:00-03:00', 'two')])
        rows = self.all_pages({'provider': 'codex', 'limit': 1})
        self.assertEqual([r['model'] for r in rows], ['two', 'one'])
        self.assertEqual(rows, app.usage_summary(START, self.db)['recent'])
        for row in rows:
            self.assertEqual(set(row), FIELDS)
            for field in ('reasoning_effort', 'service_tier', 'response_service_tier',
                          'cache_read_tokens', 'cache_creation_tokens'):
                self.assertIsNone(row[field])
        self.assertEqual(self.get({'provider': 'claude'})[1], {'rows': [], 'next': None, 'archived_before': None})

    def test_migration_backfills_exact_keys_and_replay_maintains_them(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute('DROP INDEX usage_by_request_time')
            con.execute('DROP INDEX usage_by_provider_request_time')
            con.execute('DROP INDEX usage_failed_by_request_time')
            con.execute('DROP INDEX usage_failed_by_provider_request_time')
            con.execute('ALTER TABLE usage DROP COLUMN request_time')
            con.executemany('INSERT INTO usage (collected_at,timestamp,model) VALUES (?,?,?)',
                            [('test', stamp, str(n)) for n, stamp in enumerate(
                                ['2026-10-03T00:00:00.123456789-03:00', 'invalid'])])
        for _ in range(2):
            with contextlib.closing(collector.connect(self.db)) as con:
                self.assertEqual(con.execute('SELECT request_time FROM usage ORDER BY id').fetchall(),
                                 [('2026-10-03T03:00:00.123456789Z',), (None,)])
        spool = self.db.with_name('synthetic-batch.json')
        spool.write_text(json.dumps([dict(timestamp='2026-10-03T05:00:00.123456790+02:00', model='replayed')]))
        with contextlib.closing(collector.connect(self.db)) as con:
            self.assertEqual(collector.replay_spool(con, spool), (1, 0, 0))
            self.assertEqual(con.execute("SELECT request_time FROM usage WHERE model='replayed'").fetchone(),
                             ('2026-10-03T03:00:00.123456790Z',))
        self.assertEqual([r['model'] for r in self.all_pages({'limit': 1})], ['replayed', '0'])

    def test_filtered_pages_remain_correct_before_index_and_key_migrations(self):
        self.insert(['2026-10-03T03:00:00.123456789Z'] * 8 + ['2026-10-03T03:00:01Z'],
                    ['codex', 'claude'] * 4 + ['codex'], [1, 1, 0, None, -1, 2, 1, 1, 1])
        for old_keys in (False, True):
            with contextlib.closing(sqlite3.connect(self.db)) as con, con:
                for name in ('usage_failed_by_request_time', 'usage_failed_by_provider_request_time'):
                    con.execute('DROP INDEX IF EXISTS ' + name)
                if old_keys:
                    con.execute('DROP INDEX usage_by_request_time')
                    con.execute('DROP INDEX usage_by_provider_request_time')
                    con.execute('ALTER TABLE usage DROP COLUMN request_time')
            for provider, expected in [(None, ['7', '6', '5', '4', '1', '0']),
                                       ('codex', ['6', '4', '0']), ('claude', ['7', '5', '1'])]:
                with self.subTest(old_keys=old_keys, provider=provider):
                    params = {'failed': '1', 'until': '2026-10-03T00:00:00.123456789-03:00', 'limit': 1}
                    if provider:
                        params['provider'] = provider
                    self.assertEqual([r['model'] for r in self.all_pages(params)], expected)

    def test_pages_use_read_only_connection_and_time_provider_indexes(self):
        self.insert(['2026-10-03T03:00:00Z'] * 10, ['codex', 'claude'] * 5, [1] * 10)
        original_connect = sqlite3.connect
        for provider, failed, until, index in [
                (None, False, None, 'usage_by_request_time'),
                ('claude', False, None, 'usage_by_provider_request_time'),
                (None, True, None, 'usage_failed_by_request_time'),
                ('claude', True, None, 'usage_failed_by_provider_request_time'),
                (None, False, '2026-10-03T03:00:00.000000000Z', 'usage_by_request_time'),
                (None, True, '2026-10-03T03:00:00.000000000Z', 'usage_failed_by_request_time'),
                ('claude', True, '2026-10-03T03:00:00.000000000Z', 'usage_failed_by_provider_request_time')]:
            statements = []

            def connect(*args, **kwargs):
                self.assertTrue(args[0].endswith('?mode=ro'))
                self.assertTrue(kwargs['uri'])
                con = original_connect(*args, **kwargs)
                con.set_trace_callback(statements.append)
                return con

            with patch.object(app.sqlite3, 'connect', side_effect=connect):
                page = app.usage_requests(START, self.db, provider=provider, failed=failed, until=until, limit=2)
                app.usage_requests(START, self.db, provider=provider, failed=failed, until=until, limit=2,
                                   before=app.parse_request_cursor(page['next']))
            queries = [s for s in statements if s.startswith('SELECT id,')]
            self.assertEqual(len(queries), 2)
            with contextlib.closing(original_connect(self.db)) as con:
                for query in queries:
                    plan = ' '.join(r[3] for r in con.execute('EXPLAIN QUERY PLAN ' + query))
                    self.assertIn('SEARCH usage USING INDEX ' + index, plan)
                    self.assertNotIn('TEMP B-TREE', plan)
                    self.assertTrue(query.endswith('LIMIT 3'))

    def test_kind_pages_keep_sparse_failed_index_for_each_cursor_seek(self):
        self.insert(['2026-10-03T03:00:00Z'] * 10, ['codex', 'claude'] * 5, [1] * 10)
        original_connect = sqlite3.connect
        for provider, index in [(None, 'usage_failed_by_request_time'),
                                 ('claude', 'usage_failed_by_provider_request_time')]:
            for kind in app.USAGE_FAILURE_LABELS:
                statements = []

                def connect(*args, **kwargs):
                    con = original_connect(*args, **kwargs)
                    con.set_trace_callback(statements.append)
                    return con

                with patch.object(app.sqlite3, 'connect', side_effect=connect):
                    for before in (None, ('2026-10-03T03:00:00.000000000Z', 8)):
                        app.usage_requests(START, self.db, provider=provider, kind=kind, limit=2,
                                           until='2026-10-03T03:00:00.000000000Z', before=before)
                queries = [s for s in statements if s.startswith('SELECT id,')]
                with self.subTest(provider=provider, kind=kind):
                    self.assertGreaterEqual(len(queries), 2)
                    with contextlib.closing(original_connect(self.db)) as con:
                        for query in queries:
                            plan = ' '.join(r[3] for r in con.execute('EXPLAIN QUERY PLAN ' + query))
                            self.assertIn('SEARCH usage USING INDEX ' + index, plan)
                            self.assertNotIn('TEMP B-TREE', plan)

    def test_deep_cursor_in_large_timestamp_tie_uses_bounded_sql_work(self):
        stamp = '2026-10-03T03:00:00Z'
        key = app.timestamp_key(stamp)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.executemany('INSERT INTO usage (collected_at,timestamp,request_time,provider,model,failed) '
                            "VALUES ('test',?,?,'codex',?,?)",
                            ((stamp, key, str(n), int(n % 1000 == 0)) for n in range(50000)))
        original_connect = sqlite3.connect
        steps = []

        def connect(*args, **kwargs):
            con = original_connect(*args, **kwargs)
            con.set_progress_handler(lambda: steps.append(1) or 0, 100)
            return con

        for provider in (None, 'codex'):
            steps.clear()
            with patch.object(app.sqlite3, 'connect', side_effect=connect):
                page = app.usage_requests(START, self.db, provider=provider, before=(key, 100), limit=50)
            self.assertEqual(len(page['rows']), 50)
            self.assertEqual(page['rows'][0]['model'], '98')
            self.assertLess(len(steps), 100, 'deep keyset pages must not rescan a large timestamp tie')
            for before in (None, (key, 25001)):
                steps.clear()
                with patch.object(app.sqlite3, 'connect', side_effect=connect):
                    page = app.usage_requests(START, self.db, provider=provider, failed=True,
                                              until=key, before=before, limit=20)
                self.assertEqual(len(page['rows']), 20)
                self.assertEqual(page['rows'][0]['model'], '49000' if before is None else '24000')
                self.assertLess(len(steps), 100, 'sparse failures must use bounded SQL work')


if __name__ == '__main__':
    unittest.main()
