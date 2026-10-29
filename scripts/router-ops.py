#!/usr/bin/env python3
"""Read-only daemon probe and local desktop delivery of dashboard notices."""
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import subprocess
import time
from urllib.request import Request, urlopen

STATE = Path('/var/lib/personal-codex-router-ops')
USER = 'user'
HOME = Path('/home') / USER
COLLECTOR_STATUS = Path('/var/lib/personal-codex-router/collector-status.json')


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def unit_status(unit, properties):
    try:
        result = subprocess.run(['systemctl', 'show', unit, '--no-pager',
                                 '--property=' + ','.join(properties)],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 0:
            return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {}


def usage_health(previous, status_path=COLLECTOR_STATUS, now=None):
    now = time.time() if now is None else now
    previous = previous if isinstance(previous, dict) else {}
    service = unit_status('personal-codex-router-usage.service', (
        'LoadState', 'ActiveState', 'Result', 'ExecMainStatus',
        'ExecMainExitTimestampMonotonic', 'ExecMainStartTimestampMonotonic'))
    timer = unit_status('personal-codex-router-usage.timer', ('LoadState', 'ActiveState'))
    data = {'last_success_at': previous.get('last_success_at'),
            'last_run_failed': previous.get('last_run_failed', False),
            'unit_available': service.get('LoadState') == 'loaded',
            'timer_active': (timer.get('ActiveState') == 'active'
                             if timer.get('LoadState') == 'loaded' else None)}
    if data['unit_available'] and service.get('ActiveState') != 'activating':
        result, status = service.get('Result'), service.get('ExecMainStatus')
        if result and status is not None:
            data['last_run_failed'] = result != 'success' or status != '0'
            try:
                exited = int(service.get('ExecMainExitTimestampMonotonic', '0')) / 1_000_000
                started = int(service.get('ExecMainStartTimestampMonotonic', '0')) / 1_000_000
                elapsed = time.monotonic() - exited
                if not data['last_run_failed'] and exited >= started > 0 and elapsed >= 0:
                    data['last_success_at'] = dt.datetime.fromtimestamp(now - elapsed, dt.timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
    # The collector publishes aggregates; the sandbox never opens SQLite or spools.
    data['failed_spools'] = None
    data['quarantined_records'] = None
    data['quarantine_increased'] = False
    data['last_run_at'] = None
    data['collector_status_error'] = None
    try:
        status = json.loads(status_path.read_text())
        if (not isinstance(status, dict)
                or any(type(status.get(k)) is not int or status[k] < 0
                       for k in ('quarantined_total', 'failed_spools'))):
            raise ValueError('Invalid collector counts')
        for key in ('last_success_at', 'last_run_at'):
            if key not in status:
                raise ValueError('Missing collector timestamp')
            value = status.get(key)
            if value is None and key == 'last_success_at':
                continue
            if not isinstance(value, str) or dt.datetime.fromisoformat(value).tzinfo is None:
                raise ValueError('Invalid collector timestamp')
        count = status['quarantined_total']
        data['failed_spools'] = status['failed_spools']
        data['last_success_at'] = status['last_success_at']
        data['last_run_at'] = status['last_run_at']
        data['quarantined_records'] = count
        before = previous.get('quarantined_records')
        data['quarantine_increased'] = type(before) is int and count > before
    except OSError:
        data['collector_status_error'] = 'unavailable'
    except (ValueError, UnicodeError):
        data['collector_status_error'] = 'invalid'
    return data


def write(path, data, mode=0o640):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.fchmod(f.fileno(), mode)
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def daemon_health():
    try:
        result = subprocess.run([
            '/usr/sbin/runuser', '-u', USER, '--', '/usr/bin/env',
            'CODEX_HOME=' + str(HOME / '.codex'),
            str(HOME / '.local/bin/codex'), 'app-server', 'daemon', 'version'],
            capture_output=True, timeout=8, check=False)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        status = data.get('status')
        return {'status': 'healthy' if status == 'running' else
                'down' if status in ('stopped', 'notRunning', 'notInstalled') else 'unknown'}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'status': 'unknown'}


def deliver():
    # One canonical outbox survives dashboard restarts and browser reloads.
    req = Request('http://127.0.0.1:8320/api/overview', headers={'X-Router-Request': '1'})
    try:
        with urlopen(req, timeout=5) as response:
            notices = json.load(response).get('notifications', {})
        if not notices.get('available'):
            return
        path = STATE / 'desktop-delivery.json'
        delivered = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(delivered, dict):
            return
        uid = pwd.getpwnam(USER).pw_uid
        for item in notices.get('items', []):
            if item['id'] in delivered:
                continue
            urgency = item.get('urgency') if item.get('urgency') in ('low', 'normal', 'critical') else 'normal'
            result = subprocess.run([
                '/usr/sbin/runuser', '-u', USER, '--', '/usr/bin/env',
                f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus',
                '/usr/bin/notify-send', '--app-name=Personal Router', '--urgency=' + urgency,
                '--expire-time=15000', '--', 'Personal Router · ' + item['email'], item['message']],
                capture_output=True, timeout=5, check=False)
            if result.returncode == 0:
                delivered[item['id']] = dt.datetime.now(dt.timezone.utc).isoformat()
                write(path, delivered, 0o600)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
        kept = {k: v for k, v in delivered.items() if dt.datetime.fromisoformat(v) >= cutoff}
        if kept != delivered:
            write(path, kept, 0o600)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        # No desktop session is normal; the durable dashboard inbox remains.
        return


def main():
    os.umask(0o027)
    previous = read(STATE / 'health.json')
    write(STATE / 'health.json', {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                                'codex': daemon_health(),
                                'usage_collection': usage_health(previous.get('usage_collection'))})
    deliver()


if __name__ == '__main__':
    main()
