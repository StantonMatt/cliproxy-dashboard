#!/usr/bin/env python3
"""Load the installed plugin in a transient gateway with the live unit's sandbox.

Only the transient unit and its own StateDirectory/RuntimeDirectory are mutable.
No production config or credentials are read. Requires passwordless sudo.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import urllib.request

PROPERTIES = ('User', 'Group', 'UMask', 'NoNewPrivileges', 'PrivateTmp', 'ProtectSystem', 'ProtectHome',
              'ProtectKernelTunables', 'ProtectKernelModules', 'ProtectControlGroups', 'RestrictSUIDSGID',
              'LockPersonality', 'CapabilityBoundingSet', 'RestrictAddressFamilies', 'SystemCallArchitectures')
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sudo(*args, check=True, input=None):
    result = subprocess.run(['sudo', '-n', *map(str, args)], input=input, text=True,
                            capture_output=True, timeout=30, check=check)
    return result.stdout.strip()


def properties(unit):
    raw = sudo('systemctl', 'show', unit, *['-p' + p for p in PROPERTIES])
    return dict(line.split('=', 1) for line in raw.splitlines())


class Reject(BaseHTTPRequestHandler):
    def do_CONNECT(self):
        self.send_error(403)

    def do_GET(self):
        self.send_error(403)

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    tag = 'pcr-plugin-' + secrets.token_hex(5)
    root = Path('/var/lib') / tag
    unit = tag + '.service'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Reject)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    assert port != 8317 and server.server_port != 8317
    expected = properties('cliproxyapi')
    result = {'unit': unit, 'gateway_port': port, 'mock_port': server.server_port,
              'binary': str(args.binary), 'sandbox': expected}
    started = False
    try:
        sudo('install', '-d', '-o', 'cliproxyapi', '-g', 'cliproxyapi', '-m', '0700', root, root / 'auth')
        cfg = {'config-version': 8, 'server': {'host': '127.0.0.1', 'port': port},
               'management': {'allow-remote': False, 'secret-key': 'sandbox-synthetic-management',
                              'disable-control-panel': True, 'disable-auto-update-panel': True},
               'access': {'api-keys': ['sandbox-synthetic-client']}, 'oauth': {'auth-dir': str(root / 'auth')},
               'requests': {'proxy-url': f'http://127.0.0.1:{server.server_port}'},
               'plugins': {'enabled': True, 'dir': str(args.library.parent), 'configs': {
                   'fast-mode-policy': {'enabled': True, 'priority': 100, 'policy_path': str(root / 'policy.json'),
                                        'status_path': str(root / 'status.json'), 'store': {'version': '0.1.2'},
                                        'map_unsupported_ultrafast': False, 'audit': True}}}}
        sudo('tee', root / 'config.yaml', input=json.dumps(cfg))
        sudo('tee', root / 'policy.json', input=json.dumps({'version': 1, 'files': {}}))
        sudo('chown', '-R', 'cliproxyapi:cliproxyapi', root)
        command = ['systemd-run', '--quiet', '--collect', '--unit=' + unit]
        for name, value in expected.items():
            command += ['-p', name + '=' + value]
        for prop in (f'StateDirectory={tag}', 'StateDirectoryMode=0700', f'RuntimeDirectory={tag}',
                     'RuntimeDirectoryMode=0700', f'WorkingDirectory={root}', 'RuntimeMaxSec=90',
                     'IPAddressDeny=any', 'IPAddressAllow=localhost'):
            command += ['-p', prop]
        command += ['--setenv=HTTP_PROXY=http://127.0.0.1:' + str(server.server_port),
                    '--setenv=HTTPS_PROXY=http://127.0.0.1:' + str(server.server_port),
                    '--setenv=NO_PROXY=127.0.0.1,localhost', str(args.binary), '-local-model', '-config', str(root / 'config.yaml')]
        sudo(*command)
        started = True
        pid = int(sudo('systemctl', 'show', unit, '-pMainPID', '--value'))
        result['pid'] = pid
        print(f'Started transient {unit}, gateway PID {pid}, port {port}; rejecting mock port {server.server_port}', flush=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                marker = json.loads(sudo('cat', root / 'status.json'))
                if marker.get('pid') == pid:
                    break
            except (subprocess.CalledProcessError, ValueError):
                pass
            time.sleep(.1)
        else:
            raise AssertionError('No live plugin marker in gateway sandbox')
        actual = properties(unit)
        assert actual == expected, {'expected': expected, 'actual': actual}
        assert marker['plugin'] == 'fast-mode-policy' and marker['version'] == '0.1.2'
        maps = sudo('cat', f'/proc/{pid}/maps')
        shadow = sorted({line.split()[-1] for line in maps.splitlines()
                         if '/cliproxy-pluginhost/' in line and '.so' in line})
        assert shadow, 'No content-addressed plugin mapped from private temp'
        assert sudo('readlink', f'/proc/{pid}/ns/mnt') != sudo('readlink', '/proc/1/ns/mnt')
        req = urllib.request.Request(f'http://127.0.0.1:{port}/v0/management/config',
                                     headers={'Authorization': 'Bearer sandbox-synthetic-management'})
        while time.monotonic() < deadline:
            try:
                with HTTP.open(req, timeout=5) as response:
                    result['version'] = response.headers['X-CPA-VERSION']
                break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError('Sandbox gateway HTTP listener did not become ready')
        result.update(ok=True, marker_current_pid=True, shadow_mappings=shadow, equivalent_sandbox=True)
        print('PASS equivalent sandbox, current-PID plugin marker and content-addressed private-temp mapping', flush=True)
    finally:
        if started:
            sudo('systemctl', 'stop', unit, check=False)
            sudo('systemctl', 'reset-failed', unit, check=False)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        sudo('rm', '-rf', '--', root)
        result['cleanup'] = {'unit_stopped': sudo('systemctl', 'is-active', unit, check=False) != 'active',
                             'state_directory_removed': not root.exists(),
                             'mock_stopped': not thread.is_alive()}
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(f'Stopped {unit}; removed {root}; mock stopped', flush=True)


if __name__ == '__main__':
    main()
