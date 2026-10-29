# Migrating config to v8

The live `/etc/cliproxyapi/config.yaml` uses the nested `config-version: 8`
layout following the migration on 2026-10-01. As verified 2026-10-27, the gateway
runs CLIProxyAPI 8.0.30 through
`/etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.30.conf`; see the
[October 15 activation](gateway-upgrade-8.0.30-2026-10-15.md).

8.0.43 is installed with an ignored staged drop-in; cutover is pending. The
current nested config needs no migration for that upgrade or its rollback to
8.0.30. See the [October 26 preparation](gateway-upgrade-8.0.43-2026-10-26.md).
8.0.7 and the migration rollback below are historical, not the current binary
rollback target.

Current retry policy is `request-retry: 5`, `max-retry-interval: 45` seconds and
`max-retry-credentials: 2` under `routing.retry`. The `0` in the October 1
migration record below was the value preserved at that time.

Migration is a separate, deliberate maintenance change. Upgrading the executable
to 8.0.7 can retain a legacy flat config. An authenticated GET does not migrate
the file; a successful native `PATCH /v8/management/config` does. Legacy
`/v0/management` reads continue to work after migration. The procedure below is
for repeating the migration from a legacy file.

## Performed 2026-10-01

Migration completed at approximately 01:27 UTC. The live file was
`root:cliproxyapi`, mode `0640`, and the service used `ProtectSystem=strict`.
The gateway could not write its config, so the native write ran against a
private copy offline, without loosening the live service's protections.

1. Created a fresh exact backup at
   `/var/lib/cliproxyapi-upgrade-backups/migrate-20261001T012637Z`.
   `config.legacy.yaml` holds the original bytes. The private manifest records
   the live inode, owner/mode, SHA-256 of the config, binary and `management.html`,
   and effective unit.
2. Used the verified 8.0.7 binary in an isolated `unshare --net --mount`
   namespace with only its own loopback, the real auth directory masked by an
   empty tmpfs, `/etc/cliproxyapi` bind-mounted read-only, and proxies pointed at
   a closed port. One native `PATCH /v8/management/config` set
   `routing.retry.request-retry` to its unchanged value, `0`. The private copy
   became `config-version: 8`.
3. Compared both files privately: all 16 settings mapped by
   [scripts/gateway_config.py](../scripts/gateway_config.py) matched, every
   legacy value was present in the nested file, and both secret-bearing values
   were preserved. Server host/port (8317), TLS off, and
   `oauth.auth-dir: /var/lib/cliproxyapi/auth` were unchanged.
4. Paused `personal-codex-router-usage.timer` and
   `personal-codex-router-preference.timer`, confirmed that the live file still
   equalled the backup bytes, and wrote the nested bytes in place with fsync.
   Inode `84411907` and owner/mode were unchanged. The gateway hot-reloaded
   without a restart: `config_reload` reported `config successfully reloaded`
   with 2 clients. Resumed both timers.
5. Verified management and `/v1` returned 401 without a key,
   `management.html` returned 200, routing/affinity and account priorities were
   unchanged, and there was no drift. A live `/v1/responses` returned 200 and
   the usage collector recorded it with `failed=0`; health was OK. After the
   later reboot at 2026-09-30 22:36 local (2026-10-01 01:36 UTC), the gateway
   came back on 8.0.7 with the nested config.

## Repeat procedure

Before migrating, deploy and verify the schema-aware activation and verification
scripts with `gateway_config.py`. The enable script still refuses an existing
management key; do not rerun it to migrate or update an enabled dashboard. The
verification scripts accept `--binary`, otherwise selecting the effective
`cliproxyapi` ExecStart executable, with `/opt/cliproxyapi/8.0.7/cli-proxy-api`
as the fallback. Verification that uses copied real OAuth credentials requires
its own authorized scope.

1. Choose an idle maintenance window and take a **fresh exact byte backup** of
   the legacy config outside this repo, preserving mode and owner. Record its
   SHA-256, the effective service/drop-ins, executable hash, pinned panel hash,
   and current accounts, model routes, priorities, routing strategy, affinity,
   retry, cooling and usage policy in a private migration manifest. Keep the
   historical dashboard activation backup and `docs/activation.json` intact.
   Resolve any stale `codex.identity-confuse` setting before proceeding: v8
   removes it without an equivalent setting.
