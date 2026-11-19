# Header usage and Claude subscriptions

The dashboard reads `quota.observed_at` and `quota.signals` from its normal
loopback `GET /v0/management/auth-files` inventory. The parser follows upstream
CLIProxyAPI v8.0.47's `sdk/cliproxy/auth/quota_signals.go` and management
`auth_files.go`: Claude unified 5h/7d utilization/status/reset headers and Codex
primary/secondary percentages, window lengths and absolute/relative resets.
Claude requires a weekly window and accepts an absent/null 5-hour window; a
present malformed window invalidates the reading. Codex accepts either or both
primary/secondary slots, each with complete valid usage, window length and reset
headers. An absent slot is omitted; a partial or malformed slot invalidates the
reading. Two present slots must have different window lengths. Rejected/limit-reached evidence cannot
establish positive allowance. Unknown supplemental namespaces are ignored.

Primary observations from headers or usage-endpoint reads remain valid until
the first primary reset, with each
window checked against its own reset. Future measurements and elapsed resets
are invalid; an elapsed window never implies zero usage or a fresh allowance.
Explicit unused zero with no reset retains its null date and the conservative
15-minute freshness bound. Original measurement times and `headers` provenance
survive cards, routing and the cache-only T3 facade. Each window uses the newest
still-valid measured reading; a newer invalid endpoint reading cannot shadow
valid header evidence. No raw header map or provider
response is published or persisted.

Codex merges readings by window duration because primary can mean weekly or
five-hour usage. Like Claude's omitted five-hour window, an omitted Codex window
survives only when its previous measurement is newer than the header observation
and still valid under `window_fresh`. Older, equal-time, expired or future-dated
omitted readings are dropped. If primary changes duration, a retained newer
window occupies the unused slot without changing its usage, reset or measurement
time. The native T3 projection includes only these selected real windows.
Single-window display evidence does not relax Codex's two-slot recovery guard.

Claude primary header readings retain their `allowed`, `allowed_warning` or
`rejected` status; endpoint readings carry no header status. In both automatic
routing modes, eligible accounts with a still-valid `allowed_warning` window
rank after all eligible unwarned accounts, bypassing the stability margin across
that boundary. If all are warned, the usual ranking and margins apply. A newer
non-warning reading or that window's reset clears the warning; an elapsed reset
still requires fresh allowance evidence before routing can proceed. Existing
session affinity is unchanged. The Claude account projection exposes `near_limit`
and `near_limit_windows` (`five_hour`/`seven_day`) for the dashboard card cue.

Claude/Codex successful idle usage checks run daily with independent per-account
jitter of up to one hour, rather than hourly. Each endpoint cache also detects:

- an observed available-to-error/cooldown transition in inventory `status`,
  `unavailable` or `next_retry_after` (including an initial cooled observation);
- a known window reset or retry deadline passing during that episode;
- the existing per-card manual refresh.

Episode start and consumed reset timestamps live in memory, keyed by provider
and auth index with filename/email binding. Changing error details while still
cooled does not start another episode. Observing availability ends the episode;
a later cooldown starts a new one. Deferred event markers coalesce and preserve
provider Retry-After, the scheduled-429 one-hour minimum and in-flight dedupe.
Manual reads retain their per-credential 60-second floor. Exhausted primary
windows schedule one read just after the latest blocked reset, plus jitter,
without an hourly cap. Any other primary reset (Claude `five_hour`/`seven_day`,
Codex primary/secondary) whose current reading passed after the last endpoint
read makes one read due about 60 seconds later, cooled or not; a newer header
reading with a later reset needs none, and this read does not force a profile read.
Failed reads back off exponentially up to one day.
Provider Retry-After and the scheduled-429 minimum remain minimum delays.

