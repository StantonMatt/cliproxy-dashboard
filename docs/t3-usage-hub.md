# T3 usage hub

The dashboard serves T3 Code's optional usage hub from usage observations it already holds. Add it under **Settings → Providers → Usage providers → Add hub**. Each machine uses its own read-only shim token, never the CLIProxyAPI management key. This does not change the provider configuration T3 uses to run models.

## Security and cache behavior

The shim shares the existing Linux dashboard listener on `127.0.0.1:8320`. The Mac uses the existing SSH forward at `127.0.0.1:18320`. Do not use a path prefix, port 8317, the Tailscale dashboard proxy on 8409, or the gateway management key.

Only these exact operations are served:

- `GET /v0/management/auth-files`: an allowlisted list of account IDs, provider, email, disabled flag, and optional Codex workspace ID. `id_token` here is an object containing only `chatgpt_account_id`, never a JWT.
- `POST /v0/management/api-call`: only method `GET`, a listed shim `auth_index`, and the account provider's exact usage URL (`https://chatgpt.com/backend-api/wham/usage` or `https://api.anthropic.com/api/oauth/usage`). Headers may be supplied in T3's normal format; they are validated and discarded. Unknown fields, URL variants, queries, fragments, and other methods are rejected. The body limit is 16 KiB with a three-second total read deadline.

All other management operations are denied, including credential downloads/uploads, config changes, cooldown resets, reset-credit listing and redemption. Requests with `Origin` or `Sec-Fetch-Site` are rejected, including same-origin browser requests. Existing Host restrictions apply. These two routes do not need `X-Router-Request`. The shared Tailscale proxy's existing route allowlist excludes the entire management namespace; the shim also rejects its transport marker.

Tokens are `t3u_` followed by 32 random bytes encoded as 64 lowercase hex characters. The key file is `/etc/personal-codex-router/t3-usage-keys.json`, owned by `root:cliproxyapi`, mode `0640`, with independent `linux` and `mac` entries. It is reread on every request; revocation needs no restart. All configured tokens are compared with constant-time comparison. Missing, unreadable, malformed, duplicate or empty key configuration disables the shim with a sanitized 401. Other dashboard routes continue working. The gateway key is never loaded by the shim and does not authenticate it. Tests override the key path on the scratch HTTP server; there is no new production environment setting.

Handlers cannot invoke provider requests, gateway management, dashboard refresh, reset services or CLI probes. Codex quota polling retains a private allowlisted projection with the original primary/secondary slots, usage plan, and identity binding. This projection is stripped from existing dashboard API responses. Claude uses the existing sanitized watch-cache parser in usage-only mode, bypassing the reset-service socket. Claude B's existing CLI-derived watch observations are supported without running the CLI.

Codex observations expire after 180 seconds. Claude observations expire after 900 seconds; B's windows each retain their own measurement time, so an expired model window is omitted even when another window is fresh. B's fresh windows can still be served when an older primary window makes its aggregate watch status stale. Failed observations are unavailable. Missing windows stay absent; no zero-usage windows are synthesized. Empty results return an HTTP-200 protocol envelope with `status_code: 503`, which T3 renders as unavailable. Reset timestamps in Claude bodies are ISO strings or null; Codex resets are epoch seconds.

Shim IDs are namespaced and include the cached runtime identity binding (`provider:cached-id:identity`, plus the workspace ID when present), so a previously listed request cannot silently select a replacement identity. Missing or colliding IDs, emails, workspace IDs or identity bindings fail closed. Claude cache email must uniquely match a current cached runtime identity. A fresh successful gateway identity inventory (at most 180 seconds old) is required for usage on both providers. External account changes become visible on the next normal dashboard poll; a hub request never polls to detect them.

## Protocol evidence

Read-only research on 2026-10-27 confirmed:

- Linux `0.1.1-nightly.20261018.3315`, installed AppImage `/home/user/.local/opt/t3-code-nightly/T3-Code.AppImage`: `apps/server/dist/binCli-Bhw4WaiS.mjs`, `src/usage/cliproxyApi.ts` region lines 310840–311076 and `src/usage/UsageLimitSources.ts` beginning at 311078.
- Mac `0.1.2-nightly.20261021.3473`, `/Applications/T3 Code (Nightly).app/Contents/Resources/app.asar` on `macbook`: `apps/server/dist/binCli-HOOJOCti.mjs`, corresponding region lines 323899–324162 and usage source region beginning at 324164.