2. **When the gateway cannot write its config, use the offline isolated-namespace
   method.** Keep the live service protections intact. Make a private writable
   copy outside `/etc/cliproxyapi`, then run the verified 8.0.7 executable against
   that copy in `unshare --net --mount`. Make mount propagation private before
   mounting anything so namespace mounts cannot affect the host. Enable only
   the namespace's own loopback, mask the real auth directory with an empty
   tmpfs, bind-mount `/etc/cliproxyapi` read-only, and direct proxies to a closed
   port before starting the isolated process. Do not copy real OAuth credentials
   into it. Use the existing management key privately over namespace loopback
   for **one deliberate native write**: PATCH the current unchanged
   `routing.retry.request-retry` value. Keep key material, request headers,
   config bytes and secret comparisons out of logs and this repo. Stop the
   isolated process and release the namespace after collecting its private
   result. If an already-authorized maintenance workflow allows the gateway to
   write, the same unchanged-value PATCH can run there; do not loosen live
   protections just to migrate or enable remote management.
3. Check the candidate's `config-version: 8` layout. Compare all settings mapped
   by `gateway_config.py`, every legacy value, and secret-bearing values
   privately against the fresh backup. Pause both usage and preference timers
   and wait for their active invocations to finish before installing the file.
   Confirm the live bytes still match the fresh backup; if they changed, stop
   and prepare a new candidate from a new backup. Write the complete nested
   bytes into the live file **in place**, preserving its inode, owner and mode,
   then flush/fsync. Do not use atomic replacement: it is not reliable for this
   watcher. Confirm `config_reload` reports `config successfully reloaded` and
   compare the effective policy with the private manifest through authenticated
   legacy reads.
   Verify both account identities and model routes, strategy/affinity,
   retry/cooling, prefix requirements, Antigravity credits, usage enablement,
   loopback-only management, firewall-restricted inference access, server
   host/port/TLS, auth directory, and pinned panel. The current empty
   `server.host` gives `*:8317`; nftables permits loopback and the Mac over
   Tailscale only. Mac inference is direct; management uses its SSH forward.
   Check no-key requests still return 401 on management and `/v1`, and the panel
   returns 200. Resume the timers and confirm the existing collector and
   preference timer remain healthy; do not run a second destructive usage-queue
   reader. Verify a small live response is collected with `failed=0`, health is
   OK, and no config drift occurred. Record the nested file hash and runtime
   verification privately.
4. If verification fails, pause both timers again if resumed and wait for active
   invocations to finish. Restore the **complete legacy bytes from this fresh
   backup**, checking their recorded hash, owner and mode and reconciling any
   intervening changes. Preserve the inode and
   fsync when relying on reload; otherwise use a coordinated restart and verify
   runtime again. Any executable rollback must restore the matching legacy file
   first: **never start 7.3.11 on a nested v8 file**. Restore the recorded service
   selection when rolling back the executable, retaining accounts, credentials,
   keys, panel, usage database and routing intent. Resume the timers after
   successful runtime verification.

## Rollback of the performed migration

Use `config.legacy.yaml` from
`/var/lib/cliproxyapi-upgrade-backups/migrate-20261001T012637Z`, checking it
against the private manifest. Capture a fresh private backup of the current
file and check for changes since migration before restoring; reconcile later
changes rather than silently overwriting them. Pause both timers and wait for
active invocations to finish. Write the complete legacy bytes back into
`/etc/cliproxyapi/config.yaml` **in place**, preserving the current inode and
owner/mode, then flush/fsync. CLIProxyAPI 8.0.7 can hot-reload this legacy file;
confirm reload and repeat the runtime checks before resuming the timers.

For a binary rollback, restore and verify the legacy file **before** removing
the 8.0.7 drop-in, running `daemon-reload`, and restarting the gateway. 7.3.11
can only run on the legacy layout. Follow the
[gateway rollback instructions](gateway-upgrade-8.0.7-2026-10-01.md#gateway-rollback)
in a maintenance window. `scripts/rollback-dashboard.py` deliberately refuses
nested files and must not be used for this rollback.

## Removing the dashboard after migration

`rollback-dashboard.py` deliberately refuses nested files, even if their bytes
match an activation manifest. Its existing exact-hash guard and recorded hashes
remain unchanged. It is not a gateway-version rollback tool.

Dashboard removal now requires a **manual, reviewed procedure**: capture a fresh
exact backup and hash of the current nested config, compare it with the original
activation record, and prepare a scoped reversal of the dashboard's management
and usage changes in `management` and `observability.usage`. Review the complete
candidate against the current file, preserving all later routing/provider and
account changes. Guard application against the captured current hash; coordinate
stopping the routing preference timer/invocation, suspending saved intent and
restoring baseline account priorities, then stopping the usage timer/invocation
before the same-inode config edit and runtime verification. Preserve all keys,
panel assets, databases, credentials and backups. Do not change the historical
activation hashes to bypass the guard or restore the pre-dashboard file over
later changes.
