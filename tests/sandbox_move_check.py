#!/usr/bin/env python3
"""Prove the move helper works under its production unit's sandbox, end to end with the dashboard.

Runs scripts/move-conversations.py from transient systemd-run path and service units with the
production unit's properties, against a lab under /var/tmp/pcr-move-sandbox-*. The dashboard's
real move_conversations() runs as cliproxyapi against the same lab, with a fake gateway that
reads the lab config, so each move covers the request, the helper and the dashboard's readback.
It proves the helper's sandbox and the dashboard/helper protocol only: no real gateway runs, so
it says nothing about whether a gateway build releases its pins on a TTL change.

Lab substitutions, so nothing outside the lab is written: StateDirectory and RuntimeDirectory
(which would create /var/lib and /run directories) become lab directories added to
ReadWritePaths, ReadWritePaths points at the lab config and request directory, and
BindReadOnlyPaths exposes the lab through PrivateTmp. Never touches /etc/cliproxyapi, /run,
/var/lib or /opt, and never starts, stops or reloads a production unit. Stops and resets only
the transient units it created, then removes the lab. Needs passwordless sudo; prints SKIP
otherwise.

    python3 tests/sandbox_move_check.py
"""
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / 'scripts/personal-codex-router-move.service'
PATH_UNIT = ROOT / 'scripts/personal-codex-router-move.path'
PRODUCTION_DIR = '/opt/personal-codex-router'
PRODUCTION_PATHS = {'-/etc/cliproxyapi/config.yaml': 'config', '-/run/personal-codex-router-move-request': 'requests'}
# Directory properties that would create paths outside the lab; the lab provides their directories.
SUBSTITUTED = ('RuntimeDirectory', 'RuntimeDirectoryMode', 'RuntimeDirectoryPreserve', 'StateDirectory',
               'StateDirectoryMode')
PYTHON = '/usr/bin/python3'

# Runs as cliproxyapi: the dashboard's own move against the lab, with a gateway that reads the lab config.
DRIVER = '''
import json, re, sys, time
from pathlib import Path
lab, requests = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(lab / 'dashboard'))
import server
server.MOVE_REQUEST_DIR = requests
server.MOVE_RESULT = lab / 'runtime' / 'result.json'
server.MOVE_HELPER = lab / 'helper' / 'move-conversations.py'
def call(path, timeout=None):
    # The same response type as the dashboard's api(), carrying a build the move gate accepts.
    assert path == '/config'
    text = (lab / 'etc' / 'config.yaml').read_text()
    ttl = re.search(r'session-affinity-ttl: *"?([0-9hms]+)"?', text).group(1)
    return server.signin.ManagementResponse({'routing': {'session-affinity-ttl': ttl}}, server.MOVE_TESTED_GATEWAYS[0])
started = time.monotonic()
move = server.move_conversations(server.RequestDeadline(seconds=45), call=call)
print(json.dumps({'move': move, 'seconds': round(time.monotonic() - started, 2),
                  'left': sorted(p.name for p in requests.iterdir()), 'available': server.move_available(requests)}))
'''
CREATE = ('import os,sys; os.close(os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o640))')


