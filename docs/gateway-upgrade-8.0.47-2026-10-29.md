# Gateway 8.0.47 upgrade preparation — 2026-10-29

**8.0.30 is still live. 8.0.47 is installed and staged; no cutover was performed.**
This replaces [8.0.43 as the planned target](gateway-upgrade-8.0.43-2026-10-26.md).
The implementation worker did not run a review loop, commit, push, deploy the
dashboard, reload systemd or signal the live gateway. The parent must review the
final changes before publishing them and choose the idle cutover window.

Selected [v8.0.47](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.47),
published 2026-10-29 00:27:13 UTC, revision
`90c447d3a8727d115aa7552ec93ce023736ed492`.
The [8.0.30 installation](gateway-upgrade-8.0.30-2026-10-15.md) remains the rollback;
8.0.43 remains installed and staged as a fallback preparation. Public, synthetic
evidence is in [gateway-upgrade-evaluation-v8.0.47.json](gateway-upgrade-evaluation-v8.0.47.json).

## Prepared artifacts

- Official **plugin-capable** `CLIProxyAPI_8.0.47_linux_amd64.tar.gz`,
  24,396,070 bytes. SHA-256
  `2b5111dc6a0dfe0d607457787f794330d1b477bc9daa6337c26b0cd032a7827e`
  matches both upstream `checksums.txt` and GitHub's asset digest. The checksums
  file SHA-256 is
  `a28932395a29208b37f2c048aea4793ba33400c8d437e6421e23591c2487e9d1`,
  also matching its GitHub digest.
- `/opt/cliproxyapi/8.0.47/cli-proxy-api`: `root:root`, `0755`, 73,150,760 bytes,
  in a `root:root` `0755` directory. Executable SHA-256:
  `167fff6edf264f71b7e28923b71f97cf1b49957db1612a6e5e8e570f42aeb112`.
  `-h` prints `CLIProxyAPI Version: 8.0.47, Commit: 90c447d3, BuiltAt: 2026-10-29T00:27:43Z`
  without starting a listener. 8.0.30, 8.0.43 and 8.0.7 remain intact.
- `/etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf.staged`,
  `root:root`, `0644`. **The `.staged` suffix is ignored by systemd.** Entire content:

  ```ini
  [Service]
  # Prepared 2026-10-29; ignored until the user chooses the cutover window.
  # Activate only with the reviewed 8.0.47 runbook; park the 8.0.30 drop-in first.
  ExecStart=
  ExecStart=/opt/cliproxyapi/8.0.47/cli-proxy-api -config /etc/cliproxyapi/config.yaml
  ```

No config was installed or edited. No active drop-in changed. In particular,
`model-catalog-updates.conf` remains untouched, and the candidate command omits
`-local-model`, preserving automatic catalog refresh. No plugin rebuild or
runtime plugin installation is needed. The 8.0.43 staged file remains in place;
its cleanup belongs to the parent.

## Verification

Run from `/home/user/.worktrees/personal-codex-router/gateway-8.0.47`;
`E` below denotes the task scratch directory. All gateway tests used synthetic
auth and non-forwarding loopback mocks. No production OAuth file was read and
no live usage queue was consumed. Shared `heavy` capacity was allowed to wait.

```sh
E=$HOME/.cache/agent-scratch/personal-codex-router/upgrade-8.0.47
heavy python3 -B tests/gateway_replace_integration.py --binary /opt/cliproxyapi/8.0.47/cli-proxy-api --assume-tested --scratch "$E/replace"
heavy python3 -B tests/gateway_replace_integration.py --binary /opt/cliproxyapi/8.0.30/cli-proxy-api --scratch "$E/replace"
heavy python3 -B tests/gateway_upgrade_isolated.py --binary /opt/cliproxyapi/8.0.47/cli-proxy-api --scratch "$E" --output "$E/gates-8.0.47.json"
heavy python3 -B tests/gateway_upgrade_delta_isolated.py --binary /opt/cliproxyapi/8.0.47/cli-proxy-api --scratch "$E" --output "$E/delta-8.0.47.json"
for version in 8.0.47 8.0.30; do
  for steering in false true; do
    heavy python3 -B tests/fast_mode_isolated.py --binary /opt/cliproxyapi/$version/cli-proxy-api --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.1.2.so --response-steering "$steering" --scratch "$E"
  done
  heavy python3 -B tests/codex_websockets_isolated.py --binary /opt/cliproxyapi/$version/cli-proxy-api --scratch "$E/websockets"
done
heavy python3 -B tests/sandbox_plugin_check.py --binary /opt/cliproxyapi/8.0.47/cli-proxy-api --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.1.2.so --output "$E/sandbox-plugin-8.0.47.json"
heavy python3 -B -m unittest discover -s tests -q
```

