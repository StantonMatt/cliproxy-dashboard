# Claude routing

Verified 2026-10-27: both Claude subscriptions route through the live CLIProxyAPI
8.0.30 gateway. T3's enabled `claudeAgent` instance carries the gateway environment and uses
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
timestamps. A window with a known reset stays valid until that reset; one without
a reset ages out after 15 minutes, matching the Claude cards' stale rule.
Quota errors, missing or expired windows, and stale snapshots for enabled accounts
hold priorities. One case is not reported as a hold (2026-11-16): a reading
ends at its reset, and the next one arrives a minute or two later. For ten
minutes after a reset the minute timer leaves the applied plan and its `active`
status as they are, and the card reads "Just reset · updating" instead of
"Stale". Nothing is assumed about the new allowance: priorities change only when
the new reading arrives. A reading that still has not come after ten minutes, a
read error, or a routing choice made during the gap is held as before. Disabled accounts are excluded before checking their usage.
Preferred-account mode uses the same eligibility checks and can prefer the other
account when the selected account is disabled or exhausted. One exception
(2026-11-16): an account whose only used-up allowance is a model's own weekly
window, such as Fable, still counts for routing in every mode, ranked by its
all-model weekly window as usual. With `model-level-cooling` the gateway blocks
just that model there, so its requests go to the other account and every other
model is served by the account routing prefers. The last-ready confirmations
still treat such an account as not ready. Round-robin needs no
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

## Cooldown scope (2026-11-13)

Two settings in `/etc/cliproxyapi/config.yaml` keep one refusal from removing a
Claude account, or a model on it, from routing. Both were hot-reloaded on
CLIProxyAPI 8.0.47 without a restart; keep them across upgrades.

```yaml
oauth:
    request-scoped-errors:
        claude:
            - status: 404
              match:
                - not_found_error
              action: continue
upstream:
    claude:
        model-level-cooling: true
```

- **`model-level-cooling: true`**: a Claude 429 cools only the requested model
  on that account, until the reset the response reports. Before, a Fable-only
  refusal (`7d_oi` rejected, all-model `7d` still allowed) blocked the whole
  account: Claude no longer sends 5-hour headers, and 8.0.47's Fable-only check
  in `helps/claude_ratelimit.go` requires an explicit 5-hour utilization, so the
  refusal was read as a shared-window rejection. Upstream relaxed that check
  after 8.0.47 (present in 8.0.69). When the all-model weekly allowance is
  really exhausted, each model is now cooled on its own first refusal, which
  costs one failed attempt per model before failover.
- **404 rule**: a Claude `not_found_error` moves on to the other account without
  a cooldown. Before, one `not_found_error` for `claude-fable-5-2` from
  `count_tokens` during an Anthropic overload blocked Fable on that account for
  12 hours. 8.0.69 still treats an explicit model-not-found from `count_tokens`
  that way, so an upgrade does not replace this rule. A model that is really
  gone now returns 404 on every request after both accounts are tried.

Verified on the live gateway with real upstream responses:

- A Fable request routed to Claude A while its Fable window was exhausted got
  the 429, was served by Claude B in the same request, and left A `active` with
  one `model`-scope cooldown for `claude-fable-5-2` until the Fable reset. Opus
  and Haiku requests were then served by A.
- Requests for the retired `claude-3-5-haiku-20241022`, through both
  `/v1/messages` and `/v1/messages/count_tokens`, returned 404 from both
  accounts and recorded no cooldown.

The previous config is saved under
`/var/lib/cliproxyapi-upgrade-backups/claude-quota-scope-20261113T135728Z/`.
A stale block can be cleared with `POST /v8/management/routing/cooldown/reset`
and the credential's `auth_index`.