class Lab:
    def __init__(self):
        self.tag = secrets.token_hex(4)
        self.root = Path('/var/tmp/pcr-move-sandbox-' + self.tag)
        self.etc, self.requests, self.helper = self.root / 'etc', self.root / 'requests', self.root / 'helper'
        self.state, self.runtime, self.tools = self.root / 'state', self.root / 'runtime', self.root / 'tools'
        self.config, self.result_path = self.etc / 'config.yaml', self.runtime / 'result.json'
        self.units = []
        self.failures = 0
        self.substituted = []

    def sh(self, *args, check=True, user=None, input=None):
        command = ['sudo', '-n'] + (['-u', user] if user else []) + [str(arg) for arg in args]
        out = subprocess.run(command, capture_output=True, text=True, timeout=120, input=input, cwd='/')
        if check and out.returncode:
            raise RuntimeError(f'{args[0]} exited {out.returncode}: {out.stderr.strip()[-2000:]}')
        return out.stdout.strip()

    def check(self, label, condition, detail=''):
        print(('PASS ' if condition else 'FAIL ') + label + (f' :: {detail}' if detail else ''), flush=True)
        self.failures += not condition

    def setup(self):
        self.sh('install', '-d', '-o', 'root', '-g', 'root', '-m', '0755', self.root, self.helper, self.tools,
                self.root / 'dashboard')
        self.sh('install', '-d', '-o', 'root', '-g', 'cliproxyapi', '-m', '0750', self.etc, self.runtime)
        self.sh('install', '-d', '-o', 'root', '-g', 'root', '-m', '0700', self.state)
        self.sh('install', '-d', '-o', 'cliproxyapi', '-g', 'cliproxyapi', '-m', '0770', self.requests)
        self.sh('install', '-o', 'root', '-g', 'cliproxyapi', '-m', '0640',
                ROOT / 'tests/fixtures/config-v8-nested.yaml', self.config)
        for name in ('move-conversations.py', 'gateway_config.py'):
            self.sh('install', '-o', 'root', '-g', 'root', '-m', '0644', ROOT / 'scripts' / name, self.helper / name)
        # The dashboard as installed: its modules, with the scripts it imports beside them.
        self.sh('install', '-o', 'root', '-g', 'root', '-m', '0644', *sorted((ROOT / 'dashboard').glob('*.py')),
                self.root / 'dashboard')
        self.sh('install', '-o', 'root', '-g', 'root', '-m', '0644', *sorted((ROOT / 'scripts').glob('*.py')),
                self.root)
        self.sh('sh', '-c', 'umask 022; cat > "$0"', self.tools / 'driver.py', input=DRIVER)

    def properties(self, requests):
        """The production service's properties, with the lab substitutions."""
        section, properties = None, []
        for line in SERVICE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('['):
                section = line.strip('[]')
                continue
            key, value = line.split('=', 1)
            if section == 'Unit' and key in ('StartLimitIntervalSec', 'StartLimitBurst'):
                properties.append(f'{key}={value}')
            elif section == 'Service' and key in SUBSTITUTED:
                if f'{key}={value}' not in self.substituted:
                    self.substituted.append(f'{key}={value}')
            elif section == 'Service' and key == 'ReadWritePaths':
                lab = {'config': self.config, 'requests': requests}
                paths = [f'-{lab[PRODUCTION_PATHS[path]]}' for path in value.split()]
                properties.append('ReadWritePaths=' + ' '.join(paths + [str(self.state), str(self.runtime)]))
            elif section == 'Service' and key != 'ExecStart':
                properties.append(f'{key}={value}')
        # PrivateTmp hides the host's /var/tmp; expose the lab read-only, then ReadWritePaths reopens its parts.
        properties.append(f'BindReadOnlyPaths={self.root}')
        return properties

    def exec_start(self, requests):
        command = next(line.split('=', 1)[1] for line in SERVICE.read_text().splitlines() if line.startswith('ExecStart='))
        command = command.replace(PRODUCTION_DIR, str(self.helper)).split()
        return command + ['--request', str(requests / 'request'), '--config', str(self.config),
                          '--state-dir', str(self.state), '--runtime-dir', str(self.runtime)]

    def start_pair(self, name, requests):
        watched = dict(line.split('=', 1) for line in PATH_UNIT.read_text().splitlines() if line.startswith('PathExists='))
        assert watched['PathExists'] == '/run/personal-codex-router-move-request/request', watched
        args = ['systemd-run', '--quiet', f'--unit={name}', f'--path-property=PathExists={requests}/request']
        for prop in self.properties(requests):
            args += ['-p', prop]
        self.units += [f'{name}.path', f'{name}.service']
        self.sh(*args, *self.exec_start(requests))
        print(f'started transient units {name}.path and {name}.service', flush=True)

    def path_state(self, name):
        return self.sh('systemctl', 'show', '-p', 'ActiveState,SubState,Result', '--value', f'{name}.path',
                       check=False).split()

    def settle(self, name):
        for _ in range(50):
            if self.sh('systemctl', 'show', '-p', 'ActiveState', '--value', f'{name}.service',
                       check=False) in ('inactive', 'failed'):
                return
            time.sleep(0.1)

    def meta(self):
        return self.sh('stat', '-c', '%i %U:%G %a', self.config).split()

    def ttl(self):
        return self.sh('grep', 'session-affinity-ttl', self.config).split(':', 1)[1].strip()

    def lexists(self, path):
        return subprocess.run(['sudo', '-n', 'test', '-e', str(path), '-o', '-L', str(path)]).returncode == 0

    def result(self):
        text = self.sh('cat', self.result_path, user='cliproxyapi', check=False)
        return json.loads(text) if text else None

    def wait_result(self, previous_at, timeout=12):
        ends = time.monotonic() + timeout
        while time.monotonic() < ends:
            result = self.result()
            if result and result.get('at') != previous_at:
                return result
            time.sleep(0.2)
        return None

    def move(self, requests=None):
        out = self.sh(PYTHON, '-I', self.tools / 'driver.py', self.root, requests or self.requests, user='cliproxyapi')
        return json.loads(out.splitlines()[-1])

    def caps_probe(self):
        name = f'pcr-move-sandbox-{self.tag}-caps'
        args = ['systemd-run', '--wait', '--pipe', '--quiet', '--collect', f'--unit={name}']
        for prop in self.properties(self.requests):
            if not prop.startswith(('Type=', 'StartLimit')):
                args += ['-p', prop]
        self.units.append(f'{name}.service')
        out = self.sh(*args, '/bin/sh', '-c',
                      'grep -E "^Cap(Eff|Prm|Bnd)" /proc/self/status; '
                      f'ls {self.requests} >/dev/null && echo request-dir-traversable; '
                      f'touch {self.root}/probe 2>&1 | head -1; echo probe-done', check=False)
        caps = [line for line in out.splitlines() if line.startswith('Cap')]
        self.check('no capabilities at all (CapEff, CapPrm, CapBnd are zero)',
                   len(caps) == 3 and all(line.split()[1] == '0' * 16 for line in caps), ' '.join(caps))
        self.check('request directory traversable through group access', 'request-dir-traversable' in out)
        self.check('lab outside the writable paths is read-only', 'Read-only file system' in out)

    def run(self):
        self.setup()
        name = f'pcr-move-sandbox-{self.tag}'
        before = self.meta()
        print('lab', self.root, 'config', ' '.join(before), 'ttl', self.ttl(), flush=True)
        self.caps_probe()

        # A request left for longer than 15 s before anything watches it.
        self.sh(PYTHON, '-I', '-c', CREATE, self.requests / 'request', user='cliproxyapi')
        inode = int(self.sh('stat', '-c', '%i', self.requests / 'request'))
        print('created a request; waiting 16 s before starting the path unit', flush=True)
        time.sleep(16)
        self.start_pair(name, self.requests)
        result = self.wait_result(None)
        self.settle(name)
        self.check('stale request refused, consumed, named by inode; path unit still watching',
                   result is not None and (result['outcome'], result['code'], result['request_inode'])
                   == ('refused', 'stale_request', inode) and not self.lexists(self.requests / 'request')
                   and self.path_state(name)[:2] == ['active', 'waiting'], f'{result} {self.path_state(name)}')

        expected = ['1h0m1s', '1h0m0s'] * 4
        for index in range(7):
            moved = self.move()
            self.settle(name)
            result, meta = self.result(), self.meta()
            self.check(f'move {index}: dashboard reports moved, {expected[index]} on the same inode, owner and mode, '
                       'request consumed, path unit watching',
                       moved['move'] == {'outcome': 'moved', 'code': 'moved'} and moved['left'] == []
                       and moved['available'] is True and result['value'] == expected[index]
                       and self.ttl() == f'"{expected[index]}"' and meta == before
                       and result['inode_before'] == result['inode_after'] == int(before[0])
                       and self.path_state(name)[:2] == ['active', 'waiting'],
                       f"{moved} {result['outcome']}/{result['code']} ttl={self.ttl()} meta={meta}")
            if index == 0:
                moved = self.move()
                self.check('a second move within 60 s is too_soon from the helper; config unchanged',
                           moved['move'] == {'outcome': 'not_moved', 'code': 'too_soon'} and self.ttl() == '"1h0m1s"',
                           str(moved))
            # Lab only: clear the 60 s interval so the proof needn't wait between moves.
            self.sh('rm', '-f', self.state / 'last-flip')
        self.check('result file and directory readable by the dashboard group',
                   self.sh('stat', '-c', '%G %a', self.result_path) == 'cliproxyapi 640'
                   and self.sh('stat', '-c', '%G %a', self.runtime) == 'cliproxyapi 750')
        backups = self.sh('find', self.state / 'backups', '-type', 'f', '-printf', '%f %u %m\n').splitlines()
        self.check('backups pruned to the newest 5, root-only 0600 in a 0700 directory',
                   len(backups) == 5 and all(line.endswith(' root 600') for line in backups)
                   and self.sh('stat', '-c', '%a', self.state / 'backups') == '700', '; '.join(backups))
        newest = sorted(backups)[-1].split()[0]
        self.check('newest backup holds the config from before the last move',
                   'session-affinity-ttl: "1h0m0s"' in self.sh('cat', self.state / 'backups' / newest))

        # Any entry is taken without being opened or followed; the stored interval refuses the flip.
        self.sh(PYTHON, '-I', '-c', 'import sys,time; open(sys.argv[1], "w").write(str(time.time()) + "\\n")',
                self.state / 'last-flip')
        for label, create in (
                ('symlink to the config', lambda: self.sh('ln', '-s', self.config, self.requests / 'request',
                                                          user='cliproxyapi')),
                ('root-owned file', lambda: self.sh('install', '-m', '0644', '/dev/null', self.requests / 'request')),
                ('directory', lambda: self.sh('mkdir', self.requests / 'request', user='cliproxyapi')),
                ('fifo', lambda: self.sh('mkfifo', self.requests / 'request', user='cliproxyapi'))):
            previous = (self.result() or {}).get('at')
            create()
            result = self.wait_result(previous)
            self.settle(name)
            self.check(f'{label}: taken and removed, refused too_soon, path unit watching',
                       result is not None and (result['outcome'], result['code']) == ('refused', 'too_soon')
                       and not self.lexists(self.requests / 'request')
                       and self.path_state(name)[:2] == ['active', 'waiting'], f'{result} {self.path_state(name)}')
        self.check('config untouched by refused requests', self.ttl() == '"1h0m1s"' and self.meta() == before)

        # A request directory the helper can't traverse (the old 0700 layout): consume_failed, never silent.
        old = self.root / 'old-requests'
        self.sh('install', '-d', '-o', 'cliproxyapi', '-g', 'cliproxyapi', '-m', '0700', old)
        self.start_pair(name + '-old', old)
        moved = self.move(old)
        self.check('untraversable directory: the dashboard withdraws its own request, nothing moved',
                   moved['move'] == {'outcome': 'not_moved', 'code': 'helper_not_started'} and moved['left'] == []
                   and moved['seconds'] < 4, str(moved))
        result = self.result()
        self.check('untraversable directory: the helper reports failed/consume_failed',
                   result is not None and (result['outcome'], result['code']) == ('failed', 'consume_failed'),
                   str(result))
        self.sh(PYTHON, '-I', '-c', CREATE, old / 'request', user='cliproxyapi')
        for _ in range(100):
            state = self.path_state(name + '-old')
            if state[:1] == ['failed']:
                break
            time.sleep(0.2)
        self.check('a request nobody withdraws ends in a clean start-limit failure of its path unit',
                   state == ['failed', 'failed', 'unit-start-limit-hit'], f'state={state}')
        self.check('the main path unit is unaffected', self.path_state(name)[:2] == ['active', 'waiting'])

        # A stopped path unit: the request is withdrawn untaken within about 2 s.
        self.sh('systemctl', 'stop', f'{name}.path')
        moved = self.move()
        self.check('stopped path unit: helper_not_started within about 2 s, request withdrawn',
                   moved['move'] == {'outcome': 'not_moved', 'code': 'helper_not_started'} and moved['left'] == []
                   and 2 <= moved['seconds'] < 4, str(moved))
        self.check('config unchanged after the stopped-unit move', self.ttl() == '"1h0m1s"' and self.meta() == before)

    def cleanup(self):
        # Paths first, so a stopped helper can't be retriggered by a request it hadn't removed yet.
        for unit in sorted(self.units, key=lambda unit: not unit.endswith('.path')):
            self.sh('systemctl', 'stop', unit, check=False)
        for unit in self.units:
            self.sh('systemctl', 'reset-failed', unit, check=False)
        self.sh('rm', '-rf', '--', self.root, check=False)
        leftovers = self.sh('systemctl', 'list-units', '--all', '--no-legend', f'pcr-move-sandbox-{self.tag}*',
                            check=False)
        print('stopped and reset units:', ' '.join(self.units), flush=True)
        print('leftover units:', leftovers or 'none', '| lab removed:', not self.lexists(self.root), flush=True)


def main():
    try:
        subprocess.run(['sudo', '-n', 'id', 'cliproxyapi'], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print('SKIP: passwordless sudo or the cliproxyapi user is unavailable')
        return 0
    lab = Lab()
    try:
        lab.run()
    except Exception as error:
        lab.check('sandbox run completed', False, f'{type(error).__name__}: {error}')
    finally:
        lab.cleanup()
    print('substituted for the lab:', ' '.join(lab.substituted))
    print('failures', lab.failures)
    return 1 if lab.failures else 0


if __name__ == '__main__':
    sys.exit(main())
