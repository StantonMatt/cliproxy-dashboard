# Personal Codex Router

Dedicated Linux project: `/home/user/Projects/personal-codex-router` on `user-desktop`.

This folder contains the account dashboard setup, usage collector, verification records, and rollback instructions. The existing gateway remains the `cliproxyapi` system service. Work here does not require the unrelated `ai-web-assistant` checkout.

## Open the dashboard

On the Mac, open **Open Router Dashboard.command** from the setup task's outputs folder. It copies the separate management password to the clipboard and opens:

<http://127.0.0.1:18317/management.html#/quota>

At first login, paste the password into **Management Key** and connect. If a server address is requested, use `http://127.0.0.1:18317`. The Quota page shows each connected account, its provider-reported allowance, and reset time. Browser password remembering is optional. The management key is different from the inference key used to log in to Codex.

On Linux, run `scripts/open-dashboard.sh` in the desktop session, or open:

<http://127.0.0.1:8317/management.html#/quota>

The Linux shortcut copies the password when `wl-copy` or `xclip` is available. Both hosts keep their dashboard password in `~/.config/personal-codex-router/management-key`, readable only by the user.

## Add this project to Codex

On the Mac, open Settings → Connections, find the connected `user-desktop` SSH host, and use its folder-plus button. Select `/home/user/Projects/personal-codex-router`. Future dashboard tasks can then run in this project. The folder exists; adding it to the Codex sidebar is the remaining UI step.

## What is installed

- Gateway: stock CLIProxyAPI 8.0.79 with nested-v8 configuration ([config migration](gateway-config-v8-migration.md)). It has been live since November 20 from `/opt/cliproxyapi/8.0.79` (`zz-gateway-8.0.79.conf`), with local KV disabled so session/date-pin/replay state stays in memory as on 8.0.47. The 8.0.47 drop-in is parked as `zz-gateway-8.0.47.conf.rollback-8.0.79`; rolling back removes the KV key first. See the [preparation, checks and rollback](gateway-upgrade-8.0.79-2026-11-19.md).
- Official Management Center: pinned v1.24.2 at `/etc/cliproxyapi/static/management.html`, SHA-256 checked against the upstream release asset. Automatic panel updates are disabled so changes can be validated deliberately.
- Management: enabled with a separate bcrypt-hashed key and `allow-remote: false`.
- Network: empty `server.host` gives a `*:8317` listener. The nftables `tailscale_host_guard` admits loopback and the Mac over Tailscale only; management still rejects remote requests. Mac inference connects directly to `http://100.64.0.10:8317`; management and the computer-only dashboard use SSH forwards.
- Retry policy, verified 2026-10-27: `routing.retry.request-retry: 5`, `max-retry-interval: 45` seconds, `max-retry-credentials: 2`.
- Claude inference: two active gateway-owned accounts, T3 `claudeAgent_gateway` on both machines and Linux shell `claude` through `scripts/claude-gateway.py`; see [Claude routing](claude-routing.md).
- Mac tunnel: `com.user.codex-router-dashboard` LaunchAgent, forwarding only `127.0.0.1:18317` to Linux `127.0.0.1:8317` over the existing Tailscale SSH alias. It starts at login and retries after connection failures; Tailscale and Linux must be reachable.
- Usage: collection enabled. `personal-codex-router-usage.timer` runs an independent collector every 15 seconds; the timer is enabled at boot.
- Usage database: `/var/lib/personal-codex-router/usage.sqlite3`, private to the gateway service account. The collector saves account indices, models, timestamps, latency, failure flags, and token counts. It does not save prompts, responses, API keys, or OAuth tokens.

The service's existing filesystem restrictions remain in force: `/etc/cliproxyapi` is read-only to the gateway process. Make configuration changes through the project scripts or SSH; the dashboard's config-save controls may fail under this intentional restriction.

## Model routes

October 29, 2026: Codex now has one configured identity, `account-a@example.com` (Pro). The Free Codex account was removed on October 29 at the user's request. Both Claude accounts, including Claude B at the retired Codex email, remain unchanged. Codex routing modes converge to neutral priority; there is no account failover. The two-account verification scripts skip with a clear message until two Codex identities are configured again.

Verified 2026-10-27 through the management auth-file and per-account model listings: `account-a@example.com` is Pro and `account-b@example.com` is Free. The Free listing excludes `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol` and `gpt-5.6-sol`; the Pro listing includes all four.

October 16, 2026: removed `pro1` / `pro2` from the two Codex auth records through the loopback management API, without restarting the gateway. Clients use plain model names, and the gateway selects an account whose plan supports the model. The Free account is not offered `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol` or `gpt-5.6-sol`; a direct live test on this date returned HTTP 400, “not supported when using Codex with a ChatGPT account”. Priorities, disabled flags and routing affinity are unchanged.

The T3/Codex catalog published by `~/.local/share/codex-router-models/sync.py` lists eight plain visible slugs as of 2026-10-27: `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6.1-terra`, `gpt-6-sol`, `gpt-5.6-sol`, `gpt-6-luna`, `gpt-5.6-terra` and `gpt-5.6-luna`. Account-prefixed verification below records earlier behavior. The Mac T3 `codex` provider now mirrors the desktop's published catalog every 5 minutes with a user LaunchAgent; see [Mac model catalog](mac-model-catalog.md) for installation, offline behavior, validation and rollback. This leaves the desktop publisher and the Mac's direct-login provider unchanged.

