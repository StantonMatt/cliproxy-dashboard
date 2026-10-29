# Claude subscription usage

Known usage windows emit `seconds`: 18000 for five-hour windows and 604800 for weekly windows. The dashboard's weekly hero bars draw a pace bar to the percentage of the week remaining (a dimmed, notched segment of the allowance fill when it is shorter, and a neutral bar behind the fill otherwise, so both stay visible) and a pace label comparing it with unrounded allowance remaining. Differences below 5 percentage points read "On pace"; consuming 15 or more points over pace gives the label an amber tone. Missing allowance or reset data and resets already due hide the marker. Five-hour and per-model secondary rows are unchanged; older Claude snapshots use a seven-day duration for the weekly hero.

Claude A and Claude B publish allowance observations in the dashboard's `watch_accounts` feed and also participate in [Claude gateway routing](claude-routing.md). Both gateway accounts were active on 2026-10-27; the allowance cards reported Max (20x) for A and Max (5x) for B. The allowance collectors use separate direct credentials from the gateway-owned inference credentials. Claude accounts remain separate from the Codex pool and Codex reset credits; they do not contribute to the Codex account health checks or operational notices.
Claude A’s snapshot and `watch_accounts[].email` publish a trimmed, validated email (at most 254 characters, otherwise null) from profile `account.email`, verified in Claude Code 2.1.283's `pN` schema at byte 199033257; it refreshes with the hourly plan fetch and preserves the last value on profile request failure.

The Overview opens with all four account cards in Codex and Claude groups, side by side on desktop. Routing is linked by default and can be split per provider; Codex health and allowance controls remain separate from Claude. Card names use validated email, falling back to the account label; sub-lines are **Claude A** / **Claude B** and are omitted when they repeat the name. The dashboard no longer shows notice tiles; Linux desktop notifications continue independently. With no watch snapshots, the Claude group is hidden and the Codex cards take the full width.

Recent requests includes both gateway providers, using the existing bounded candidate query and timestamp ordering to select twelve rows. Each row publishes `provider` (`codex` or `claude`; legacy missing/empty providers are Codex) and `email` resolved by provider and auth index from cached gateway account metadata, or null when unknown. Claude rows have null `fast_mode_allowed` and `forwarded_service_tier`; Codex fast-mode policy is never applied to them. Top-level account/model/hour aggregates, `usage.since`, and collection timestamps remain Codex-only. Claude aggregates live under `usage.claude`; its `since` is the earliest valid Claude request timestamp across all recorded history, independent of the selected range, or null when absent. The combined chart and history labels use the earlier provider start. All recorded totals include the full history; the daily chart displays up to the latest 30 days. Today's chart and the week range keep their existing local-time boundaries.

The desktop user's collector reads `GET https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1` with the `anthropic-beta: oauth-2025-04-20` header. It collects reported five-hour and weekly allowance percentages, reset times, per-model weekly windows (including Fable), distinct non-null legacy weekly windows, and extra-usage enabled state, amount used, monthly limit, utilization, and currency. Weekly windows are merged only by matching model names (case-insensitive); equal percentages and reset times do not establish model identity. Missing/null values remain unknown. The plan comes from the OAuth profile, as described below.

Snapshot and API contract: `extra_usage.used` and `extra_usage.limit` are **major currency units**, such as USD dollars. The endpoint's `used_credits` and `monthly_limit` are divided by 100 for USD (also the default when currency is absent). The collector follows Claude Code's formatter for other currencies: JPY, KRW, and VND remain unchanged; other currencies are divided by 100. No exchange-rate conversion is performed. For example, `used_credits: 320` and `monthly_limit: 5000` become `used: 3.2` and `limit: 50`. Saved snapshots already contain major units and are never divided again when retaining last-known values.

