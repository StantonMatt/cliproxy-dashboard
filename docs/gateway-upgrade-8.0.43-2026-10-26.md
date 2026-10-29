# Gateway 8.0.43 upgrade preparation — 2026-10-26

**8.0.30 is still live. 8.0.43 is installed and staged; no cutover was performed.**
The user chooses the restart window. This implementation worker did not run a
review loop, commit, push, deploy the dashboard, reload systemd or signal the
live gateway. The parent must review the final changes before publishing them.

Selected [v8.0.43](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.43),
published 2026-10-25 15:37:22 UTC, revision
`307803510bce87f986fcf7b1a84190f105c7b4aa`. The existing
[8.0.30 installation](gateway-upgrade-8.0.30-2026-10-15.md) remains the rollback.
Public, synthetic evidence is in
[gateway-upgrade-evaluation-v8.0.43.json](gateway-upgrade-evaluation-v8.0.43.json).

## Prepared artifacts

- Official **plugin-capable** `CLIProxyAPI_8.0.43_linux_amd64.tar.gz`,
  24,022,497 bytes; SHA-256
  `7825540634898e11fe9baf255b683856da5d9e75b95c66835891ebb40a79b526`
  matches both upstream `checksums.txt` and GitHub's asset digest. The checksums
  file SHA-256 is
  `9c4ca4bf749368d1f9a47f4ae115443ad5a89f92fdcfca59576ad2332a218f95`.
- `/opt/cliproxyapi/8.0.43/cli-proxy-api`: `root:root`, `0755`, 72,062,568 bytes,
  in a `root:root` `0755` directory, matching the 8.0.30 installation.
  Executable SHA-256:
  `0e6445974aca0fd46c85847f3d06d950c8a67f6936af4584a01b9866388128ff`.
  `-h` prints `CLIProxyAPI Version: 8.0.43, Commit: 30780351, BuiltAt: 2026-10-25T15:38:02Z`
  and exits without starting a listener. 8.0.30 and 8.0.7 remain intact.
- `/etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.43.conf.staged`,
  `root:root`, `0644`. **The `.staged` suffix is ignored by systemd.** Its entire
  content is:

  ```ini
  [Service]
  # Prepared 2026-10-26; ignored until the user chooses the cutover window.
  # Activate only with the reviewed 8.0.43 runbook; park the 8.0.30 drop-in first.
  ExecStart=
  ExecStart=/opt/cliproxyapi/8.0.43/cli-proxy-api -config /etc/cliproxyapi/config.yaml
  ```

No candidate config was installed. No active drop-in was edited, including the
stale “NOT ACTIVE” comment in `zz-gateway-8.0.30.conf`. Cutover parks that file
and activates the new accurately labelled drop-in. No plugin rebuild or runtime
plugin installation is needed.

## Verification

Run from the task worktree; `E` below denotes
`~/.cache/agent-scratch/personal-codex-router/upgrade-8.0.43`. All gateway tests
used separate loopback ports, synthetic auth and a non-forwarding mock. No test
read production credentials or consumed the live destructive usage queue.

```sh
E=$HOME/.cache/agent-scratch/personal-codex-router/upgrade-8.0.43
heavy python3 tests/gateway_replace_integration.py --binary /opt/cliproxyapi/8.0.43/cli-proxy-api --assume-tested --scratch "$E/replace"
heavy python3 tests/gateway_replace_integration.py --binary /opt/cliproxyapi/8.0.30/cli-proxy-api --scratch "$E/replace"
heavy python3 tests/gateway_upgrade_isolated.py --binary /opt/cliproxyapi/8.0.43/cli-proxy-api --output "$E/gates-8.0.43.json"
heavy python3 tests/fast_mode_isolated.py --binary /opt/cliproxyapi/8.0.43/cli-proxy-api --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.1.2.so --scratch "$E"
heavy python3 tests/fast_mode_isolated.py --binary /opt/cliproxyapi/8.0.30/cli-proxy-api --model gpt-6-luna --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.1.2.so --scratch "$E"
heavy python3 tests/sandbox_plugin_check.py --binary /opt/cliproxyapi/8.0.43/cli-proxy-api --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.1.2.so --output "$E/sandbox-plugin-8.0.43.json"
```

