"""Identity-bound routing and disconnect operations with private, durable backups."""
import datetime as dt
import os
from pathlib import Path
import secrets
import re
import shutil
import stat
import time
from urllib.parse import urlencode

from account_add import AccountError, confirmation_text, gateway_compat
import account_access
import credential_replace
import routing_policy
from request_deadline import RequestDeadline, LOCK_WAIT_SECONDS

BACKUP_DIR = Path('/var/lib/personal-codex-router/disconnected')


def resolve(rows, key, auth_index):
    matches = [r for r in rows if r.get('name') == key]
    if len(matches) != 1 or matches[0].get('auth_index') != auth_index:
        raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
    return matches[0]


def binding(row):
    return tuple(row.get(k) for k in ('name', 'auth_index', 'type', 'email', 'label', 'path', 'source', 'runtime_only'))


def confirm_disconnect(record, confirm_text):
    expected = confirmation_text(record)
    if (not isinstance(confirm_text, str) or not isinstance(expected, str)
            or not expected.strip() or confirm_text.strip().casefold() != expected.strip().casefold()):
        raise AccountError('confirm_mismatch', 'Type the confirmation text to disconnect this account.')


def credential_source(record):
    name = record.get('name')
    source = record.get('path')
    if (record.get('source') != 'file' or record.get('runtime_only') is True
            or not isinstance(name, str) or len(name) > 200
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9@._+-]*\.json', name) or '..' in name
            or source != str(Path(credential_replace.AUTH_DIR) / name)):
        raise ValueError('Listed credential file unavailable')
    return source