- **Credential replacement: 8.0.47 passed 50 randomized runs and 12 in-flight refresh races.** Seed 953134927: 47 `signed_in`, three `saved_unconfirmed`; every unconfirmed result was `refresh_after`, converged through the watcher, and correctly stayed unconfirmed. Disk/runtime matched in every randomized run. Idle Pro→Free, next refresh, priority/enable PATCH sequence, lock waiting and isolated restart persistence passed. PATCH-only changed neither disk nor runtime. The locked six-field writer is unchanged. The **8.0.30 rollback control also passed 50 randomized runs and 12 refresh races**, seed 761474924: 48 signed in and 2 saved unconfirmed, all `refresh_after`; its PATCH-only control and restart persistence passed.
- **Routing/config/move/failover/Claude wire: 62/62.** Strict nested-v8 config PATCH passed without unknown/removed/deprecated-key warnings. Consumed retry is **5**, max interval **45**, with **two** retry credentials. Seven existing plain Codex and Opus/Sonnet/Haiku 5.5 routes reached the correct mock. Both providers changed from cache hit to `cache miss, new binding` after a same-inode TTL edit. Both providers recovered from synthetic 429 and 503 in the same request and retained the new binding. Unauthenticated API/management requests returned 401. The thinking `drop_block` and native `sdk-ts` caller profile/system checks passed.
- **Usage collection: 33/33 records persisted**, both providers; **zero quarantines and failed spools**. The scratch queue was a JSON list and the unchanged collector dropped additive `effective_service_tier`. Its original queue URL was asserted and rewritten to the scratch port before opening; no request went to 8317.
- **New delta checks: 16/16.** Valid `X-Api-Key` plus foreign bearer succeeds; wrong key plus foreign bearer returns 401. Embedded Terra is listed and completes inference. Negotiated gzip/identity JSON, compressed upstream SSE, standard versus Codex private metadata policy and completed events all passed. Compressible requests larger than 1024 bytes remained uncompressed with the default upstream-request setting off.
- **Fast-mode: 53/53 HTTP cases in all four runs** (8.0.47 and 8.0.30, steering false/true), plus failover, policy/config reload, library path replacement, status lifecycle and isolated restart. The original installed ABI-1/schema-6 v0.1.2 library was used without rebuilding. Serial turns enforce policy on both versions. Duplex turns enforce it on 8.0.47; 8.0.30 retains its explicitly asserted expected bypass. See the source reassessment below; the initial old expectation was preserved in the evaluation JSON.
- **WebSockets: 39/39 assertions, 24 completed synthetic responses and six fixture tests on each binary.** Native **Codex CLI 0.166.0**, isolated homes and mock auth only, exited 0 for both serial and duplex cases. Warmup→first inference reused the upstream socket. Native duplex inference enforces policy on 8.0.47 and retains the expected bypass on 8.0.30. Exact per-turn token/account multisets, first/follow-up/reconnect, continuation IDs, transport flag HTTP→WS→HTTP, watched-inode steering reload and detection of real late tier overrides passed. This is successful-turn accounting; rejected-turn accounting and real billing remain unverified.
- **Plugin sandbox: passed as `cliproxyapi`**, with matching User/Group, UMask, privilege/capability, address-family, PrivateTmp, ProtectSystem/Home, kernel and cgroup restrictions; extra localhost-only egress. The current-PID v0.1.2 marker and `/proc/<pid>/maps` confirmed a content-addressed plugin in private temp. Transient `pcr-plugin-19ab62294c.service`, PID 528447, gateway port 52975, mock 38487, was stopped; task-owned state/runtime directories and mock were removed/closed.
- **Focused tests:** 33 sign-in retry, 40 move, 37 replacement, 47 plugin installer and eight Claude compatibility tests passed. Separate in-process controls removed only the new allowlist entries and confirmed sign-in was refused before session creation and moves became unavailable. **Full `heavy python3 -B -m unittest discover -s tests -q`: 855 tests, zero failures**, including a fresh run after the version-specific harness changes. SQLite `ResourceWarning` diagnostics were emitted. The WebSocket check-only run passed six fixture methods.
- **Offline merged-unit `systemd-analyze verify`: exit 0**; only two unrelated installed XFS `CPUAccounting` warnings. Runbook shell syntax and **12 embedded Python blocks** parsed, changed harness Python parsed, and `git diff --check` passed. No frontend code changed; no browser suite or live UI acceptance was performed.