- **Credential replacement: both builds passed**, normal counts of 50 randomized
  runs and 12 in-flight refresh races each. 8.0.43 seed 598404492: 48 `signed_in`,
  two `saved_unconfirmed`; 8.0.30 seed 1055362323: 47 and three. Every randomized
  runtime matched disk. Every unconfirmed result was `refresh_after`, converged
  through the watcher, and remained unconfirmed as intended. Idle Pro→Free,
  next refresh, priority/enable PATCH sequence, lock waiting and isolated restart
  persistence passed. PATCH-only control changed neither disk nor runtime on
  either build. The locked six-field writer remains unchanged; raw whole-file
  upload is not used. `signin.TESTED_GATEWAYS` now retains 8.0.30 and adds 8.0.43.
- **Routing, move, failover, config, Claude wire and collection: 62/62 checks.**
  The nested-v8 fixture loads without unknown/removed/deprecated-key warnings and
  passes strict `PATCH /v8/management/config`. Consumed management values confirm
  retry **5** and maximum retry interval **45**, with two retry credentials.
  All seven plain Codex routes from the local catalog and Opus/Sonnet/Haiku 5.5
  routes advertised and reached their matching mock provider. Both Codex and
  Claude sessions logged a cache hit, then `cache miss, new binding` after the
  TTL changed in place, with the config inode preserved. Both providers survived
  synthetic **429 and 503** errors in the same request and remained on the second
  account for the follow-up. Both unauthenticated API checks returned 401.
  `MOVE_TESTED_GATEWAYS` independently retains 8.0.30 and adds 8.0.43.
- **Usage collection: 33/33 records saved**, both providers, zero quarantines or
  failed spools. The actual 8.0.43 queue returned a JSON list; the existing
  collector reduced the additive `effective_service_tier` field away. Its URL
  was intercepted and rewritten to the scratch port, with an assertion on the
  original URL; no request went to 8317. No collector production code changed.
- **Fast-mode: 53/53 HTTP cases on both builds**, plus synthetic failover,
  policy/config hot reload, hot library path replacement, marker lifecycle,
  isolated restart and the existing duplex-websocket limitation assertions.
  The original first attempt failed before plugin loading because `gpt-5.5` is
  absent from 8.0.43's bundled catalog. The harness now accepts `--model` and
  defaults to `gpt-6-luna`; enforcement assertions were not weakened. The binary
  is explicit in both existing harnesses, avoiding a stale version default.
- **Plugin sandbox: passed** as `cliproxyapi`, with the live unit's User, Group,
  UMask, PrivateTmp, ProtectSystem, ProtectHome, privilege, capability, address
  family and kernel/cgroup restrictions. StateDirectory and RuntimeDirectory
  were task-owned; an extra IP allowlist restricted egress to localhost. The
  current-PID v0.1.2 marker and `/proc/<pid>/maps` proved a content-addressed
  library loaded from `/tmp/cliproxy-pluginhost/pid-3028787/` inside the private
  mount namespace. Transient `pcr-plugin-28079d9bbe.service`, PID 3028787,
  port 59041, mock 35403, was stopped and its state directory removed.
  The first probe loaded the plugin but checked the listener too soon; adding
  a bounded readiness wait fixed the probe.
- **Claude compatibility:** all eight existing mixed-provider compatibility
  unit tests passed. The binary wire fixture additionally verifies preserved
  thinking gets `prefix_mismatch_behavior: drop_block` and a native `sdk-ts`
  request retains its caller profile and system marker. Its first malformed
  dummy signature was correctly stripped; the final fixture generates a
  structurally valid synthetic protobuf signature locally. No real signature
  or credential is copied. The new generic per-minute cooldown behavior and
  all tool-argument normalization variants are not exhaustively tested here.
- Source recheck at the exact tagged revision: normalized TTL participates in
  routing-state equality, and `sdk/cliproxy/service_config.go` replaces the
  selector when that state changes. `internal/watcher/events.go` handles writes
  to the watched config inode. `internal/pluginhost/shadow.go` uses host temp
  for library copies. ABI 1/schema 6 and function-table layouts remain unchanged
  from the prior installations; the existing plugin passed without rebuilding.
- Offline merged-unit `systemd-analyze verify` exited 0. Its only diagnostics
  were two unrelated installed XFS units' removed `CPUAccounting` option.
- Focused checks passed: 33 sign-in retry, 40 move, 36 credential replacement,
  47 plugin installer and eight Claude compatibility tests. The broader
  `heavy python3 -m unittest discover -s tests -q` passed **803 tests**. It emitted
  SQLite `ResourceWarning` diagnostics; there were no test failures.
  No frontend code changed, so no browser suite was required.

