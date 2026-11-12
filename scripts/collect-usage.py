#!/usr/bin/env python3
"""Persist only allowlisted usage metadata in the database, recovery spool and
quarantine; never save keys or prompt content.
"""
import argparse
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str((ROOT.parent if ROOT.name == 'scripts' else ROOT) / 'dashboard'))
from usage_time import timestamp_key

DB = Path('/var/lib/personal-codex-router/usage.sqlite3')
KEY = Path('/etc/cliproxyapi/dashboard-management-key')
CACHE_TOKEN_FIELDS = ('cache_read_tokens', 'cache_creation_tokens')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cached_tokens', *CACHE_TOKEN_FIELDS, 'total_tokens')
TEXT_FIELDS = ('timestamp', 'provider', 'model', 'auth_index')
TIER_FIELDS = ('service_tier', 'response_service_tier')
FAILURE_KINDS = ('rate_limit', 'capacity', 'auth', 'server_error', 'timeout',
                 'cancelled', 'other', 'unknown')
FAILURE_CODES = {
    'rate_limit_exceeded': 'rate_limit', 'rate_limit_error': 'rate_limit',
    'usage_limit_reached': 'rate_limit', 'server_is_overloaded': 'capacity',
    'overloaded_error': 'capacity', 'model_capacity_exceeded': 'capacity',
    'authentication_error': 'auth', 'invalid_api_key': 'auth',
    'permission_error': 'auth', 'request_timeout': 'timeout',
    'context_deadline_exceeded': 'timeout', 'context_canceled': 'cancelled',
    'request_cancelled': 'cancelled', 'stream_truncated': 'other',
}


def failure_metadata(record):
    """Reduce diagnostics to an enum before any durable write; never retain bodies.

    The canonical fields also accept already-reduced spools, including a replay
    after a crash. Unknown provider codes and arbitrary text never leave memory.
    """
    fail = record.get('fail')
    fail = fail if isinstance(fail, dict) else {}
    status = fail.get('status_code', record.get('status_code'))
    status = status if type(status) is int and 100 <= status <= 599 else None
    if record.get('failed') is not True:
        return dict(status_code=status, failure_kind=None, failure_code=None)
    if not fail and record.get('failure_kind') in FAILURE_KINDS:
        code = record.get('failure_code')
        return dict(status_code=status, failure_kind=record['failure_kind'],
                    failure_code=code if isinstance(code, str) and code in FAILURE_CODES else None)
    body = fail.get('body')
    body = body[:16384] if isinstance(body, str) else ''
    code = fail.get('code', record.get('failure_code'))
    code = code if isinstance(code, str) else None
    # Inspect structured error fields, not unrelated response content.
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get('error', parsed.get('response', {}).get('error')
                           if isinstance(parsed.get('response'), dict) else None)
        error = error if isinstance(error, dict) else parsed
        values = [error.get(key) for key in ('code', 'type', 'message')]
        body = ' '.join(value for value in values if isinstance(value, str))
        if code not in FAILURE_CODES:
            code = next((value for value in values[:2] if isinstance(value, str)
                         and value in FAILURE_CODES), None)
    code = code if isinstance(code, str) and code in FAILURE_CODES else None
    text = body.lower()
    kind = FAILURE_CODES.get(code)
    # Specific overload/cancellation/timeout evidence wins over generic 5xx,
    # including a mid-stream error whose diagnostic status is still 200.
    if re.search(r'\b(server_is_overloaded|overloaded_error|selected model is at capacity)\b', text):
        kind = 'capacity'
    elif re.search(r'\b(context cancel(?:ed|led)|client cancel(?:ed|led)|request cancel(?:ed|led))\b', text):
        kind = 'cancelled'
    elif re.search(r'\b(context deadline exceeded|i/o timeout|timed out|request timeout)\b', text):
        kind = 'timeout'
    elif re.search(r'\b(rate limit exceeded|rate_limit_exceeded|rate_limit_error)\b', text):
        kind = 'rate_limit'
    if kind is None:
        if status == 429:
            kind = 'rate_limit'
        elif status in (401, 403):
            kind = 'auth'
        elif status in (408, 504):
            kind = 'timeout'
        elif status == 499:
            kind = 'cancelled'
        elif status is not None and status >= 500:
            kind = 'server_error'
        elif record.get('failure_kind') in FAILURE_KINDS:
            kind = record['failure_kind']
        elif body.strip() or (status is not None and status >= 400):
            kind = 'other'
        else:
            kind = 'unknown'
    return dict(status_code=status, failure_kind=kind, failure_code=code)

