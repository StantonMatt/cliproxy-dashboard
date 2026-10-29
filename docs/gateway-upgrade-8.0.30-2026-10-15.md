# Gateway 8.0.30 upgrade preparation — 2026-10-15

**Current status (2026-10-27):** 8.0.30 remains live via `zz-gateway-8.0.30.conf`; 8.0.43 is installed with an ignored staged drop-in, and cutover is pending with 8.0.30 retained for rollback ([current runbook](gateway-upgrade-8.0.43-2026-10-26.md)). The preparation blockers and rollback to 8.0.7 below are historical.

**Activated 2026-10-15 19:09:48 UTC (16:09:48 -03:00) by user decision, with the
sign-in blocker accepted.** Production has run 8.0.30 since then. The blocker
(dashboard Codex sign-in did not persist on 8.0.30) is **resolved by the locked
direct replacement** described in
[Codex sign-in replacement](#codex-sign-in-replacement), once that dashboard change
is deployed. The preparation notes below describe the state before cutover.

Selected [v8.0.30](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.30),
published 2026-10-15 13:55:09 UTC: GitHub's latest stable release at preparation.
Tag revision: `5be2cdab0869cbaa3d401891f740aebf7e15aa99`.
This retains the versioned installation/rollback pattern from the
[8.0.7 upgrade](gateway-upgrade-8.0.7-2026-10-01.md).

## Prepared artifacts

- Official plugin-capable Linux amd64 asset:
  `CLIProxyAPI_8.0.30_linux_amd64.tar.gz` (23,409,585 bytes).
  SHA-256 `fd5a4bcb59b1f55b88b48575940fb0e4f920bfd4a9f96a6e9b755fc4c5b6fecc`
  matches the published `checksums.txt` **and** GitHub's asset digest.
  Checksums-file SHA-256:
  `79c1c6dd7a1db26d6b35eeee6b18659fcf337649155828bdea99e00139885935`.
- Installed `/opt/cliproxyapi/8.0.30/cli-proxy-api`, `root:root`, mode `0755`,
  in a `root:root` directory with mode `0755`, matching 8.0.7.
  Executable SHA-256:
  `87226b1da29006d96b13eea3da7a7b3b8a8761a7dad52207d3a44e51b32fb663`.
  Banner: 8.0.30, commit `5be2cdab`, built 2026-10-15 13:55:45 UTC.
  `/opt/cliproxyapi/8.0.7` and `/opt/cliproxyapi/7.3.11` remain intact.
- Staged `/etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.staged`,
  `root:root`, mode `0644`. Its suffix is ignored by systemd. It resets ExecStart
  to `/opt/cliproxyapi/8.0.30/cli-proxy-api -config /etc/cliproxyapi/config.yaml`;
  remote model-catalog updates stay enabled, as in the existing drop-in.
- **No config changes required for parsing or effective routing policy. No
  candidate config was created.** Key-name diff: added `[]`, removed `[]`,
  renamed `[]`. A config change does not solve the sign-in blocker.
- Root-private evidence:
  `/var/lib/cliproxyapi-upgrade-backups/prepare-8.0.30-20261015/` (mode `0700`).
  Includes release metadata/notes, checksums, source diffs, preparation baseline,
  probe scripts/logs/results and `evidence-sha256.json`. This is preparation
  evidence, **not a fresh pre-cutover credential backup**.
- Public, credential-free check results:
  [gateway-upgrade-evaluation-v8.0.30.json](gateway-upgrade-evaluation-v8.0.30.json).

## Compatibility and blockers

The current nested `config-version: 8` config starts under both binaries. A
same-value `PATCH /v8/management/config` on the isolated copy passes strict v8
field validation. There are no unknown/deprecated config warnings. Nested
`routing.retry`, `routing.cooldown`, strategy/affinity and retained root
`quota-exceeded.switch-project` / `quota-exceeded.switch-preview-model` all
produce the same consumed management values. Do not remove `quota-exceeded`
merely because it is a root block: these two fields are still supported.
`quota-exceeded.antigravity-credits` already moved to
`oauth.providers.antigravity.antigravity-credits` in this install.

**Blocking: existing dashboard device sign-in cannot import new tokens.** With
one synthetic file-backed Codex credential, `PATCH /v0/management/auth-files/fields`
of `access_token`, `refresh_token`, `id_token`, `expired`, `last_refresh`, and
`plan_type` returns HTTP 200 / `{"status":"ok"}` in both versions. In 8.0.7 the
new tokens/plan persist and read back. In 8.0.30 the old tokens and plan remain,
both immediately and after one second. Stable auth identity, prefix, priority
and unrelated metadata remain intact. The installed dashboard files match the
repository's `server.py`, `routing_policy.py`, and `signin.py` byte for byte;
`signin.py:317` uses exactly this PATCH and rejects the subsequent readback as
`Import verification failed`.

Source explains the result: [auth_files_fields.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/internal/api/handlers/management/auth_files_fields.go#L307)
updates via the ordinary auth manager context; [codex_persist.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/sdk/cliproxy/auth/codex_persist.go#L105)
merges credential fields from disk unless the operation carries explicit auth
creation intent. [MergeCodexCredentialsFromDisk](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/internal/auth/codex/token.go#L180)
restores the old credential partition. This safety behavior affects the existing
token-field PATCH integration. Resolve it through an upstream fix or a separately
reviewed, concurrency-safe dashboard integration change before cutover. Do not
replace the dashboard's import with an unguarded whole-file upload.

**Sign-in follow-up: still blocked; no unsafe dashboard workaround installed.**
An implementation investigation of the complete tagged source found no supported
HTTP option that carries auth-creation intent for an existing auth's token-only
PATCH. [persist_policy.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/sdk/cliproxy/auth/persist_policy.go#L9)
defines creation intent as an internal Go context value. Its production callers
are SDK login and management `saveTokenRecord`, not an HTTP header or PATCH field.

The raw JSON `POST /v0/management/auth-files?name=<existing-name>` API would retain
the filename and can persist new tokens in both versions, but is **not an accepted
fix**. [writeAuthFile](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/internal/api/handlers/management/auth_files_crud.go#L260)
uses `os.WriteFile` before `upsertAuthRecord`, without a conditional write or the
credential file lock. The same implementation exists in
[8.0.7](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.7/internal/api/handlers/management/auth_files_crud.go#L260).
A synthetic diagnostic held `<auth-file>.lock` exclusively and observed the
upload overwrite the JSON before releasing the lock in **both versions**. In
8.0.30 the HTTP response waited for the later persistence lock, after the unsafe
write had already occurred. Returning 200 and checking the result afterward
cannot protect a concurrent refresh from that earlier overwrite. A dashboard
controller flock serializes our controller only; the gateway does not share it.
Disabling an account also supplies no drain barrier for an in-flight refresh.

The gateway's own [Codex OAuth flow](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.30/internal/api/handlers/management/auth_files_provider_oauth.go#L301)
uses `saveTokenRecord` with creation intent, but derives its filename from email,
plan and account hash. It has no target-existing-file option; a plan change can
create a different filename/identity. It does not satisfy this dashboard's
stable-file requirement. No version-detection branch solves these API gaps.

A safe gateway-side integration must expose an explicit credential replacement
operation: acquire the existing per-auth mutation gate and credential file lock,
validate the expected identity/version, merge only the approved token/plan fields
into current metadata, save with creation intent, and update runtime state while
fencing stale refresh results. Preserve filename/index, prefix, priority,
disabled and unknown metadata. Retain the dashboard's readback verification and
verify rollback compatibility. This requires a separately reviewed gateway-side
change; **`dashboard/signin.py` is deliberately unchanged and cutover is blocked**.

**Resolved 2026-10-16 without a gateway change.** The dashboard now writes the
token fields itself under the gateway's own `<file>.lock` flock, as the gateway's
Codex writers do, then uses the fields PATCH only to sync memory from disk and
verifies the runtime token. See [Codex sign-in replacement](#codex-sign-in-replacement).
The raw upload above remains rejected.

**Fast-mode installer incompatibility fixed in repository.**
`scripts/install-fast-mode-plugin.py::verify_host_event` now matches the exact
`pluginhost: plugin registered` / `pluginhost: plugin hot reloaded` messages and
continues to require matching plugin ID, version and library path. It no longer
pins `[host.go:381]` / `[host.go:408]`; 8.0.30's 383/410 locations and future line
shifts are accepted. Focused regression tests fail on the previous implementation
and pass with the fix. Unmodified integration assertions pass on **both binaries**,
including registration, hot library replacement and marker checks. This installer
runs from the checkout and does not require dashboard deployment. No plugin was
reinstalled on production.

Other relevant changes reviewed:

- 8.0.9–11 add `client.codex` and shared `upstream.*` settings. Historical
  `codex.optimize-multi-agent-v2`, `providers.codex.optimize-multi-agent-v2`, and
  `oauth.providers.codex.optimize-multi-agent-v2` load as aliases of
  `client.codex.optimize-multi-agent-v2`; shared Codex settings such as
  `response-steering`, `model-level-cooling`, cloaking and bootstrap buffering
  move from `oauth.providers.codex.*` to `upstream.codex.*`. Historical YAML
  aliases load; strict native writes require canonical paths. These fields are
  absent from the live config, so no rename is needed. `client.codex.enable-apply-patch`
  is new; the later fix retains template freeform capability by default.
- `routing.session-affinity-rebind-on` (8.0.25) defaults to `any`, retaining the
  previous failover/rebinding policy. Leave it unset. Existing retry keys keep
  their nested v8 paths. Auth refresh/registration/cooldown fencing improves
  throughout 8.0.12–30, including preserving active cooldowns against stale
  successes in 8.0.30. These are runtime changes, not requested policy changes.
- `server.max-request-body-bytes` (8.0.26) defaults to an unlimited body;
  `requests.streaming.send-keepalive-before-ft` (8.0.27) defaults off.
  `requests.tls-handshake-timeout-seconds` (8.0.29) defaults to zero/off; its
  short-lived `upstream.claude.tls-handshake-timeout-seconds` spelling is retired.
  None are present here. No Claude/Anthropic settings were edited or proposed.
- `server.github-token` and custom `models` catalogs are optional additions.
  Model definitions change upstream; the existing command deliberately retains
  remote catalog refresh. The pinned Management Center remains v1.24.2.
- `/v0/management` remains available. Native v8 config responses add API-key
  auth indexes; native v8 auth-file quota responses conditionally expose
  `quota_plugin_id` and no longer advertise declarative `quota_probe` execution
  as native plugin quota support. The dashboard uses legacy `/v0` endpoints and
  its own `/api-call` quota/reset adapter, so this v8 quota distinction does not
  change its reads. New usage metadata is additive; the collector already
  reduces records to an allowlist. The queue response remains a JSON list.
- Existing Codex auth JSON needs no format migration. New optional `siwc` /
  `use_siwc` fields support Sign in with ChatGPT. Codex saves now use sidecar
  `.lock` files, atomic replacement and credential-partition merging; JSON-only
  dashboard readers remain compatible. Synthetic tests confirm ordinary
  priority and disabled-status writes, ID-token projection and unknown metadata
  preservation. Real refresh/token rotation remains untested by design.

## Codex sign-in replacement

Design and contracts: [account access](account-access.md#saving-the-sign-in-cliproxyapi-8030).
In short, `dashboard/credential_replace.py` overlays exactly the six token fields
under `flock(<abs path>.lock)`. It never makes a management call while holding
the lock, then sends the fields PATCH (a pure memory sync, since disk already
holds the tokens). It verifies disk, the listing claims and the runtime token
via a loopback `/api-call` echo. A failed runtime check is reported as
`saved_unconfirmed`, with no rollback, follow-up verification or deferred routing enable. The dashboard unit
gains `ReadWritePaths=-/var/lib/cliproxyapi/auth` (optional, so a missing directory
fails sign-in closed instead of stopping the dashboard). Sign-in is allowed only on
builds in `signin.TESTED_GATEWAYS` (`8.0.30` at the time of these tests), read from `X-CPA-VERSION`.
The [October 26 preparation](gateway-upgrade-8.0.43-2026-10-26.md) adds validated
8.0.43 support in the checkout; 8.0.30 remains live until its chosen cutover.

Harness results, 2026-10-16. All runs used the **unmodified** installed binaries in
a scratch dir on 127.0.0.1:18417 with synthetic credentials. A loopback CONNECT
proxy on 18419 with a scratch CA faked the token endpoint and refused every
other host. `SSL_CERT_FILE` also limits the scratch gateway's TLS trust to that
CA.

Current verification, 2026-10-17: after removing the retry lifecycle and token
continuity heuristic, completion confirms only the exact tokens it wrote.
Changed tokens or unfinished verification remain `saved_unconfirmed`; the file
watcher reloads disk without any dashboard follow-up. Normal account polling
controls the card, and routing is never enabled later for this outcome.

Two `heavy python3 tests/gateway_replace_integration.py --runs 60` runs against
`/opt/cliproxyapi/8.0.30/cli-proxy-api` (8.0.30, commit 5be2cdab) passed:

| Run | Seed | `signed_in` | `saved_unconfirmed` | Runtime == disk | Unconfirmed watcher convergence |
| --- | --- | --- | --- | --- | --- |
| 1 | 1008003766 | 52 | 8 | 60/60 | 8/8 |
| 2 | 605858102 | 54 | 6 | 60/60 | 6/6 |

In all 120 randomized runs, the new refresh token remained on disk or was
consumed by a subsequent simulated refresh; it was never lost. All 14 unconfirmed
runs were `refresh_after`: runtime equalled the current disk credential after
watcher reload, with no dashboard retry, and the outcome stayed unconfirmed.
When the fake provider rotated the credential, runtime and disk held those new
rotated tokens rather than the already-spent sign-in tokens.

Both runs also passed the PATCH-only control (disk and runtime unchanged), idle
Pro→Free replacement with metadata preserved, use of the new refresh token on
the next refresh, 12/12 in-flight refresh races, the enable/priority PATCH sequence,
and restart persistence. The in-flight writes waited 1.12–1.51 seconds across
both runs for the refresh lock. No check failed.

Historical runs before that removal: two 60-run sweeps (seeds 49826192 and
728566200) reported 60 `signed_in` and 0 `saved_unconfirmed` each. The second
accepted three rotated credentials through the removed continuity heuristic.
Earlier code ended 4/50, 5/50 and 5/60 randomized runs unconfirmed, all in
`refresh_after` mode; the removed retry confirmed them. These results describe
the older implementation, not the current confirmation contract.

Same harness against `/opt/cliproxyapi/8.0.7/cli-proxy-api` with `--assume-tested`
(seed 330562957): the control PATCH works there. Idle replacement, next refresh,
the PATCH sequence and restart pass. Replacement during an in-flight refresh
**fails 12/12**: 8.0.7 has no Codex file lock, so the refresh lands after the
sign-in and overwrites the new refresh token, even though verification had passed
in the meantime. Randomized runs failed 12/50 for the same reason. **8.0.7 is not
allowlisted.**

`python3 tests/sandbox_lock_check.py` ran the writer as `cliproxyapi` under the
dashboard unit's sandbox properties, with `ReadWritePaths` set to a scratch dir
under `/var/lib/pcr-sandbox-*` (removed afterwards). The results:

- It shares the flock with a holder outside the sandbox: it timed out while
  that lock was held, then wrote once it was released. The holder keeps the lock
  until the probe reports it was blocked, so slow sandbox startup cannot hide a
  missing lock.
- The repository's dashboard unit grants `ReadWritePaths=-/var/lib/cliproxyapi/auth`
  (optional, as above).
- It wrote the file with mode 0600.
- Writes to another `cliproxyapi`-owned scratch dir, to `/etc` and to `/opt` were
  refused with `EROFS`.
- A 127.0.0.1 listener inside the sandbox accepted a connection, so the runtime
  echo works under `IPAddressAllow=localhost`.

**Upgrade checklist addition.** Before or right after any gateway upgrade, run
`python3 tests/gateway_replace_integration.py --binary <new binary>
--assume-tested` (and `tests/sandbox_lock_check.py` if the unit changed). Add the
version to `TESTED_GATEWAYS` in `dashboard/signin.py` only if every check passes.
Until then, dashboard sign-in fails closed with **Gateway version not validated
for sign-in**, and accounts are re-signed through the management UI. A rollback
to 8.0.7 needs no harness run: 8.0.7 fails it and stays excluded on purpose, so
only the management UI can re-sign accounts until 8.0.30 is back.

## Release notes reviewed

All stable releases 8.0.8 through 8.0.30 were read. Full upstream notes are in
the private preparation evidence; the combined source comparison is
[v8.0.7...v8.0.30](https://github.com/router-for-me/CLIProxyAPI/compare/v8.0.7...v8.0.30).
The following summarizes each release's changes; provider-specific entries are
context, not configuration changes to this installation.

- 8.0.8: Grok client version update.
- 8.0.9: client Codex config; Responses `apply_patch` translation.
- 8.0.10: executor/plugin translation and Gemini signature handling.
- 8.0.11: shared upstream config and historical v8 aliases; Responses usage fixes.
- 8.0.12: terminal auth-state/refresh safeguards; v8 API-key auth indexes;
  plugin cooldown callback; Codex patch/search capability fixes.
- 8.0.13: auth snapshots; reasoning lifecycle, Codex usage/schema/citation fixes.
- 8.0.14: custom catalogs/reload; auth mutation locking and persistence merging;
  websocket disconnect reporting and multiple translation fixes.
- 8.0.15: truncated stream errors and terminal-chunk disconnect handling.
- 8.0.16: clean-EOF Responses completion, usage/effort reporting and translation fixes.
- 8.0.17: refreshed-credential selection, version/epoch fencing and stale-refresh protection.
- 8.0.18: speech routing restrictions; compact SSE error JSON.
- 8.0.19: global GitHub-token config and provider response-error handling.
- 8.0.20: attachment/empty-turn translation and dynamic Grok CLI version updates.
- 8.0.21: native config formatting; concurrent auth/model registration reconciliation.
- 8.0.22: Responses websocket interrupts; detach unselected plugins on config apply.
- 8.0.23: Codex model preservation, late interrupts and portable tool-call IDs.
- 8.0.24: usage access-provider/native-key metadata; canceled-stream cooldown
  preservation; tool-search switches and model capability hints.
- 8.0.25: Sign in with ChatGPT; affinity rebind option; native v8 quota contract fixes.
- 8.0.26: request body limit; websocket preservation across reload/auth removal;
  plugin multi-auth support.
- 8.0.27: pre-first-token keepalive option.
- 8.0.28: config-access race fixes; Codex SSE scanner; HTTP/2 lifecycle-drop
  handling; schema/model capability updates.
- 8.0.29: optional shared TLS-handshake timeout; model-registration fencing;
  robust watcher deletion reconciliation; model updates.
- 8.0.30: stale config-save/reload serialization; hashed-secret remote-storage
  preparation; stale-success cooldown protection; provider/header/schema fixes.

## Verification performed

Isolated probes ran sequentially on namespace loopback port 18347, with an empty
auth directory initially, then deliberately invalid synthetic Codex credentials.
They used a sanitized copy of the current config, private network/mount namespaces,
the real auth directory hidden by tmpfs, `/etc/cliproxyapi` mounted read-only in
the namespace, a closed outbound proxy and local mock HTTP responses. No real
OAuth credential was copied or used by any second gateway instance.

- Management/config compatibility: **8.0.7 37/37; 8.0.30 33/37**. Four failures
  are immediate/delayed sign-in credential/plan persistence, described above.
  Startup, strict config validation, effective policy, unauthenticated 401s,
  identical pinned panel bytes, file listing/download shape, priority/status
  mutations/readback, quota/reset/sign-in relay envelope, missing-file refresh
  error, models and usage-queue shape checks passed.
- No unknown/deprecated config warnings. Expected warnings were blocked
  Antigravity version refresh (both), blocked Grok npm refresh (8.0.30), and
  deliberately exercised 401/404 requests. No upstream calls escaped isolation.
- The initial fast-mode preparation required a scratch log-location adaptation.
  After the repository verifier fix, the **unmodified** isolated harness passed
  **53 HTTP cases on 8.0.7 and 53 on 8.0.30**, plus synthetic two-account failover,
  policy/config reload, hot library replacement, marker lifecycle, isolated
  restart and websocket warning checks. No log normalization was used for these
  follow-up runs. The known duplex follow-up policy limitation remains.
- Follow-up focused tests: **47 installer tests passed**, including source-line
  shift, wrong-event/plugin/version/path and malformed-log cases; **16 existing
  sign-in fields tests passed**. These synthetic unit tests do not establish
  8.0.30 import compatibility.
- The compatibility harness was rerun unchanged: **8.0.7 37/37; 8.0.30 33/37**,
  reproducing the same four sign-in failures. A separate direct call to
  `SigninManager.import_tokens` persisted and verified on 8.0.7 and raised
  `Import verification failed` on 8.0.30. Stable identity and metadata survived
  on both; new credentials did not persist on 8.0.30.
- The upload-lock diagnostic above confirms that switching to a whole-file POST
  would violate the refresh-safety requirement, despite passing uncontended
  persistence/readback on both versions. No real OAuth exchange was attempted.
- All test gateways and mock servers were terminated and waited for; namespaces
  released. No persistent process or listener was created for this task.
- Follow-up scripts, selected tagged source and detailed synthetic results are
  retained in the root-private preparation evidence under
  `implementation-followup-20261015/`; task scratch was removed after archiving.
- An offline merged unit (existing effective unit plus the staged override)
  passed `systemd-analyze verify` with exit 0. Its only diagnostics were two
  unrelated installed XFS units using the removed `CPUAccounting` option.
  Documentation shell blocks pass `bash -n`; local links and evidence counts
  were checked, and `git diff --check` passed.

No full suite/build was necessary for the bounded installer verifier change.
Both finite plugin integration runs used `heavy`. No review loop, Git commit,
push, branch operation or live cutover was run by this implementation worker.

## Cutover commands — orchestrator only, after resolving blockers

**Do not execute while the sign-in blocker remains.** Resolve and review the
integration issue, rerun the affected checks against the exact intended binary,
and choose an idle window. A single restart loses existing in-memory affinity
bindings and interrupts active requests; configured routing/affinity remains.

The version names sort lexically: `zz-gateway-8.0.7.conf` sorts **after**
`zz-gateway-8.0.30.conf`. Leaving both active would still select 8.0.7. Move the
old drop-in to the ignored rollback suffix before activating the new one.
Do not run the nested-to-legacy migration or dashboard rollback script.

Preflight the artifact, baseline and service:

```sh
cd /home/user/Projects/personal-codex-router
printf '%s  %s\n' 87226b1da29006d96b13eea3da7a7b3b8a8761a7dad52207d3a44e51b32fb663 /opt/cliproxyapi/8.0.30/cli-proxy-api | sha256sum -c -
sudo -n python3 - <<'PY'
import hashlib, json
from pathlib import Path
b = Path('/var/lib/cliproxyapi-upgrade-backups/prepare-8.0.30-20261015')
baseline = json.loads((b / 'production-baseline.json').read_text())
for name, path in [('config_sha256', '/etc/cliproxyapi/config.yaml'),
                   ('dropin_sha256', '/etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf'),
                   ('panel_sha256', '/etc/cliproxyapi/static/management.html')]:
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == baseline[name], name + ' changed: revalidate preparation'
print('Preparation inputs unchanged; no secret values displayed')
PY
systemctl show cliproxyapi -p MainPID -p ActiveState -p ExecStart -p DropInPaths
systemctl is-active personal-codex-router-usage.timer personal-codex-router-preference.timer
```

Pause the two timers and wait for existing invocations to finish; do not kill
their active work. Run this block with shell error checking enabled:

```sh
set -e
sudo -n systemctl stop personal-codex-router-usage.timer personal-codex-router-preference.timer
python3 - <<'PY'
import subprocess, time
units = ['personal-codex-router-usage.service', 'personal-codex-router-preference.service']
deadline = time.monotonic() + 130
while True:
    states = [subprocess.check_output(['systemctl', 'show', u, '-p', 'ActiveState', '--value'], text=True).strip() for u in units]
    if all(s in ('inactive', 'failed') for s in states):
        break
    if time.monotonic() >= deadline:
        raise SystemExit('Active invocation did not finish; do not cut over')
    time.sleep(1)
PY
sudo -n systemctl start personal-codex-router-usage.service
sudo -n python3 - <<'PY'
from pathlib import Path
import datetime, hashlib, json, shutil, subprocess
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
b = Path('/var/lib/cliproxyapi-upgrade-backups') / ('cutover-8.0.30-' + stamp)
b.mkdir(mode=0o700)
subprocess.run(['cp', '-a', '/etc/cliproxyapi', str(b / 'etc-cliproxyapi')], check=True)
subprocess.run(['cp', '-a', '/var/lib/cliproxyapi/auth', str(b / 'auth')], check=True)
subprocess.run(['cp', '-a', '/etc/systemd/system/cliproxyapi.service', str(b / 'cliproxyapi.service')], check=True)
subprocess.run(['cp', '-a', '/etc/systemd/system/cliproxyapi.service.d', str(b / 'dropins')], check=True)
(b / 'effective-unit.txt').write_bytes(subprocess.check_output(['systemctl', 'cat', 'cliproxyapi']))
manifest = {str(p.relative_to(b)): hashlib.sha256(p.read_bytes()).hexdigest() for p in b.rglob('*') if p.is_file()}
(b / 'sha256.json').write_text(json.dumps(manifest, indent=2))
print('Fresh private cutover backup:', b)
PY
# REQUIRED GATE: only after a separately reviewed dashboard/gateway import fix.
# Deploy the compatible dashboard while the gateway still runs 8.0.7.
# Existing installer backs up its explicit manifest and atomically installs
# root:root 0644 dashboard files under /opt/personal-codex-router/dashboard.
# It restarts dashboard + preview, checks gateway PID stays unchanged, and
# does not restart the gateway. It includes signin.py and its dependencies.
python3 scripts/install-operations.py --account-access-only
sudo -n python3 scripts/install-operations.py --account-access-only --apply
# Timers are paused here, so aggregate `ok` (which includes collection health)
# can legitimately be false. Gate only on dashboard and gateway readiness.
curl --fail --silent -H 'X-Router-Request: 1' http://127.0.0.1:8320/api/health | python3 -c 'import json,sys; r=json.load(sys.stdin); assert r["health"]["gateway"]["status"] == "healthy"; print("Dashboard and gateway ready")'
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf.rollback-8.0.30
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf.rollback-8.0.30
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.staged /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf
sudo -n systemctl daemon-reload
sudo -n systemctl restart cliproxyapi
sudo -n systemctl start personal-codex-router-usage.timer personal-codex-router-preference.timer
```

The dashboard deploy commands are a **future gated step**, not an instruction
to deploy the current unchanged sign-in module. Complete and review the import
fix first. Deploy/verify the compatible dashboard **before** selecting 8.0.30 and
restarting the gateway so no dashboard window uses the incompatible old importer.
The scoped installer also replaces its listed unchanged dependencies and restarts
the preview service; inspect its dry-run manifest. Keep that compatible dashboard
installed during an 8.0.7 binary rollback. This worker executed none of these
installation or restart commands.

If anything fails after pausing timers, restore a working binary selection and
resume both timers. Do not leave collection/controller timers paused. Auth may
refresh while backing up: preserve the backup for recovery, but **do not restore
old credential files during an ordinary binary rollback**.

## Post-cutover checks — orchestrator

```sh
systemctl is-active cliproxyapi personal-codex-router-dashboard personal-codex-router-usage.timer personal-codex-router-preference.timer
systemctl show cliproxyapi -p MainPID -p ExecStart -p DropInPaths
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8317/v1/models
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8317/v0/management/config
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8317/management.html
curl --fail --silent -H 'X-Router-Request: 1' http://127.0.0.1:8320/api/health | python3 -c 'import json,sys; r=json.load(sys.stdin); print({"ok":r["ok"],"gateway":r["health"]["gateway"]["status"],"collection":r["health"]["usage_collection"]["status"]})'
curl --fail --silent -H 'X-Router-Request: 1' http://127.0.0.1:8320/api/overview | python3 -c 'import json,sys; r=json.load(sys.stdin); g=r["gateway"]; print({"online":g["online"],"codex_accounts":len(g["accounts"]),"routing":g["routing"]})'
systemctl show personal-codex-router-preference.service personal-codex-router-usage.service -p Id -p Result -p ExecMainStatus
systemctl list-timers --all personal-codex-router-preference.timer personal-codex-router-usage.timer
```

Expect effective 8.0.30, 401 / 401 / 200 on the three unauthenticated gateway
URLs, two Codex accounts, unchanged strategy/affinity/priorities/model prefixes
and saved routing selection, healthy collection and no drift. Inspect only
version/startup/warning classifications from the new gateway's journal; never
paste raw logs containing credentials. Compare live config/panel hashes privately
with preparation; neither should change. Confirm fast-mode marker/enforcement
uses the new PID. After the next minute, verify the routing timer's latest run
completed successfully and retained the saved selection without drift. Use the
existing collector, never a second reader of the destructive live usage queue.

October 16, 2026: the unchanged-prefix expectation above was intentionally superseded by removal of the Codex `pro1` / `pro2` prefixes through hot reload. Clients now use plain model names; see [Model routes and restore instructions](standard-management-setup.md#model-routes).

One small Codex-provider Responses request through **8317**, with credentials
read internally and only success booleans printed:

```sh
sudo -n python3 - <<'PY'
import json, urllib.request, yaml
from pathlib import Path
c = yaml.safe_load(Path('/etc/cliproxyapi/config.yaml').read_bytes())
body = {'model': 'gpt-6-luna', 'stream': False, 'store': False,
        'instructions': 'Reply only UPGRADE_OK.', 'input': 'Reply UPGRADE_OK.'}
req = urllib.request.Request('http://127.0.0.1:8317/v1/responses',
    data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + c['access']['api-keys'][0], 'Content-Type': 'application/json'})
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with client.open(req, timeout=90) as response:
    result = json.load(response)
    text = ''.join(p.get('text', '') for item in result.get('output', []) for p in item.get('content', []) if p.get('type') == 'output_text')
    ok = response.status == 200 and result.get('status') == 'completed' and 'UPGRADE_OK' in text
    print({'http_status': response.status, 'completed': result.get('status') == 'completed', 'marker_ok': 'UPGRADE_OK' in text})
    assert ok
PY
```

Confirm the normal collector records this response with `failed=0`. Also rerun
the isolated sign-in regression checks after the prerequisite repair; a live
device sign-in needs the user's participation and was not performed here.

## Rollback to 8.0.7

Use the same timer pause/wait procedure above first. No config restoration is
needed: both versions use the existing nested v8 config. The following assumes
the cutover reached activation; inspect filenames if it failed partway through.

```sh
set -e
sudo -n test -f /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf.rollback-8.0.30
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.staged
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf.staged
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf.rollback-8.0.30 /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf
sudo -n systemctl daemon-reload
sudo -n systemctl restart cliproxyapi
sudo -n systemctl start personal-codex-router-usage.timer personal-codex-router-preference.timer
```

Verify effective 8.0.7 and repeat health, access-control, dashboard, routing and
one collected Responses check. Preserve current credentials, routing state,
usage DB, plugin and pinned panel. Do not restore pre-upgrade auth snapshots or
roll back to 7.3.11; that older version needs the separate legacy-config rollback
in the [October 1 record](gateway-upgrade-8.0.7-2026-10-01.md#gateway-rollback).
After a rollback, dashboard sign-in fails closed (`gateway_untested`): 8.0.7 lacks
the credential lock the dashboard's replacement relies on (see
[Codex sign-in replacement](#codex-sign-in-replacement)). Re-sign accounts through
the management UI until 8.0.30 or a newly validated build is back.

## Verified after cutover

The user chose to cut over with the sign-in blocker unresolved, so the gated
dashboard deploy step was skipped (no dashboard change was needed for 8.0.30).

- Cutover: 2026-10-15 19:09:48 UTC. Preflight artifact checksum and preparation
  input hashes (config, 8.0.7 drop-in, panel) matched.
- Fresh private backup:
  `/var/lib/cliproxyapi-upgrade-backups/cutover-8.0.30-20261015T190948Z`.
- Effective command `/opt/cliproxyapi/8.0.30/cli-proxy-api -config
  /etc/cliproxyapi/config.yaml`, new MainPID 1083839; journal reports
  `Version: 8.0.30`. The 8.0.7 drop-in is parked as
  `zz-gateway-8.0.7.conf.rollback-8.0.30`.
- Access control: `/v1/models` 401, `/v0/management/config` 401,
  `/management.html` 200.
- Dashboard `/api/health`: ok, gateway healthy, collection healthy. Overview:
  online, two Codex accounts, routing round-robin with 1h session affinity and
  subagent affinity unchanged.
- One `gpt-6-luna` Responses request through 8317 completed with the marker.
- Usage and preference services both ran successfully after the timers resumed.
- Three `POST /v1/responses` requests returned 400 within the first second after
  startup (client traffic in flight during the restart); no further 4xx POSTs
  were observed in the following minutes.
- Not verified: dashboard sign-in import (known broken, accepted). Fixed on
  2026-10-16 by the [locked direct replacement](#codex-sign-in-replacement),
  pending deployment and a live sign-in.