Root-only, byte-for-byte backups are in `/var/backups/cliproxyapi-auth-prefix-20261016/`, with original ownership and modes preserved. To restore routing prefixes, read each backup's `prefix` privately and PATCH only that field via `http://127.0.0.1:8317/v0/management/auth-files/fields`: `{"name":"<original filename>","prefix":"<backed-up prefix>"}` (`pro1` for `account-a@example.com`, `pro2` for `account-b@example.com`). Authenticate using the local management key without printing it; verify the same account identity before and after, unchanged priority/disabled/status, saved prefix and `/v1/models`. Rerun `codex-router-model-access.service` and the user `codex-router-model-catalog.service`. Preserve current tokens; no gateway restart is needed.

## Quotas versus usage history

The Quota page reads the provider's current subscription allowance. It can include activity from before this dashboard was enabled. On 2026-09-22, both quota queries succeeded: account-a@example.com reported 1% weekly usage and account-b@example.com reported 56%. These are dated observations, not permanent values. The provider returned a weekly window for each account; no five-hour allowance was invented.

Request/token history begins when collection is enabled. The collector preserves `cached_tokens` and also stores nullable `cache_read_tokens` and `cache_creation_tokens` independently; older rows and omitted or invalid values remain unknown (`NULL`), with no inferred split. Dashboard account/model summaries for Codex and Claude, and the collector's `--summary`, expose each split sum plus `cache_read_requests` / `cache_creation_requests` (the number of rows with a valid value for that field). A sum is `null` when no value is known, and `0` when known values sum to zero; compare each count with `requests` to detect partial coverage. Recent API rows include both nullable token values. Deploy the collector with the [full installation procedure](../README.md#update-an-existing-installation), which also handles the fast-mode plugin; its next pass adds the columns, and the dashboard can read the database before that migration.

The official panel does not provide our custom persistent token-history view; the [Personal Router overview](../README.md#everyday-access) now displays the collected history. To inspect saved totals from the command line:

```sh
sudo python3 /opt/personal-codex-router/collect-usage.py --summary
```

Account mapping at verification time:

| Account | Usage account index |
| --- | --- |
| account-a@example.com | example-index-a |
| account-b@example.com | example-index-b |

The upstream usage queue removes records when read. The collector commits them to SQLite with full synchronous writes, but an interrupted queue response, a crash between dequeue and commit, or expired uncollected records can produce gaps. This is best-effort operational history, not a billing ledger. Run only one queue consumer.

## Initial validation — 2026-09-22

These results record the original setup, when both Codex accounts were Pro. See [Model routes](#model-routes) for current plans and unprefixed routes, and the [README](../README.md#development-and-validation) for later failover verification.

- Mac → SSH tunnel → dashboard HTML: HTTP 200; release checksum matches.
- Authenticated management: HTTP 200; no-key request: HTTP 401.
- Direct management access through the Linux Tailscale address, even with the valid key: HTTP 403.
- Both upstream quota queries: HTTP 200, Pro plan, current weekly usage/reset data.
- Both the account-a@example.com and account-b@example.com account-prefixed routes for `gpt-6-astra`: completed streaming tests with the expected response markers.
- Collector metadata filtering and database persistence checks passed; live usage records persisted.
- Browser interaction could not be verified because computer-use tooling returned `unsupported Codex auth method: apikey`. First browser login and visual confirmation remain for the user.
- Automatic account failover was not tested by this dashboard setup.

See `docs/activation.json`, `docs/quota-verification.json`, and `docs/streaming-verification.json` for dated evidence.

## Maintenance and rollback

The enable script is guarded against replacing an existing management key. Do not rerun it as an update mechanism.

This gateway version watches the config file inode. Atomic replacement did not hot-reload during setup, so one gateway restart was required. Future reviewed edits should preserve that inode if relying on hot reload; verify the resulting runtime settings.

The live config is now nested, so `rollback-dashboard.py` deliberately refuses
it. Removing management/statistics activation requires the
[manual procedure after migration](gateway-config-v8-migration.md#removing-the-dashboard-after-migration).
The following guarded rollback commands apply only to an eligible legacy config:

```sh
sudo python3 /home/user/Projects/personal-codex-router/scripts/rollback-dashboard.py
```

Apply it only when rollback is intended:

```sh
sudo python3 /home/user/Projects/personal-codex-router/scripts/rollback-dashboard.py --apply
```

It refuses to overwrite configuration changed since setup. It stops the collector, restores the pre-dashboard config, and preserves the usage database, accounts, keys, panel asset, and secure backups. Verify management routes become HTTP 404 after reload. Backups are under `/var/lib/cliproxyapi-dashboard-backups/20260922T162042Z`.

To stop the Mac tunnel:

```sh
launchctl bootout "gui/$(id -u)/com.user.codex-router-dashboard"
```

The LaunchAgent file remains in `~/Library/LaunchAgents/com.user.codex-router-dashboard.plist`; move it out of that folder if it should remain disabled after the next login. To restore the tunnel with the file in place:

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.user.codex-router-dashboard.plist"
```

## Upstream references

- [Official Management Center](https://github.com/router-for-me/Cli-Proxy-API-Management-Center)
- [Management Center v1.24.2](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/releases/tag/v1.24.2)
- [Web UI documentation](https://help.router-for.me/management/webui)
- [Management API documentation](https://help.router-for.me/management/api)

No code was committed or pushed during setup.
