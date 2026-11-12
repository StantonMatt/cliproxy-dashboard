# Claude Fable 5.2 applied record — 2026-11-09

The model catalog refresh was applied on `router` on 2026-11-09.

## What was wrong

CLIProxyAPI 8.0.47 registers Claude OAuth models from its process catalog, embedded at startup and refreshed from the official remote source on startup and every three hours ([registration](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/sdk/cliproxy/service_models.go#L148), [updater](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/registry/model_updater.go#L16)). The official catalog added 5.2 at 19:07:56 UTC on 2026-11-09 ([`router-for-me/models@7e3acaf`](https://github.com/router-for-me/models/commit/7e3acaf1718cde6e1f3623d142e29a177221e66a)); the stale catalog caused HTTP 400 `unknown provider for model claude-fable-5-2`. No upgrade (8.0.62's embedded catalog also lacks 5.2), alias or config edit was needed: a same-ID alias cannot register an absent model, and aliasing to 5.1 would send the wrong upstream model.

## What was applied

This forced refresh updates all three process-wide catalogs and has no exact catalog-snapshot undo API. It makes no inference call, restart or config write; withdrawal below does not undo unrelated catalog metadata updates.

```sh
sudo -n python3 - <<'PY'
import json
from pathlib import Path
import subprocess
import time
import urllib.request

http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
pid = subprocess.check_output(['systemctl', 'show', 'cliproxyapi.service',
                               '-p', 'MainPID', '--value'], text=True).strip()
assert Path('/proc/' + pid + '/exe').resolve() == Path('/opt/cliproxyapi/8.0.47/cli-proxy-api')
management = Path('/etc/cliproxyapi/dashboard-management-key').read_text().strip()
client = Path('/home/user/.config/personal-codex-router/claude-gateway.key').read_text().strip()
def request(path, key, body=None):
    req = urllib.request.Request('http://127.0.0.1:8317' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with http.open(req, timeout=90) as response:
        return json.load(response)
def models():
    return {row['id'] for row in request('/v1/models', client)['data']}
before = models()
result = request('/v8/management/routing/model-definitions/refresh', management, {})
assert result.get('ok') and result['catalogs']['catalog']['refreshed'], 'Catalog refresh failed'
deadline = time.monotonic() + 20
while 'claude-fable-5-2' not in models():
    assert time.monotonic() < deadline, '5.2 did not register; inspect exclusions/source before proceeding'
    time.sleep(.2)
assert before <= models(), 'Some previous model routes disappeared; inspect catalog changes'
assert subprocess.check_output(['systemctl', 'show', 'cliproxyapi.service',
    '-p', 'MainPID', '--value'], text=True).strip() == pid
print('Fable 5.2 registered; previous routes retained; gateway PID unchanged')
PY
```

5.2 registered, previous routes were retained, the gateway PID stayed unchanged, and a real call afterwards returned `claude-fable-5-2`.

## Usage probe

`FABLE_PROBE_MODEL` in `scripts/collect-claude-usage.py` is now `claude-fable-5-2`. B's direct 5.2 probe during validation returned `cli_unavailable` because its all-model weekly window was at 100%, but still reported the Fable weekly bucket (6%), so the switch did not wait for B's reset. Refusal telemetry, Haiku fallback and reset scheduling are unchanged. Deploy the collector with the `--claude-watch-only` commands in [Claude usage](claude-usage.md).

## Withdraw / restore

Run the guarded config editor with `withdraw` to exclude 5.2 through the watched config. It preserves the original bytes, ownership, mode and inode and refuses existing exclusion rules. Withdrawal was tested only on an isolated gateway, not on production. Do not restart the service.

```sh
sudo -n python3 - withdraw <<'PY'
import copy
import os
from pathlib import Path
import sys
import yaml

path = Path('/etc/cliproxyapi/config.yaml')
mode = sys.argv[1]
assert mode in ('withdraw', 'restore')
fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
with os.fdopen(fd, 'r+b') as stream:
    raw = stream.read().decode()
    before = yaml.safe_load(raw)
    assert before['config-version'] == 8
    assert 'oauth-excluded-models' not in before
    oauth = next(value for key, value in yaml.compose(raw).value if key.value == 'oauth')
    indent = oauth.value[0][0].start_mark.column
    pad = ' ' * indent
    block = (pad + '# Fable 5.2 route withdrawal 2026-11-09\n' +
             pad + 'excluded-models:\n' + pad + '  claude:\n' +
             pad + '    - claude-fable-5-2\n')
    expected = copy.deepcopy(before)
    if mode == 'withdraw':
        assert 'excluded-models' not in before['oauth']
        assert 'Fable 5.2 route withdrawal 2026-11-09' not in raw
        lines = raw.splitlines(keepends=True)
        lines.insert(oauth.value[0][0].start_mark.line, block)
        updated = ''.join(lines)
        expected['oauth']['excluded-models'] = {'claude': ['claude-fable-5-2']}
    else:
        assert before['oauth']['excluded-models'] == {'claude': ['claude-fable-5-2']}
        assert raw.count(block) == 1
        updated = raw.replace(block, '', 1)
        expected['oauth'].pop('excluded-models')
    assert yaml.safe_load(updated) == expected
    stream.seek(0)
    stream.write(updated.encode())
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())
print('Config edit saved; wait for watched reload and verify model list')
PY
```

After either edit, verify the route and unchanged PID without inference:

```sh
python3 - withdraw <<'PY'
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
wanted = sys.argv[1] == 'restore'
pid = subprocess.check_output(['systemctl', 'show', 'cliproxyapi.service', '-pMainPID', '--value'])
key = Path.home().joinpath('.config/personal-codex-router/claude-gateway.key').read_text().strip()
http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 20
while True:
    req = urllib.request.Request('http://127.0.0.1:8317/v1/models', headers={'Authorization': 'Bearer ' + key})
    with http.open(req, timeout=15) as response:
        names = {row['id'] for row in json.load(response)['data']}
    if ('claude-fable-5-2' in names) == wanted:
        break
    assert time.monotonic() < deadline, 'Watched route update not observed'
    time.sleep(.2)
assert {'claude-fable-5-1', 'claude-opus-5-5', 'claude-haiku-5-5'} <= names
assert subprocess.check_output(['systemctl', 'show', 'cliproxyapi.service', '-pMainPID', '--value']) == pid
print('Requested route state verified; controls retained; PID unchanged')
PY
```

Use `restore` in both blocks to re-enable 5.2. If the editor refuses because another exclusion list exists or comments/layout were rewritten, stop and merge the single model into the current config; do not overwrite other work or restore old auth files.

## Evidence

[claude-fable-5-2-validation-2026-11-09.json](claude-fable-5-2-validation-2026-11-09.json) is the pre-apply validation snapshot: `live_changed: false` / `probe_model_changed: false` describe the state before the apply and probe switch.
[`tests/gateway_fable_isolated.py`](../tests/gateway_fable_isolated.py) reproduces the stale-catalog 400, refresh and withdrawal on an isolated gateway:
`python3 -B tests/gateway_fable_isolated.py --catalog <official models.json at 7e3acaf> --scratch <dir> --output <file>`
