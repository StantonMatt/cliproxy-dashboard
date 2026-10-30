# Claude routing

Verified 2026-10-27: both Claude subscriptions route through the live CLIProxyAPI
8.0.30 gateway. T3's enabled `claudeAgent_gateway` instance uses
`http://127.0.0.1:8317` on Linux and `http://100.64.0.10:8317` directly over
Tailscale on the Mac. Linux's interactive shell `claude` function invokes
`scripts/claude-gateway.py`. Gateway-owned credentials serve inference; the
[allowance collectors](claude-usage.md) retain their separate direct credentials.
Management stays loopback-only; Mac management/dashboard access uses SSH
forwards. See [network and retry policy](standard-management-setup.md#what-is-installed)
and the [activation record](claude-gateway-2026-10-15.md#phase-2-results--2026-10-15).

Routing controls are linked by default: one selection applies to Codex, then
Claude. Splitting the controls allows an independent selection per gateway.
Splitting preserves both saved selections and gateway priorities. Relinking
applies the current Codex selection to Claude through `/api/routing-all`, so
both gateways follow the shared choice. Claude offers round-robin, earliest weekly reset, higher
remaining weekly quota, and either preferred email. Codex now has only the Pro
identity configured; its modes converge to neutral priority without quota data.
Split routing to prefer Claude B independently. Shared preferred-email requests
must name an identity configured for both providers. Preferences affect new
sessions; healthy sessions retain gateway affinity. Without a saved Claude
selection, the minute timer leaves Claude priorities untouched, preserving the
existing A-first configuration on deployment.

The shared controller in `dashboard/routing_policy.py` filters gateway records by
provider before identifying accounts. Claude A is `account-a@example.com` and
Claude B is `account-b@example.com`. A read-only management GET on October
15, 2026 confirmed those emails on the two Claude auth records, with corresponding
`Claude A` / `Claude B` notes, and on the local usage snapshots. Filenames are read
from the gateway rather than constructed. Missing, extra, duplicate, unknown, or
conflicting identities hold routing. Existing unexpected manual priorities and
changed baseline filenames also prevent writes. Writes change only priority and
require readback confirmation.

Automatic modes rank the all-model weekly window. Both the 5-hour and all-model
weekly windows must report finite remaining percentages and future reset times.
An unstarted window with a null or missing reset and 100% remaining (or zero used
when remaining is absent) counts as full, with a synthetic reset of now plus five
hours for the 5-hour window or seven days for any other window. This ranks an
unstarted weekly allowance latest for reset preference. Other reported allowance
windows must also be valid and non-exhausted. The collectors preserve measured
timestamps. Their five-minute cadence allows at most
15 minutes of observation age, matching the Claude cards' existing stale threshold.
Quota errors, missing or expired windows, and stale snapshots for enabled accounts
hold priorities. Disabled accounts are excluded before checking their usage.
Preferred-account mode uses the same eligibility checks and can prefer the other
account when the selected account is disabled or exhausted. Round-robin needs no
quota observations. The existing stability margins apply: five percentage points
for quota ranking and 15 minutes for reset ranking.

Claude does not use Codex's ten-minute neutral fallback. Its quota-dependent
changes, including retries of interrupted writes, require valid usage again.
Claude state is private under `/var/lib/personal-codex-router-routing/claude/`,
with its own selection, status, baseline, suspension and lock files. Codex retains
its existing files, defaults, fallback and pending-plan recovery. Both systemd
services already permit writes under that parent state directory.

`POST /api/claude-routing` uses the same request gates, deadline, validation and
200 / 202 / 503 result meanings as Codex routing. Overview exposes
`gateway.claude_preference` alongside `gateway.preference`; drift requires priority
observations newer than the controller status. `gateway.claude_accounts` contains
only safe routing metadata and remains separate from Codex
usage history, health, reset balances and sign-in.

The existing preference service runs `prefer-earliest-reset.py --apply
--all-providers`. It handles saved policies independently within bounded request
deadlines. The scoped `install-operations.py --account-access-only` manifest
includes the HTML and updated service; deployment reloads systemd units and
restarts only dashboard and preview services. No new timer is needed.

Each Claude card also has a Use for routing switch. It changes only that verified
Claude credential's disabled flag through the loopback management API, with an
identity-bound readback. Disabling the last confirmed routable Claude account
requires confirmation. Unknown or stale allowance on the other account also
requires confirmation. A disabled preferred account is excluded and the saved
policy can choose the other eligible account. Codex credentials are never targets
of this endpoint.
CLIProxyAPI 8.0.30 has no conditional mutation API, so identity checks immediately
before a status or priority PATCH and identity-bound readback detect replacements
but cannot eliminate a replacement between the final GET and PATCH.

## Dashboard API

All three new POST routes require the existing recognized Host,
`X-Router-Request: 1`, same-origin checks, write access and an `application/json`
body of at most 1024 bytes. They share the existing 45-second request deadline
and the preview proxy's 50-second account-mutation timeout.

- `gateway.routing_linked` is a boolean in `/api/overview`. It defaults to true
  when `/var/lib/personal-codex-router-routing/linked.json` does not exist.
  `POST /api/routing-link` accepts exactly `{"linked": true}` or
  `{"linked": false}` and returns HTTP 200 with the same object. The flag is
  atomically written with mode 0600 under the routing controller lock; this flag
  endpoint alone changes no selection, while the relinking UI applies the Codex
  choice through `/api/routing-all`. A damaged or unreadable flag projects false.
- `POST /api/routing-all` accepts the existing routing selection body:
  `{"mode": "round-robin"}`, `{"mode": "earliest-weekly-reset"}`,
  `{"mode": "highest-remaining-quota"}`, or
  `{"mode": "preferred-account", "preferred_email": "<connected email>"}`.
  It validates both providers first and returns
  `{"codex": <Codex endpoint result>, "claude": <Claude endpoint result>}`.
  HTTP 200 means both applied; 202 means at least one is held or pending; 503
  means at least one failed or could not be confirmed, taking precedence over
  202. An exception on one side produces that side's existing
  `{"error": "Routing change not confirmed. Refresh and try again."}` response.
  Held responses retain their controller fields and add `message`. The second
  provider is attempted within the same deadline even if the first fails.
  Applied changes are never rolled back because the other provider failed.
- Every `gateway.claude_accounts[]` and `watch_accounts[]` entry exposes
  `routing_enabled` (boolean or null) and `routing_id` (opaque 64-character identity hash).
  If the gateway pair cannot be verified, both fields are null; only verified
  disabled credentials project false. Controls stay disabled while identity is unknown.
  `runtime_available` is false for a gateway credential marked unavailable or in
  error, true otherwise, and null on watch accounts with no unique runtime match.
  Routing eligibility, timer reconciliation, and the last-ready toggle guard use
  the same check for raw gateway records and projected `runtime_available` values.
  Readiness requires verified enabled routing, runtime availability, and fresh valid
  allowances; an invalid reset marker cannot qualify as an unused allowance.
  `POST /api/claude/accounts/routing` accepts exactly
  `{"account_id": "<routing_id>", "enabled": false, "confirmed": false}`.
  HTTP 200 returns `{"enabled": false}` (or true when enabling). HTTP 409
  `{"error": "last_ready"}` requests confirmation; resend the same identity and
  choice with `confirmed: true`. HTTP 409 `{"error": "account_changed"}` requires
  a fresh overview. Invalid input returns 400; unconfirmed mutations return 503
  `{"error": "Account change could not be confirmed. Try again."}`.

No new deployed source file, migration, timer, or gateway restart is required.
The linked flag is created on the first explicit link/split change; the existing
scoped installer already includes all changed backend and proxy files.

## Upstream priority scope

Verified against CLIProxyAPI v8.0.30, commit
`5be2cdab0869cbaa3d401891f740aebf7e15aa99`:

- [`sdk/cliproxy/auth/conductor_selection.go:1852-1877`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/sdk/cliproxy/auth/conductor_selection.go#L1852):
  the legacy selector builds candidates matching the requested provider and
  registered route model before passing them to priority filtering.
- [`sdk/cliproxy/auth/scheduler.go:401-426`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/sdk/cliproxy/auth/scheduler.go#L401):
  the built-in selector chooses the requested provider's model shard.
- [`scheduler.go:1158-1180`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/sdk/cliproxy/auth/scheduler.go#L1158):
  that shard includes only credentials supporting the model.
- [`scheduler.go:1317-1327`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/sdk/cliproxy/auth/scheduler.go#L1317):
  highest priority is selected within the shard.
- [`scheduler.go:445-513`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/sdk/cliproxy/auth/scheduler.go#L445):
  mixed-provider requests compare only eligible providers' model shards. If a
  model were deliberately registered across providers, their priorities could
  compete for that shared route. Claude credentials do not enter Codex model
  candidates in the current distinct provider/model routes.

The global strategy remains round-robin with the existing session and subagent
affinity. No credential, model-route, gateway restart or extra-usage change is part
of this feature.
