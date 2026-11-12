#!/usr/bin/env python3
"""Run the source checkout's tests through heavy; notify the desktop on failure."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import site_config

LOGS = Path.home() / '.cache/personal-codex-router/test-runs'
KEEP = 14


def prune_logs(directory, keep=KEEP):
    # Only remove this helper's own regular logs, leaving unrelated files alone.
    logs = sorted((p for p in directory.glob('tests-*.log') if p.is_file() and not p.is_symlink()),
                  key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    for path in logs[keep:]:
        path.unlink()


def notify(log):
    try:
        result = subprocess.run([
            '/usr/bin/gdbus', 'call', '--session', '--dest', 'org.freedesktop.Notifications',
            '--object-path', '/org/freedesktop/Notifications',
            '--method', 'org.freedesktop.Notifications.Notify',
            json.dumps('Personal Router'), '0', json.dumps(''), json.dumps('Personal Router tests failed'),
            json.dumps('See ' + str(log)), '[]', "{'urgency': <byte 1>}", '15000'],
            capture_output=True, timeout=10, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run(directory=LOGS):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    fd = os.open(directory / '.nightly.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A nightly test run is already active.')
            return 0
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S-')
        log_fd, name = tempfile.mkstemp(prefix='tests-' + stamp, suffix='.log', dir=directory)
        log = Path(name)
        with os.fdopen(log_fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            try:
                site = site_config.load()
                checkout = Path(site['source_checkout'])
                runner = checkout / 'scripts/run-tests.py'
                if not checkout.is_dir() or not runner.is_file() or not (checkout / 'tests').is_dir():
                    raise RuntimeError('Source checkout or test runner is missing')
                result = subprocess.run([str(Path.home() / '.local/bin/heavy'), sys.executable, str(runner)],
                                        cwd=checkout, stdout=stream, stderr=subprocess.STDOUT, check=False)
                code = result.returncode
            except (KeyError, OSError, RuntimeError):
                stream.write('Nightly tests could not start. Check site.json source_checkout and ~/.local/bin/heavy.\n')
                code = 1
            stream.flush()
            os.fsync(stream.fileno())
        prune_logs(directory)
        print(f'Nightly tests {"passed" if code == 0 else "failed"}; log: {log}')
        if code and not notify(log):
            print('Desktop notification unavailable; failure log retained.', file=sys.stderr)
        return 0 if code == 0 else 1


if __name__ == '__main__':
    os.umask(0o077)
    raise SystemExit(run())