`signin.TESTED_GATEWAYS` and the separate `MOVE_TESTED_GATEWAYS` retain 8.0.30 and 8.0.43 and add **8.0.47** only after the corresponding candidate gates passed. Neither allowlist was deployed to `/opt/personal-codex-router`.


## Upstream 8.0.43 → 8.0.47 assessment

Source was read at **each release tag**, not inferred from release notes.
Exact tag revisions and source assertions are retained in the evaluation JSON.
The installed release is unmodified upstream; local behavior changes are only
the dashboard's two independently tested version allowlists.

- **8.0.44 full-chain compression.** In tagged
  [`config_types.go`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.44/internal/config/config_types.go),
  negotiated downstream HTTP compression defaults **on**, with a 1024-byte
  threshold for non-streaming responses; SSE flushes remain streaming.
  `server.response-compression.{enabled,min-bytes}` can configure it.
  Upstream and downstream Codex WebSocket `permessage-deflate` default **on**,
  negotiated by the peer (`upstream.codex.websocket-compression`). Upstream
  HTTP request zstd compression defaults **off**
  (`upstream.codex.upstream-request-compression`, threshold key
  `upstream-request-compression-min-bytes`, default 1024). It is restricted to
  appropriate ChatGPT OAuth routes and has 400/415 fallback. Codex upstream
  compressed responses are decoded before translation/usage extraction.
  The delta harness proved gzip upstream SSE decoding, downstream gzip/identity
  negotiation, Terra inference and metadata behavior. The collector's urllib
  client does not advertise gzip/zstd, so its queue stays plain JSON; the
  unchanged collector persisted all isolated records. Fast-mode sees decoded
  bodies before executor transport encoding. Downstream HTTP compression also
  applies to Claude when negotiated; the Claude wire fixture passed. Request
  logging decodes compressed bodies with bounded zstd output/window; full body
  logging remains off. The isolated native Codex client test covers its protocol; the live T3 UI
  and every compression codec were not exercised. The raw WS mock does not
  negotiate permessage-deflate; compressed WS frames were not independently tested.
- **8.0.44 interceptor body opt-out; 8.0.47 frontend auth opt-out and raw patches.**
  Host ABI remains **1**; schema advances from **6 → 7 → 8**.
  [`requestInterceptorOmitsBody`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/pluginhost/adapters_interceptors.go)
  and `frontendAuthOmitsBody` return false below schema 7. Opt-out needs both
  schema 7 and an explicit capability. Raw byte-range patching needs schema 8
  and an explicit patch capability. Our installed v0.1.2 plugin still advertises
  ABI 1/schema 6, receives JSON bodies and uses the existing interception path.
  The unchanged installed library passed the complete fast-mode matrix and
  equivalent systemd sandbox. No rebuild, new opt-out or patch capability is used.
  **Important additional change in `0fe38224`:** subsequent duplex create/append/steer
  frames now run the ordinary before/after-auth hooks. The first unchanged
  steering-true regression correctly failed its old bypass expectation: both
  outbound tiers were absent. Source inspection confirmed the fix; assertions
  now require enforcement specifically on 8.0.47 and retain the expected bypass
  on 8.0.30/8.0.43. This is a stronger version-specific check, not a workaround.
  The v0.1.2 plugin still emits its unconditional, now-stale duplex warning.