The strict config endpoint normalizes YAML formatting. An early probe's literal
TTL string replacement therefore made no edit; the final harness uses the
existing `gateway_config.validated_edit` and still writes the same inode.
Updater requests for Antigravity and npm were refused by the mocks. Real OAuth
refresh, subscription quota, provider signatures, live streaming/Claude Code/T3
UI behavior and load are outside this preparation's synthetic evidence. The
known websocket follow-up fast-mode limitation remains.

## Live baseline and cleanup

The effective executable remained `/opt/cliproxyapi/8.0.30/cli-proxy-api`, PID
**2654**, with the same `ActiveEnterTimestamp` and `ExecMainStartTimestamp`
(`Mon 2026-07-27 16:44:09 -04`) and `NRestarts=0` before and after preparation.
The active drop-ins were unchanged; the ignored 8.0.43 staged file was the only
addition. Private hash/mode/owner comparisons found no changes under
`/etc/cliproxyapi`, `/opt/personal-codex-router`, `/opt/cliproxyapi/8.0.30` or
`/opt/cliproxyapi/8.0.7`. No task action wrote `/var/lib/cliproxyapi` or changed
router units/timers. Two auth files changed while the live service/timers kept
running, and its journal recorded two auth-file watcher updates. Their cause
was not further established; no claim of byte-identical auth is made.

Final isolated policy gateway PID 3041819 used port 41435, mock 45035. Fast-mode
8.0.43 PIDs 3014561/3015665 used 55029, mock 43955; the 8.0.30 control PIDs
3033148/3033712 used 48701, mock 38653. Replacement used 18417 with the token
mock on 18419: 8.0.43 PIDs 2988129/2994825, control PIDs 3028812/3033771.
All owned test gateways were terminated and waited for. All mock servers were
closed; both transient sandbox attempts were stopped and their state removed.
No persistent process, browser tab or listener was left by this task.

Small preparation metadata, private baseline hashes, synthetic logs and test
results remain under `~/.cache/agent-scratch/personal-codex-router/upgrade-8.0.43/`.
Downloaded/extracted duplicate binaries, synthetic auth/config/CA directories
and task-owned plugin shadow files were removed. The pre-existing read-only
upstream reference clone was left alone. These artifacts are preparation
evidence, **not fresh production credential backups**.

## Cutover — user-selected idle window only

**Do not execute these commands as preparation.** First finish the parent's
review-fix-loop, publish/review the intended change, and use that reviewed tree.
The new allowlists must be deployed with the gateway cutover. The preparation
left `/opt/personal-codex-router` unchanged, so merely restarting the gateway
would leave the old dashboard refusing sign-in and conversation moves on 8.0.43.
No config migration, plugin reinstall or credential restoration is needed.

One gateway restart interrupts active requests and loses in-memory affinity
bindings. Agree an idle window across both machines, finish pending sign-ins,
and stop clients from sending requests until checks finish. The cutover smoke
below deliberately exercises both accounts; if one is disabled, expired or
quota-limited, agree how to validate it before proceeding. Do not silently
enable accounts or change their priority, prefix, saved routing or fast policy.

Preflight (retain these shell variables for subsequent blocks):

```sh
set -euo pipefail
cd /home/user/Projects/personal-codex-router
printf '%s  %s\n' 0e6445974aca0fd46c85847f3d06d950c8a67f6936af4584a01b9866388128ff /opt/cliproxyapi/8.0.43/cli-proxy-api | sha256sum -c -
printf '%s  %s\n' 87226b1da29006d96b13eea3da7a7b3b8a8761a7dad52207d3a44e51b32fb663 /opt/cliproxyapi/8.0.30/cli-proxy-api | sha256sum -c -
sudo -n test -f /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.43.conf.staged
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.43.conf
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.rollback-8.0.43
systemctl show cliproxyapi -p MainPID -p ExecStart -p DropInPaths
python3 scripts/install-operations.py --account-access-only
CUTOVER_DIR=/var/lib/cliproxyapi-upgrade-backups/cutover-8.0.43-$(date -u +%Y%m%dT%H%M%SZ)
sudo -n install -d -o root -g root -m 0700 "$CUTOVER_DIR"
sudo -n python3 - "$CUTOVER_DIR" <<'PY'
import json, subprocess, sys
from pathlib import Path
system = ['personal-codex-router-' + u for u in ('usage.timer', 'preference.timer', 'ops.timer', 'backup.timer', 'move.path', 'dashboard.service')]
def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0
# User-manager state is captured separately by the invoking desktop user below.
(Path(sys.argv[1]) / 'resume-system.json').write_text(json.dumps([u for u in system if active(u)]))
(Path(sys.argv[1]) / 'before-service.txt').write_bytes(subprocess.check_output(['systemctl', 'show', 'cliproxyapi', '-pMainPID', '-pExecStart', '-pActiveEnterTimestamp']))
PY
python3 - <<'PY' | sudo -n tee "$CUTOVER_DIR/resume-user.json" >/dev/null
import json, subprocess
units = ['personal-codex-router-' + u for u in ('claude-usage.timer', 'claude-b-usage.timer', 'claude-keepalive.timer')]
print(json.dumps([u for u in units if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', u]).returncode == 0]))
PY
```

