"""Capture dummy legacy/native v8 files; run explicitly, never during unit tests."""
import argparse
import bcrypt
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import yaml

BINARY_SHA256 = '3ee48dcfdf1d6aa352712ac47dde0a67bdfcaeeb790ea2a54ddd4fab37dfba51'
FIXTURES = Path(__file__).with_name('fixtures')


def capture(binary):
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == BINARY_SHA256
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix='router-config-fixture-') as directory, socket.socket() as closed:
        root = Path(directory)
        # Bound but never listening: connection refused, with no external forwarding.
        closed.bind(('127.0.0.1', 0))
        proxy = 'http://127.0.0.1:' + str(closed.getsockname()[1])
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        assert port != 8317 and closed.getsockname()[1] != 8317
        auth = root / 'auth'
        auth.mkdir(mode=0o700)
        for name in ('a', 'b'):
            (auth / (name + '.json')).write_text(json.dumps({
                'type': 'codex', 'email': name + '@example.invalid',
                'account_id': 'dummy-' + name, 'access_token': 'dummy-' + name,
                'refresh_token': '', 'expired': '2099-01-01T00:00:00Z',
                'proxy_url': proxy, 'disabled': False}))
        key = 'dummy-fixture-management-key'
        source = {
            'host': '127.0.0.1', 'port': port, 'auth-dir': str(auth),
            'api-keys': ['dummy-fixture-client-key'], 'proxy-url': proxy,
            'remote-management': {'allow-remote': False,
                'secret-key': bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode(),
                'disable-control-panel': True, 'disable-auto-update-panel': True},
            'usage-statistics-enabled': False, 'debug': False,
            'logging-to-file': False, 'request-log': False,
            'discovery': {'enabled': False}, 'pprof': {'enable': False},
            'request-retry': 0, 'max-retry-credentials': 2, 'max-retry-interval': 0,
            'disable-cooling': True, 'save-cooldown-status': True,
            'force-model-prefix': True,
            'routing': {'strategy': 'round-robin', 'session-affinity': True,
                'session-affinity-ttl': '1h', 'session-affinity-subagents': True},
            'quota-exceeded': {'switch-project': True, 'switch-preview-model': True,
                'antigravity-credits': True}}
        config = root / 'config.yaml'
        config.write_text(yaml.safe_dump(source, sort_keys=False))
        before = config.read_bytes()
        inode = config.stat().st_ino
        env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': str(root),
            'XDG_CONFIG_HOME': str(root), 'TMPDIR': str(root),
            'MANAGEMENT_STATIC_PATH': str(root / 'static')}
        env.update({name: proxy for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                            'http_proxy', 'https_proxy', 'all_proxy')})
        env.update(NO_PROXY='127.0.0.1,localhost,::1', no_proxy='127.0.0.1,localhost,::1')
        base = f'http://127.0.0.1:{port}'
        def call(method, data=None):
            request = urllib.request.Request(base + '/v8/management/config', method=method,
                data=json.dumps(data).encode() if data is not None else None,
                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            with client.open(request, timeout=2) as response:
                assert response.status == 200
                return json.load(response)
        with (root / 'process.log').open('wb') as log:
            process = subprocess.Popen([str(binary), '-config', str(config), '-local-model'],
                cwd=root, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('Isolated gateway exited')
                    try:
                        call('GET')
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Isolated gateway did not become ready')
                        time.sleep(.1)
                assert config.read_bytes() == before, 'Startup/read changed legacy bytes'
                patch = {'routing': {'retry': {'request-retry': 0}}}
                call('PATCH', patch)
                after = config.read_bytes()
                assert yaml.safe_load(after)['config-version'] == 8
                assert config.stat().st_ino == inode
                FIXTURES.mkdir(exist_ok=True)
                for name, raw in (('config-legacy.yaml', before), ('config-v8-nested.yaml', after)):
                    (FIXTURES / name).write_bytes(raw)
                (FIXTURES / 'config-provenance.json').write_text(json.dumps({
                    'binary_version': '8.0.7', 'binary_sha256': BINARY_SHA256,
                    'command': 'python3 -B tests/generate_config_fixtures.py',
                    'native_patch': patch, 'patch_count': 1,
                    'startup_and_read_preserved_bytes': True, 'same_inode': True,
                    'dummy_values_only': True, 'proxy': 'closed loopback port; environment and per-auth proxies',
                    'before_sha256': hashlib.sha256(before).hexdigest(),
                    'after_sha256': hashlib.sha256(after).hexdigest()}, indent=2) + '\n')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print('Captured legacy/native fixtures; isolated process stopped and temporary files removed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path('/opt/cliproxyapi/8.0.7/cli-proxy-api'))
    capture(parser.parse_args().binary)
