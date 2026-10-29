# Backups and recovery

Daily snapshots run on Linux at **04:15–04:25 America/Santiago**, including while the Mac and Linux GUI are closed. A persistent systemd timer catches up after downtime. The last successful snapshot is shown in the dashboard; more than 36 hours old is stale, and a failed run is shown separately from the last success.

Snapshots live in `/var/lib/personal-codex-router-backups/`, owned by root with directory mode `0700`. Files inside snapshots are `0600`; original ownership and modes are recorded in the manifest for deliberate recovery. The dashboard sees only summary metadata in `/var/lib/personal-codex-router-ops/backup.json`, not backup contents or credentials.

**Accepted limitation: these are same-disk backups containing credentials.** The user chose to accept this arrangement. Snapshots in `/var/lib/personal-codex-router-backups/` include the gateway management key, gateway-owned Codex and Claude OAuth account files, Codex authentication and provider configuration, and the dedicated loopback SSH key. `/home` and `/var/lib` share the same NVMe filesystem, so disk loss or compromise affects both originals and backups, including those credentials. This protects against accidental settings changes and failed deployments, not disk failure, theft, or loss of the Linux computer. No off-device destination is configured. Copying these snapshots elsewhere requires a private, encrypted destination.

## Contents and retention

- Gateway settings, management asset/key, gateway-owned Codex and Claude OAuth account files, router settings and billing confirmations.
- Installed dashboard/scripts and this source project, plus relevant systemd units and preview firewall/configuration.
- Usage database, created with SQLite's online backup API and checked with `integrity_check`. WAL and SHM sidecars are never copied.
- Pending allowlisted usage spools, including interrupted temporary files and the `failed/` subdirectory, preserved as stable bytes even when malformed.
- Shared Codex `config.toml`, `auth.json`, `AGENTS.md`, saved global/sidebar settings, daemon settings, startup unit, separate Linux client settings, launcher, desktop entry, and dedicated loopback SSH configuration/key.
- Durable reset request IDs and automatic policies, notification episodes, legacy dismissal flags and desktop delivery deduplication state, and routing baseline.

Task histories, active Codex databases, worktrees, model binaries, browser profiles, unrelated project files, and unrelated credential stores are excluded. This is settings recovery, not a complete computer or Codex task-history backup. Remote pairing may need to be re-established if other application state is lost.

Each ordinary file is read only when stable; JSON/TOML files are parsed before acceptance. This is a verified collection of individually consistent files over a short interval, not a globally atomic snapshot of every running service. Source SQLite databases stay live and use their supported backup API. Completed snapshots are checksum verified before success is recorded. Failed/incomplete snapshots remain private and are never counted as successful.

Completed snapshots older than **14 days** are removed after a successful backup, keeping at least the latest two even after extended downtime. Incomplete runs and restore rehearsals are excluded from automatic pruning and should be inspected if disk space becomes an issue. Pre-existing manual backup directories are untouched:

- `/var/lib/cliproxyapi-dashboard-backups/20260922T162042Z`
- `/home/user/.local/state/personal-codex-router/backups/`
- `/home/user/.codex/gateway-integration/` (dated directories)

## Run and inspect

```sh
sudo systemctl start personal-codex-router-backup.service
systemctl list-timers personal-codex-router-backup.timer
sudo cat /var/lib/personal-codex-router-ops/backup.json
sudo ls /var/lib/personal-codex-router-backups/
```

Only the last command lists names. Do not print backup configuration/authentication files in a task or paste their contents into chat.

Use the snapshot name reported by `backup.json` in the next commands. Verification and rehearsal perform **no production restore**:

```sh
sudo python3 /opt/personal-codex-router/router-backup.py verify \
  /var/lib/personal-codex-router-backups/SNAPSHOT
sudo python3 /opt/personal-codex-router/router-backup.py rehearse \
  /var/lib/personal-codex-router-backups/SNAPSHOT \
  /var/lib/personal-codex-router-backups/rehearsals/NEW-UNIQUE-NAME
```

Rehearsal refuses existing destinations and production paths, stages files privately, and verifies every checksum and copied database. The September 24 deployment's rehearsal is `/var/lib/personal-codex-router-backups/rehearsals/20260924T150109Z`, with 144 files verified. It leaves current databases and settings untouched.

## Deliberate restore procedure

1. Identify the affected component, take a fresh snapshot if possible, and verify the selected older snapshot. Review its private `manifest.json` locally to identify exact files, ownership, modes, and completion time. Do not replace files changed since that snapshot without checking those differences.
2. Run the rehearsal above. Files are under `REHEARSAL/files/` followed by their original absolute path with the leading `/` removed. Recovery copies must restore each file's original owner, group, and mode from the manifest; staged secret files remain `0600`.
3. **Dashboard code only:** copy the selected installed files from `files/opt/personal-codex-router/dashboard/` into the installed dashboard, root-owned `0644`. Restore matching Python modules and static assets together. Restart only `personal-codex-router-dashboard.service`, then verify the overview, both accounts, automatic toggles, and reset balances. The initial pre-deployment runtime is in snapshot `20260924T150109Z`; it predates the operations module. Do not overwrite reset or usage state for a code rollback.
4. **Router state or usage database:** schedule a maintenance moment; stop only the dashboard and usage collector/timer before replacing their state. Preserve the current copies, including SQLite sidecars. Install the verified database without old WAL/SHM sidecars, owned by `cliproxyapi:cliproxyapi`, mode `0600`, in a directory mode `0700`. Restart only those components and verify records. Do not overwrite newer reset request IDs or automatic settings with older ones casually; the deterministic IDs prevent selecting a different request for the same credit, but older outcome records still need reconciliation with the provider.
5. **Gateway configuration or OAuth:** do not restore while active model requests are running. Prefer a reviewed in-place configuration update preserving the watched inode; verify live management settings after hot reload. Atomic file replacement does not reliably trigger this gateway version's watcher. Do not restart the gateway without a maintenance moment. Preserve both accounts, routes, affinity and current credentials; restoring an old OAuth token may require signing in again. A config backup whose `routing.session-affinity-ttl` parses to a different duration from the live value (for example `1h0m1s` against `1h` or `1h0m0s`) hot-reloads like a conversation move and releases every Codex and Claude affinity pin. After any move the config's SHA-256 has changed, so the hash-pinned fast-mode rollback (`scripts/install-fast-mode-plugin.py --rollback`) refuses until it is re-pinned. Stop `personal-codex-router-move.path` before restoring and start it afterwards; see [Move conversations](move-conversations-2026-10-17.md).
6. **Shared Codex configuration/SSH:** wait until tasks are idle before changes requiring daemon restart. Restore only selected configuration files and the dedicated loopback SSH key with original permissions. Do not restore PID records, sockets, lock files, live databases, or overwrite task histories. Recover the `codex` executable using its normal installation/update mechanism if missing. Re-enable `codex-shared-server-start.service` in the user manager and user linger if rebuilding the machine. Use the separate **Codex — Shared Linux** launcher, then verify `codex app-server daemon version`, Mac SSH, and phone Remote access.
7. Check service health, both account identities, routing/affinity, automatic reset toggles, latest usage records, and a fresh backup. Confirm each affected client can reconnect before calling recovery complete.

To stop future scheduled backups without deleting existing snapshots:

```sh
sudo systemctl disable --now personal-codex-router-backup.timer
```