Pause timer/path activations and wait for existing invocations rather than killing
them. Keep the dashboard running for the scoped installer's mandatory overview
preflight; all users must remain idle and pending sign-ins must be finished.
Stop the dashboard immediately after that installer, before selecting the new
binary. Leave the live gateway running until the single restart below.
If any step fails after this point, recover with the rollback/resume blocks;
do not leave the timers paused.

```sh
sudo -n systemctl stop personal-codex-router-usage.timer personal-codex-router-preference.timer personal-codex-router-ops.timer personal-codex-router-backup.timer personal-codex-router-move.path
systemctl --user stop personal-codex-router-claude-usage.timer personal-codex-router-claude-b-usage.timer personal-codex-router-claude-keepalive.timer
python3 - <<'PY'
import subprocess, time
units = ['personal-codex-router-' + u for u in ('usage.service', 'preference.service', 'ops.service', 'backup.service', 'move.service')]
user = ['personal-codex-router-' + u for u in ('claude-usage.service', 'claude-b-usage.service', 'claude-keepalive.service')]
deadline = time.monotonic() + 330
while True:
    states = [subprocess.check_output(['systemctl', *(['--user'] if is_user else []), 'show', u, '-pActiveState', '--value'], text=True).strip() for is_user, names in ((False, units), (True, user)) for u in names]
    if all(s in ('inactive', 'failed') for s in states):
        break
    if time.monotonic() >= deadline:
        raise SystemExit('An invocation did not finish; recover without cutting over')
    time.sleep(1)
PY
sudo -n test ! -e /run/personal-codex-router-move-request/request
sudo -n systemctl start personal-codex-router-usage.service
sudo -n python3 /opt/personal-codex-router/router-backup.py run
sudo -n python3 - "$CUTOVER_DIR" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path
b = Path(sys.argv[1])
for source, name in [('/etc/cliproxyapi', 'config'), ('/var/lib/cliproxyapi/auth', 'auth'),
                     ('/etc/systemd/system/cliproxyapi.service.d', 'dropins'),
                     ('/opt/personal-codex-router/dashboard', 'dashboard')]:
    subprocess.run(['cp', '-a', source, str(b / name)], check=True)
(b / 'effective-unit.txt').write_bytes(subprocess.check_output(['systemctl', 'cat', 'cliproxyapi']))
(b / 'sha256.json').write_text(json.dumps({str(p.relative_to(b)): hashlib.sha256(p.read_bytes()).hexdigest() for p in b.rglob('*') if p.is_file()}, indent=2))
print('Fresh private backups:', b)
PY
# Inspect and apply the reviewed dashboard manifest; it restarts only dashboard/preview.
sudo -n python3 scripts/install-operations.py --account-access-only --apply
# The installer needs the running dashboard for preflight; stop it for the cutover/check window.
sudo -n systemctl stop personal-codex-router-dashboard.service
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.rollback-8.0.43
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.43.conf.staged /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.43.conf
sudo -n systemctl daemon-reload
sudo -n systemctl restart cliproxyapi
systemctl is-active cliproxyapi
systemctl show cliproxyapi -p MainPID -p ExecStart -p DropInPaths
```

The two `mv` operations remove the stale 8.0.30 header from the effective unit
and make rollback explicit. Verify no later-sorting 8.0.7 drop-in is active.
The scoped dashboard installer has its own reviewed file manifest and backups;
it does call `daemon-reload`, which is allowed only during this chosen cutover.
Do not run a full router install or nested-to-legacy migration.

## Post-restart checks — before resuming clients

Keep the preference timer, move path and dashboard paused during the four
account smoke requests below. This future live check uses real inference and
briefly disables the peer within each provider, restoring it in `finally`.
It requires both accounts initially enabled; it never enables a previously
disabled account. It must run only in the agreed idle window. Do not execute
it as part of isolated preparation.