- **8.0.44 discovery/config.** `server.discovery.advertise-management` is
  deprecated and ignored, but retained so old configs load. Management is no
  longer advertised through DNS-SD. Discovery is disabled in our nested-v8
  fixture. The added compression keys are optional; no existing fixture key was
  removed, and strict `PATCH /v8/management/config` and warning checks passed.
  No nested-to-legacy migration or config PATCH belongs in this cutover.
- **8.0.45 transient turn admission limits.**
  [`codex_executor_terminal.go`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.45/internal/runtime/executor/codex_executor_terminal.go)
  classifies `turn_rate_limit_exceeded` as transient/model-scoped capacity,
  honors provider retry hints and keeps quota exhaustion separate. Early WS
  429 before visible output can retry another credential without closing the
  downstream socket (commit `86a1aab`). Upstream Codex disconnect handling keeps
  downstream open where recoverable (commit `f73e66a`). Existing HTTP 429/503
  failover and successful WS continuation/native-client tests passed. These
  exact error-frame/retry-hint/disconnect paths were source-assessed, not injected
  by our harness. **Only one Codex account currently remains**, so credential
  rotation cannot create extra live Codex capacity. No promise of a general 429
  fix or transparent recovery after every partially visible turn is made.
- **8.0.45 model refresh.**
  [`RefreshModelDefinitions`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.45/internal/api/handlers/management/model_definitions.go)
  adds authenticated `POST /v8/management/routing/model-definitions/refresh`, validates
  sources and reports refresh errors. We do not need to call it during preparation
  or change the collector/dashboard. **8.0.47 embeds `gpt-6.1-terra`** in both
  `internal/registry/models/models.json` and `codex_client_models.json`; the
  synthetic `-local-model` instance advertised it and completed inference.
  The live command must still omit `-local-model` so later models remain visible.
  Actual remote catalog refresh and the live model list were not queried.
- **8.0.45 unauthenticated-body descriptor fix.** Commit `477605d` defers body
  capture until authenticated/needed, closes incomplete unauthenticated bodies,
  bounds protocol/header reads and backs off descriptor exhaustion. It changes
  neither our auth keys nor management exposure. Scratch missing/wrong-key probes
  returned 401. Descriptor-exhaustion/load behavior was source-assessed, not
  stress-tested against either gateway.
- **8.0.46 metadata filtering.**
  [`responsesSSEFramer`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.46/sdk/api/handlers/openai/openai_responses_handlers.go)
  filters `response.metadata` and `codex.*` private SSE events for standard
  Responses clients; official Codex identification by User-Agent/Originator
  retains its metadata while filtering rate limits. Internal `responsesapi.*`
  timing and subscription-usage events are withheld. The downstream WS path
  also suppresses moderation metadata for non-Codex clients. The new compressed
  SSE fixture proves the standard/Codex distinction and completion delivery.
  Claude `/v1/messages` is separate and passed its compatibility/wire checks.
  Dashboard collection uses management usage records, not these private events;
  its persisted records passed unchanged. T3's Codex backend uses the native
  Codex protocol, but actual T3/Claude UI acceptance remains a cutover check.
- **8.0.46 late WS interrupt rejection.** Terminal responses reject stale
  interrupts with `no active response to interrupt`, instead of fabricating an
  incomplete response; active interruptions remain separate. Source reviewed
  in HTTP-backed and duplex paths at v8.0.46. Ordinary serial/native completion
  passed; late-interrupt frames were not injected locally. Response steering
  must remain explicitly false for this rollout and its 8.0.30 rollback. The
  separate per-frame hook fix is tested for create follow-ups and native warmup,
  not every full-duplex/interrupt/error interaction.
- **X-Api-Key precedence.** A valid `X-Api-Key` with a foreign bearer returned
  models; replacing it with a wrong key returned **401**, on the synthetic
  scratch port. No real key or bearer was used.

