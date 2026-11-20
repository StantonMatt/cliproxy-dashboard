#!/usr/bin/env python3
"""Private, file-consistent settings snapshots and online SQLite backups.

No production restore command is provided. Rehearsal verifies and stages files
in a NEW directory; an operator chooses individual files during maintenance.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import time
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_config

HOME = Path(site_config.desktop().pw_dir)
PROJECT = Path(site_config.load().get('source_checkout', site_config.REPO))
BACKUPS = Path('/var/lib/personal-codex-router-backups')
STATUS = Path('/var/lib/personal-codex-router-ops/backup.json')
USAGE_SPOOL = Path('/var/lib/personal-codex-router/usage.sqlite3.spool')
EXCLUDE = {'.git', '__pycache__', '.codex-tmp', 'node_modules'}
TEMPORARY = re.compile(r'.+\.tmp\.[0-9a-f]+')


def atomic_json(path, value, mode=0o600):
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), mode)
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stable_copy(source, target, validate=True):
    """Reject symlinks and changing files; validate settings JSON/TOML."""
    for _ in range(5):
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as f:
            before = os.fstat(f.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Only regular files can be snapshotted')
            data = f.read()
            after = os.fstat(f.fileno())
        current = source.stat(follow_symlinks=False)
        signature = lambda s: (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(current) or signature(before) != signature(after):
            time.sleep(0.1)
            continue
        try:
            if validate and source.suffix == '.json':
                json.loads(data)
            elif validate and source.suffix == '.toml':
                tomllib.loads(data.decode())
        except (ValueError, UnicodeError):
            time.sleep(0.1)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open('xb') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return before
    raise RuntimeError('A settings file did not stabilize')


def sqlite_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + 90
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('Database snapshot deadline reached')
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=3)
    dst = sqlite3.connect(target)
    try:
        os.chmod(target, 0o600)
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
        if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('Database verification failed')
    finally:
        dst.close()
        src.close()
    with target.open('rb') as f:
        os.fsync(f.fileno())
    return source.stat()


def sources():
    required = [Path('/etc/cliproxyapi'), Path('/opt/personal-codex-router')]
    desktop_sources = [PROJECT, Path('/var/lib/cliproxyapi/auth'),
                       Path('/var/lib/personal-codex-router/reset-controls.json'),
                       Path('/var/lib/personal-codex-router/credit-spending-controls.json'),
                       Path('/var/lib/personal-codex-router/usage.sqlite3'),
                       HOME / '.codex/config.toml', HOME / '.codex/auth.json',
                       HOME / '.codex/app-server-daemon/settings.json',
                       HOME / '.config/systemd/user/codex-shared-server-start.service']
    # The policy is regenerated from controls; plugin status is process liveness.
    optional = desktop_sources + [Path('/var/lib/personal-codex-router/fast-mode-controls.json'), USAGE_SPOOL, Path('/var/lib/personal-codex-router/billing.json'),
                Path('/var/lib/personal-codex-router/notifications.json'),
                Path('/var/lib/personal-codex-router-ops/desktop-delivery.json'),
                Path('/var/lib/personal-codex-router-routing/baseline.json'),
                Path('/etc/personal-codex-router'), Path('/etc/tailscale/host-guard.nft'),
                HOME / '.codex/AGENTS.md', HOME / '.codex/.codex-global-state.json',
                HOME / '.config/personal-codex-router', HOME / '.local/state/personal-codex-router',
                HOME / '.local/bin/codex-shared-client',
                HOME / '.local/share/applications/codex-shared-client.desktop']
    optional += [HOME / '.ssh' / name for name in (
        'config', 'authorized_keys', 'id_ed25519_codex_loopback',
        'id_ed25519_codex_loopback.pub', 'known_hosts_codex_loopback')]
    client = HOME / '.local/share/codex-shared-client/codex-home'
    optional += [client / name for name in ('config.toml', 'auth.json', '.codex-global-state.json')]
    optional += list(Path('/etc/systemd/system').glob('personal-codex-router-*'))
    optional += [Path('/etc/systemd/system/cliproxyapi.service'),
                 Path('/etc/systemd/system/cliproxyapi.service.d'),
                 Path('/etc/systemd/system/tailscale-host-guard.service')]
    for path in required:
        if not path.exists() or path.is_symlink():
            raise RuntimeError('Required backup source missing or symlinked')
    return required + [p for p in optional if p.exists() and not p.is_symlink()]


def regular_files(paths, exclude=()):
    result = set()
    for path in paths:
        if any(path.is_relative_to(p) for p in exclude):
            continue
        if path.is_file() and not path.is_symlink():
            result.add(path)
        elif path.is_dir() and not path.is_symlink():
            for directory, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = [n for n in dirs if n not in EXCLUDE
                           and not (Path(directory) / n).is_symlink()
                           and not any((Path(directory) / n).is_relative_to(p) for p in exclude)]
                for name in files:
                    p = Path(directory) / name
                    if any(p.is_relative_to(excluded) for excluded in exclude):
                        continue
                    spool = p.is_relative_to(USAGE_SPOOL)
                    excluded = ('.pyc', '-wal', '-shm') if spool else ('.pyc', '-wal', '-shm', '.tmp')
                    # `<name>.tmp.<hex>`: an unfinished atomic write, such as a dashboard sign-in's.
                    temporary = not spool and TEMPORARY.fullmatch(name)
                    if (not p.is_symlink() and p.is_file() and not name.endswith(excluded) and not temporary
                            and not (spool and name == 'collector.lock')):
                        result.add(p)
    return sorted(result)


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


@contextmanager
def collector_lock(timeout=10):
    try:
        fd = os.open(USAGE_SPOOL / 'collector.lock', os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield
        return
    with os.fdopen(fd, 'rb') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Usage collector lock deadline reached') from None
                time.sleep(min(0.05, remaining))
        yield


def snapshot(paths, destination):
    destination.mkdir(mode=0o700)
    entries = []
    def copy(source):
        relative = source.relative_to('/')
        target = destination / 'files' / relative
        if source.suffix in ('.sqlite', '.sqlite3', '.db'):
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                header = stream.read(16)
            if header != b'SQLite format 3\x00':
                raise ValueError(
                    f'Non-SQLite database: {source}; set server.kv-store.disabled: true '
                    '(see docs/gateway-upgrade-8.0.79-2026-11-19.md). '
                    'This file cannot be backed up by SQLite online backup.')
            info = sqlite_copy(source, target)
            method = 'sqlite-online-backup'
        else:
            # Recovery spools include interrupted and failed JSON; preserve their
            # stable bytes even when they cannot be parsed as settings files.
            info = stable_copy(source, target, validate=not source.is_relative_to(USAGE_SPOOL))
            method = 'stable-file-read'
        entries.append({'path': str(relative), 'sha256': digest(target),
                        'bytes': target.stat().st_size, 'mode': stat.S_IMODE(info.st_mode),
                        'uid': info.st_uid, 'gid': info.st_gid, 'method': method})
    # Protect only usage enumeration and copies, not the whole settings backup.
    usage_sources = (USAGE_SPOOL, USAGE_SPOOL.with_suffix(''))
    other_files = regular_files(paths, exclude=usage_sources)
    usage_paths = [p for p in paths if any(p.is_relative_to(s) for s in usage_sources)]
    usage_paths += [s for s in usage_sources if any(s.is_relative_to(p) for p in paths)]
    if usage_paths:
        with collector_lock():
            for source in regular_files(usage_paths):
                copy(source)
    for source in other_files:
        copy(source)
    manifest = {'version': 1, 'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'scope': 'router and shared Codex settings; not Codex task history or off-device backup',
                'entries': entries}
    atomic_json(destination / 'manifest.json', manifest)
    sync_dir(destination)
    return manifest


def verify(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('version') != 1 or not manifest.get('entries'):
        raise ValueError('Invalid manifest')
    seen = set()
    for row in manifest['entries']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts or str(relative) in seen:
            raise ValueError('Unsafe manifest path')
        seen.add(str(relative))
        path = directory / 'files' / relative
        if path.is_symlink() or not path.resolve().is_relative_to((directory / 'files').resolve()):
            raise ValueError('Unsafe backup path')
        if digest(path) != row['sha256'] or path.stat().st_size != row['bytes']:
            raise ValueError('Backup checksum mismatch')
        if row['method'] == 'sqlite-online-backup':
            con = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
            try:
                if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Database integrity check failed')
            finally:
                con.close()
    return manifest


def rehearse(directory, destination):
    manifest = verify(directory)
    # Never overwrite anything or stage into a production hierarchy.
    destination = destination.absolute()
    if destination.parent.resolve() != destination.parent:
        raise ValueError('Rehearsal parent must not contain symlinks')
    if not destination.is_relative_to(BACKUPS / 'rehearsals') and not destination.is_relative_to(Path('/tmp')):
        raise ValueError('Rehearsal must use /tmp or the private backup rehearsal directory')
    if destination.exists() or destination.is_symlink():
        raise ValueError('Rehearsal directory must not exist')
    destination.mkdir(mode=0o700, parents=True)
    for row in manifest['entries']:
        target = destination / 'files' / row['path']
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(directory / 'files' / row['path'], target)
        target.chmod(0o600)
    atomic_json(destination / 'manifest.json', manifest)
    verify(destination)
    return len(manifest['entries'])


def prune(root, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    snapshots = sorted(p for p in root.glob('20??????T??????Z') if p.is_dir() and not p.is_symlink())
    # Retain at least the latest two successful snapshots even after long downtime.
    for path in snapshots[:-2]:
        completed = dt.datetime.fromisoformat(json.loads((path / 'manifest.json').read_text())['completed_at'])
        if (now - completed).total_seconds() > 14 * 86400:
            shutil.rmtree(path)


def run():
    os.umask(0o077)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    with (BACKUPS / 'backup.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        staging = BACKUPS / ('.incomplete-' + name)
        previous = {}
        try:
            if STATUS.exists():
                previous = json.loads(STATUS.read_text())
            manifest = snapshot(sources(), staging)
            verify(staging)
            staging.rename(BACKUPS / name)
            sync_dir(BACKUPS)
            previous = {'last_success_at': manifest['completed_at'], 'snapshot': name,
                        'file_count': len(manifest['entries']), 'storage': 'same_disk'}
            prune(BACKUPS)
            atomic_json(STATUS, {**previous, 'last_attempt_at': manifest['completed_at'], 'ok': True}, 0o640)
            print(json.dumps({'snapshot': name, 'files': len(manifest['entries']), 'verified': True}))
        except Exception:
            atomic_json(STATUS, {**previous, 'last_attempt_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'ok': False}, 0o640)
            # Keep incomplete files private for diagnosis; never mark them recoverable.
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('run')
    check = commands.add_parser('verify')
    check.add_argument('snapshot', type=Path)
    stage = commands.add_parser('rehearse')
    stage.add_argument('snapshot', type=Path)
    stage.add_argument('destination', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'run':
            run()
        elif args.command == 'verify':
            print(json.dumps({'verified_files': len(verify(args.snapshot.resolve())['entries'])}))
        else:
            print(json.dumps({'rehearsed_files': rehearse(args.snapshot.resolve(), args.destination)}))
    except Exception as error:
        # No raw paths/content/provider errors in the journal.
        print('Backup operation failed: ' + type(error).__name__)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