```sh
# The restart returns before the listener is up; wait up to 60 s for management to answer 401.
python3 - <<'PY'
import time, urllib.error, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 60
while True:
    try:
        client.open('http://127.0.0.1:8317/v0/management/config', timeout=5)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Gateway management did not answer within 60 s; roll back')
    time.sleep(1)
PY
sudo -n python3 - <<'PY'
import json, subprocess, urllib.request, uuid, yaml
from pathlib import Path
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
c = yaml.safe_load(Path('/etc/cliproxyapi/config.yaml').read_bytes())
management_key = Path('/etc/cliproxyapi/dashboard-management-key').read_text().strip()
def call(path, body=None, management=False, method=None):
    key = management_key if management else c['access']['api-keys'][0]
    req = urllib.request.Request('http://127.0.0.1:8317' + path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'session_id': str(uuid.uuid4())})
    with client.open(req, timeout=90) as r:
        # Only the management middleware sets the version header; inference is checked by its body.
        if management:
            assert r.headers['X-CPA-VERSION'] == '8.0.43'
        return json.load(r)
def mgmt(path, body=None, method=None):
    return call('/v0/management' + path, body, True, method)
rows = mgmt('/auth-files')['files']
pools = {p: sorted([r for r in rows if r.get('type') == p], key=lambda r: r['name']) for p in ('codex', 'claude')}
assert all(len(pool) == 2 and all(not r.get('disabled') for r in pool) for pool in pools.values()), 'Stop: both enabled accounts per provider required'
before = {r['name']: (r['auth_index'], r.get('priority', 0), r.get('prefix', ''), r.get('disabled', False)) for pool in pools.values() for r in pool}
for provider, pool in pools.items():
    for index, target in enumerate(pool):
        peer = pool[1-index]
        try:
            mgmt('/auth-files/status', {'name': peer['name'], 'disabled': True}, 'PATCH')
            current = mgmt('/auth-files')['files']
            enabled = [r for r in current if r.get('type') == provider and not r.get('disabled')]
            assert len(enabled) == 1 and enabled[0]['auth_index'] == target['auth_index']
            if provider == 'codex':
                result = call('/v1/responses', {'model': 'gpt-6-luna', 'stream': False, 'store': False, 'instructions': 'Reply only UPGRADE_OK.', 'input': 'Reply UPGRADE_OK.'})
                text = ''.join(p.get('text', '') for item in result.get('output', []) for p in item.get('content', []) if p.get('type') == 'output_text')
                assert result.get('status') == 'completed' and 'UPGRADE_OK' in text
            else:
                result = call('/v1/messages', {'model': 'claude-haiku-5-5', 'max_tokens': 32, 'stream': False, 'messages': [{'role': 'user', 'content': 'Reply only UPGRADE_OK.'}]})
                assert any('UPGRADE_OK' in p.get('text', '') for p in result.get('content', []))
            print(provider, 'account', index + 1, 'smoke passed')
        finally:
            mgmt('/auth-files/status', {'name': peer['name'], 'disabled': peer.get('disabled', False)}, 'PATCH')
after = {r['name']: (r['auth_index'], r.get('priority', 0), r.get('prefix', ''), r.get('disabled', False)) for r in mgmt('/auth-files')['files'] if r['name'] in before}
assert after == before, 'Account routing metadata changed'
pid = int(subprocess.check_output(['systemctl', 'show', 'cliproxyapi', '-pMainPID', '--value']))
status = json.loads(Path('/var/lib/cliproxyapi/fast-mode-plugin-status.json').read_text())
assert status['pid'] == pid and status['plugin'] == 'fast-mode-policy' and status['version'] == '0.1.2' and not status.get('disabled')
assert '/cliproxy-pluginhost/' in Path(f'/proc/{pid}/maps').read_text()
# Listener stays wildcard behind the nftables guard; management stays loopback-only.
assert c['server']['host'] == '' and c['management']['allow-remote'] is False
print('Both providers/accounts and current-PID plugin marker passed; no credentials displayed')
PY
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8317/v1/models
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8317/v0/management/config
sudo -n systemctl start personal-codex-router-usage.service
sudo -n systemctl start personal-codex-router-dashboard.service
# Type=simple returns before the listener and first gateway poll; allow up to 60 s.
python3 - <<'PY'
import json, time, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 60
while True:
    try:
        req = urllib.request.Request('http://127.0.0.1:8320/api/health', headers={'X-Router-Request': '1'})
        with client.open(req, timeout=5) as r:
            if json.load(r)['health']['gateway']['status'] == 'healthy':
                break
    except (OSError, ValueError, KeyError):
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Dashboard did not report a healthy gateway within 60 s')
    time.sleep(1)
print('Dashboard and gateway ready; collection timer still paused')
PY
```