Verified read-only against the installed official clients: `~/.local/share/claude/versions/2.1.283`, byte 202519257, defines `Di(e,n,r="precise")` with `let h=e/100` (the preceding `AQ` set excludes JPY/KRW/VND); at byte 232267217 the usage row passes `used_credits` and `monthly_limit` to `Di`. In `/usr/lib/claude-desktop/resources/app.asar`, `.vite/build/index.chunk-C0vuTD4w.js`, byte 148340, `rl(e)` returns `(e/100).toFixed(2)` and `il(e)` applies it to both fields. The desktop's `.vite/build/index.chunk-CxyX_nIQ.js`, function `LHr` at byte 4806143, also formats both fields through `formatNumber(e/100,{style:"currency",currency:"USD",…})`.

Credentials come from `~user/.claude/.credentials.json`, under `claudeAiOauth`. The file is read once, read-only. Only an access token whose `expiresAt` is more than 60 seconds in the future is used. Otherwise the snapshot reports `expired_token` without an HTTP request. The collector never refreshes tokens, calls an OAuth token endpoint, writes or copies credentials, or logs or persists tokens. Network requests are restricted to `api.anthropic.com`; redirects and environment proxies are disabled.

The installed script is `/opt/personal-codex-router/collect-claude-usage.py`. Its atomic, allowlisted snapshot is `/var/lib/personal-codex-router-watch/claude.json` (0640), in a directory owned by `user:cliproxyapi` (2750). The dashboard reads this file without accessing Claude credentials. Failures preserve the last successful data and update `attempted_at`, status, and a short error code. Retry state lives in the same snapshot; consecutive failures back off from five to thirty minutes. Expired-token skips do not increment failures or extend backoff; after an expired-token status, credentials are rechecked immediately so renewal is visible without waiting for an old expiry backoff. Successful data older than fifteen minutes is marked stale. The browser also ages cached data into the stale state and dims the card when overview requests fail or remain pending. Credential or request errors remain available in the last-attempt tooltip with the last-known values.

`personal-codex-router-claude-usage.service` and `.timer` are user units in `~user/.config/systemd/user`. The timer runs every five minutes with up to twenty seconds of jitter. Existing user linger permits collection with the desktop session closed. The operations installer previews these paths and backs up existing watch state and unit files before applying.

To disable collection, run as `user`:

```sh
systemctl --user disable --now personal-codex-router-claude-usage.timer
systemctl --user stop personal-codex-router-claude-usage.service
```

The saved values remain visible and age into stale status. Remove `/var/lib/personal-codex-router-watch/claude.json` as its owner to hide the watch account entirely. Claude Code owns sign-in and token renewal.

## Claude B

The second watch account uses a Max (5x) subscription and a long-lived `claude setup-token` stored in `~/.config/personal-codex-router/claude-b.token` (0600, one line). The collector reads it once without changing or copying it. A missing, empty, or malformed file publishes `missing_token` without requests. B has the same allowance card and stale rules, with **account-b@example.com**, **Claude B**, and **Max (5x)** in its header. Its service configures `CLAUDE_B_EMAIL` and `CLAUDE_B_PLAN` (one of Pro, Max (5x) or Max (20x); anything else hides the badge); the collector applies A’s trimmed, at-most-254-character email validation and publishes it even when usage is unavailable. The setup-token cannot read the OAuth profile. Extra usage, keepalive, and reset redemption remain unavailable for B. A missing token shows “Sign-in token missing”.

`collect-claude-usage.py --account b` publishes `/var/lib/personal-codex-router-watch/claude-b.json` (0640, `user:cliproxyapi`). Its user service and timer are `personal-codex-router-claude-b-usage.{service,timer}`. The five-minute timer makes at most one usage-endpoint attempt per hour, honoring longer `Retry-After` deadlines. When that endpoint has no fresh data, it dispatches a tiny, hardened official CLI probe on each timer run (at most one dispatch per four minutes). It uses `claude-fable-5-1` at most hourly to obtain the weekly Fable window, and Haiku between those attempts. With no recorded Fable attempt, an unexpired Fable measurement defers the first attempt until it is an hour old or expires; otherwise the next eligible CLI poll tries Fable. The non-secret `fable_probe_attempted_at` timestamp is persisted before dispatch, including failed attempts. A failed Fable probe falls back to Haiku in the same poll and cannot retry Fable for an hour, even if no Fable window is held. Both models use the tiny “Reply with OK only.” prompt, an 8-token output cap, disabled thinking and retries, and the existing CLI hardening. Fresh endpoint data bypasses all CLI probes. Endpoint and CLI dispatch deadlines remain unchanged and survive service restarts. CLI output is parsed in memory and discarded; failures retain the last successful values.

