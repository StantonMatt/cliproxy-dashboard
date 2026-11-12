#!/usr/bin/env python3
"""Release CLIProxyAPI session-affinity bindings through a guarded config flip.

The dashboard creates one empty request file, as ``cliproxyapi``, in its own
group-accessible runtime directory.  This program runs from a root systemd path
service with no capabilities, so group access is what lets it remove the
request.  It never reads the request: only the cliproxyapi user, members of
the cliproxyapi group and root can create entries in the 0770 directory, a
stray entry can at most cause one TTL flip per minute, and the result names the
request by its inode.
"""
import argparse
import copy
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

# Imported by load_config_modules() only after the request is taken, so a loaded
# machine still takes it well inside the dashboard's 2 s start window.
yaml = gateway_config = None

CONFIG = Path('/etc/cliproxyapi/config.yaml')
REQUEST = Path('/run/personal-codex-router-move-request/request')
STATE = Path('/var/lib/personal-codex-router-move')
RUNTIME = Path('/run/personal-codex-router-move')
CONFIG_GROUP = 'cliproxyapi'
# Every value parses to a different duration from its successor.  After the
# first move both values have the same length, so rewrites never shrink.
NEXT_TTL = {'1h': '1h0m1s', '1h0m0s': '1h0m1s', '1h0m1s': '1h0m0s'}
MIN_INTERVAL = 60.0
MAX_REQUEST_AGE = 15.0
BACKUPS_KEPT = 5


class Outcome(Exception):
    """A finished move attempt with a stable reason ``code`` for the dashboard."""
    outcome = 'failed'

    def __init__(self, code, **evidence):
        super().__init__(code)
        self.code = code
        self.evidence = evidence


class Refused(Outcome):
    """Nothing was written."""
    outcome = 'refused'


class Failed(Outcome):
    """The attempt failed before anything was written."""
    outcome = 'failed'


class Uncertain(Outcome):
    """The write began and a later step failed; the gateway may have reloaded."""
    outcome = 'uncertain'


def _stamp(now=None):
    return dt.datetime.fromtimestamp(time.time() if now is None else now,
                                    dt.timezone.utc).isoformat()