# NULL sums must remain NULL until a non-NULL observation arrives. Counts are
# stored separately for averages and cache-token coverage. Invalid timestamps
# have a NULL hour: retain their provider/collection metadata, but range queries
# exclude them just as julianday(timestamp) >= julianday(start) does.
ROLLUP_SUMS = ('failed', 'input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens',
               'latency_ms', *CACHE_TOKEN_FIELDS)
ROLLUP_COUNTS = ('latency_ms', *CACHE_TOKEN_FIELDS)
ROLLUP_VALUES = ('requests', *ROLLUP_SUMS, *(field + '_count' for field in ROLLUP_COUNTS),
                 'last_collected_at')
ROLLUP_SELECT = ("coalesce(nullif(provider, ''), 'codex'), "
                 "strftime('%Y-%m-%dT%H:00:00Z', timestamp), auth_index, model, count(*), "
                 + ', '.join('sum(' + field + ')' for field in ROLLUP_SUMS) + ', '
                 + ', '.join('count(' + field + ')' for field in ROLLUP_COUNTS)
                 + ', max(collected_at)')
ROLLUP_INSERT = ('INSERT INTO usage_hourly (provider, hour, auth_index, model, '
                 + ', '.join(ROLLUP_VALUES) + ') SELECT ' + ROLLUP_SELECT + ' FROM usage ')
ROLLUP_UPSERT = (' ON CONFLICT DO UPDATE SET '
                 + ', '.join(field + '=coalesce(usage_hourly.' + field + '+excluded.' + field
                             + ', usage_hourly.' + field + ', excluded.' + field + ')'
                             for field in ROLLUP_VALUES if field != 'last_collected_at')
                 + ', last_collected_at=coalesce(max(usage_hourly.last_collected_at, excluded.last_collected_at), '
                 + 'usage_hourly.last_collected_at, excluded.last_collected_at)')


def normalize_tier(value, *, served=False):
    # An omitted request tier means normal; an omitted response is unobserved.
    if value is None:
        return 'unknown' if served else 'normal'
    if not isinstance(value, str):
        return 'unknown'
    value = value.strip().lower()
    if value == '' and served:
        return 'unknown'
    if value in ('', 'default', 'auto', 'normal'):
        return 'normal'
    if value == 'fast':
        return 'priority'
    return value if value in ('priority', 'ultrafast') else 'unknown'


def normalize_effort(value):
    value = value.strip().lower() if isinstance(value, str) else ''
    return value if re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', value) else None


def normalize_latency(value):
    return value if (type(value) in (int, float) and 0 <= value < 2**63
                     and (type(value) is int or math.isfinite(value))) else None