Claude Code 2.1.301's `rate_limit_event.rate_limit_info` exposes top-level `status`, `rateLimitType`, `utilization`, and `resetsAt`, plus optional `unifiedWindows.five_hour`, `.seven_day`, and `.seven_day_overage_included` with independent utilization fractions and Unix reset seconds. Verified read-only in the installed executable: schema at byte **218642760**, serializer `hmr` at **226789627**, and model-label map `oae` around **221180256**, which explicitly calls `seven_day_overage_included` the **Fable limit**. The collector maps that optional bucket (or the matching top-level representative window) to `weekly_model`, model `Fable`. Fractions become percentages once. Haiku polls retain the last Fable measurement and its original `measured_at` until its reset passes, when utilization becomes unknown until measured again. CLI output is not retained.

B also retains endpoint model windows from `limits[].kind=weekly_scoped` and distinct legacy weekly windows. Each window carries its own `measured_at`. Missing or malformed readings preserve the prior value and measurement time across source changes and snapshot reloads; after a retained window's reset, its utilization becomes unknown until measured again. The card's `checked_at` remains the oldest primary-window measurement, so an old optional model window does not age fresh primary readings or force a CLI poll when endpoint data is fresh. A source must supply finite, nonnegative utilization for at least one primary window before it counts as a primary success. Model-only measurements are kept without refreshing primary success times or suppressing fallback; model-only and reset-only samples still report `no_usage_data`. Extra-usage flags in CLI telemetry do not establish spend amounts and are not displayed. Poll cadence and endpoint backoff are unchanged. If neither source has ever supplied a model window, it remains absent; the collector does not infer B's allowance from A's data.

Preview and deploy the watch changes with `python3 scripts/install-operations.py --claude-watch-only`, then `sudo -n python3 scripts/install-operations.py --claude-watch-only --apply`. This also ships the dashboard's Codex sign-in credential writer, backend and static assets, dashboard unit, preview proxy and the updated `/opt/personal-codex-router/router-backup.py` (mode `0755`), which skips unfinished credential writes. It backs up watch snapshots and replaced files under `/var/lib/personal-codex-router-backups/claude-watch-deployments`, with separate deployment-backup status. It excludes private token/config files, enables B's timer, and restarts only the dashboard because its Python feed cannot hot reload. It verifies that the gateway and shared Codex process stay unchanged. Run `systemctl --user start personal-codex-router-claude-b-usage.service` to collect immediately; persisted deadlines still apply.

To renew B, rerun `claude setup-token` and update both T3's Claude B `CLAUDE_CODE_OAUTH_TOKEN` environment variable and the token file, retaining mode 0600. To disable B, run `systemctl --user disable --now personal-codex-router-claude-b-usage.timer`; saved data remains visible and ages into stale status. Remove only B's snapshot to hide its card.

## Keeping the token fresh

The user timer `personal-codex-router-claude-keepalive.timer` checks every fifteen minutes with up to thirty seconds of jitter, including missed checks after downtime. If `expiresAt` is within five minutes or already expired, `/opt/personal-codex-router/claude-token-keepalive.py` runs one tiny Haiku print-mode request, at most once per hour. Normally this costs at most a few tiny requests a day; failed attempts and runs without renewal also count toward the hourly limit. The request has no tools, one turn, no session persistence, a temporary working directory, and a ninety-second timeout. User/project settings and configured MCP servers are excluded, and an environment allowlist excludes authentication, API-target, and proxy overrides. Only when the stored expiry advances and is in the future does it start the usage collector immediately.

The helper reads only the expiry field and never touches token values itself: Claude Code owns credential reads, writes, renewal, and its OAuth refresh lock. A separate private attempt timestamp under `~/.local/state/personal-codex-router/claude-keepalive.json` (0600, directory 0700) bounds requests across runs. CLI output is discarded and logs contain fixed messages only.