Expect 401 on both unauthenticated requests. Check the model list against the
pre-cutover routes without dumping keys. Privately compare config, plugin
policy, saved routing preferences and account metadata with the fresh backup;
OAuth refresh may legitimately rotate tokens, so do not compare or restore old
token values. The collector service must finish successfully and show fresh
rows for both providers/accounts in the existing database. Never add a second
live usage-queue reader. Inspect only version/startup/warning classifications
from the new PID's journal, keeping raw logs private.

Resume only previously active timers/path units using the saved state:

```sh
sudo -n python3 - "$CUTOVER_DIR" <<'PY'
import json, subprocess, sys
from pathlib import Path
units = json.loads((Path(sys.argv[1]) / 'resume-system.json').read_text())
if units:
    subprocess.run(['systemctl', 'start', *units], check=True)
PY
sudo -n cat "$CUTOVER_DIR/resume-user.json" | python3 -c 'import json,subprocess,sys; u=json.load(sys.stdin); subprocess.run(["systemctl","--user","start",*u],check=True) if u else None'
systemctl is-active cliproxyapi personal-codex-router-dashboard personal-codex-router-usage.timer personal-codex-router-preference.timer personal-codex-router-move.path
systemctl show personal-codex-router-usage.service personal-codex-router-preference.service -p Id -p Result -p ExecMainStatus
```

After the next collection/preference cycle, require healthy collection and no
routing drift. Open the dashboard in the normal browser and verify both Codex
cards, both Claude cards, routing controls, fast-mode enforcement, recent usage
and normal navigation. No sign-in is needed just to prove the version gate;
the isolated harness covers credential replacement. Finish by confirming the
gateway still listens on `*:8317` behind the nftables guard (loopback and the Mac
over Tailscale only), management and the dashboard backend stay loopback-only,
Mac inference works over Tailscale, and Mac management/dashboard work over SSH.

Codex WebSockets stay off during the cutover. Enable them afterwards only through
[the WebSockets rollout](codex-websockets.md), which requires response steering
to stay off.

## Rollback to 8.0.30

If a gate fails, retain private diagnostics, keep clients idle, restore any
temporary disabled flag before proceeding, and select 8.0.30. If the failure
preceded the gateway restart, restore the drop-in selection without restarting
an already healthy 8.0.30 process. The following block handles either file
activation state; restart only if a new gateway was actually started:

```sh
sudo -n python3 - <<'PY'
from pathlib import Path
d = Path('/etc/systemd/system/cliproxyapi.service.d')
new, staged = d / 'zz-gateway-8.0.43.conf', d / 'zz-gateway-8.0.43.conf.staged'
old, parked = d / 'zz-gateway-8.0.30.conf', d / 'zz-gateway-8.0.30.conf.rollback-8.0.43'
if new.exists():
    assert not staged.exists()
    new.rename(staged)
if parked.exists():
    assert not old.exists()
    parked.rename(old)
assert old.exists() and not new.exists()
PY
sudo -n systemctl daemon-reload
# Only if the cutover started 8.0.43 (or the attempted restart failed). A restart discards the
# in-memory usage queue, so first collect any smoke-check records while management still answers:
sudo -n systemctl start personal-codex-router-usage.service || echo 'Usage drain failed; uncollected smoke records will be lost'
sudo -n systemctl restart cliproxyapi
systemctl show cliproxyapi -p MainPID -p ExecStart -p ActiveState
# Wait for management to answer 401 before the collector reads the queue.
python3 - <<'PY'
import time, urllib.error, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 60
while True:
    try:
        client.open('http://127.0.0.1:8317/v0/management/config', timeout=5)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Gateway management did not answer within 60 s')
    time.sleep(1)
PY
sudo -n systemctl start personal-codex-router-usage.service
```

Keep the compatible dashboard: both allowlists retain 8.0.30. Restore no config
or OAuth files during an ordinary binary rollback; doing so could resurrect
spent refresh tokens or undo later routing changes. Do not select 8.0.7: it
fails the replacement gate and is not validated for moves. Then run the resume
block, repeat dashboard/plugin/collection/access checks on 8.0.30, and tell the
user whether the cutover succeeded or was rolled back. Fresh private backups
are recovery material, not an automatic rollback script.