For Claude, a due event reads OAuth profile before usage. Profile and usage have
separate endpoint budgets; a blocked or failed profile does not prevent an
otherwise permitted usage read. A successful profile event marks usage due once,
without bypassing its provider deadline. Manual refresh availability is the earliest of the profile
and usage budgets, so a usage rate limit does not block a permitted profile retry.
Profile requests use `/api-call` with
`$TOKEN$`, `https://api.anthropic.com/api/oauth/profile` and
`anthropic-beta: oauth-2025-04-20`; never B's watch setup-token. The primary watch
collector's idle success schedule is also daily plus jitter; its existing
exhaustion/reset deadlines and Retry-After remain. Its timer can still wake every
five minutes to inspect deadlines. An active primary watch suppresses duplicate
ordinary dashboard reads; explicit events can request a bounded gateway read.
The Claude B setup-token watch reads its usage endpoint once a day, just after an exhausted primary reset, and just after the Fable weekly reset; one CLI probe stands in when that due read fails or the endpoint is still deferred. The primary watch also reads just after any unexhausted 5-hour or weekly reset.

Successful primary watches release ownership when their primary observations
expire, even while a saved daily read is still ahead; gateway reads then follow
their normal event schedule and backoff. Provider cooldowns and failed-attempt
backoffs keep watch ownership. The collector clamps saved successful schedules
to required reset reads or the daily read's latest jitter deadline, preserving
provider Retry-After. An idle zero with no reset still expires after 15 minutes;
expiry does not add a recurring endpoint poll or imply fresh allowance.

Model windows, extra spend and reset grants keep their own measured times.
Primary headers cannot freshen them, and omitted optional fields retain their
last-known readings. A known model window remains usable until its reset rather
than causing a routing hold after one hour. Cards show old optional readings as
last known with measurement times on hover. Routing and T3 omit expired primary
readings. An unused Claude weekly allowance with no reset sorts after known
resets; no future reset is invented.

The Fable window has two more sources (2026-11-16), so its bar no longer waits
for the daily read after a reset is redeemed in the Claude app:

- **Fable response headers.** A response to a Fable request also carries the
  Fable weekly window (`anthropic-ratelimit-unified-7d_oi-*`). The gateway
  keeps only the last response's headers per account, so the dashboard takes
  the Fable figure whenever the response it samples was a Fable one, and keeps
  that reading when later responses are for other models. A newer endpoint
  reading still replaces it. A `rejected` slot reads as fully used; a malformed
  slot is left out and does not discard the primary readings.
- **A read after a redeemed reset.** Weekly usage only falls inside a window
  when a reset is redeemed. When the all-model weekly figure in the headers
  falls by five points or more (`RESET_DROP`) before the window's reset time,
  the dashboard reads the usage endpoint once, on the existing budget and
  behind any provider Retry-After. The comparison is with the previous headers,
  so an endpoint that still reports the old figure cannot repeat the read.

Claude profiles read all `has_claude_*` flags, organization plan/tier, subscription status
and recognized billing type. Inventory email must match profile email. Ended
statuses, or a non-active/non-trialing status with no true entitlement flag, show
**Subscription ended · Renew**; `past_due`, `unpaid` and `incomplete` show **Payment due · Pay**.
Active/trialing status or any true `has_claude_*` flag establishes entitlement.
The `claude_pro` organization type supplies the Pro plan when flags are absent;
without a subscription status or entitlement flags it also establishes entitlement.
Max 5x is displayed as **Max**, and Max 20x, tiers containing `ultra`, or a true
`has_claude_ultra` flag as **Ultra**. Legacy names normalize without a plan-change
cue. Current profiles supply the plan name. Failed
profiles and observations older than 25 hours show their last-known time. A
plan change between two known observations shows previous → new for 24 hours;
initial observations never show a change. Profiles contain no renewal date.
Hosted invoice URLs are discarded; links always use
`https://claude.ai/settings/billing`.

Automatic recovery uses `UsageCache.recover` and `GatewayState.reset_cooldown`.
It reserves at most one attempt per observed cooldown episode before calling
`POST /v0/management/reset-quota` with the auth index through the existing
loopback `api()` helper. Every reported Claude primary window must have positive,
non-rejected, valid readings measured strictly after episode start (weekly is
required; 5-hour is optional). Codex still requires both slots. Every known model window
(including the matching watch) must have no unexpired exhausted reading;
malformed, future or ambiguous evidence blocks recovery. Claude additionally
requires a current profile in the ok state with entitlement under the same rule.
Routing, T3, collection, and cards accept weekly-only usage; cards show only
reported windows. No temporary verification limit or new API field is invented.
A final inventory read under the lifecycle lock verifies identity, enabled state and continuing
cooldown. Immediately before the POST, the reserved episode, auth index and start
must still match the cache; a replaced episode records a superseded outcome. The dashboard records attempt/completion times and success/failure,
and cards show “Cooldown cleared automatically at …” on success. Failed or
unconfirmed attempts do not retry within the episode. No recovery loop runs.

