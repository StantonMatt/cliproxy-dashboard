#!/usr/bin/env python3
"""Mirror the desktop's account-filtered Codex catalog without losing offline use."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time

MAX_BYTES = 16 * 1024 * 1024
SOURCE_COMMAND = 'cat "$HOME/.local/share/codex-router-models/models.json"'


def visible_models(content):
    if len(content) > MAX_BYTES:
        raise ValueError('Catalog exceeds size limit')
    models = json.loads(content)['models']
    if not isinstance(models, list) or not models:
        raise ValueError('Empty or invalid catalog')
    slugs = set()
    visible = set()
    for model in models:
        slug = model['slug']
        if not isinstance(slug, str) or not slug or '/' in slug or slug in slugs:
            raise ValueError('Invalid or duplicate model identity')
        if model.get('visibility') not in ('list', 'hide'):
            raise ValueError('Invalid model visibility')
        slugs.add(slug)
        if model['visibility'] == 'list':
            visible.add(slug)
    if not visible:
        raise ValueError('No visible models')
    return visible


def fetch_catalog(host, port):
    if not host or host.startswith('-'):
        raise ValueError('Invalid SSH host')
    # No config, key or credential is transferred. Bound connection and total time.
    return subprocess.run(
        ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
         '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1',
         '-p', str(port), host, SOURCE_COMMAND],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=30
    ).stdout


def validate_catalog(path, expected, codex, codex_home):
    """Use the local CLI's schema and model/list, with no inference request."""
    process = subprocess.Popen(
        [str(codex), 'app-server', '--listen', 'stdio://',
         '-c', 'model_catalog_json=' + json.dumps(str(path))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=dict(os.environ, CODEX_HOME=str(codex_home), RUST_LOG='error'))
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)

    def send(message):
        process.stdin.write(json.dumps(message).encode() + b'\n')
        process.stdin.flush()

    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'mac_catalog_validation', 'version': '1.0'},
            'capabilities': {'experimentalApi': True}}})
        deadline = time.monotonic() + 25
        pending = b''
        while time.monotonic() < deadline:
            for key, _ in selector.select(min(1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    raise ValueError('Codex could not load catalog')
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    response = json.loads(line)
                    if response.get('id') == 1:
                        if response.get('error') or 'result' not in response:
                            raise ValueError('Codex initialization failed')
                        send({'method': 'initialized', 'params': {}})
                        send({'id': 2, 'method': 'model/list', 'params': {
                            'includeHidden': False, 'limit': 100}})
                    elif response.get('id') == 2:
                        result = response.get('result', {})
                        received = {model['id'] for model in result.get('data', [])}
                        if response.get('error') or result.get('nextCursor') or received != expected:
                            raise ValueError('Codex rejected or filtered catalog')
                        return
        raise TimeoutError('Codex catalog validation timed out')
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()


def refresh(directory, host, port, codex, codex_home):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        content = fetch_catalog(host, port)
        expected = visible_models(content)
        target = directory / 'models.json'
        if target.exists() and target.read_bytes() == content:
            print(f'Catalog unchanged: {len(expected)} visible models')
            return
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.models-', delete=False) as candidate:
                name = candidate.name
                candidate.write(content)
                candidate.flush()
                os.fsync(candidate.fileno())
            validate_catalog(Path(name), expected, codex, codex_home)
            os.replace(name, target)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
        print(f'Updated Codex catalog: {len(expected)} visible models')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--source-host', default='router')
    parser.add_argument('--ssh-port', type=int, default=22)
    parser.add_argument('--codex', type=Path, default=Path('/opt/homebrew/bin/codex'))
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.t3/codex-home')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        refresh(args.directory, args.source_host, args.ssh_port, args.codex, args.codex_home)
    except Exception as error:
        # Never log remote output, config, headers or exception text.
        print(f'Catalog refresh failed ({type(error).__name__}); keeping the previous catalog')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
