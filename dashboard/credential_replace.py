"""The only dashboard code that writes the gateway's auth directory.

Since CLIProxyAPI v8.0.25 the Codex credential on disk is the source of truth:
a management PATCH without creation intent restores every token field from disk
(sdk/cliproxy/auth/codex_persist.go:106-125, internal/auth/codex/token.go:182-220).
A sign-in therefore replaces the token fields on disk under the gateway's own
per-file flock (internal/auth/codex/filelock_unix.go:11-24, `Abs(path)+".lock"`),
which gateway Codex saves, including an in-flight refresh, also hold.

Management upload and delete write the auth directory without that lock. The
file is re-checked (inode, size, mtime and ctime) immediately before the rename,
so an upload or delete since the read refuses the write; only the instant
between that check and the rename stays open.

Hold that lock only for local file work. Never make a management call while it
is held: gateway saves wait for this lock, and its file store serializes all
accounts behind them. replace() takes no management client, and holding()
lets callers assert the rule.
"""
from contextlib import contextmanager
import datetime as dt
import errno
import fcntl
import json
import math
import os
import re
import secrets
import stat
import threading
import time

AUTH_DIR = '/var/lib/cliproxyapi/auth'
TOKEN_FIELDS = ('access_token', 'refresh_token', 'id_token', 'expired', 'last_refresh', 'plan_type')
LOCK_WAIT_SECONDS = 90
MAX_BYTES = 1 << 20
# A temp file this old belongs to a writer that died before renaming it.
STALE_TEMP_SECONDS = 600
NAME = re.compile(r'codex-[A-Za-z0-9@._+-]{1,200}\.json')
# Refusals that depend only on the listing, the file or its lock and do not clear
# on their own; preflight() checks every one. The others (lock_timeout, changed,
# unreadable, invalid_json, invalid_tokens) can pass on another attempt.
LASTING = frozenset({'invalid_name', 'path_mismatch', 'not_file', 'not_regular', 'wrong_owner',
                     'no_access', 'too_large', 'not_codex', 'siwc_present', 'lock_untrusted'})
# Why opening the credential failed. Anything else, such as a file deleted
# between the listing and the open (ENOENT), reads as transient `unreadable`.
OPEN_ERRORS = {errno.ELOOP: 'not_regular', errno.ENXIO: 'not_regular',
               errno.EACCES: 'no_access', errno.EPERM: 'no_access'}
# Temp files of any Codex credential, as _write names them.
TEMP = re.compile(NAME.pattern + r'\.tmp\.[0-9a-f]{24}')

_local = threading.local()