After a subscription quota reset, the dashboard also checks model-specific
cooldowns on accounts still marked `active`. A recovery episode makes usage
due on the existing reader budget; provider `Retry-After` is still respected.
Clearing requires a current subscription, shared allowance measured after the
episode began, and positive post-episode allowance for every blocked model.
Newer model readings supersede older watch readings; equally recent conflicting
readings still prevent a reset. Unknown models and non-quota blocks are held.
The gateway reset endpoint clears all model blocks, so the dashboard rechecks
account identity and the cooldown set immediately before calling it. A new block
or account-state change cancels that attempt. No gateway restart is needed.

That proof only arrives with a usage read, and a reset redeemed in the Claude
app causes none: the next read is the daily one, or the card's refresh button.
So the dashboard also releases a Claude model quota block every three minutes
without proof (`MODEL_BLOCK_RETRY`), and the next request for that model tests
it against Claude. If the limit still applies, that request gets the `429`, is
served by the other account, and the gateway sets the block again; otherwise the
model simply works. Either way a reset, redeemed or scheduled, takes effect
within about four minutes with no action, at the cost of one refused upstream
request per account every three minutes while a model is exhausted and in use.
The release applies only when every block on the account is a model-scoped
quota `429` and the account is otherwise routable, and it passes the same final
recheck. A block that returns within fifteen minutes of a release starts no
usage or profile read; one that stays away causes a single usage read after
fifteen minutes so the card shows the new allowance. A release is not shown as
"Cooldown cleared automatically"; that notice is kept for proven clears.
Pressing refresh on the account's card after redeeming a reset clears the
block at once.

### Overload and other transient errors (2026-11-14)

A short upstream failure must not take the only account with allowance out of
routing. On 2026-11-14 at 06:35 Fable was exhausted on Claude A and Claude B
answered one request with `overloaded_error`. The gateway blocked Fable on B
for its default 60 seconds, the client's retry a second later went to A and got
the quota `429`, and because 60 seconds is longer than `max-retry-interval`
(45) the gateway returned that `429` instead of waiting. The client reported
"Usage limit reached" and stopped the turn. The same thing happened at 02:37.
Overload was the most common Claude upstream failure in the gateway log for
the week before (17 entries).

Two more settings, hot-reloaded the same way, prevent it:

```yaml
routing:
    cooldown:
        transient-error-cooldown-seconds: 15
oauth:
    request-scoped-errors:
        claude:
            - status: 502
              match:
                - overloaded_error
              action: stop
            - status: 529
              match:
                - overloaded_error
              action: stop
```

- **Overload rules**: Claude's overload is a capacity state, not an account
  state, so it now blocks nothing. The error goes back to the client as it is,
  and Claude Code retries it. `502` is how the gateway labels an `error` event
  inside a stream that already answered `200`; `529` is the status before a
  stream starts. The action is `stop`, not `continue`, so an overload is never
  turned into the other account's quota refusal.
- **`transient-error-cooldown-seconds: 15`**: every other transient failure
  (`408`, `500`, `502`, `503`, `504`, `520`-`526`) blocks the model for 15
  seconds instead of 60. That is inside `max-retry-interval`, so a request that
  finds no other usable account waits for the block to end and is retried,
  instead of failing. Keep this value below `max-retry-interval`. It applies to
  every provider, Codex included.

An overload cannot be produced on demand, so these were checked against the
8.0.47 source with fake upstreams instead of live (15 cases, all passing):

- With the rules, a `502` overload in a stream and a `529` before one leave the
  model unblocked and return the original error; a `529` is not retried on the
  other account.
- Replaying the 06:35 sequence: with the old settings the retry returns A's
  `429` at once; with the 15-second cooldown alone it waits 16 seconds and is
  served by B; with the overload rule it is served by B at once and A is never
  tried.

The reload itself is confirmed in the gateway log (`transient-error-cooldown-seconds:
0 -> 15`, `oauth-request-scoped-errors[claude]: 1 -> 3 entries`), with no
restart. The previous config is saved under
`/var/lib/cliproxyapi-upgrade-backups/claude-overload-scope-20261114T102403Z/`.