def backup_file(record):
    source = credential_source(record)
    directory = BACKUP_DIR
    if (directory.is_symlink() or not directory.is_dir()
            or Path(source).parent.resolve() == directory.resolve()
            or Path(source).parent.resolve() in directory.resolve().parents):
        raise ValueError('Private backup directory unavailable')
    info = directory.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Private backup directory unavailable')
    # Direct file opens never follow a credential symlink. Refuse virtual/external files.
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as original:
        before = os.fstat(original.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
            raise ValueError('Credential file unavailable')
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        target = directory / (stamp + '-' + secrets.token_hex(6) + '-' + record['name'])
        output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(output, 'wb') as saved:
            os.fchmod(saved.fileno(), 0o600)
            shutil.copyfileobj(original, saved)
            saved.flush()
            os.fsync(saved.fileno())
        after = os.fstat(original.fileno())
        current = os.stat(source, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(current):
            raise AccountError('account_changed', 'The account changed during backup. Refresh and try again.', 409)
    parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return source, signature(current)


def remove_locked(record, expected, deadline):
    """Only Codex writers share this lock. Never call management while holding it."""
    credential_source(record)
    directory = credential_replace._auth_dir(credential_replace.AUTH_DIR, os.geteuid())
    try:
        with credential_replace.credential_lock(directory, record['name'],
                wait=deadline.timeout(LOCK_WAIT_SECONDS)):
            # _target also refuses SIWC, which has additional gateway-owned commit gates.
            _, actual = credential_replace._target(directory, record['name'], os.geteuid())
            if actual != expected:
                raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
            _, saved = backup_file(record)
            actual = credential_replace.fingerprint(os.stat(record['name'], dir_fd=directory, follow_symlinks=False))
            if actual != expected or saved != expected:
                raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
            deadline.timeout(LOCK_WAIT_SECONDS)
            os.unlink(record['name'], dir_fd=directory)
            os.fsync(directory)
    finally:
        os.close(directory)


def confirm_removed(call, key, auth_index, source, deadline):
    # The watcher processes local removals asynchronously. An absent disk file alone
    # says nothing about runtime routing; check both inventory and the model registry.
    for attempt in range(10):
        rows = call('/auth-files').get('files', [])
        models = call('/auth-files/models?' + urlencode({'name': key})).get('models')
        if (not os.path.lexists(source) and isinstance(models, list) and not models
                and not any(r.get('name') == key or r.get('auth_index') == auth_index
                            or r.get('path') == source for r in rows)):
            return
        if attempt < 9:
            time.sleep(min(.1, deadline.timeout(.1)))
    raise AccountError('unconfirmed', 'Disconnect could not be confirmed. Refresh before retrying.', 503)


def remove_managed(call, record):
    """Other providers have no shared writer lock; retry an unstable backup once."""
    for attempt in range(2):
        current = resolve(call('/auth-files').get('files', []), record['name'], record['auth_index'])
        if binding(record) != binding(current):
            raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
        try:
            source = credential_source(current)
            before = os.stat(source, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Credential file unavailable')
            _, saved = backup_file(current)
            after = credential_replace.fingerprint(os.stat(source, follow_symlinks=False))
            if credential_replace.fingerprint(before) != saved or saved != after:
                raise AccountError('account_changed', 'The account changed during backup. Refresh and try again.', 409)
        except FileNotFoundError:
            if attempt == 0:
                continue
            raise AccountError('account_changed', 'The account changed during backup. Refresh and try again.', 409) from None
        except AccountError as error:
            if error.code == 'account_changed' and attempt == 0:
                continue
            raise
        except (ValueError, OSError):
            raise AccountError('disconnect_unsafe',
                'The gateway credential cannot be safely disconnected. Refresh before retrying.', 409) from None
        # A gateway refresh can still land between this check and DELETE. Filenames
        # bind the identity, but restoring the backup may then need a new sign-in.
        call('/auth-files?' + urlencode({'name': current['name']}), method='DELETE')
        return source


class AccountMutations:
    def __init__(self, call, lifecycle_lock, changed, ready):
        self.call, self.lock, self.changed, self.ready = call, lifecycle_lock, changed, ready

    def apply(self, key, auth_index, enabled=None, disconnect=False, deadline=None, confirm_text=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(self.call)
        with deadline.lock(self.lock):
            rows = call('/auth-files').get('files', [])
            record = resolve(rows, key, auth_index)
            if disconnect:
                confirm_disconnect(record, confirm_text)
            provider = record.get('type')
            if not isinstance(provider, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', provider):
                raise AccountError('account_changed', 'The account provider is unavailable.', 409)
            if not disconnect and provider in ('codex', 'claude'):
                raise AccountError('invalid_provider', 'Use the existing provider routing control.')
            with routing_policy.controller_flock(timeout=deadline.timeout(LOCK_WAIT_SECONDS),
                                                  state_dir=routing_policy.state_path(provider)):
                rows = call('/auth-files').get('files', [])
                current = resolve(rows, key, auth_index)
                if binding(record) != binding(current):
                    raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
                if disconnect:
                    confirm_disconnect(current, confirm_text)
                    if not gateway_compat(getattr(call('/config'), 'version', None))['supported']:
                        raise AccountError('disconnect_unsafe',
                            'Safe disconnect is unavailable on this gateway version.', 409)
                    last_ready = not self.ready(rows, provider, key, call)
                    if provider == 'codex':
                        source = credential_source(current)
                        signature = credential_replace.fingerprint(os.stat(source, follow_symlinks=False))
                        # Bracket the fresh identity read with disk stamps. The second
                        # check, backup and unlink all happen under the gateway file lock.
                        latest = resolve(call('/auth-files').get('files', []), key, auth_index)
                        if binding(current) != binding(latest):
                            raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
                        try:
                            remove_locked(current, signature, deadline)
                        except credential_replace.ReplaceError:
                            raise AccountError('disconnect_unsafe',
                                'The gateway credential cannot be safely disconnected. Refresh before retrying.', 409) from None
                    else:
                        source = remove_managed(call, current)
                    confirm_removed(call, key, auth_index, source, deadline)
                    result = dict(disconnected=True, key=key, last_ready=last_ready)
                else:
                    call('/auth-files/status', {'name': key, 'auth_index': auth_index, 'disabled': not enabled}, method='PATCH')
                    actual = resolve(call('/auth-files').get('files', []), key, auth_index)
                    if binding(current) != binding(actual) or not account_access.runtime_matches(actual, {'disabled': not enabled}):
                        raise AccountError('unconfirmed', 'The routing change could not be confirmed.', 503)
                    result = dict(key=key, enabled=enabled)
        try:
            self.changed()
        except Exception:
            pass  # The verified mutation stands even if the normal refresh cannot start.
        return result
