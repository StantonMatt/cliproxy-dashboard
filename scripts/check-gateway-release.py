#!/usr/bin/env python3
"""Check public CLIProxyAPI releases anonymously. Never download or install code."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Newest first; 30 releases (~0.8 MB with notes) always include the latest stable one and stay under the size cap.
API = 'https://api.github.com/repos/router-for-me/CLIProxyAPI/releases?per_page=30'
STATE = Path('/var/lib/personal-codex-router/gateway-release.json')


def version(value):
    match = re.fullmatch(r'v?(\d{1,9})\.(\d{1,9})\.(\d{1,9})', value) if isinstance(value, str) else None
    return tuple(map(int, match.groups())) if match else None


def load(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.gateway-release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check(path=STATE, *, now=None):
    now = time.time() if now is None else now
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = load(path)
        retry = previous.get('retry_after')
        if type(retry) in (int, float) and retry > now:
            return False
        state = {key: previous[key] for key in ('latest_version', 'release_url', 'etag', 'checked_at') if key in previous}
        state.update(version=1, attempted_at=dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat())
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'personal-codex-router-release-check',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if isinstance(state.get('etag'), str):
            headers['If-None-Match'] = state['etag']
        try:
            with urlopen(Request(API, headers=headers), timeout=10) as response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise ValueError('Release response too large')
                releases = json.loads(payload)
                if not isinstance(releases, list):
                    raise ValueError('Invalid release response')
                stable = [row for row in releases if isinstance(row, dict)
                          and row.get('draft') is False and row.get('prerelease') is False
                          and version(row.get('tag_name')) is not None]
                if not stable:
                    raise ValueError('No stable version found')
                latest = max(stable, key=lambda row: version(row['tag_name']))['tag_name'].removeprefix('v')
                state.update(latest_version=latest,
                             release_url='https://github.com/router-for-me/CLIProxyAPI/releases/tag/v' + latest)
                state.pop('etag', None)
                if response.headers.get('ETag'):
                    state['etag'] = response.headers['ETag']
            state.update(status='ok', checked_at=state['attempted_at'])
        except HTTPError as error:
            if error.code == 304 and version(state.get('latest_version')) is not None:
                state.update(status='ok', checked_at=state['attempted_at'])
            else:
                limited = error.code in (403, 429)
                state.update(status='error', error='rate_limited' if limited else 'http_error')
                if limited:
                    # Honor both GitHub's primary quota reset and Retry-After;
                    # bound untrusted headers and avoid an immediate retry loop.
                    delays = [now + 3600]
                    for name, relative in (('Retry-After', True), ('X-RateLimit-Reset', False)):
                        try:
                            value = int(error.headers.get(name, ''))
                            delays.append(now + value if relative else value)
                        except (ValueError, TypeError):
                            pass
                    state['retry_after'] = min(now + 7 * 86400, max(delays))
        except (OSError, URLError, ValueError, TypeError):
            state.update(status='error', error='unavailable')
        save(path, state)
        return state['status'] == 'ok'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=STATE)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        ok = check(args.state)
    except (OSError, ValueError):
        ok = False
    print('Gateway release check completed.' if ok else 'Gateway release check unavailable; previous observation retained.')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