To disable it, run as `user`:

```sh
systemctl --user disable --now personal-codex-router-claude-keepalive.timer
```

Flag evidence was read from the installed Claude Code 2.1.283 binary without executing it. Its embedded help describes `-p` as “Print response and exit”, `--model` as accepting model aliases (the model parser recognizes `haiku`), `--tools ""` as disabling all built-in tools, and `--no-session-persistence` as disabling saved sessions. `--max-turns <turns>` is supported with “Maximum number of agentic turns in non-interactive mode” but hidden from normal help. `--setting-sources` chooses user/project/local settings sources; an empty list excludes them. `--strict-mcp-config` ignores all MCP configurations except explicit `--mcp-config` entries, of which this request supplies none. `--bare` is deliberately omitted because its help says OAuth is never read.

## Limit resets

### Availability

On 2026-09-30, `GET https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1` with the Claude Code OAuth token returned `cedar_ember` with `eligible: false`, `ineligible_reason: "surface"`, `at_limit: false`, `exhausted: []`, `grants: []`, and `next_grant_id: null`, while the Claude desktop app showed a usable **Full reset**. Anthropic withholds resets from this OAuth surface. The dashboard shows **Use the Claude app for resets** and disables grant actions and the auto-use toggle with that reason. The redemption daemon stays installed; an already enabled automatic policy resumes normal checks if the surface becomes eligible.

The usage request now includes `?cedar_ember=1&skip_spend=1`. The collector publishes an allowlisted `resets` block containing eligibility, current limit state, cooldown, next grant, available count, and grants. Grant labels are local: scopes covering both `five_hour` and `seven_day` are `full`, including any additional scopes; scopes covering `five_hour` without `seven_day` are `five_hour`; remaining scopes are `other`. The normalized aliases `all`, `full`, and `five_hour` remain supported. A missing extra-usage block preserves the last known value because `skip_spend` may omit it. Plan labels come from `/api/oauth/profile`: `default_claude_max_20x` becomes **Max (20x)**, `default_claude_max_5x` becomes **Max (5x)**, and recognized Pro organizations become **Pro**. Unknown plans remain null. The snapshot caches the plan and profile-attempt time; profile requests run at most hourly.

The separate `personal-codex-router-claude-redeemer.service` runs as `user` through systemd --user, with `Restart=on-failure`. It alone reads credentials for redemption. It listens on `/var/lib/personal-codex-router-watch/claude-redeem.sock` (0660, `user:cliproxyapi`, inherited from the setgid directory). It checks `SO_PEERCRED` before reading and accepts only the dashboard user's UID. Requests are limited to 4 KiB, four concurrent connections and 60 accepted connections per minute. Only consume, policy, and status actions are exposed; strict keys reject caller-supplied organization and request IDs.

The dashboard asks for explicit confirmation before `POST /api/claude/resets/consume` with `{"grant_id":"…","confirmed":true}`. The daemon reads a currently valid token, fetches the organization afresh, then fetches usage afresh. It validates eligibility, grant usability, paused state, expiry, next-grant selection, and any limit requirement. A grant with `use_requires_limit: false` can be used below the limit. The daemon enforces at most three POST attempts per ten minutes, provider cooldowns, and ten minutes after each confirmed success. It does not modify gateway routes, accounts, or affinity.

A single lock covers validation, durable operation state, provider dispatch, and outcome recording. The private state `~/.local/state/personal-codex-router/claude-resets.json` (0600, directory 0700) records a daemon-generated UUID request ID and pending operation **before** dispatch; both the file and parent directory are fsynced. The daemon never writes `claude.json`. Its separate allowlisted `/var/lib/personal-codex-router-watch/claude-resets.json` (0640) publishes policy, pending state, and the last event, without organization IDs or tokens. The dashboard reports the reset service unavailable if status cannot be reached or the published heartbeat is older than two minutes.