Real OAuth refresh, subscription quota, provider signatures, live streaming,
live Claude Code/T3 UI, Mac access, real billing and sustained load remain
outside this synthetic preparation. The WebSocket regression harness retains
the duplex bypass as an **expected policy failure on 8.0.30/8.0.43** and requires
per-turn enforcement on 8.0.47. Neither result authorizes enabling steering. See [Keep response steering off](codex-websockets.md#keep-response-steering-off).
The Windows setup guide was separately re-checked against v8.0.47 source;
this Linux preparation does not validate a Windows installation.

## Live baseline and cleanup

The live gateway remained **PID 4574**, actual executable
`/opt/cliproxyapi/8.0.30/cli-proxy-api`, `NRestarts=0`, with identical
`ActiveEnterTimestamp` and `ExecMainStartTimestamp`
(`Tue 2026-10-27 17:43:06 -03`), ExecStart and active DropInPaths before/after.
Private SHA-256/owner/mode comparisons found **zero changed baseline files** under
`/etc/cliproxyapi`, deployed `/opt/personal-codex-router`, the existing 8.0.30,
8.0.43 and 8.0.7 installations, and the service drop-in directory. Its only added
file is the ignored 8.0.47 staged drop-in. The candidate's installed hash,
root ownership and mode were checked again. No live gateway/dashboard/router
unit was restarted, reloaded or signaled; no active drop-in or config was edited.
No production auth content was read or written, including for hashing; auth byte
identity is therefore not claimed.

**All 31 recorded owned PIDs exited** (gateways, native Codex,
fixture/harness processes); mock listeners closed and the transient sandbox unit
stopped. No persistent server, listener or browser tab remains. Replacement used
only 18417 with the token mock on 18419; the JSON records other isolated ports
and process identities. Final cleanup removed duplicate archive/extracted
binary/source, synthetic auth/config/CA/client-home/plugin copies and the one
remaining task-owned plugin shadow directory. The installed release and both
staged candidates remain.

Small release/checksum metadata, sanitized results, private baseline hashes and
synthetic logs remain in
`~/.cache/agent-scratch/personal-codex-router/upgrade-8.0.47/` (mode 0700).
These are preparation evidence, **not fresh production credential backups**.


## Worktree changes

All changes are uncommitted in `gateway-8.0.47` from main `53a722d`. No AGENTS file
was changed, no review loop was run and no commit/push was performed. The parent
must run its authoritative review before publishing. Changed/new files:

- `README.md`
- `dashboard/server.py`
- `dashboard/signin.py`
- `docs/account-access.md`
- `docs/codex-websockets.md`
- `docs/fast-mode-policy.md`
- `docs/gateway-config-v8-migration.md`
- `docs/gateway-upgrade-8.0.30-2026-10-15.md`
- `docs/gateway-upgrade-8.0.43-2026-10-26.md`
- `docs/gateway-upgrade-8.0.7-2026-10-01.md`
- `docs/move-conversations-2026-10-17.md`
- `docs/resilience-2026-09-25.md`
- `docs/standard-management-setup.md`
- `docs/title-generation-model-catalog.md`
- `tests/codex_websockets_isolated.py`
- `tests/fast_mode_isolated.py`
- `tests/test_credential_replace.py`
- `tests/test_move_conversations.py`
- `tests/test_signin_retry.py`
- `docs/gateway-upgrade-8.0.47-2026-10-29.md`
- `docs/gateway-upgrade-evaluation-v8.0.47.json`
- `tests/gateway_upgrade_delta_isolated.py`

## Cutover — user-selected idle window only

**Do not execute these commands as preparation.** First finish the parent's
review-fix-loop, publish/review the intended change, and use that reviewed tree.
The new allowlists must be deployed with the gateway cutover. The preparation
left `/opt/personal-codex-router` unchanged, so merely restarting the gateway
would leave the old dashboard refusing sign-in and conversation moves on 8.0.47.
No config migration, plugin reinstall or credential restoration is needed.

One gateway restart interrupts active requests and loses in-memory affinity
bindings. Agree an idle window across both machines, finish pending sign-ins,
and stop clients from sending requests until checks finish. The cutover smoke
below deliberately exercises the one Codex and two Claude accounts; if one is disabled, expired or
quota-limited, agree how to validate it before proceeding. Do not silently
enable accounts or change their priority, prefix, saved routing or fast policy.

Preflight (retain these shell variables for subsequent blocks):

```sh
set -euo pipefail
cd /home/user/Projects/personal-codex-router
printf '%s  %s\n' 167fff6edf264f71b7e28923b71f97cf1b49957db1612a6e5e8e570f42aeb112 /opt/cliproxyapi/8.0.47/cli-proxy-api | sha256sum -c -
printf '%s  %s\n' 87226b1da29006d96b13eea3da7a7b3b8a8761a7dad52207d3a44e51b32fb663 /opt/cliproxyapi/8.0.30/cli-proxy-api | sha256sum -c -
sudo -n test -f /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf.staged
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.43.conf
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.rollback-8.0.47
systemctl show cliproxyapi -p MainPID -p ExecStart -p DropInPaths
python3 scripts/install-operations.py --account-access-only
CUTOVER_DIR=/var/lib/cliproxyapi-upgrade-backups/cutover-8.0.47-$(date -u +%Y%m%dT%H%M%SZ)
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
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.rollback-8.0.47
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf.staged /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf
sudo -n systemctl daemon-reload
sudo -n systemctl restart cliproxyapi
systemctl is-active cliproxyapi
systemctl show cliproxyapi -p MainPID -p ExecStart -p DropInPaths
```

The two `mv` operations remove the stale 8.0.30 header from the effective unit
and make rollback explicit. Verify no later-sorting 8.0.7 drop-in is active. Leave the ignored 8.0.43
staged file parked; never activate both candidates. Neither the staged nor active
8.0.47 command may add `-local-model`.
The scoped dashboard installer has its own reviewed file manifest and backups;
it does call `daemon-reload`, which is allowed only during this chosen cutover.
Do not run a full router install or nested-to-legacy migration.

## Post-restart checks — before resuming clients

The smoke check expects exactly one Codex auth file. The Free Codex account was
disabled on 2026-10-29 at the user's request; back up and remove its `codex-…`
auth file (never Claude B's file with the same email) after the scoped dashboard
install and before the restart, so the single-account dashboard is already in place.

Keep the preference timer, move path and dashboard paused during the three
account smoke requests below. This future live check uses real inference and
briefly disables the Claude peer for each Claude request, restoring it in `finally`.
It requires all three accounts initially enabled; it never enables a previously
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
            assert r.headers['X-CPA-VERSION'] == '8.0.47'
        return json.load(r)
def mgmt(path, body=None, method=None):
    return call('/v0/management' + path, body, True, method)
rows = mgmt('/auth-files')['files']
pools = {p: sorted([r for r in rows if r.get('type') == p], key=lambda r: r['name']) for p in ('codex', 'claude')}
assert {p: len(pool) for p, pool in pools.items()} == {'codex': 1, 'claude': 2}, 'Stop: expected one Codex and two Claude accounts'
assert all(not r.get('disabled') for pool in pools.values() for r in pool), 'Stop: all three accounts must already be enabled'
assert any(m['id'] == 'gpt-6.1-terra' for m in call('/v1/models')['data']), 'Terra missing from catalog'
before = {r['name']: (r['auth_index'], r.get('priority', 0), r.get('prefix', ''), r.get('disabled', False)) for pool in pools.values() for r in pool}
for provider, pool in pools.items():
    for index, target in enumerate(pool):
        peers = [r for r in pool if r is not target]
        try:
            for peer in peers:
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
            for peer in peers:
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
rows for both providers and all three accounts in the existing database. Never add a second
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
routing drift. Open the dashboard in the normal browser and verify the one Codex
card, both Claude cards, routing controls, fast-mode enforcement, recent usage
and normal navigation. No sign-in is needed just to prove the version gate;
the isolated harness covers credential replacement. Finish by confirming the
gateway still listens on `*:8317` behind the nftables guard (loopback and the Mac
over Tailscale only), management and the dashboard backend stay loopback-only,
Mac inference works over Tailscale, and Mac management/dashboard work over SSH.

Do not enable Codex WebSockets during the cutover. Enable them afterwards only through
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
new, staged = d / 'zz-gateway-8.0.47.conf', d / 'zz-gateway-8.0.47.conf.staged'
old, parked = d / 'zz-gateway-8.0.30.conf', d / 'zz-gateway-8.0.30.conf.rollback-8.0.47'
if new.exists():
    assert not staged.exists()
    new.rename(staged)
if parked.exists():
    assert not old.exists()
    parked.rename(old)
assert old.exists() and not new.exists()
PY
sudo -n systemctl daemon-reload
# Only if the cutover started 8.0.47 (or the attempted restart failed). A restart discards the
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
