# Linux operations improvements — September 24, 2026

**Current status (2026-10-27):** This is the September 24 work record. The [shared dashboard](../README.md#everyday-access) now permits routing, account sign-in/switches and reset controls; its read-only restrictions and unresolved Mac DNS issue below are historical. Gateway inference is firewall-restricted on `*:8317`, while management and the dashboard backend remain loopback-only ([current setup](standard-management-setup.md#what-is-installed)).

Task runs on user-Desktop in `/home/user/Projects/personal-codex-router`, independently of the Mac (offline at discovery). Source directory is not a Git repository. No commit/push planned.

## Scope and safeguards

Implement automatic local backups with retention and restore rehearsal; compact dashboard health; account-aware, deduplicated local notifications; private phone project previews. Preserve gateway, managed Codex daemon, account credentials, routing/affinity, task history, and unrelated projects. Restart only the dashboard when deploying backend changes. Never test real reset redemption. Keep backups/credentials outside source.

## Discovery

- Gateway `cliproxyapi`, dashboard `personal-codex-router-dashboard`, usage and preference timers are installed. Dashboard binds loopback 8320 and has systemd network restrictions. Gateway uses port 8317 with host firewall restrictions; management rejects remote access.
- Managed Codex daemon 0.156.1 uses production `/home/user/.codex` and its private control socket. User unit `codex-shared-server-start.service` is only a oneshot bootstrap: active/exited is not proof the daemon is responsive. Use the read-only daemon version probe for health.
- Shared Linux GUI has a separate home under `~/.local/share/codex-shared-client`; loopback SSH and launcher must be recoverable.
- Manual backups: `/var/lib/cliproxyapi-dashboard-backups/20260922T162042Z`, `~/.local/state/personal-codex-router/backups/`, and dated `~/.codex/gateway-integration/` directories. Preserve them. No router backup timer exists.
- Both `/home` and `/var/lib` are on the same NVMe filesystem. Scheduled snapshots here provide same-disk recovery only.
- Tailscale Linux address `100.64.0.10`, DNS `user-desktop.tailnet-example.ts.net`. Mac is `100.64.0.11`. At discovery the phone is not listed. Asked user to connect phone and identify its device name.
- No Tailscale Serve/Funnel configuration. Existing host guard allows selected services only from the Mac and drops other incoming tailnet traffic. Preview access must retain this boundary and allow only identified personal devices.
- Other tasks have active loopback preview processes; leave them untouched. Discover existing local proxy before selecting the preview implementation.

## Implementation and verification

Implemented and deployed on September 24:

- `scripts/router-backup.py`, daily backup service/timer: private snapshots, 14-day retention with at least two preserved, stable settings reads, SQLite online backup plus integrity/checksum verification, and guarded non-destructive rehearsal. Root-only storage outside source. The first pre-deployment snapshot is `20260924T150109Z` (144 files). Its rehearsal verified all 144 files under `/var/lib/personal-codex-router-backups/rehearsals/20260924T150109Z`. A service-run snapshot `20260924T150130Z` verified 155 files. Existing manual backups preserved.
- `dashboard/operations.py`, `scripts/router-ops.py`, operations service/timer: actual managed daemon probe, account/gateway freshness, backup health, durable account notices, local desktop delivery. Summary directory root:cliproxyapi `2750`, summary files `0640`, secrets/inbox `0600`. The probe shares host `/tmp` because the actual daemon socket is a symlink into `/tmp/codex-daemon-1000`; its first isolated-tmp probe correctly showed unknown, and this was fixed and reverified.
- Dashboard updates: compact five-item health strip and notices visible only when actionable, with original styling and full account email addresses. Same-origin dismissal endpoint retains the existing loopback and request security checks. Structured reset failure metadata preserves deterministic redemption IDs, five-minute window and ten-second retry policy.
- `scripts/router-preview.mjs` and `router-preview.py`: root-managed registrations, private portal, nine explicitly selected loopback website slots, default four-hour expiry, Host/Origin checks, source allowlist, HTTP/WebSocket forwarding, and active connection revocation. Service runs as a dynamic unprivileged user. Only `100.64.0.10:8400–8409` listeners added. Gateway and dashboard management endpoints are not preview targets.
- User connected **User's S25+**, Android, `100.64.0.12`, on the same Tailscale owner identity. Preview rules allow it and the existing Mac `100.64.0.11`. Only a preview-port rule and a non-private-interface drop were added to the dedicated host guard. Phone was not granted gateway, SSH or dashboard management access.
- Tailscale MagicDNS is disabled and Linux `accept-dns=false`; the friendly DNS name failed a real browser navigation. Working portal is **http://100.64.0.10:8400/**. DNS settings were preserved. Tailscale Serve and Funnel remain unconfigured.

Checks completed:

- All 37 Python tests passed. New cases cover live WAL snapshots versus uncommitted writes, permissions/checksums, refusal of destructive rehearsal, retention, stale/unknown health, sign-in detection, restart/dismissal/outage deduplication, confirmed versus inferred renewals, reset failure resolution, corrupted notice state, protected dismissal, HTTP spoofing/host/origin rejection, WebSocket round-trips, and removal/expiry. Subsequent notice-state validation changes passed the six affected operations tests.
- JavaScript syntax checks and billing/calendar checks in Fortaleza and New York passed. Systemd unit validation passed; only unrelated pre-existing deprecated CPUAccounting warnings appeared.
- Browser verified the deployed dashboard at desktop and 412-pixel phone widths: all five health indicators healthy, no horizontal overflow or browser errors, ordinary empty inbox hidden. A synthetic unconfirmed renewal was rendered only in the task's isolated browser page and inspected visually; no production notice or real renewal event was invented. Restored live page afterward.
- Private preview sample page loaded through the deployed proxy and its POST connection-check button returned “Connection confirmed”. User phone check requested asynchronously; response pending as of this record.
- Linux `notify-send` accepted a clearly labeled local delivery check. The desktop submission was verified by its successful exit; no phone push delivery is claimed.
- Source/runtime dashboard files match. Gateway PID remained `2258689`; the managed Codex PID record stayed unchanged during deployment. Routing remains earliest weekly reset, session and subagent affinity enabled. Both accounts healthy, both automatic toggles true, live reset attempt count **zero**. No real reset was consumed.

Documentation: `backups-and-recovery.md`, `health-and-notifications.md`, `private-previews.md`; current shared-server architecture added above historical material in `mobile-remote.md`.

## Processes and outstanding checks

- Persistent: existing dashboard loopback `8320`; operations and backup timers; new private preview service `100.64.0.10:8400–8409`. Gateway/shared daemon untouched. Other tasks' preview processes untouched.
- Temporary phone-check service: `router-preview-check-20260924`, loopback `8421`, exposed only as `preview-check` on private port `8401`. Started about 15:03 UTC, with a 30-minute process and registration limit. Stop it and remove its registration after the user confirms the phone check; otherwise it expires automatically. This sample is intentionally retained only for that pending check.
- Browser automation uses only session `router-ops-20260924`; close it when verification is finished.
- Phone push and off-device backups are not configured. Current notifications use the dashboard and Linux desktop. A future external destination/provider requires a user choice; no provider was selected and no messages were sent to other people.
- No commit or push was made. The repository publishing review requirement was not triggered.

Final verification at 15:13 UTC: service-run snapshot **`20260924T151339Z`** completed at `2026-09-24T15:13:40+00:00` with **160 files**. Rehearsal verified all 160 under `rehearsals/final-20260924T151339Z`. No snapshot file had group/other access. Browser session `router-ops-20260924` was closed. The bounded sample remains only for the pending phone check until approximately 12:33 America/Santiago; its registration and process both expire.

## Phone dashboard follow-up

September 30, 2026 note: the restrictions below describe the September 24 deployment. Since [September 26](routing-selector-2026-09-26.md), the shared dashboard permits routing changes, reset policy changes, and reset redemption, and the proxy strips the obsolete read-only header. Raw gateway management routes and dashboard WebSocket upgrades remain blocked.

The user tried `router.localhost:18320` on the phone and received “This site can't be reached”. That address is the Mac's SSH tunnel; on a phone it resolves to the phone itself. This was an access-path gap, not a gateway or Codex failure.

Added permanent dashboard access at **http://100.64.0.10:8409/**, using the existing private proxy and device/firewall boundaries. The preview portal links to it. Port 8409 is now reserved, leaving eight temporary project slots. The proxy forwards only fixed dashboard assets, overview/health reads, refresh, and notice dismissal. Reset consumption/policy changes, management routes, and WebSocket upgrades are blocked on the dashboard origin. It stamps `X-Router-Read-Only: 1` regardless of client headers; the backend enforces account-management restrictions and the UI disables reset changes and hides advanced management. Backend binding stays loopback. No gateway/SSH access was opened to the phone.

Also corrected a separate proxy issue discovered during investigation: user-clicked top-level GET links from another app were rejected by the cross-site check. Such navigations now work, while cross-site API requests, iframes, POSTs, and unexpected Origins stay blocked.

Pre-change backup: `20260924T153216Z`. All 37 tests passed with added cases for the phone route allowlist, spoofed read-only headers, blocked mutations/management/traversal routes, reserved slot, and cross-site navigation/API differences. Real browser at 412px verified both accounts, five healthy states, no overflow, and no browser errors at the phone URL. Live negative requests returned 403 without using a real credit. Gateway/shared Codex processes remained unchanged.

The temporary phone sample expired and its registration was removed. The permanent dashboard and portal remain available. The user confirmed the new phone address loads: **“That works well.”** Explained that `.localhost` always refers to the current device and the old bookmark relies on a Mac-local SSH tunnel; the Tailscale address can be bookmarked across all three devices.

## Friendly shared name — preparation and activation

User requested a friendly shared name. Selected `router`, with intended dashboard `http://router:8409/` and portal `http://router:8400/`. Inspected Tailscale's current MagicDNS documentation and actual configuration. MagicDNS is disabled tailnet-wide; no global nameservers or split DNS routes are configured. Chrome could reach the existing owner's Google account but requires passkey verification before entering the Tailscale admin console. Asked the user to complete that account sign-in; the Chrome tab in “🌐 Dashboard name” is retained for handoff. No password, API key, or Tailscale state credential was extracted.

Prepared the local side after backup `20260924T154525Z`:

- Set only the Tailscale hostname to `router` and Linux `accept-dns=true`. Confirmed Tailscale reports `router.tailnet-example.ts.net` with the same IPv4/IPv6 addresses. Linux OS hostname remains `user-desktop`. The recorded Mac SSH configuration uses alias `user-desktop` pointing directly to `100.64.0.10`, so it does not depend on the former Tailscale DNS name.
- Added explicit optional `aliases` to the private proxy's Host allowlist and configured only `router` and `router.tailnet-example.ts.net`. Preserved old accepted hosts and the IP fallback. Generated links still use the IP until shared DNS actually works.
- Preview tests passed, including new alias/port/Origin/readonly/revocation checks; Node syntax check passed. Installed the proxy and restarted only the private preview service. Live requests to both new Host names and the original IP returned 200 with two accounts and `read_only=true`. Gateway PID and shared Codex process fingerprint remained unchanged.
- Direct query to Tailscale's resolver (`100.100.100.100`) resolves `router.tailnet-example.ts.net` to `100.64.0.10`. This is **not** proof that the OS, Mac, or phone can resolve it while MagicDNS is off. Ordinary external DNS still works after enabling local DNS acceptance.

At the end of preparation, the remaining work was user passkey sign-in and MagicDNS activation. Preparation was saved in snapshot `20260924T154800Z`.

After the user confirmed sign-in was complete, enabled MagicDNS in the existing personal Tailscale admin console. The admin page showed “Disable MagicDNS”, and Linux `tailscale dns status` confirmed it is enabled with search suffix `tailnet-example.ts.net`. No global DNS provider or DNS override was added. Both `router` and `router.tailnet-example.ts.net` now resolve through the ordinary Linux system resolver to `100.64.0.10`. Both named dashboard overview endpoints returned HTTP 200 with two accounts and `read_only=true`; the IP fallback also passed. Chrome loaded `http://router:8409/` and rendered the actual account overview. One account currently shows its weekly allowance exhausted; naming does not modify account state.

Set preview config `address` to `router` atomically under the existing registration lock. CLI output is now `http://router:8400/`, and the live portal links to `http://router:8409/`. No service restart was needed for this config update. Updated README and current access docs. Gateway PID and shared Codex process fingerprint still match the pre-name-change values. Asked for a real phone check of the new name; phone and Mac client-specific DNS behavior has not been verified from those devices yet. Existing device allowlists, firewall, management restrictions, and stable IP fallback remain intact. No commits or pushes were made.

The user subsequently confirmed both addresses work on the phone and Linux, but reported that they do not work on the Mac. Explained that port 8409 is the account dashboard and 8400 is the project preview portal. Mac remains online at `100.64.0.11`; Tailscale ping returned directly in 121 ms. Confirmed its IPv4 is allowed in the application config, systemd network policy, and firewall. Linux-to-Mac SSH is refused (Remote Login is not available), so Mac client DNS preferences cannot be inspected through SSH. Asked whether the direct IP dashboard works on the Mac to distinguish name resolution from HTTP/connectivity problems. No Mac or global DNS changes made during this investigation yet.

The user confirmed only the IP address works on Mac, narrowing the problem to its DNS path. The Mac's `accept-dns` setting has not been read directly. The original setup record says Mac DNS was preserved and that Teleport previously interfered with Tailscale enrollment; do not change either VPN or assume Teleport is currently active. The next targeted fix is enabling Tailscale DNS locally on the Mac with `tailscale set --accept-dns=true` (or the installed app's bundled CLI), then testing the full name. This can be done without disconnecting Tailscale or restarting the gateway/shared daemon.

The user ran the Mac DNS fix and confirmed **“that fixed it”**. Shared-name setup is complete: Linux browser verification passed, and the user confirmed phone and Mac access. Updated current access docs to remove the pending Mac investigation. Keep Tailscale DNS enabled on each device; the original IP addresses remain available as fallbacks. No gateway/shared Codex restart or wider access was needed.