Consume has one shared **20-second lock-wait budget** for the in-process and private-file locks, followed by profile, usage, and redemption requests at **10 seconds each**: a **50-second lock/provider budget** (plus local state I/O). The dashboard's mutation socket deadline is **55 seconds**, the preview proxy uses **65 seconds** only for `/api/claude/resets/consume` and `/api/claude/resets/policy`, and the browser's POST deadline is **75 seconds**. Other preview routes keep their **30-second** timeout; dashboard status reads keep **2 seconds**. Status makes no provider calls and returns the cached result immediately when redemption owns the lock. Lock timeout does not dispatch or automatically retry a reset.

Available reset counts exclude paused grants and grants whose reported `ends_at` is at or before the current time. The collector and dashboard share this expiry rule; the browser recomputes the count as cached grants expire, including on its display ticker.

Each operation also stores a private SHA-256 account fingerprint before dispatch, computed from the organization UUID and the account/user ID from the fresh OAuth profile when available. Neither the fingerprint nor the raw IDs are published. An uncertain retry re-fetches the profile and refuses with `account_changed` if that fingerprint differs, preserving the original unresolved operation and request ID. Older unresolved operations without a fingerprint refuse with `account_unbound` and require local `resolve`; the daemon never guesses their account or retroactively binds them.

Only a non-error response with `result: "reset"` confirms consumption and records `reset`. On the first attempt, results `already_used`, `not_limited`, `cooldown`, `ineligible`, and `unavailable` record `rejected` with `result_<value>` and durably clear the pending operation, as do HTTP 400, 401, 403, 404, or 422 with `http_<status>`. `stamp_indeterminate` or `reset_unconfirmed` in either `result` or `reason` always retain uncertainty, even alongside a recognized result, and record `result_<value>`. Missing or unknown results, including `{}`, remain `uncertain` with `ambiguous`; unknown provider text is never published. Other 4xx responses (including 408, 409, 425, and 429), 5xx, redirects, timeouts, disconnects, malformed responses, and error-envelope bodies record `uncertain`. A failed rejection save keeps the operation uncertain unless the rejected outcome was persisted; local failures never establish rejection. Pending operations become uncertain on startup. All new redemptions and automatic dispatch stop while an attempt is unresolved. A manual retry of the **same grant** within ten minutes reuses the original request ID and repeats all validation. This follows the official desktop client's behavior; provider deduplication is not a documented guarantee. Every 4xx response or known refusal result on an ambiguous retry retains uncertainty and records its fixed `http_<status>` or `result_<value>` code, because the original attempt may have consumed the grant. Non-reset outcomes never consume an automatic expiry episode. Balances never establish an outcome.

Read-only client evidence (byte offsets are zero-based in the installed files):

- `~/.local/share/claude/versions/2.1.283`, byte **222653153**, schema factory `Gn`: `result` enum is exactly `reset`, `already_used`, `not_limited`, `cooldown`, `ineligible`, `unavailable`. The remaining parsed fields are optional `reason` (nullable reason enum), `resets_left` (nullable nonnegative integer), `cleared` (scope array), `weekly_resets_at`, and `cooldown_until` (nullable strings). `stamp_indeterminate` and `reset_unconfirmed` belong to the reason enum `zn`, not the result enum. `Je`, POST endpoint at byte **222655604**, parses this schema; its local transport outcomes also include `auth_error`, `rate_limited`, and `error`, which are not provider result values.
- `/usr/lib/claude-desktop/resources/ion-dist/assets/v1/shared-7-C6Gmj8pB.js`, byte **368615**, enum `nV`, and function `vV` at byte **372049** (response parser): the same six results; additional parsed fields are `reason`, boolean `reset`, `grant_id`, nonnegative `resets_left`, `cleared`, `weekly_resets_at`, and `cooldown_until`. Reason enum `rV` includes both indeterminate reasons. `/usr/lib/claude-desktop/resources/ion-dist/assets/v1/cc072325e-CWU0kP7u.js`, function `P`, POST endpoint at byte **3869**: `reset_landed` requires `result === "reset"` or separate balance-based recovery. `not_limited` clears a limit without confirming reset consumption, and `already_used` does not confirm this attempt. This daemon does not infer success from balance recovery. Unknown results remain uncertain here, rather than adopting the CLI schema's `unavailable` fallback.
- Desktop function `$B`, byte **368253** in that shared asset, labels session-plus-weekly scopes `both` without requiring exactly two scopes. This supports the `full` label with extra scopes; the daemon retains the specified primary-limit classification for partial grants.
- `/usr/lib/claude-desktop/resources/app.asar` contains no occurrences of `reset_rate_limits`, `cedar_ember`, or `not_limited`; reset handling resides in the separate `ion-dist` assets above.

