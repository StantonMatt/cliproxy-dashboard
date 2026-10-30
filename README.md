# Personal Router

Setting this up on Windows? See [the Windows setup guide](docs/windows-setup.md).

Shared dashboard: **[http://router:8409/](http://router:8409/)** over Tailscale. MagicDNS is enabled. The name is verified in Linux Chrome, and the user confirmed it works on the phone and Mac. The existing computer-only address `http://router.localhost/` retains full management controls through loopback/SSH.

September 24 operations: [daily backups and recovery](docs/backups-and-recovery.md), [compact health and account notifications](docs/health-and-notifications.md), and [private phone previews](docs/private-previews.md) run on Linux independently of the Mac. **Shared dashboard: http://router:8409/**. Project previews: **http://router:8400/**. Accepted limitation: backups in `/var/lib/personal-codex-router-backups/` contain credentials on the same disk as the originals, so disk loss or compromise affects both. Linux desktop notifications continue; the dashboard no longer shows notice tiles.

Project on Linux: `/home/user/Projects/personal-codex-router`.

Personal Router shows the configured Codex Pro account (`account-a@example.com`) and its gateway usage, with earned-reset controls. The Free Codex account was retired on October 29; both Claude accounts remain independent. It is separate from the general Management Center and from the unrelated ai-web-assistant checkout.

[Codex account access](docs/account-access.md) lets you route Free and paid accounts with a per-account preference, excludes signed-out credentials from routing, offers device-code sign-in on desktop, Mac and phone, and documents scoped deployment.

[Claude subscription usage](docs/claude-usage.md) shows both Claude subscriptions with independent gateway routing. The allowance collectors use Claude Code's current token for A (Max 20x, with confirmed limit resets) and a long-lived setup-token for B (Max 5x). Inference uses separate gateway-owned Claude credentials.

## Earned reset credits

Click Resets at the bottom of the Codex account card to open its modal. It lists expiry dates, changes Use reset to Confirm on the same control, and includes automatic expiry redemption settings. Only the earliest-expiring available credit can be redeemed; the server also enforces this against a fresh balance. The remaining Codex account retains its saved automatic expiry redemption setting.

The Linux dashboard service checks cached deadlines every ten seconds and attempts the specific credit during its final five minutes. Failed or unconfirmed attempts retry on subsequent ten-second checks until success, expiry, or opt-out; network request time can lengthen the interval. It works with the browser closed. Automatic and manual redemption impose no remaining-allowance threshold. The provider decides whether it can restore allowance and returns its result. Expiry redemption still verifies the earliest available, supported credit against a fresh balance. Credits with unknown expiry or already-expired credits are skipped. Network outages or Linux downtime can prevent redemption; exact timing is best effort. This never purchases credits.

Each account/credit has one durable redemption ID shared by manual and automatic attempts, including retries after restarts. Settings, redemption outcomes and the last event are stored privately in `/var/lib/personal-codex-router/reset-controls.json`. The browser only receives allowlisted credit metadata; the gateway retains credentials. Turn off the checkbox per account to disable automation.

Validation: 26 focused Python tests passed on Mac and Linux, JavaScript syntax checked, live balances and enabled toggles verified in the browser. All redemption tests used mocked provider responses; no real credit was consumed. Provider request shape follows the upstream Codex backend client's `redeem_request_id` and `credit_id` contract. This direct provider adapter fails closed when fresh eligibility or response shape cannot be verified.

With one configured Codex account, every Codex routing mode uses neutral priority without needing quota observations. There is no Codex fallback account. The configured roster is `EMAILS` in `dashboard/routing_policy.py`; adding a second identity there restores account selection. The overview publishes `gateway.codex_account_count` (currently `1`), including while an unconfigured auth file awaits deletion. Reset credits, fast mode and recorded usage remain available.

Choose routing from the shared Overview dropdown to apply the same choice to Codex and Claude: round-robin, earliest weekly reset, higher remaining quota, or a preferred email. Split the controls to choose independently per gateway; changing the link setting alone leaves both saved selections intact. Higher remaining quota favours the eligible account with the larger weekly allowance percentage and uses round-robin when percentages match. The controller checks the saved choices every minute. Healthy existing sessions keep their assigned account unless you opt in to **Move all conversations now** after a routing change; that releases both providers' affinity pins for their next request, at most once a minute. In-flight requests finish on their existing account. Moves need the full `install-operations.py --apply`. Scoped installs don't install or update the move helper or its units, so moves keep using what the last full install set up; run a full install whenever the helper or its units change. Without a full install, or on a gateway version moves have not been verified on, routing changes apply immediately as before. See [Claude routing and API contracts](docs/claude-routing.md), [moving conversations](docs/move-conversations-2026-10-17.md), and `docs/routing-selector-2026-09-26.md`.

Claude routing retains all five choices, including either Claude email. Split routing to prefer Claude B, whose email is no longer a configured Codex identity. It defaults to Claude A; the timer leaves Claude priorities untouched until a selection is saved. Claude cards have the same Use for routing switch as Codex, including confirmation before disabling the last routable account. The same minute timer reconciles saved choices independently, using Claude's local usage snapshots (5-hour and weekly windows, at most 15 minutes old). Missing, stale, or uncertain usage on enabled accounts holds the existing priorities until fresh observations arrive. Disabled accounts are excluded. Conversation moves always release both providers. Claude state lives separately under `/var/lib/personal-codex-router-routing/claude/`; Codex priorities, credentials, affinity, and extra-usage settings are independent.

## Everyday access

Open **[http://router:8409/](http://router:8409/)** with Tailscale connected and Tailscale DNS enabled. The shared address shows accounts, usage, health and resets, and allows data refresh, routing changes, automatic reset policy changes, reset redemption, Codex account sign-in, and per-account routing switches. It uses the same private device restrictions as the original IP address; raw gateway management routes are not forwarded.

For computer access through loopback/SSH, use **http://router.localhost/**. On the Mac, an automatic SSH tunnel carries the connection privately to the Linux desktop over Tailscale. On Linux, the local address reaches the dashboard directly. This `.localhost` address does not work on the phone because it refers to the device opening it. Advanced gateway management uses the separate Management Center described below.

The everyday overview needs no management-key login: access comes through the local/private connection. No management key or upstream OAuth token is sent to the browser. Tailscale and the Linux desktop must be available when using it from the Mac. The Mac tunnel starts at user login and retries after interruptions.

Fallback addresses:

- Mac: `http://router.localhost:18320/`
- Linux: `http://127.0.0.1:8320/`
- Shared dashboard with Tailscale: `http://router.tailnet-example.ts.net:8409/` or `http://100.64.0.10:8409/`. The IP fallback does not depend on DNS.

## Pages

- **Overview:** the Codex account and both Claude account cards together at the top, in a **Codex accounts** group and a **Claude accounts** group: side by side on desktop, stacked below 1180px, one card per row on phones. Codex cards show billing renewal date and countdown, remaining allowance, allowance reset countdown and routing availability; Claude A and Claude B cards show usage by email, with a Claude routing selector in their section. Below the cards, the **Codex** region holds routing, health, today’s recorded tokens and requests, cached input, and recent requests. Billing dates include a two-day review buffer; unconfirmed dates are visibly labeled. See `docs/billing-renewals.md` for sources and maintenance.
- **Usage:** **Codex usage**, for today, the last seven days, or all recorded history; account and model totals; a request chart. All-history totals include all records, while its chart shows up to the latest 30 days.
- **System:** Codex gateway status, routing and affinity settings, recording status, and a link to advanced management.
- The theme button switches between dark and light and remembers the preference in this browser.

Codex quotas refresh every minute, with a manual refresh button. Recorded usage refreshes every 15 seconds. Unknown or unavailable data is shown as unavailable, rather than as a zero allowance. Provider refresh failures preserve and label the last known quota.

## Subscription allowance versus token history

The provider reports each account’s current allowance and reset window independently. These are not added into a single interchangeable quota. Window lengths can differ by plan; the dashboard shows the windows reported for each account.

Usage history starts with the collector’s first recorded request on September 22, 2026. A request can contain many cached input tokens. Token totals are operational records, not a dollar bill or a calculation of subscription allowance.

The upstream usage queue removes records when read. Records already collected persist in SQLite, but a crash during dequeue/commit or expired uncollected records can leave gaps. Treat this as best-effort operational history. Run only one queue consumer.

## Components

| Component | Location / service |
| --- | --- |
| Existing model gateway | `cliproxyapi.service`, stock CLIProxyAPI 8.0.30 live; 8.0.43 installed and staged ([preparation](docs/gateway-upgrade-8.0.43-2026-10-26.md)), `*:8317` behind the host firewall |
| Overview backend + static site | `personal-codex-router-dashboard.service`, `/opt/personal-codex-router/dashboard`, Linux loopback 8320 |
| Linux friendly address | `personal-codex-router-url.socket` and `.service`, loopback port 80 |
| Claude gateway routing | Active for both subscriptions; T3 `claudeAgent_gateway` on Linux and Mac, Linux shell `claude` via `scripts/claude-gateway.py` ([routing](docs/claude-routing.md), [activation and acceptance](docs/claude-gateway-2026-10-15.md)) |
| Usage collector | `personal-codex-router-usage.timer`, every 15 seconds |
| Usage database | `/var/lib/personal-codex-router/usage.sqlite3` |
| Mac overview tunnel | `~/Library/LaunchAgents/com.user.personal-router.plist`, loopback 18320 → Linux 8320 |
| Mac friendly address | `/Library/LaunchDaemons/com.user.personal-router-url.plist`, loopback 80 → Mac 18320 |
| Mac address helper | `/Library/Application Support/PersonalCodexRouter/loopback-forwarder.py` |
| Mac management tunnel | `~/Library/LaunchAgents/com.user.codex-router-dashboard.plist`, loopback 18317 → Linux 8317 |

Per-account fast mode: [plugin policy, deployment and websocket limits](docs/fast-mode-policy.md).

Codex WebSockets: [rollout with response steering kept off](docs/codex-websockets.md); planned after the 8.0.43 cutover.

T3 usage hub: [read-only, separately keyed limits for T3 Code](docs/t3-usage-hub.md).

Gateway upgrade, verification and rollback: [October 26 preparation for 8.0.43](docs/gateway-upgrade-8.0.43-2026-10-26.md). 8.0.43 is installed with an ignored staged drop-in; 8.0.30 remains live until the user chooses a restart window. The checkout validates sign-in and conversation moves on both versions; its dashboard changes are not deployed by this preparation. Dashboard Codex sign-in writes the new tokens under the gateway's own credential lock and verifies the runtime token ([details](docs/gateway-upgrade-8.0.30-2026-10-15.md#codex-sign-in-replacement)). It is allowed only on validated gateway builds: after an upgrade, re-run `tests/gateway_replace_integration.py` before extending the allowlist. Legacy 8.0.7 fails that harness and is excluded on purpose; dashboard sign-in would be unavailable on that build. 8.0.30 is the rollback target for the staged upgrade; see the [October 1 upgrade record](docs/gateway-upgrade-8.0.7-2026-10-01.md) for 8.0.7 history. The live config uses the nested v8 layout; see the [completed October 1 migration and repeat procedure](docs/gateway-config-v8-migration.md).

Verified 2026-10-27: `zz-gateway-8.0.30.conf` selects the live executable. `server.host` is empty, so inference listens on `*:8317`; nftables admits loopback and only the Mac over Tailscale. Management remains loopback-only (`management.allow-remote: false`). Mac inference connects directly to `http://100.64.0.10:8317`; management and the computer-only dashboard address use SSH forwards. Gateway retry policy is `request-retry: 5`, `max-retry-interval: 45` seconds and `max-retry-credentials: 2`. The Free Codex account's model listing excludes `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol` and `gpt-5.6-sol`; see [model routes](docs/standard-management-setup.md#model-routes).

The Mac system launch daemon reserves port 80 but runs its forwarding process as `user`. Its log belongs in `~/Library/Logs/PersonalCodexRouter/url-helper.log`; a path under `/var/log` prevented that unprivileged process from starting and has a dedicated repair script.

Linux services and the Mac address helper/tunnels start automatically through systemd and launchd. No DNS/hosts-file edits, public exposure, exit node, or new inbound tailnet firewall rules are required. Chrome resolves the `.localhost` address locally.

## Advanced management

The original Management Center remains available:

- Mac: `http://127.0.0.1:18317/management.html#/quota`
- Linux: `http://127.0.0.1:8317/management.html#/quota`

It uses a separate management key, stored privately at `~/.config/personal-codex-router/management-key` on each host. Its Remember password option is separate from the everyday overview, which does not ask for a key. The existing service filesystem restrictions can block config-save operations in the Management Center; reviewed SSH changes remain the supported way to edit runtime configuration.

The official panel is pinned to v1.24.2 and checked against its release SHA-256. Automatic panel updates remain disabled.

## Development and validation

Export a committed tree without private identifiers or Git history with `python3 scripts/export-public.py --out /path/to/empty/export`; scan it again with `python3 scripts/export-public.py --check /path/to/export`.

Source files are in `dashboard/`; installed runtime copies live under `/opt/personal-codex-router`. For dashboard/watch changes, preview `python3 scripts/install-operations.py --claude-watch-only`, then deploy with `sudo -n python3 scripts/install-operations.py --claude-watch-only --apply`; this backs up replaced runtime files and watch snapshots. Collect B immediately with `systemctl --user start personal-codex-router-claude-b-usage.service`. Restart only `personal-codex-router-dashboard` for backend changes. Static assets do not require a service restart. Avoid restarting the model gateway for dashboard work.

Run the Python suite and JavaScript checks with:

```sh
heavy python3 -m unittest discover -s tests -q
heavy bash -c 'for testfile in tests/test_*_ui.cjs; do node "$testfile" || exit; done'
heavy env TZ=America/Fortaleza node tests/test_billing_dates.cjs
heavy env TZ=America/New_York node tests/test_billing_dates.cjs
node --check dashboard/static/app.js
node --check scripts/router-preview.mjs
git diff --check
```

For account-access changes, preview `python3 scripts/install-operations.py --account-access-only`, then apply with `sudo -n python3 scripts/install-operations.py --account-access-only --apply`. This explicit manifest backs up replaced files, installs the dashboard unit (write access to the Codex auth dir for sign-in), reloads systemd and restarts only the dashboard and Tailscale preview proxy, never the gateway. Both this scope and `--claude-watch-only` ship the credential writer, dashboard backend and static assets, preview proxy and updated `router-backup.py` (mode `0755`), so scheduled backups skip unfinished credential writes. Sign-in sessions and outcomes are in memory; restarting the dashboard interrupts pending sign-ins. See [account access](docs/account-access.md) for the isolated visual fixture and security limits.

The tests cover device sign-in, identity validation, the locked credential writer, immediate disk/listing/runtime-token verification, post-import refresh preservation, serialized mutation races, routing switches, proxy and installer scope, unknown quota handling, timezone-aware usage boundaries, credential filtering, host/origin restrictions, and denial of management/traversal paths. The overview never forwards arbitrary management routes. All its SQL connections use read-only mode; the service has write access to SQLite’s directory only because SQLite needs its WAL shared-memory sidecar.

The initial setup record is in `docs/standard-management-setup.md`. Dated evidence is in `docs/`. Model streaming was verified for both accounts during the management setup. On September 22, automatic account failover passed in both directions in an isolated instance of then-installed 7.3.11 using that date's production routing/retry policy. Each test invalidated a copied credential after establishing session affinity; the same request recovered through the other account and the following request stayed there. That test did not cover real quota exhaustion or mid-stream failures; see `docs/failover-verification.json`. The [8.0.43 preparation](docs/gateway-upgrade-8.0.43-2026-10-26.md) separately records synthetic 429/503 failover checks. Phone pairing requirements are in `docs/mobile-remote.md`.

## Stop or roll back the overview

Stopping the overview does not change the model gateway or either account’s authentication.

On Linux:

```sh
sudo systemctl disable --now personal-codex-router-url.socket
sudo systemctl stop personal-codex-router-url.service
sudo systemctl disable --now personal-codex-router-dashboard.service
```

On the Mac:

```sh
sudo launchctl bootout system/com.user.personal-router-url
launchctl bootout "gui/$(id -u)/com.user.personal-router"
```

These commands stop the current Mac jobs; move their plist files out of the LaunchDaemons/LaunchAgents folders if they should remain disabled after a reboot/login. Preserve the files to restore them later. The prior Management Center and its tunnel remain available.

To also roll back the original management/statistics activation, follow the [manual procedure after config migration](docs/gateway-config-v8-migration.md#removing-the-dashboard-after-migration). `scripts/rollback-dashboard.py` deliberately refuses the live nested config and also guards against changes since activation. Preserve later gateway settings, usage records and secure backups.

No code was committed or pushed during setup. Before future commits or pushes, follow this project's AGENTS.md review requirement.