Both builds send `Authorization: Bearer <managementKey>` to list accounts, then POST `{auth_index, method:"GET", url, header}` to `api-call`. They decode `{status_code:number, body:string}`. Codex windows require numeric `used_percent`, with optional numeric/null `reset_at` and optional numeric `limit_window_seconds`. Claude primary windows require numeric `utilization` and string/null `resets_at`; model limits use `kind:"weekly_scoped"`, numeric `percent`, optional string/null `resets_at`, and `scope.model.display_name`.

Both call `credits(config, account).pipe(orElseSucceed(() => void 0))` after decoding Codex usage (Linux around 311015, Mac around 324100). A credit-list HTTP failure is therefore optional: quotas remain available and `resetCredits` is absent. The shim returns HTTP 403 for that URL. Usage failures instead reach `makeUnavailableUsageLimits`. Disabled accounts are filtered before polling. The optional Codex workspace ID becomes T3's `workspaceId` and `Chatgpt-Account-Id` header, so it is included when available.

Linux resolves `new URL('/v0/management/...', config.url)` from the origin. Mac resolves `new URL('v0/management/...', config.url + '/')`, retaining a prefix. Use bare origins for compatibility. Mac additionally accepts `id_token.plan_type` as a plan fallback; the shim supplies the observed usage `plan_type` when present, which both builds accept.

The unit fixtures are transcribed from these regions. The shim's actual fixture account list and four usage envelopes (including a secondary-only Codex window and null Claude reset) were also accepted by the unmodified schema definitions and Effect decoders extracted from **both** bundles. No app was started or settings changed for this check. This is protocol validation, not a live T3 settings or UI acceptance test.

To reproduce extraction, run the Linux AppImage with `--appimage-extract` in a task-owned directory under `~/.cache/agent-scratch/personal-codex-router/t3-bundle/`, and copy only the Mac app.asar over SSH into that scratch directory. An ASAR reader can read the four initial little-endian uint32 values, parse the JSON header using the fourth value as its length, then read each entry at `8 + second_value + int(entry.offset)` for `entry.size` bytes. Extract the server dist `.mjs` chunks, then take the schema/decoder declarations before `isUsageLimitSourceError` from the named region with their original schema/Effect chunk imports. Use `runPromise(decodeAuthFiles(...))`, `runPromise(decodeApiResponse(...))`, and the provider's string-body decoder. Keep this optional check outside the repository; delete the extracted application files after verification.

## Deploy

These commands are an operator procedure; implementation does not execute them. Review the combined backend change before deployment. The scope ships the complete `server.py` in this checkout, including any integrated work from other tasks; inspect that combined diff first.

From the task worktree on Linux:

```sh
cd /home/user/.worktrees/personal-codex-router/t3-usage-hub
python3 scripts/install-operations.py --t3-usage-only
sudo -n python3 scripts/install-operations.py --t3-usage-only --apply
```

This scope backs up only replaced `dashboard/server.py` and `dashboard/t3_usage.py` if present, installs those two files, and restarts only `personal-codex-router-dashboard.service`. It does not reload units, restart the gateway or preview, touch provider credentials/configuration, provision tokens, or change T3 settings. It checks the gateway PID and Codex process fingerprint before and after. Snapshot/status files are private under `/var/lib/personal-codex-router-backups/t3-usage-deployments`; the key file is excluded. Other installer scopes that ship `server.py` also include the new module.

After deployment allow the normal dashboard poll to populate the cache. Do not add a new collector, manual probe or refresh loop for T3. Focused checks are:

```sh
python3 -m unittest discover -s tests -p test_t3_usage.py -v
python3 -m unittest discover -s tests -p test_install_operations.py -v
python3 -m unittest discover -s tests -p test_dashboard.py -q
```

Run the full Python suite through `~/.local/bin/heavy python3 -m unittest discover -s tests -p 'test_*.py'` when required. Tests use fixture caches and scratch loopback listeners, not the live gateway.