Grant parsing and selection were checked read-only against both installed clients on 2026-09-30. They are not identical: desktop derives usability; Code trusts more of the server response. The daemon and collector use their intersection plus the existing local safeguards. `collect-claude-usage.py:grant_usable` is the shared predicate; `claude-redeemer.py:validate_grant` adds local dispatch constraints and is called by manual consume, uncertain retry, and tick. The collector publishes `usable` separately from the raw boolean `usable_now`; the dashboard requires `usable === true`, expiry, service availability, cooldown, and pending-operation checks. Cached snapshots preserve the actual scope enums rather than deriving scopes from display labels.

Exact grant evidence (zero-based **byte** offsets, including the embedded CLI JavaScript):

- Code `~/.local/share/claude/versions/2.1.283`: scope enum `Te` and grant schema `Fn` at **222652252**, status schema `Jn` at **222652737**, grant parser `$n` at **222653523**, status parser `jn` at **222653943**, next-grant selector `$t` at **222664153**, anytime selector `tr` at **222666465**, wall selector `hue` at **222657456**, early-use predicate `Ye` at **222657940**, and retry-ID selector `Gt` at **222664056**.
- Desktop `/usr/lib/claude-desktop/resources/ion-dist/assets/v1/shared-7-C6Gmj8pB.js`: scope enum `ZB` at **367931**, grant parser `pV` at **370317**, status parser `mV` at **370890**, early-use predicate `hV` at **371630**, next-grant selector `_V` at **371858**. `bV` fetches usage and applies `mV(response.cedar_ember)`; `nV`/`vV` parse redemption outcomes, not grant selection. `$B` labels `five_hour` alone as session, non-session scopes alone as weekly, and a mixture as both. These labels do not establish usability.

Both clients recognize only these scope strings; unknown entries are ignored, and non-array scope values become `[]`:

```text
five_hour, seven_day, seven_day_overage_included, seven_day_opus,
seven_day_sonnet, seven_day_cowork, seven_day_omelette, seven_day_oauth_apps
```

Desktop parsing and selection, including missing-field defaults:

```text
string(x) = x if x is a nonempty string, otherwise null
 date(x) = string(x) if Date.parse(x) is valid, otherwise null
count(x) = floor(x) if x is a finite nonnegative number, otherwise null
scopes(x) = recognized enum entries in enum order if x is an array, otherwise []

parseGrant(raw, next):
  require object, id matching /^[a-z0-9_-]{1,40}$/
  require count(resets_total) >= 1, count(resets_left) != null, date(ends_at) != null
  total = count(resets_total); left = min(count(resets_left), total)
  starts_at = date(raw.starts_at)                   # missing/invalid -> null; never a gate
  ends_at = date(raw.ends_at)                      # required, but parser does not compare to now
  clears = scopes(raw.clears)                      # missing/invalid -> []
  blocking = scopes(raw.blocking) minus clears     # only uncovered blockers remain
  paused = raw.paused === true                     # missing/invalid -> false
  use_requires_limit = raw.use_requires_limit !== false  # missing/invalid -> true
  usable_now = raw.usable_now === true && id == next && !paused && blocking is empty
  label = string(raw.label)                       # missing/invalid -> null; no usability effect
  percent_used = finite values for clears, rounded and clamped to [0,100], or {}

parseStatus(raw):
  require object and boolean eligible              # missing/invalid -> no status
  exhausted = scopes(raw.exhausted) if raw.at_limit === true else []
  at_limit = exhausted is not empty                # missing at_limit/exhausted -> false
  cooldown_until = date(raw.cooldown_until)        # missing/invalid -> null
  next = string(raw.next_grant_id) if cooldown_until == null else null
                                                  # ANY valid cooldown date vetoes next, even a past date
  grants = parseGrant rows from raw.grants only if eligible and grants is an array
  discard malformed rows; keep first parsed row of each id
  selected = first grant where usable_now &&
             (!use_requires_limit || (at_limit && clears intersects exhausted))
  set each grant.usable_now = (grant is selected)
  next_grant_id = selected.id if eligible and selected exists, otherwise null
  weekly_resets_at = date(raw.weekly_resets_at)     # display only
  ineligible_reason = null if eligible, otherwise recognized reason or null/unrecognized

select(status, wallScope = null):
  require eligible and non-null next_grant_id; find that id (no fallback)
  if wallScope != null:
    require wallScope in clears
    reject early use (!use_requires_limit && clears does not intersect exhausted)
  return selected grant, otherwise null
```