def reduce_record(record):
    def invalid_type(value):
        if (isinstance(value, dict) and set(value) == {'_invalid_type'}
                and value['_invalid_type'] in ('dict', 'list', 'str', 'int', 'float', 'bool', 'NoneType')):
            return value
        return {'_invalid_type': type(value).__name__}

    if not isinstance(record, dict):
        return invalid_type(record)
    if set(record) == {'_invalid_type'}:
        return invalid_type(record)
    # Keep an unbindable integer record quarantinable without persisting its value.
    if type(record.get('latency_ms')) is int and record['latency_ms'] >= 2**63:
        return invalid_type(record)
    result = {}
    for field in TEXT_FIELDS:
        if field in record:
            value = record[field]
            result[field] = (invalid_type(value) if isinstance(value, (dict, list))
                             else str(value)[:256])
    for field in TIER_FIELDS:
        if field in record:
            result[field] = normalize_tier(record[field], served=field == 'response_service_tier')
    if 'reasoning_effort' in record:
        value = record['reasoning_effort']
        result['reasoning_effort'] = (normalize_effort(value) if isinstance(value, str) or value is None
                                      else invalid_type(value))
    if 'failed' in record:
        value = record['failed']
        result['failed'] = value if type(value) is bool else invalid_type(value)
    result.update({key: value for key, value in failure_metadata(record).items() if value is not None})
    for field, pattern in (('request_id', r'[0-9a-f]{8}'),
                           ('execution_id', r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')):
        value = record.get(field)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            result[field] = value
    if 'latency_ms' in record:
        value = record['latency_ms']
        result['latency_ms'] = (normalize_latency(value) if type(value) in (int, float) or value is None
                                else invalid_type(value))
    if 'tokens' in record:
        tokens = record['tokens']
        if isinstance(tokens, dict) and set(tokens) != {'_invalid_type'}:
            result['tokens'] = {}
            for field in TOKEN_FIELDS:
                if field in tokens:
                    value = tokens[field]
                    if type(value) is int:
                        result['tokens'][field] = value if 0 <= value < 2**63 else None
                    else:
                        result['tokens'][field] = None if value is None else invalid_type(value)
        else:
            result['tokens'] = None if tokens is None else invalid_type(tokens)
    return result


def normalize(record):
    if not isinstance(record, dict) or '_invalid_type' in record:
        raise ValueError('Usage record is not an object')
    result = {k: str(record.get(k, ''))[:256] for k in TEXT_FIELDS}
    # Persist the gateway's per-request value only; never infer it from the model.
    result.update({field: normalize_tier(record.get(field), served=field == 'response_service_tier') for field in TIER_FIELDS})
    result['reasoning_effort'] = normalize_effort(record.get('reasoning_effort'))
    result['failed'] = int(record.get('failed') is True)
    result.update(failure_metadata(record))
    for field in ('request_id', 'execution_id'):
        result[field] = record.get(field)
    latency = record.get('latency_ms')
    result['latency_ms'] = normalize_latency(latency)
    tokens = record.get('tokens') or {}
    if not isinstance(tokens, dict) or '_invalid_type' in tokens:
        raise ValueError('Usage tokens are not an object')
    for field in TOKEN_FIELDS:
        value = tokens.get(field)
        result[field] = value if type(value) is int and 0 <= value < 2**63 else None
    return result


def connect(db):
    con = sqlite3.connect(db, timeout=10)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('BEGIN IMMEDIATE')
        con.execute('''CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY, collected_at TEXT NOT NULL,
            timestamp TEXT, provider TEXT, model TEXT, auth_index TEXT,
            failed INTEGER, latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER, cached_tokens INTEGER, cache_read_tokens INTEGER,
            cache_creation_tokens INTEGER, total_tokens INTEGER,
            reasoning_effort TEXT, service_tier TEXT, response_service_tier TEXT)''')
        # Additive migration preserves old rows as unknown (NULL), without backfilling.
        columns = {row[1] for row in con.execute('PRAGMA table_info(usage)')}
        for column in ('reasoning_effort', *TIER_FIELDS):
            if column not in columns:
                con.execute(f'ALTER TABLE usage ADD COLUMN {column} TEXT')
        for column in CACHE_TOKEN_FIELDS:
            if column not in columns:
                con.execute(f'ALTER TABLE usage ADD COLUMN {column} INTEGER')
        for column, kind in (('status_code', 'INTEGER'), ('failure_kind', 'TEXT'),
                             ('failure_code', 'TEXT'), ('request_id', 'TEXT'), ('execution_id', 'TEXT')):
            if column not in columns:
                con.execute(f'ALTER TABLE usage ADD COLUMN {column} {kind}')
        if 'failure_kind' not in columns:
            # Old reduction discarded the evidence. Backfill only what is known.
            con.execute("UPDATE usage SET failure_kind = 'unknown' WHERE failed")
        # Publish the exact sort keys and their indexes atomically. Only this
        # collector writes usage rows; dashboard connections remain mode=ro.
        if 'request_time' not in columns:
            con.execute('ALTER TABLE usage ADD COLUMN request_time TEXT')
            con.create_function('usage_timestamp_key', 1, timestamp_key, deterministic=True)
            con.execute('UPDATE usage SET request_time = usage_timestamp_key(timestamp)')
        con.execute('CREATE INDEX IF NOT EXISTS usage_by_request_time ON usage(request_time DESC, id DESC)')
        con.execute('''CREATE INDEX IF NOT EXISTS usage_by_provider_request_time
            ON usage(coalesce(nullif(provider, ''), 'codex'), request_time DESC, id DESC)''')
        con.execute('''CREATE INDEX IF NOT EXISTS usage_failed_by_request_time
            ON usage(request_time DESC, id DESC) WHERE failed''')
        con.execute('''CREATE INDEX IF NOT EXISTS usage_failed_by_provider_request_time
            ON usage(coalesce(nullif(provider, ''), 'codex'), request_time DESC, id DESC) WHERE failed''')
        con.execute('CREATE INDEX IF NOT EXISTS usage_by_account ON usage(auth_index, timestamp)')
        con.execute('''CREATE TABLE IF NOT EXISTS usage_batches (
            spool_id TEXT PRIMARY KEY, collected_at TEXT NOT NULL)''')
        con.execute('''CREATE TABLE IF NOT EXISTS usage_quarantine (
            id INTEGER PRIMARY KEY, spool_id TEXT NOT NULL, record_index INTEGER NOT NULL,
            collected_at TEXT NOT NULL, raw_json TEXT NOT NULL, error TEXT NOT NULL)''')
        # The journal reader is independent of the destructive usage collector.
        # It stores allowlisted metadata only and commits its cursor with events.
        con.execute('''CREATE TABLE IF NOT EXISTS usage_retry_events (
            id INTEGER PRIMARY KEY, cursor TEXT NOT NULL UNIQUE,
            invocation_id TEXT NOT NULL, request_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, event_kind TEXT NOT NULL, status_code INTEGER NOT NULL)''')
        con.execute('''CREATE INDEX IF NOT EXISTS usage_retry_events_by_request
            ON usage_retry_events(invocation_id, request_id, id)''')
        con.execute('''CREATE INDEX IF NOT EXISTS usage_retry_events_429
            ON usage_retry_events(invocation_id, request_id) WHERE event_kind='upstream_429' ''')
        con.execute('''CREATE TABLE IF NOT EXISTS usage_journal_state (
            id INTEGER PRIMARY KEY CHECK (id = 1), cursor TEXT NOT NULL,
            since TEXT NOT NULL, last_event_at TEXT NOT NULL, last_scanned_at TEXT NOT NULL)''')
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_hourly'").fetchone():
            if (con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_retention'").fetchone()
                    and con.execute('SELECT 1 FROM usage_retention WHERE pruned_rows > 0').fetchone()):
                raise RuntimeError('Hourly history is missing after raw retention; restore it from backup, never backfill pruned raw history')
            con.execute('''CREATE TABLE usage_hourly (
                provider TEXT NOT NULL, hour TEXT, auth_index TEXT, model TEXT,
                requests INTEGER NOT NULL, failed INTEGER, input_tokens INTEGER,
                output_tokens INTEGER, cached_tokens INTEGER, total_tokens INTEGER,
                latency_ms REAL, cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
                latency_ms_count INTEGER NOT NULL, cache_read_tokens_count INTEGER NOT NULL,
                cache_creation_tokens_count INTEGER NOT NULL, last_collected_at TEXT)''')
            # Integer 0 cannot collide with a TEXT key, including the empty string.
            # Ordinary UNIQUE columns would allow duplicate NULL groups on replay.
            con.execute('''CREATE UNIQUE INDEX usage_hourly_key ON usage_hourly
                (provider, ifnull(hour, 0), ifnull(auth_index, 0), ifnull(model, 0))''')
            con.execute('CREATE INDEX usage_hourly_by_provider_hour ON usage_hourly(provider, hour)')
            con.execute(ROLLUP_INSERT + 'GROUP BY 1, 2, 3, 4')
        con.commit()
    except BaseException:
        con.close()
        raise
    return con


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def spool_response(directory, req):
    with urllib.request.urlopen(req, timeout=10) as response:
        records = json.load(response)
    if not isinstance(records, list):
        raise ValueError('Unexpected usage queue response')
    records = [reduce_record(record) for record in records]
    # The loss window is only the milliseconds between the HTTP read and fsync;
    # invalid whole responses raise without ever spooling raw bytes.
    path = directory / (uuid.uuid4().hex + '.json')
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    # Buffered writes either complete or raise; raw writes may be short.
    with os.fdopen(fd, 'wb') as spool:
        # Make even an interrupted temporary response discoverable after a restart.
        sync_directory(directory)
        spool.write(json.dumps(records).encode())
        spool.flush()
        os.fsync(spool.fileno())
    temporary.replace(path)
    sync_directory(directory)
    return path


def replay_spool(con, path):
    saved = 0
    quarantined = 0
    if not con.in_transaction:
        con.execute('BEGIN IMMEDIATE')
    try:
        if not con.execute('SELECT 1 FROM usage_batches WHERE spool_id = ?', (path.stem,)).fetchone():
            try:
                records = json.loads(path.read_bytes())
                if not isinstance(records, list):
                    raise ValueError('Unexpected usage queue response')
            except (ValueError, UnicodeError):
                con.rollback()
                failed = path.parent / 'failed'
                failed.mkdir(mode=0o700, exist_ok=True)
                failed.chmod(0o700)
                target = failed / path.name
                if target.exists():
                    target = failed / (uuid.uuid4().hex + path.suffix)
                path.chmod(0o600)
                path.replace(target)
                sync_directory(failed)
                sync_directory(path.parent)
                return 0, 0, 1
            collected_at = datetime.datetime.fromtimestamp(
                path.stat().st_mtime, datetime.timezone.utc).isoformat()
            for index, raw in enumerate(records):
                raw = reduce_record(raw)
                try:
                    record = normalize(raw)
                    record['collected_at'] = collected_at
                    record['request_time'] = timestamp_key(record['timestamp'])
                    inserted = con.execute('''INSERT INTO usage (collected_at,timestamp,provider,model,auth_index,
                        failed,latency_ms,input_tokens,output_tokens,reasoning_tokens,cached_tokens,
                        cache_read_tokens,cache_creation_tokens,total_tokens,reasoning_effort,service_tier,response_service_tier,request_time,
                        status_code,failure_kind,failure_code,request_id,execution_id)
                        VALUES (:collected_at,:timestamp,:provider,:model,:auth_index,:failed,:latency_ms,
                        :input_tokens,:output_tokens,:reasoning_tokens,:cached_tokens,:cache_read_tokens,
                        :cache_creation_tokens,:total_tokens,:reasoning_effort,:service_tier,:response_service_tier,:request_time,
                        :status_code,:failure_kind,:failure_code,:request_id,:execution_id)''', record)
                except (OverflowError, ValueError, sqlite3.InterfaceError, sqlite3.IntegrityError) as exc:
                    con.execute('''INSERT INTO usage_quarantine
                        (spool_id,record_index,collected_at,raw_json,error) VALUES (?,?,?,?,?)''',
                        (path.stem, index, collected_at, json.dumps(reduce_record(raw)), type(exc).__name__))
                    quarantined += 1
                    continue
                # Outside the quarantine handler: any rollup failure must roll
                # back the entire batch, including this successful usage insert.
                con.execute(ROLLUP_INSERT + 'WHERE id = ? GROUP BY 1, 2, 3, 4' + ROLLUP_UPSERT,
                            (inserted.lastrowid,))
                saved += 1
            # The marker and rows commit together, before removing the spool.
            con.execute('INSERT INTO usage_batches (spool_id,collected_at) VALUES (?,?)',
                        (path.stem, collected_at))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    path.unlink()
    sync_directory(path.parent)
    return saved, quarantined, 0


def write_status(db, con, directory, successful):
    path = db.with_name('collector-status.json')
    try:
        previous = json.loads(path.read_text())
        last_success = previous.get('last_success_at') if isinstance(previous, dict) else None
    except (OSError, ValueError):
        last_success = None
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    failed = directory / 'failed'
    data = {'quarantined_total': (con.execute('SELECT count(*) FROM usage_quarantine').fetchone()[0]
                                  if con is not None else None),
            'failed_spools': sum(p.is_file() for p in failed.iterdir()) if failed.exists() else 0,
            'last_success_at': stamp if successful else last_success, 'last_run_at': stamp}
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, 'w') as status:
            os.fchmod(status.fileno(), 0o640)
            json.dump(data, status)
            status.flush()
            os.fsync(status.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def collect(db=DB, key_path=KEY, lock_timeout=15):
    directory = db.with_name(db.name + '.spool')
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    sync_directory(directory.parent)
    lock_fd = os.open(directory / 'collector.lock', os.O_RDWR | os.O_CREAT, 0o600)
    saved = quarantined = failed = 0
    with os.fdopen(lock_fd, 'rb') as lock:
        # The service's 90s timeout leaves collection time after this 15s backup wait.
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        con = None
        successful = False
        try:
            con = connect(db)
            for path in sorted(directory.iterdir()):
                if path.suffix in ('.json', '.tmp'):
                    count, rejected, broken = replay_spool(con, path)
                    saved += count
                    quarantined += rejected
                    failed += broken
            key = key_path.read_text().strip()
            # Reserve a writable transaction before the destructive queue read.
            con.execute('BEGIN IMMEDIATE')
            req = urllib.request.Request('http://127.0.0.1:8317/v0/management/usage-queue?count=1000',
                                         headers={'Authorization': 'Bearer ' + key})
            path = spool_response(directory, req)
            count, rejected, broken = replay_spool(con, path)
            saved += count
            quarantined += rejected
            failed += broken
            successful = True
        finally:
            try:
                if con is not None:
                    con.rollback()
                # Publish committed aggregates while still holding the collector lock.
                write_status(db, con, directory, successful)
            finally:
                if con is not None:
                    con.close()
    print(json.dumps({'saved_records': saved, 'quarantined_records': quarantined, 'failed_spools': failed}))


def summary(db=DB):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in con.execute('PRAGMA table_info(usage)')}
        cache_totals = ', '.join(
            f'sum({field if field in columns else "NULL"}) AS {field}, '
            f'count({field if field in columns else "NULL"}) AS {field.removesuffix("_tokens")}_requests'
            for field in CACHE_TOKEN_FIELDS)
        rows = con.execute('''SELECT auth_index, model, count(*) AS requests,
            sum(failed) AS failures, sum(input_tokens) AS input_tokens,
            sum(output_tokens) AS output_tokens, sum(cached_tokens) AS cached_tokens,
            ''' + cache_totals + ''',
            sum(total_tokens) AS total_tokens, min(timestamp) AS first_request,
            max(timestamp) AS last_request FROM usage GROUP BY auth_index,model''')
        print(json.dumps([dict(row) for row in rows], indent=2))
    finally:
        con.close()


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    try:
        summary() if args.summary else collect()
    except Exception as exc:
        # Network errors can include sensitive response bodies; log only the type.
        print(json.dumps({'error_type': type(exc).__name__}))
        raise SystemExit(1)
