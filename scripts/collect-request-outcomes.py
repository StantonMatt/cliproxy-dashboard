#!/usr/bin/env python3
"""Non-destructive journal evidence for 429s recovered by the gateway.

This reader never calls the gateway or joins usage rows by time. Journal bodies
are inspected in memory; only IDs, timestamps and numeric outcomes are stored.
Deploy separately with journal read access. No live installation is implicit.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import threading

spec = importlib.util.spec_from_file_location('usage_collector', Path(__file__).with_name('collect-usage.py'))
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

PREFIX = re.compile(r'^\[[^\]]+\] \[([0-9a-f]{8})\] \[[^\]]+\] \[([^\]]+\.go):\d+\]\s*(.*)$', re.DOTALL)
RESPONSE = re.compile(r'^([1-5]\d{2})\s*\|[^|]*\|[^|]*\|\s*(?:POST|GET)\s+"([^"?]+)(?:\?[^\"]*)?"')
ENDPOINTS = {'/v1/responses', '/v1/messages', '/v1/chat/completions', '/v1/completions'}
GATEWAY_UNIT = 'cliproxyapi.service'


def journal_position(entry):
    cursor = entry.get('__CURSOR')
    stamp = entry.get('__REALTIME_TIMESTAMP')
    if (not isinstance(cursor, str) or not re.fullmatch(r'[A-Za-z0-9=;_-]{1,1024}', cursor)
            or not isinstance(stamp, str) or not re.fullmatch(r'\d{1,18}', stamp)):
        raise ValueError('Invalid journal position')
    timestamp = dt.datetime.fromtimestamp(int(stamp) // 1000000, dt.timezone.utc).replace(
        microsecond=int(stamp) % 1000000).isoformat()
    return cursor, timestamp


def reduce_entry(entry):
    cursor, timestamp = journal_position(entry)
    if entry.get('_SYSTEMD_UNIT') != GATEWAY_UNIT:
        return None
    invocation = entry.get('_SYSTEMD_INVOCATION_ID')
    message = entry.get('MESSAGE')
    if not isinstance(invocation, str) or not re.fullmatch(r'[0-9a-f]{32}', invocation):
        return None
    match = PREFIX.match(message) if isinstance(message, str) else None
    if match is None:
        return None
    request_id, source, body = match.groups()
    if (source == 'conductor_execution.go' and
            re.match(r'^429\s*\|[^|]*\|\s*upstream execution failed:', body)):
        kind, status = 'upstream_429', 429
    elif source == 'gin_logger.go':
        response = RESPONSE.match(body)
        if response is None or response[2] not in ENDPOINTS:
            return None
        kind, status = 'response', int(response[1])
    else:
        return None
    return (cursor, invocation, request_id, timestamp, kind, status)


def ingest_entries(con, entries, *, batch_size=500, restart_coverage=False):
    """Commit events and cursor together. Rereading a chunk is idempotent."""
    scanned = captured = 0
    try:
        for entry in entries:
            cursor, timestamp = journal_position(entry)
            event = reduce_entry(entry)
            if not con.in_transaction:
                con.execute('BEGIN IMMEDIATE')
            if event is not None:
                captured += con.execute('''INSERT INTO usage_retry_events
                    (cursor,invocation_id,request_id,timestamp,event_kind,status_code)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(cursor) DO NOTHING''', event).rowcount
            con.execute('''INSERT INTO usage_journal_state
                (id,cursor,since,last_event_at,last_scanned_at) VALUES (1,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,
                    last_event_at=excluded.last_event_at,last_scanned_at=excluded.last_scanned_at''',
                (cursor, timestamp, timestamp, dt.datetime.now(dt.timezone.utc).isoformat()))
            if restart_coverage and scanned == 0:
                con.execute('UPDATE usage_journal_state SET since=? WHERE id=1', (timestamp,))
            scanned += 1
            if scanned % batch_size == 0:
                con.commit()
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return dict(scanned_entries=scanned, captured_events=captured)


def cursor_reachable(cursor, timeout=30):
    """True only if the saved entry still exists; vacuumed journals seek elsewhere silently."""
    command = ['journalctl', '_SYSTEMD_UNIT=' + GATEWAY_UNIT, '--no-pager', '--output=json',
               '--cursor=' + cursor]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True) as process:
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        try:
            line = process.stdout.readline()
        finally:
            timer.cancel()
            process.kill()
            process.wait()
    try:
        return bool(line) and json.loads(line).get('__CURSOR') == cursor
    except ValueError:
        return False


def collect(db=collector.DB, *, since='7 days ago', timeout=180):
    lock_path = db.with_name(db.name + '.journal.lock')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with contextlib.closing(collector.connect(db)) as con:
            state = con.execute('SELECT cursor FROM usage_journal_state WHERE id=1').fetchone()
            # A vacuumed checkpoint means entries were lost: restart coverage at the
            # next entry read so retry counts never claim the gap was observed.
            gap = bool(state) and not cursor_reachable(state[0])
            # Exact trusted unit field: --unit also includes other units' messages
            # about the gateway (e.g. manager messages and coredumps).
            command = ['journalctl', '_SYSTEMD_UNIT=' + GATEWAY_UNIT, '--no-pager', '--output=json']
            command += ['--after-cursor=' + state[0]] if state else ['--since=' + since]
            # No --follow. Suppress diagnostics: journalctl can quote raw content.
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True) as process:
                timer = threading.Timer(timeout, process.kill)
                timer.start()
                try:
                    def entries():
                        for line in process.stdout:
                            yield json.loads(line)
                        if process.wait() != 0:
                            raise RuntimeError('Journal read failed; cursor was not reset')
                    result = ingest_entries(con, entries(), restart_coverage=gap)
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            # An empty successful incremental read still refreshes freshness.
            if state and result['scanned_entries'] == 0:
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                with con:
                    con.execute('UPDATE usage_journal_state SET last_scanned_at=? WHERE id=1', (now,))
                    if gap:
                        con.execute('UPDATE usage_journal_state SET since=? WHERE id=1', (now,))
            result['coverage_gap'] = gap
            return result


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=collector.DB)
    parser.add_argument('--since', default='7 days ago', help='Initial backfill only; later runs resume by cursor')
    args = parser.parse_args()
    try:
        print(json.dumps(collect(args.db, since=args.since)))
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__}))
        raise SystemExit(1)