Code parsing and selection:

```text
parseGrant(raw):
  require id matching /^[a-z0-9_-]{1,40}$/ and integer resets_left >= 0
  resets_total = raw value if integer >= 0, otherwise 0
  label = raw value if string, otherwise ""
  starts_at, ends_at = raw value if string, otherwise null   # optional/nullable, no date validation here
  clears, blocking = recognized array entries, otherwise []
  paused = raw value if boolean, otherwise false
  usable_now = raw value if boolean, otherwise false
  use_requires_limit = raw value if boolean, otherwise true
  percent_used = recognized integer percentages [0,100], otherwise omitted

parseStatus(raw):
  require object and boolean eligible
  at_limit = raw value if boolean, otherwise false
  exhausted = recognized array entries, otherwise []        # not gated by at_limit here
  grants = parsed valid rows from array, otherwise []; no deduplication
  next_grant_id = valid id string referencing a parsed row, otherwise null
  cooldown_until, weekly_resets_at = raw value if string, otherwise null
  # parser does NOT recompute usable_now, remove blockers, clamp left to total,
  # filter grants when ineligible, or gate next on cooldown/eligibility/limit

select(status, now):                                      # $t, used by anytime tr
  require non-null next_grant_id; find first matching row (no fallback)
  require usable_now && !paused
  ends = Date.parse(ends_at) if valid, otherwise unknown
  reject if ends is known and ends <= now
  return grant and ends

selectWall(status, currentWall, now):                     # hue calls $t
  require active wall with type and future reset time, status and next id
  require reset feature enabled and lower-priority continuation inactive
  grant = select(status, now)
  require currentWall.type in grant.clears
  reject early use (!use_requires_limit && clears does not intersect exhausted)
  return grant

retryRequestId(grant):                                    # Gt/Qe/Wt
  reuse same unresolved grant's id only in same account epoch within 600000 ms
  otherwise create a new UUID; claiming blocks concurrent claims
```

`starts_at` is informational in both clients: neither blocks a future start. Neither client selector independently requires a positive balance; desktop clamps balances, and Code requires the field to parse. Desktop enforces eligibility, cooldown presence, uncovered blockers, and the exhausted-scope intersection in its parser; Code's anytime selector depends on the server's `usable_now` and `next_grant_id` for those constraints. The local shared predicate applies all desktop conditions, Code's expiry check, and the existing positive-balance and unique-id checks. Display aliases `all`, `full`, and `five_hour` still label legacy rows, but never manufacture scope arrays for usability.

The local parser retains only safe recognized scopes and uses timezone-bearing ISO dates, nonnegative integer counts bounded to 1,000,000, and normalized epoch dates in saved snapshots. This is stricter than desktop's fractional counts and JavaScript's permissive date parser. A known valid expiry is required because desktop rejects missing/invalid `ends_at`, even though Code permits it. Numeric cooldown deadlines remain a conservative local compatibility constraint. An ineligible reason, retained POST constraints, or invalid fresh boolean fields can additionally block daemon dispatch. The local `provider_valid` flags retain malformed-field refusals through snapshot normalization so the collector also disables those grants. Fresh explicit fields reconcile saved POST constraints; omitted fields cannot lift them. Both the original fresh usability decision and the decision after applying retained POST constraints must pass: saved expiry, total, scopes, or next ID cannot repair missing fresh client requirements.