## Provision independent machine keys

Run separately on Linux after approving token provisioning. This command refuses to replace an existing key file, creates both 256-bit tokens without printing them, and publishes the complete file atomically with the required owner and mode:

```sh
sudo -n python3 - <<'PY'
import grp, json, os, secrets, tempfile
from pathlib import Path
path = Path('/etc/personal-codex-router/t3-usage-keys.json')
path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
fd, name = tempfile.mkstemp(prefix='.t3-usage-keys-', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o640)
        os.fchown(stream.fileno(), 0, grp.getgrnam('cliproxyapi').gr_gid)
        json.dump({machine: 't3u_' + secrets.token_hex(32) for machine in ('linux', 'mac')}, stream)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.link(name, path)  # Exclusive: never overwrite existing keys.
finally:
    os.unlink(name)
print('Usage hub machine keys provisioned; no token output.')
PY
```

Copy the Linux token directly into the local clipboard, without terminal output or shell variables:

```sh
sudo -n python3 - <<'PY' | wl-copy --paste-once
import json, sys
with open('/etc/personal-codex-router/t3-usage-keys.json') as stream:
    sys.stdout.write(json.load(stream)['linux'])
PY
```

On the Mac, copy its token through SSH directly into `pbcopy` (use the existing Linux SSH alias `router`, or its configured Tailscale SSH hostname):

```sh
ssh router 'sudo -n python3 -' <<'PY' | pbcopy
import json, sys
with open('/etc/personal-codex-router/t3-usage-keys.json') as stream:
    sys.stdout.write(json.load(stream)['mac'])
PY
```

## Configure T3 on each machine

In **Settings → Providers → Usage providers → Add hub**, set:

- Label: **Personal Router**.
- Linux URL: **http://127.0.0.1:8320**.
- Mac URL: **http://127.0.0.1:18320**, using the existing SSH forward to Linux 8320. Do not start a duplicate tunnel. If a forward is absent, establish it with `ssh -N -L 127.0.0.1:18320:127.0.0.1:8320 router` and keep that owned process available while T3 needs it.
- Management key field: paste **that machine's shim token** from the clipboard.
- Enable the hub and save. Keep existing model provider settings as they are.

Clear the clipboard after pasting (`wl-copy --clear` on Linux; `printf '' | pbcopy` on Mac). Confirm enabled account usage is shown, missing windows remain absent, and no reset-credit controls appear. A stale or failed observation should be unavailable. The UI may show T3's polling time; the shim enforces source age before serving anything.

## Rollback and revocation

Remove or disable **Personal Router** in T3 on the affected machine. Revoke its token on Linux with the command below (choose `linux` or `mac` in the literal `machine` variable). This atomically preserves the other machine's token and the file permissions, prints no tokens, and requires no service restart:

```sh
sudo -n python3 - <<'PY'
import grp, json, os, tempfile
from pathlib import Path
machine = 'mac'
path = Path('/etc/personal-codex-router/t3-usage-keys.json')
with path.open() as stream:
    keys = json.load(stream)
keys.pop(machine, None)
fd, name = tempfile.mkstemp(prefix='.t3-usage-keys-', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o640)
        os.fchown(stream.fileno(), 0, grp.getgrnam('cliproxyapi').gr_gid)
        json.dump(keys, stream)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, path)
finally:
    if os.path.exists(name):
        os.unlink(name)
print('Selected machine token revoked; no token output.')
PY
```

To rotate rather than revoke, replace `keys.pop(machine, None)` with `keys[machine] = 't3u_' + secrets.token_hex(32)` and add `secrets` to the imports, then copy the new token privately into that machine's hub. Removing all entries disables the shim.

For a backend rollback, read the scoped `status.json` to locate the recorded snapshot and verify it with `scripts/router-backup.py verify <snapshot-directory>`. Restore only its saved backend files under `files/opt/personal-codex-router/dashboard/` with root ownership and `0644` mode, then restart only the dashboard. An initial deployment has no old `t3_usage.py` to restore; leaving that unused module alongside the restored pre-shim server is harmless. Do not restore live credentials/configuration or restart the gateway for this rollback.
