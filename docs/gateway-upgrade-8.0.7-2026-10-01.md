# Gateway upgrade to 8.0.7 — 2026-10-01

**Current status (2026-10-27):** 8.0.30 is live; 8.0.43 is installed and staged, with 8.0.30 retained for rollback ([current runbook](gateway-upgrade-8.0.43-2026-10-26.md)). This page preserves the October 1 history and its 7.3.11 rollback; 8.0.7 is not the current rollback target.

Production CLIProxyAPI was upgraded from 7.3.11 to 8.0.7 at approximately 00:57 UTC. The config was subsequently migrated to the nested v8 layout at approximately 01:27 UTC; see the [migration record](gateway-config-v8-migration.md#performed-2026-10-01).

Reason: upstream [issue #6247](https://github.com/router-for-me/CLIProxyAPI/issues/6247), fixed by [commit 9e71c20](https://github.com/router-for-me/CLIProxyAPI/commit/9e71c20) and released in v8.0.7. An upstream disconnect before the first payload now fails over to the next credential, with a 60-second cooldown on the failing credential, and is classified as `server_error` instead of HTTP 408 `invalid_request_error`. Partial-output disconnects are unchanged and out of scope. Evidence: [8.0.7 evaluation](gateway-upgrade-evaluation-v8.0.7.json) and [September 30 evaluation](gateway-upgrade-evaluation-2026-09-30.json).

Pre-upgrade isolated compatibility checks used a sanitized production config. All management calls consumed by this project returned identical consumed shapes: 7.3.11 passed 34/34 checks; 8.0.7 passed 37/37. Usage collection was identical. Hot reload, the pinned panel, and the dashboard management key worked. v8 drops `codex.identity-confuse`; it is false in production, so there is no effect.

## Applied

- Installed the verified binary at `/opt/cliproxyapi/8.0.7/cli-proxy-api`, root-owned, mode `0755`, SHA-256 `3ee48dcfdf1d6aa352712ac47dde0a67bdfcaeeb790ea2a54ddd4fab37dfba51`. `/opt/cliproxyapi/7.3.11` is untouched.
- Private upgrade backup: `/var/lib/cliproxyapi-upgrade-backups/20261001T005730Z`, root-owned, mode `0700`. It contains `/etc/cliproxyapi`, the auth directory, unit and drop-ins, effective unit, and SHA-256 manifest. Router snapshot: `20261001T005737Z`.
- Paused `personal-codex-router-usage.timer` and `personal-codex-router-preference.timer`, then performed a final usage drain.
- Added `/etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf`. It resets `ExecStart` to `/opt/cliproxyapi/8.0.7/cli-proxy-api -config /etc/cliproxyapi/config.yaml`, preserving the arguments from `model-catalog-updates.conf`.
- Ran `daemon-reload` and one gateway restart; ready in 0.3 seconds. Resumed both timers.

Verification: startup reported 8.0.7 with no warnings. `config.yaml` SHA-256 was unchanged. Management and `/v1` returned 401 without a key; `management.html` returned 200. Dashboard readback confirmed both accounts, priorities, routing strategy, affinity settings, quota and credit reads unchanged. A live `POST /v1/responses` returned 200 and was recorded by the usage collector with `failed=0`. Health was OK.

For later verification, check the effective service command, startup version and warnings, binary checksum, both timers, dashboard settings and health, and collection of a small live response. Keep management on Linux loopback.

## Gateway rollback

The live config is now nested. Use a maintenance window for the restart and **restore the legacy config before rolling back the executable**. The fresh pre-migration file is `/var/lib/cliproxyapi-upgrade-backups/migrate-20261001T012637Z/config.legacy.yaml`. Follow the [migration rollback procedure](gateway-config-v8-migration.md#rollback-of-the-performed-migration): verify the private manifest, back up the current config and reconcile any later changes, pause both timers and wait for active invocations, then write the legacy bytes into `/etc/cliproxyapi/config.yaml` in place, preserving inode/owner/mode and flushing/fsyncing. Confirm 8.0.7 hot-reloads it successfully. Never start 7.3.11 on a nested v8 config.

Only after restoring and verifying the legacy file, remove the binary-selection drop-in and restart:

```sh
sudo rm /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.7.conf
sudo systemctl daemon-reload
sudo systemctl restart cliproxyapi
```

Verify startup reports 7.3.11 and repeat the runtime checks above, then resume both timers and check collection and health. Do not use `scripts/rollback-dashboard.py` for gateway rollback: it deliberately refuses nested files and targets dashboard activation.

## Completed native config migration

Completed on 2026-10-01 at approximately 01:27 UTC. The live gateway could not write its root-owned config under `ProtectSystem=strict`, so the verified 8.0.7 binary migrated a private copy using one native write in an isolated network/mount namespace. After private equivalence checks, the nested bytes were installed in place and hot-reloaded without restarting the gateway. Routing, affinity, priorities and both accounts were preserved; live response collection and health passed. A later reboot brought the gateway back on 8.0.7 with the nested config.

See [gateway-config-v8-migration.md](gateway-config-v8-migration.md) for the performed steps, checks, repeat procedure and rollback. The activation and verification scripts handle both layouts through [scripts/gateway_config.py](../scripts/gateway_config.py), and [scripts/rollback-dashboard.py](../scripts/rollback-dashboard.py) keeps its exact-hash guard and refuses nested files.