Local dispatch also requires a valid token with more than 60 seconds left, fresh organization and usage, no unresolved other operation, fewer than three POSTs in ten minutes, provider deadlines, and ten minutes after confirmed success. A permitted uncertain retry revalidates the **same** grant and shared predicate, reuses the request ID, and must remain inside ten minutes. Auto-use also requires enabled policy, no master lock, a known expiry within `(now, now + 300]`, no unresolved operation, and no previous automatic success for that grant/expiry episode. Tick invokes `validate_grant` before `consume`, which fetches and validates again; unusable candidates never write an attempt or spend budget. Scope-specific wall selectors are documented above but the dashboard is an anytime surface with no requested wall scope.

After ten minutes, inspect the actual result in the official Claude client and resolve locally as `user`:

```sh
python3 /opt/personal-codex-router/claude-redeemer.py resolve --as reset
# Or, when independently confirmed that no reset happened:
python3 /opt/personal-codex-router/claude-redeemer.py resolve --as not_reset
```

Resolution is not exposed to the dashboard. It shares the daemon's file lock, so it is safe to run while the service is active. Choosing `reset` retains the local success cooldown and records an automatic grant's expiry episode as used.

Automatic use defaults off. `POST /api/claude/resets/policy` accepts only `{"enabled":true|false}`. Policy versions increase monotonically; an older waiting enable cannot override a newer disable. Disable is serialized with dispatch: once acknowledged, no further automatic POST starts while disabled. The daemon checks every thirty seconds, uses only a usable grant with a known expiry in its final five minutes, and permits at most one automatic success per grant and expiry episode. It repeats all manual safeguards, never retries uncertain operations automatically, and never makes a request with an expired or nearly expired token.

Enabling automatic use pins the policy to the current profile's private account fingerprint. Ticks, consume requests, and enabling policy verify the account afresh; status reads make no network calls. The account-pin match and check timestamp are cached in memory and private state, and status publishes only `account_pin_match` and `account_pin_checked_at`, never the fingerprint. An unchecked or unverifiable pin has an unknown (`null`) match; startup uses the saved check until the next tick. A different account publishes automatic use as off with an `account_changed` event and cannot dispatch automatically; re-enabling in the dashboard pins the policy to the current account. Legacy enabled policies without a fingerprint publish off with `account_unbound` until explicitly re-enabled. Profile or credential failures also keep automatic use off until the account can be verified.

To lock automatic use off locally, create the master switch:

```sh
mkdir -p ~/.config/personal-codex-router
touch ~/.config/personal-codex-router/claude-auto-reset-disabled
```

While that file exists, `automatic_locked` is true and enable returns HTTP 423. Remove it to unlock the toggle. Manual consumption remains available. Both new dashboard POSTs use the existing same-origin, JSON, `X-Router-Request`, and read-only checks; the private phone/Mac proxy explicitly allows these two paths. The dashboard sends each socket request once and never retries a redemption itself.

Sandbox assessment: the dashboard already allows `AF_UNIX`. Linux UNIX socket `connect()` checks socket write permissions but does not require a writable filesystem mount for the socket inode, so `ProtectSystem=strict` does not require another `ReadWritePaths` entry. `ProtectHome=true` hides the private credential/state files; the socket is under `/var/lib`. No dashboard sandbox change was made. Live verification under the installed systemd sandbox remains a deployment check; no services or privileged sandbox probes were run for this implementation.

The operations installer includes the daemon, user unit, and private state path in its preview and pre-deployment backups. Backup enumeration copies regular files and excludes the socket. To stop redemption, run `systemctl --user disable --now personal-codex-router-claude-redeemer.service`; saved grants and usage remain visible.