v8.0.47's `Manager.ResetQuota` clears every model state; this guarded use is
intentional. The API cannot atomically compare upstream evidence with the clear,
so a new upstream error can still race the final inventory read. Restart drops
in-memory profiles and episode state: old pre-start evidence cannot clear a
newly observed episode, and initial plan changes are not fabricated.

The Claude card menu accepts a user-confirmed renewal calendar date, displaying
“Renews <date> · set by you” and a review cue two days earlier. This metadata
never establishes entitlement or advances itself. `/api/accounts/billing`
requires same-origin write access and current filename/auth-index binding under
the lifecycle lock. Records also bind email, preserve Codex billing records,
and use serialized atomic same-directory replacement, mode 0600, and file and
directory fsync. No credential files change.

Deployment includes `dashboard/quota_headers.py`, `dashboard/subscriptions.py`,
the changed dashboard modules/UI, and `scripts/collect-claude-usage.py`. Both
scoped backend module manifests include the new modules; the T3-only scope does
not ship the UI/collector. Preserve private billing/watch state. Parent
integration owns deployment and live acceptance. Validation here uses synthetic
HTTP/provider fixtures and JavaScript rendering harnesses; no live reset,
inference, service restart, configuration change or deployment was performed.


Claude allowance warnings use `gateway.claude_warnings`, projected server-side
from the same validated usage windows and routing identities. An enabled account
with any limit below 10% contributes one amber row per limit and reset period.
The row survives stale, missing, failed, or newer positive readings until its
reset, routing is turned off, or its account is removed. The red alert replaces
amber when every routing-enabled account is blocked by a primary limit or
account unavailability; model limits alone never trigger it. Unknown observations
cannot start an all-out episode or end an existing one. A retained countdown says
“Reset due · checking” after zero, until fresh recovery is observed.

`operations.NoticeStore` keeps warning history and banner dismissals in its
configured `NOTICES` file (`/var/lib/personal-codex-router/notifications.json`).
Writes use a private temporary file, fsync and atomic replacement with mode 0600.
Overview projection never writes. `POST /api/claude-warnings/dismiss` accepts only
`{"rows":["<24-character warning ID>"]}` for currently visible amber rows, through
the existing same-origin JSON protections; read-only connections cannot dismiss.
`banner_dismissed` is independent of desktop delivery. IDs bind account filename
and auth index, limit/model and reset period. Resets within five minutes of the
original reset belong to the same period; the oldest saved ID and reset remain
the anchor. Legacy duplicate records collapse on the next write, preserving any
banner dismissal and consumed notification state. Old IDs remain dismissal aliases.
Crossing from low to zero cannot rearm a dismissed banner or a notification.
Dismissal persistence failures roll back memory and return HTTP 503 for the UI
to show “Warning not dismissed. Try again.” The device proxy permits the exact
POST dismissal route with its existing same-origin protections and strict JSON
validation; it is UI state available from the phone. All-out episode IDs and their notice
keys are episode-owned rather than reset-derived, so reset jitter never rearms them.

Claude low notices are normal urgency and occur once per period. All-out notices
are critical, occur once per episode, and suppress low notices first observed in
the same check. Fresh recovery adds one normal “Claude ready again · you can
switch back” notice. New Claude notices include the desktop’s local clock time
in brackets. `router-ops.py` delivers through the existing durable outbox;
critical notifications have no expiry. No browser sends desktop notifications.

The fixture `tests/claude_warnings_fixture.py` uses only synthetic accounts and
isolated state. Start it with `--port <own port> --state-dir <scratch directory>`.
Run `tests/claude_warnings_browser.mjs` through `heavy`, passing the fixture URL
and screenshot directory. `PLAYWRIGHT_MODULE` may point to an installed Playwright
package and `PLAYWRIGHT_CHROMIUM` to its browser binary. It checks the eight approved
states and routing-off, signed-out and reset-due states at 1440 and 390 pixels in
both themes, page placement, and dismissal from a separate browser context.
