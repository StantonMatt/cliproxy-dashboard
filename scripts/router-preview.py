#!/usr/bin/env python3
"""Register an explicitly selected Linux loopback website for personal devices."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import time

CONFIG = Path('/etc/personal-codex-router/previews.json')
DENIED = {2375, 2376, 5432, 54321, 54322, 6379, 8317, 8320, 32825, 32826, 32827, 32828}


def add(config, name, port, hours=None, host=None, permanent=False):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', name):
        raise ValueError('Use a short lowercase project name with hyphens')
    if not 1024 <= port <= 65535 or port in DENIED or config['base_port'] <= port < config['base_port'] + 10:
        raise ValueError('Choose a development HTTP port, not a management or preview port')
    if permanent and hours is not None:
        raise ValueError('Choose either --permanent or --hours')
    host = host or f'localhost:{port}'
    if not re.fullmatch(r'(?:[a-z0-9-]+\.)*localhost(?::[0-9]{1,5})?', host):
        raise ValueError('Upstream Host must be localhost or a .localhost name')
    now = time.time()
    active = [p for p in config['previews'] if p.get('permanent') is True or (p.get('expires_at') or 0) > now]
    current = next((p for p in active if p['name'] == name), None)
    if current and (current['target'] != port or current['host'] != host):
        raise ValueError('Remove the existing preview before changing its target')
    if current and current.get('permanent') is True:
        if hours is not None:
            raise ValueError('Remove the permanent preview before choosing a duration')
        permanent = True
    if hours is not None and not 0 < hours <= 24:
        raise ValueError('Choose a duration above zero and at most 24 hours')
    used = {p['listen'] for p in active}
    # Stable slots are not reused until explicitly removed, even after expiry.
    used.update(p['listen'] for p in config['previews'])
    old = next((p for p in config['previews'] if p['name'] == name), None)
    slot = old['listen'] if old else next((p for p in range(config['base_port']+1, config['base_port']+9) if p not in used), None)
    if slot == config['base_port'] + 9:
        raise ValueError('This slot is reserved for the private dashboard')
    if slot is None:
        raise ValueError('All eight project slots are reserved; remove an old preview first')
    entry = {'name': name, 'target': port, 'host': host, 'listen': slot, 'expires_at': None if permanent else now + (4 if hours is None else hours) * 3600}
    if permanent: entry['permanent'] = True
    config['previews'] = [p for p in config['previews'] if p['name'] != name] + [entry]
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list')
    register = commands.add_parser('add')
    register.add_argument('name')
    register.add_argument('port', type=int)
    duration = register.add_mutually_exclusive_group()
    duration.add_argument('--hours', type=float)
    duration.add_argument('--permanent', action='store_true')
    register.add_argument('--host')
    remove = commands.add_parser('remove')
    remove.add_argument('name')
    args = parser.parse_args()
    try:
        with CONFIG.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            config = json.loads(CONFIG.read_text())
            if args.command == 'add':
                entry = add(config, args.name, args.port, args.hours, args.host, args.permanent)
                print(f"https://{config.get('tls_hostname', config['dns'])}:{entry['listen']}/")
            elif args.command == 'remove':
                config['previews'] = [p for p in config['previews'] if p['name'] != args.name]
            else:
                print(f"https://{config.get('tls_hostname', config['dns'])}:{config['base_port']}/")
                for p in config['previews']:
                    print(f"{p['name']}: https://{config.get('tls_hostname', config['dns'])}:{p['listen']}/ ({'permanent' if p.get('permanent') is True else 'active' if (p.get('expires_at') or 0) > time.time() else 'expired'})")
                return
            tmp = CONFIG.with_suffix('.tmp')
            with tmp.open('w') as output:
                os.fchmod(output.fileno(), 0o644)
                json.dump(config, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            tmp.replace(CONFIG)
    except (OSError, ValueError) as error:
        print('Preview update failed: ' + (str(error) if isinstance(error, ValueError) else 'run with sudo; check configuration'))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
