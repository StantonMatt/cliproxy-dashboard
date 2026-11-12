#!/usr/bin/env python3
"""Archive verified raw usage batches before pruning; hourly history is permanent."""
import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

DB = Path('/var/lib/personal-codex-router/usage.sqlite3')
ARCHIVES = DB.parent / 'usage-archive'
ROOT = Path(__file__).resolve().parents[1]


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_archive(directory, columns, rows, cutoff):
    path = directory / ('usage-' + cutoff[:10] + '-' + uuid.uuid4().hex + '.jsonl.gz')
    temporary = path.with_suffix('.partial')
    digest = hashlib.sha256()
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as stream:
                header = encoded(dict(format=1, table='usage', columns=columns, before=cutoff))
                stream.write(header)
                digest.update(header)
                for row in rows:
                    line = encoded(row)
                    digest.update(line)
                    stream.write(line)
                stream.write(encoded(dict(rows=len(rows), sha256=digest.hexdigest())))
            raw.flush()
            os.fsync(raw.fileno())
        # Reopen and decompress all bytes, including the gzip CRC/trailer, before
        # publishing the archive. Verification is repeated before each deletion.
        verify_archive(temporary, columns, rows, cutoff)
        temporary.replace(path)
        sync_directory(directory)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def verify_archive(path, columns, rows, cutoff):
    digest = hashlib.sha256()
    with gzip.open(path, 'rb') as stream:
        header = stream.readline()
        if json.loads(header) != dict(format=1, table='usage', columns=columns, before=cutoff):
            raise ValueError('Archive header mismatch')
        digest.update(header)
        for row in rows:
            line = stream.readline()
            if line != encoded(row):
                raise ValueError('Archive row mismatch')
            digest.update(line)
        trailer = json.loads(stream.readline())
        if trailer != dict(rows=len(rows), sha256=digest.hexdigest()) or stream.read(1):
            raise ValueError('Archive count or checksum mismatch')
    return digest.hexdigest()


def cutoff_for(now):
    # Keep at least 90 complete days. Whole UTC days preserve hourly rollups at
    # the retention boundary, and avoid removing part of an archived hour.
    return (now.astimezone(dt.timezone.utc) - dt.timedelta(days=90)).replace(
        hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def retain(db=DB, archives=ARCHIVES, *, dry_run=False, now=None, batch_size=500):
    if not 1 <= batch_size <= 1000:
        raise ValueError('Batch size must be between 1 and 1000')
    db, archives = Path(db), Path(archives)
    # Never put private usage archives in the source or installed application.
    if archives.resolve().is_relative_to(ROOT):
        raise ValueError('Usage archives must be outside the application directory')
    cutoff = cutoff_for(now or dt.datetime.now(dt.timezone.utc))
    cutoff_key = cutoff[:-1] + '.000000000Z'
    # Preserve the rare sub-millisecond rows whose SQLite rounded hour differs
    # from strftime(timestamp). The dashboard adds these raw boundary corrections
    # to hourly aggregates; pruning them would subtly change historic totals.
    predicate = ("(request_time < ? OR request_time IS NULL) AND julianday(timestamp) < julianday(?)"
                 " AND strftime('%Y-%m-%dT%H:00:00Z', timestamp) = "
                 "strftime('%Y-%m-%dT%H:00:00Z', julianday(timestamp))"
                 # usage.id is a plain INTEGER PRIMARY KEY: deleting the highest row would
                 # let SQLite reuse archived IDs, which archive restores deduplicate by.
                 " AND id < (SELECT max(id) FROM usage)")
    args = (cutoff_key, cutoff)
    mode = 'ro' if dry_run else 'rw'
    con = sqlite3.connect(db.resolve().as_uri() + '?mode=' + mode, uri=True, timeout=0.25)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_hourly' AND type='table'").fetchone():
            raise ValueError('Hourly history is missing; retention refused')
        if not dry_run:
            con.execute('PRAGMA synchronous=FULL')
        eligible = con.execute('SELECT count(*) FROM usage WHERE ' + predicate, args).fetchone()[0]
        result = dict(dry_run=dry_run, before=cutoff, eligible=eligible, deleted=0, archives=0)
        if dry_run or not eligible:
            return result
        pruned = 0
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_retention' AND type='table'").fetchone():
            pruned = con.execute('SELECT coalesce(sum(pruned_rows), 0) FROM usage_retention').fetchone()[0]
        raw_count = con.execute('SELECT count(*) FROM usage').fetchone()[0]
        hourly_count = con.execute('SELECT coalesce(sum(requests), 0) FROM usage_hourly').fetchone()[0]
        if hourly_count < raw_count + pruned:
            raise ValueError('Hourly history is incomplete; retention refused')
        archives.mkdir(mode=0o700, exist_ok=True)
        archives.chmod(0o700)
        # The directory entry itself must survive power loss before the database
        # can durably commit a deletion referring to an archive inside it.
        sync_directory(archives.parent)
        lock_fd = os.open(archives / '.retention.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            columns = [r[1] for r in con.execute('PRAGMA table_info(usage)')]
            id_index = columns.index('id')
            # Freeze the upper ID so a busy collector cannot keep the run alive.
            maximum = con.execute('SELECT max(id) FROM usage').fetchone()[0]
            after = 0
            while True:
                rows = con.execute('SELECT * FROM usage WHERE id > ? AND id <= ? AND ' + predicate
                                   + ' ORDER BY id LIMIT ?', (after, maximum, *args, batch_size)).fetchall()
                if not rows:
                    break
                path = write_archive(archives, columns, rows, cutoff)
                verify_archive(path, columns, rows, cutoff)
                # Compression, rereading and fsync happen outside the write lock.
                # Recheck the exact source rows inside a short write transaction;
                # any change or lock timeout leaves the archive and rows intact.
                con.execute('BEGIN IMMEDIATE')
                try:
                    ids = [r[id_index] for r in rows]
                    placeholders = ','.join('?' for _ in ids)
                    current = con.execute('SELECT * FROM usage WHERE id IN (' + placeholders
                                          + ') ORDER BY id', ids).fetchall()
                    if current != rows:
                        raise ValueError('Usage changed after archiving; retention refused')
                    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_hourly'").fetchone():
                        raise ValueError('Hourly history is missing; retention refused')
                    con.execute('''CREATE TABLE IF NOT EXISTS usage_retention (
                        id INTEGER PRIMARY KEY CHECK (id=1), raw_before TEXT NOT NULL,
                        pruned_rows INTEGER NOT NULL, last_archive TEXT NOT NULL)''')
                    con.execute('''INSERT INTO usage_retention VALUES (1, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET raw_before=max(raw_before, excluded.raw_before),
                        pruned_rows=pruned_rows+excluded.pruned_rows, last_archive=excluded.last_archive''',
                                (cutoff, len(rows), path.name))
                    con.execute('DELETE FROM usage WHERE id IN (' + placeholders + ')', ids)
                    con.commit()
                except BaseException:
                    con.rollback()
                    raise
                after = ids[-1]
                result['deleted'] += len(rows)
                result['archives'] += 1
        return result
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DB)
    parser.add_argument('--archive-dir', type=Path, default=ARCHIVES)
    parser.add_argument('--batch-size', type=int, default=500)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(retain(args.db, args.archive_dir, dry_run=args.dry_run, batch_size=args.batch_size)))
    except (OSError, ValueError, sqlite3.Error):
        # Exceptions can contain row values or private filesystem information.
        print('Usage retention failed; unverified rows were not deleted.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