def _owner_gid(name=CONFIG_GROUP):
    import grp
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def load_config_modules():
    global yaml, gateway_config
    if gateway_config is not None:
        return
    import yaml as yaml_module
    try:
        import gateway_config as module
    except ModuleNotFoundError:  # Installed beside this script under /opt.
        import importlib.util
        spec = importlib.util.spec_from_file_location('gateway_config', Path(__file__).with_name('gateway_config.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    yaml, gateway_config = yaml_module, module


class NoRequest(Exception):
    """The request was gone before it could be taken; there is nothing to report."""


def read_request(path=REQUEST, now=None, max_age=MAX_REQUEST_AGE):
    """Take the request entry, whatever it is, and return its inode.

    The entry is always removed, even when it is refused: a request that stays
    would retrigger the path unit until its start limit fails it, so a removal
    failure replaces any refusal.  Whoever removes the entry owns it, so one the
    dashboard has already withdrawn is never acted on.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise NoRequest() from None
    except OSError:
        raise Failed('consume_failed') from None
    # A future ctime is as untrustworthy as an old one after a clock step.
    stale = abs((time.time() if now is None else now) - info.st_ctime) > max_age
    try:
        if stat.S_ISDIR(info.st_mode):
            os.rmdir(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        raise NoRequest() from None
    except OSError:
        raise Failed('consume_failed', request_inode=info.st_ino) from None
    if stale:
        raise Refused('stale_request', request_inode=info.st_ino)
    return info.st_ino


def _read_config(config_path, required_uid, required_gid):
    try:
        fd = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise Refused('config_symlink' if error.errno == errno.ELOOP else 'config_unavailable') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        group = _owner_gid() if required_gid is None else required_gid
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != required_uid or group is None
                or info.st_gid != group or stat.S_IMODE(info.st_mode) not in (0o640, 0o660)):
            raise Refused('config_unsafe')
        return info, stream.read()


def _last_flip(state_dir):
    try:
        return float((state_dir / 'last-flip').read_text().strip())
    except (OSError, ValueError):
        return None


def preflight(config_path=CONFIG, state_dir=STATE, min_interval=MIN_INTERVAL, now=None,
              required_uid=0, required_gid=None):
    """Return the current config and its metadata, or raise ``Refused``."""
    load_config_modules()
    info, raw = _read_config(config_path, required_uid, required_gid)
    try:
        mapping = yaml.safe_load(raw.decode())
        ttl = gateway_config.get(mapping, 'session-affinity-ttl')
        affinity = gateway_config.get(mapping, 'session-affinity')
    except (UnicodeError, ValueError, yaml.YAMLError):
        raise Refused('config_unparsed') from None
    if not isinstance(ttl, str) or ttl not in NEXT_TTL:
        raise Refused('unsupported_ttl')
    if affinity is not True:
        raise Refused('affinity_disabled')
    last = _last_flip(state_dir)
    current = time.time() if now is None else now
    # A last flip in the future means the clock stepped back; treat it as stale.
    if last is not None and 0 <= current - last < min_interval:
        raise Refused('too_soon')
    return {'ttl': ttl, 'mapping': mapping, 'raw': raw, 'inode': info.st_ino,
            'owner': (info.st_uid, info.st_gid), 'mode': stat.S_IMODE(info.st_mode)}


def _only_ttl_changed(before, after):
    before = copy.deepcopy(before)
    after = copy.deepcopy(after)
    path = gateway_config.setting_path(before, 'session-affinity-ttl')
    left, right = before, after
    for key in path[:-1]:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        left, right = left.get(key), right.get(key)
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    old, new = left.get(path[-1]), right.get(path[-1])
    right[path[-1]] = old
    return before == after and old != new


def write_last_flip(state_dir=STATE, now=None):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / 'last-flip'
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(str(time.time() if now is None else now) + '\n')
    temp.chmod(0o600)
    os.replace(temp, path)


def _prune_backups(backups, keep):
    """Keep ``keep`` and the newest others, up to ``BACKUPS_KEPT`` in all."""
    entries = []
    for path in backups.glob('config-*.yaml'):
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and path != keep:
            entries.append((info.st_mtime_ns, path.name, path))
    # The new backup is always kept, even if a clock step makes it look oldest.
    for _, _, path in sorted(entries, reverse=True)[BACKUPS_KEPT - 1:]:
        try:
            path.unlink()
        except OSError as error:
            print(f'move-conversations: could not prune a backup: {type(error).__name__}', file=sys.stderr)


def write_backup(state_dir, original, inode):
    backups = state_dir / 'backups'
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backups, 0o700, follow_symlinks=False)
    target = backups / f'config-{time.time_ns():020d}-{inode}.yaml'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    _prune_backups(backups, target)
    return target


def _read_bytes(path):
    with open(path, 'rb') as stream:
        return stream.read()


def _restore(config_path, original):
    """Best effort: put the original bytes back on the same inode."""
    try:
        with open(config_path, 'r+b') as stream:
            stream.write(original)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        return False
    return True


def flip(config_path=CONFIG, state_dir=STATE, min_interval=MIN_INTERVAL, now=None,
         required_uid=0, required_gid=None):
    """Flip the scalar in place and return evidence suitable for the result file."""
    checked = preflight(config_path, state_dir, min_interval, now, required_uid, required_gid)
    original = checked['raw']
    next_ttl = NEXT_TTL[checked['ttl']]
    try:
        candidate = gateway_config.validated_edit(original, {'session-affinity-ttl': next_ttl})
        after_mapping = yaml.safe_load(candidate.decode())
    except (UnicodeError, ValueError, yaml.YAMLError):
        raise Refused('edit_unsafe') from None
    if not _only_ttl_changed(checked['mapping'], after_mapping):
        raise Refused('edit_unsafe')
    # An in-place shrink would expose stale trailing bytes until the truncate.
    if len(candidate) < len(original):
        raise Refused('edit_shrinks')
    try:
        write_backup(state_dir, original, checked['inode'])
    except OSError:
        raise Failed('backup_failed') from None
    evidence = {'value': next_ttl, 'inode_before': checked['inode']}
    try:
        gateway_config.write_same_inode(config_path, candidate, original)
    except ValueError:
        raise Refused('config_changed') from None
    except Exception:
        try:
            unchanged = _read_bytes(config_path) == original
        except OSError:
            unchanged = False
        if unchanged:
            raise Failed('write_failed') from None
        code = 'write_interrupted_restored' if _restore(config_path, original) else 'write_interrupted'
        raise Uncertain(code, **evidence) from None
    try:
        after = os.stat(config_path)
        data = _read_bytes(config_path)
    except OSError:
        raise Uncertain('readback_failed', **evidence) from None
    evidence.update(inode_after=after.st_ino, sha256=hashlib.sha256(data).hexdigest())
    if ((after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) !=
            (checked['inode'], *checked['owner'], checked['mode'])):
        raise Uncertain('config_metadata_changed', **evidence)
    if data != candidate:
        raise Uncertain('readback_mismatch', **evidence)
    return evidence


def write_result(result, runtime_dir=RUNTIME):
    runtime_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    runtime_dir.chmod(0o750)
    target = runtime_dir / 'result.json'
    temp = runtime_dir / '.result.json.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o640)
        json.dump(result, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)


def handle(request_path=REQUEST, config_path=CONFIG, state_dir=STATE, runtime_dir=RUNTIME,
           min_interval=MIN_INTERVAL, now=None, required_uid=0, required_gid=None):
    """Take the request, flip the TTL and write the result; None when there was no request."""
    result = {'request_inode': None, 'outcome': 'failed', 'code': 'error', 'value': None,
              'inode_before': None, 'inode_after': None, 'sha256': None, 'at': None}
    try:
        result['request_inode'] = read_request(request_path, now=now)
        load_config_modules()
        result.update(outcome='written', code='written',
                      **flip(config_path, state_dir, min_interval, now, required_uid, required_gid))
    except NoRequest:
        return None
    except Outcome as error:
        result.update(outcome=error.outcome, code=error.code, **error.evidence)
    except Exception:
        result.update(outcome='failed', code='error')
    if result['outcome'] in ('written', 'uncertain'):
        # The gateway may have reloaded either way, so the interval applies.
        try:
            write_last_flip(state_dir, now)
        except OSError:
            if result['outcome'] == 'written':
                result['code'] = 'last_flip_not_recorded'
    # Stamped only after the request was taken, so a run that started before the
    # dashboard created it still reports a time no earlier than the request.
    result['at'] = _stamp(now)
    write_result(result, runtime_dir)
    return result


def main():
    # systemd stops a timed-out run with SIGTERM; never let that interrupt the
    # same-inode write.  TimeoutStopSec bounds a genuine hang with SIGKILL.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, default=REQUEST)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--state-dir', type=Path, default=STATE)
    parser.add_argument('--runtime-dir', type=Path, default=RUNTIME)
    args = parser.parse_args()
    result = handle(args.request, args.config, args.state_dir, args.runtime_dir)
    print(json.dumps({'outcome': result['outcome'], 'code': result['code']} if result else
                     {'outcome': None, 'code': 'no_request'}))


if __name__ == '__main__':
    main()
