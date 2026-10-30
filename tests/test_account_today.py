"""Generic account cards share the usage report's local calendar day."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import providers
import server as app


class FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 30, 12)
        return value if tz is None else value.astimezone(tz)


class AccountTodayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'usage.sqlite3'
        previous_tz = os.environ.get('TZ')

        def restore_timezone():
            if previous_tz is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = previous_tz
            time.tzset()

        self.addCleanup(restore_timezone)
        os.environ['TZ'] = 'America/Santiago'
        time.tzset()
        clock = patch.object(app.dt, 'datetime', FrozenDatetime)
        clock.start()
        self.addCleanup(clock.stop)
        self.gateway = {'inventory': [
            dict(type='codex', name='codex-file.json', auth_index='shared', email='same@example.test'),
            dict(type='xai', name='xai-file.json', auth_index='shared', email='same@example.test'),
            dict(type='xai', name='unused-file.json', auth_index='unused'),
        ], 'accounts': [dict(key='codex-file.json', id='different-display-id')]}

    def create_db(self, legacy=False):
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY,
                collected_at TEXT, timestamp TEXT, model TEXT, auth_index TEXT,
                failed INTEGER, latency_ms REAL, input_tokens INTEGER,
                output_tokens INTEGER, cached_tokens INTEGER, total_tokens INTEGER'''
                        + (')' if legacy else ', provider TEXT)'))

    def insert(self, rows):
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.executemany('INSERT INTO usage (provider, auth_index, timestamp, failed) VALUES (?,?,?,?)', rows)

    def overview(self, since='1970-01-01T00:00:00Z'):
        handler = object.__new__(app.Handler)
        handler.path = '/api/overview?since=' + since
        handler.headers = {}
        handler.allowed = Mock(return_value=True)
        handler.send = Mock()
        handler.server = Mock()
        handler.server.state.snapshot.return_value = self.gateway
        handler.server.state.fast_modes = None
        handler.server.state.notices.view.return_value = []
        with patch.object(app, 'DB', self.db), patch.object(app, 'health', return_value={}), \
                patch.object(app, 'watch_accounts', return_value=[]):
            handler.do_GET()
        status, body = handler.send.call_args.args
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_overview_today_matches_auth_index_and_provider_with_local_hours(self):
        self.create_db()
        self.insert([
            ('codex', 'shared', '2026-10-29T23:59:59-03:00', 1),
            ('codex', 'shared', '2026-10-30T00:00:00-03:00', 0),
            ('codex', 'shared', '2026-10-30T23:15:00.125-03:00', 1),
            ('codex', 'shared', '2026-10-31T00:00:00-03:00', 1),
            ('xai', 'shared', '2026-10-30T04:30:00Z', 0),
            # Names and emails are not usage auth indexes.
            ('xai', 'unused-file.json', '2026-10-30T08:00:00Z', 0),
            ('xai', 'same@example.test', '2026-10-30T08:00:00Z', 0),
        ])
        # A selected usage range must not change the basic card's day.
        report = self.overview('2026-11-01T00:00:00Z')
        groups = {group['id']: group for group in report['providers']}
        codex = groups['codex']['accounts'][0]
        self.assertEqual(codex['today'], dict(requests=2, success_rate=0.5,
            hours=[1] + [0] * 22 + [1],
            last_request_at=FrozenDatetime.fromisoformat('2026-10-31T02:15:00.125+00:00').timestamp()))
        self.assertEqual((codex['requests'], codex['failures']), (2, 1))
        xai, unused = groups['xai']['accounts']
        self.assertEqual(xai['today'], dict(requests=1, success_rate=1.0,
            hours=[0, 1] + [0] * 22,
            last_request_at=FrozenDatetime.fromisoformat('2026-10-30T04:30:00+00:00').timestamp()))
        self.assertEqual((xai['requests'], xai['failures']), (1, 0))
        self.assertEqual(unused['today'], dict(requests=0, success_rate=None,
                                             hours=[0] * 24, last_request_at=None))
        for provider, group in groups.items():
            summaries = {row['auth_index']: row for row in report['usage']['providers'][provider]['today_accounts']}
            for account in group['accounts']:
                self.assertEqual(len(account['today']['hours']), 24)
                self.assertTrue(all(type(n) is int for n in account['today']['hours']))
                self.assertEqual(sum(account['today']['hours']), account['requests'])
                self.assertEqual(account['requests'], summaries.get(account['auth_index'], {}).get('requests', 0))
            self.assertEqual(report['usage']['providers'][provider]['accounts'], [])

    def test_today_uses_one_grouped_query_for_all_accounts_and_providers(self):
        self.create_db()
        self.insert([(provider, 'shared', '2026-10-30T12:00:00Z', 0) for provider in ('codex', 'xai')])
        statements = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            con = connect(*args, **kwargs)
            con.set_trace_callback(statements.append)
            return con

        with patch.object(app.sqlite3, 'connect', side_effect=traced):
            self.overview()
        grouped_today = [sql for sql in statements if "'localtime'" in sql and 'GROUP BY' in sql]
        self.assertEqual(len(grouped_today), 1)

    def test_missing_empty_and_unavailable_history_have_the_zero_contract(self):
        for state in ('missing', 'empty', 'unavailable'):
            with self.subTest(state=state):
                if state == 'empty':
                    self.create_db()
                elif state == 'unavailable':
                    self.db.write_bytes(b'not a sqlite database')
                report = self.overview()
                for group in report['providers']:
                    for account in group['accounts']:
                        self.assertEqual(account['today'], dict(requests=0, success_rate=None,
                            hours=[0] * 24, last_request_at=None))
                        self.assertEqual((account['requests'], account['failures']), (0, 0))

    def test_legacy_or_empty_provider_rows_remain_codex_usage(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                self.create_db(legacy=legacy)
                with contextlib.closing(sqlite3.connect(self.db)) as con, con:
                    con.execute('''INSERT INTO usage (timestamp, auth_index, failed)
                        VALUES ('2026-10-30T03:00:00Z', 'shared', 1)''')
                    if not legacy:
                        con.execute("UPDATE usage SET provider = ''")
                accounts = self.overview()['providers']
                self.assertEqual(accounts[0]['accounts'][0]['today']['requests'], 1)
                self.assertEqual(accounts[0]['accounts'][0]['today']['success_rate'], 0.0)
                self.assertEqual(accounts[1]['accounts'][0]['today']['requests'], 0)
                self.db.unlink()

    def test_claude_watch_identity_does_not_replace_usage_identity(self):
        self.create_db()
        self.gateway['inventory'] = [dict(type='claude', name='claude-file.json', auth_index='gateway-index',
                                          email='owner@example.test')]
        self.insert([('claude', 'gateway-index', '2026-10-30T04:00:00Z', 0)])
        usage = app.usage_summary('1970-01-01T00:00:00Z', self.db)
        watched = [dict(id='watch-id', auth_index='watch-index', email='owner@example.test')]
        account = providers.groups(self.gateway, watched, usage)[0]['accounts'][0]
        self.assertEqual(account['today']['requests'], 1)
        self.assertEqual(account['today']['hours'], [0, 1] + [0] * 22)


if __name__ == '__main__':
    unittest.main()