class ReplaceError(Exception):
    """A fixed, non-secret reason; nothing was written."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def holding():
    """True while this thread holds a credential lock."""
    return getattr(_local, 'depth', 0) > 0


def stamp(epoch):
    # The gateway parses RFC3339 (sdk/cliproxy/auth/types.go:1051-1053).
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def target_name(name, listed_path, auth_dir=None):
    auth_dir = AUTH_DIR if auth_dir is None else auth_dir
    if (not isinstance(name, str) or not NAME.fullmatch(name) or '..' in name
            or os.path.basename(name) != name or '/' in name or '\\' in name):
        raise ReplaceError('invalid_name')
    if not isinstance(listed_path, str) or not listed_path or listed_path != os.path.join(auth_dir, name):
        raise ReplaceError('path_mismatch')
    return name


def listed_file(record, auth_dir=None):
    """The listing must describe a file credential at AUTH_DIR/name; returns (name, path)."""
    if record.get('runtime_only') is True or record.get('source') != 'file':
        raise ReplaceError('not_file')
    return target_name(record.get('name'), record.get('path'), auth_dir), record['path']


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key')
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError('Non-finite number')


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('Non-finite number')
    return value


def parse(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_strict_object,
                           parse_constant=_reject_constant, parse_float=_finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReplaceError('invalid_json') from None
    if not isinstance(value, dict):
        raise ReplaceError('invalid_json')
    return value


def _trusted_lock(info, uid):
    # Owner read and write: the gateway opens it O_RDWR too.
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o600 != 0o600:
        raise ReplaceError('lock_untrusted')


def check_lock(directory, name, uid):
    """Refuse an existing sidecar lock that credential_lock() would refuse; never creates it.

    Returns whether the lock exists.
    """
    try:
        info = os.stat(name + '.lock', dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise ReplaceError('lock_untrusted') from None
    _trusted_lock(info, uid)
    return True


@contextmanager
def credential_lock(directory, name, wait=LOCK_WAIT_SECONDS, clock=time.monotonic, sleep=time.sleep, uid=None):
    """The gateway's sidecar lock: open O_CREAT 0600 and flock(LOCK_EX), polled with a deadline."""
    uid = os.geteuid() if uid is None else uid
    try:
        fd = os.open(name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
    except OSError:
        # A symlink, directory or socket reads as lock_untrusted.
        if not check_lock(directory, name, uid):
            raise  # It could not be created; nothing to say about an existing lock.
        # An existing lock that passes the check yet cannot be opened will not open next time either.
        raise ReplaceError('lock_untrusted') from None
    try:
        _trusted_lock(os.fstat(fd), uid)
        deadline = clock() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise ReplaceError('lock_timeout') from None
                sleep(0.05)
        _local.depth = getattr(_local, 'depth', 0) + 1
        try:
            yield
        finally:
            _local.depth -= 1
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def fingerprint(info):
    """Identity plus content stamps: an in-place write changes size, mtime or ctime."""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(directory, name, uid):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        # O_NOFOLLOW refuses a symlink with ELOOP; a socket gives ENXIO.
        raise ReplaceError(OPEN_ERRORS.get(error.errno, 'unreadable')) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReplaceError('not_regular')
        if info.st_uid != uid:
            raise ReplaceError('wrong_owner')
        if info.st_size > MAX_BYTES:
            raise ReplaceError('too_large')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise ReplaceError('too_large')
            chunks.append(chunk)
        return parse(b''.join(chunks)), fingerprint(info)
    finally:
        os.close(fd)


def _auth_dir(auth_dir, uid):
    directory = os.open(auth_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if os.fstat(directory).st_uid != uid:
            raise ReplaceError('wrong_owner')
        return directory
    except BaseException:
        os.close(directory)
        raise


def _target(directory, name, uid):
    """Every refusal that depends only on the file; replace() and preflight() share it."""
    current, read_at = _read(directory, name, uid)
    if current.get('type') != 'codex':
        raise ReplaceError('not_codex')
    if 'siwc' in current or 'use_siwc' in current:
        raise ReplaceError('siwc_present')
    return current, read_at


def preflight(record, auth_dir=None, uid=None):
    """Raise ReplaceError for any reason replace() would refuse this listed credential now.

    Takes no lock (its sidecar file is never created) and writes nothing to the
    credential; it only removes stale temp files, as replace() does (sweep()).
    """
    auth_dir = AUTH_DIR if auth_dir is None else auth_dir
    uid = os.geteuid() if uid is None else uid
    name, _ = listed_file(record, auth_dir)
    try:
        directory = _auth_dir(auth_dir, uid)
    except OSError:
        raise ReplaceError('unreadable') from None
    try:
        sweep(directory, uid)
        _target(directory, name, uid)
        check_lock(directory, name, uid)
    finally:
        os.close(directory)


def sweep(directory, uid, now=None):
    """Remove every Codex credential's temp files left by a writer that died before renaming.

    Only `<codex name>.tmp.<24 hex>` regular files owned by uid and untouched for
    STALE_TEMP_SECONDS; a live write renames its temp file within seconds.
    """
    now = time.time() if now is None else now
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    for entry in entries:
        if not TEMP.fullmatch(entry):
            continue
        try:
            info = os.stat(entry, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == uid and now - info.st_mtime > STALE_TEMP_SECONDS:
                os.unlink(entry, dir_fd=directory)
        except OSError:
            pass


def _write(directory, name, temp, raw, read_at):
    """Create temp, re-check the target, then rename. Returns only after the rename."""
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            raise ReplaceError('changed') from None
        if fingerprint(info) != read_at:
            raise ReplaceError('changed')
        os.rename(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=directory)
        except OSError:
            pass
        raise


def replace(name, listed_path, overlay, check, auth_dir=None, uid=None,
            wait=LOCK_WAIT_SECONDS, clock=time.monotonic, sleep=time.sleep):
    """Overlay exactly TOKEN_FIELDS onto the credential, atomically, under its lock.

    check(current) validates the bytes read under the lock and raises to abort;
    it must not call the management API. Every other key keeps its value and type.
    Returning means the rename happened; raising means nothing was written.
    """
    auth_dir = AUTH_DIR if auth_dir is None else auth_dir
    uid = os.geteuid() if uid is None else uid
    target_name(name, listed_path, auth_dir)
    if set(overlay) != set(TOKEN_FIELDS) or not all(isinstance(overlay[k], str) and overlay[k] for k in TOKEN_FIELDS):
        raise ReplaceError('invalid_tokens')
    committed = False
    try:
        directory = _auth_dir(auth_dir, uid)
        try:
            sweep(directory, uid)
            with credential_lock(directory, name, wait, clock, sleep, uid):
                current, read_at = _target(directory, name, uid)
                check(current)
                updated = dict(current)
                updated.update({key: overlay[key] for key in TOKEN_FIELDS})
                raw = json.dumps(updated, separators=(',', ':'), allow_nan=False).encode()
                # Never end in .json: the watcher would load the temp file as an account.
                _write(directory, name, name + '.tmp.' + secrets.token_hex(12), raw, read_at)
                committed = True
                try:
                    os.fsync(directory)
                except OSError:
                    pass  # Already replaced; durability of the rename is best effort.
        finally:
            os.close(directory)
    except Exception:
        if not committed:
            raise
        # Unlock or close failed after the rename: the write stands, so report it.
