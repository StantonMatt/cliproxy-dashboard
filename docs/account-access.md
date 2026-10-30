# Codex account access

Every 60 seconds, the dashboard reads Codex entries from the gateway's
management auth-file list. The explicit nested `id_token.plan_type` claim supplies
the plan label: `prolite` displays **Pro 100**, and unfamiliar non-empty plan
values display a title-cased label (underscores and hyphens become spaces).
Missing or malformed claims display **Unknown**. Labels do not determine plan
validity: any non-empty string is accepted for sign-in, enable and refresh.
Usage responses supply quota bars and plan-disagreement refresh observations;
they never replace the displayed claim or its renewal estimate. Free and paid
accounts use the same routing eligibility, quota exhaustion and failover rules. The plan claim never changes the saved
**Use for routing** preference. Polling never enables or disables accounts;
accounts already off stay off until the user enables routing or completes sign-in.

A fresh Free claim produces the Linux desktop notification **ChatGPT reports
a Free plan**, regardless of the disabled flag. Legacy dismissal flags
remain valid for the same identity and continuous Free episode; a new identity or
a later Free episode gets a new notice. Sign-in notices remain independent.
The dashboard no longer shows notice tiles. Free cards retain their plan badge and have the same controls as paid cards.
A passed renewal date on a Free plan reads **Plan ended** in the neutral colour.
Without cached allowance, disabled cards show **Not available while disabled**.

Signed-out credentials are excluded by `routing_policy.ineligible_reason` when
`auth_state` is `sign_in_required` or availability is `auth_required`, without
PATCHing the gateway's disabled flag. Their routing switch renders off and locked
with **Sign in to use for routing**, while preserving the saved preference.
After sign-in the switch reflects that preference again. Eligibility changes
immediately reconcile routing so healthy accounts remain available.

Only successful HTTP 200 usage observations with a non-empty string plan that
disagrees with a non-empty claim trigger a forced gateway token refresh, at most
once per identity per ten minutes, including failed attempts. The next poll displays the refreshed claim.
A 401 does not trigger this refresh: the gateway already handles refresh on 401.
Polls and reset-triggered refreshes share a reentrant in-process lifecycle lock and
the routing controller's cross-process `controller.lock`. Status switches, routing
selection, controller priorities, rollback and the sign-in sync and enable steps
use that same protocol. The sign-in disk write is the exception: it waits up to
90 seconds for the gateway's own per-file lock, holding neither of those locks
(see [Saving the sign-in](#saving-the-sign-in-cliproxyapi-8030)).
Lock acquisition always precedes credential read/merge/write/readback.
Thread and file lock contention both wait within one bounded acquisition window
(at most 60 seconds, or the operation's remaining budget). A failed acquisition
or failed worker launch clears refresh/session activity so later polls and sign-in
attempts can retry. In-memory session locks never wrap gateway transport calls.
Completion callbacks run after the import releases it.

There is no new durable state file; sign-in outcomes are written only as journal
lines (see **Journal** below). Refresh
throttles live in memory; existing notification storage retains desktop notification episodes and legacy dismissal flags.
No backup source changes are needed. Gateway status PATCHes are verified against
runtime state; sign-in also verifies persistence of its final disabled flag.

The **Use for routing** switch on each Codex card
sets and verifies the gateway's disabled flag under the lifecycle lock. It is
interactive for every plan while signed in. Turning off the last ready account
requires **Turn off** confirmation; **Keep on**, Escape, or leaving the card cancels
the confirmation. **Keep on** receives initial focus. A later explicit off supersedes a pending sign-in's permission
to enable the account. Routing reconciles after confirmed changes.
CLIProxyAPI 8.0.30 has no conditional mutation API, so identity checks immediately
before the status PATCH and identity-bound readback detect replacements but
cannot eliminate a replacement between the final GET and PATCH.

## Dashboard sign-in

Use **Sign in** on a card that says **Sign in required**. Its button reads
**Sign in**, **Try again** or is absent according to the current sign-in outcome.
Healthy cards have no sign-in action. This works on desktop, Mac and phone
through [the shared Tailscale dashboard](http://router:8409/).

The code panel replaces the allowance block. Check **Sign in as** before opening
the OpenAI page: the browser may be signed in to your other account. Copy the code
and approve it at OpenAI. The link uses OpenAI's prefilled verification URL when
available. Copy also works through a selection-based fallback on the plain HTTP
Tailscale address; if copying is blocked, the code is selected for manual copying.
The dashboard never opens the OpenAI page automatically.

One in-memory session belongs to each existing account. Reloading or opening the
dashboard on another device joins that session; Cancel ends it for every device.
The browser checks the overview every three seconds while a session is visible,
and when the tab becomes visible again. The desktop polls at OpenAI's interval
(default five seconds), independently of any browser, for up to fifteen minutes.
An approved sign-in remains **Approved · finishing** during immediate saving and
verification. Successful sign-in on any non-empty string plan, including Free,
updates credentials and enables routing through `enable_after_signin`, unless an explicit
off superseded it. The common success outcome is `signed_in`. Results last ten
minutes or until the next attempt. No success is shown for a failed import.
A confirmed sign-in outranks a **Sign in required** read before it: the card and its
routing switch ignore an account check older than the `signed_in`
outcome (`quota_attempted_at` before the outcome time), because the dashboard
refreshes the account in the background just after confirming. The browser
reloads when a sign-in request returns; normal polling reads the refreshed account.
Recent failures remain visible even if saving already cleared the original auth
error. A `save_failed` outcome permits another device sign-in for that identity.
Start-time `gateway_untested`, `save_unavailable` and `identity_unavailable`
refusals offer no button while fresh. A saved but unconfirmed sign-in
(`saved_unconfirmed`) is informational: normal polling determines the card.
While the account still needs sign-in, it shows **Sign in required**, the ordinary
**Sign in** button for a fresh device flow, and **Saved, but the gateway hasn't
confirmed it yet.** Once the account authenticates, it shows its normal card,
without a success message. No saved sign-in is retried or enabled later.
Outcomes expire after ten minutes, retired identities are pruned on overview
polling, and the outcome map is capped at 128 entries.

A dashboard restart discards sessions and outcomes. The next overview returns to
the account's current strip; there is no browser-storage interruption marker.
Unlike the original design rules, a restart does not show **Sign-in was interrupted**
or offer a special restart retry. This is intentional: the browser does not retain
evidence of a session after the dashboard loses its in-memory state.
Sign-in and switch state are server-authoritative. Start/cancel/route errors last
only until the next successful overview poll started after the failure. An unchanged session handle and
code keep the existing code-panel DOM, preserving focus and manual selection.
When the panel closes, focus stays on its equivalent card target across later polls.
Last-ready confirmation is dismissed when the overview changes that account.
A backend-required confirmation survives the overview current at rejection and
any poll already in flight; only a later poll can invalidate it automatically.

## Identity, import and access limits

The dashboard service has no outbound network, so it uses the gateway's management
`/api-call` relay with the fixed device-code and token URLs and request formats
from CLIProxyAPI v8.0.7.
Device-token polling treats HTTP 403 and 404 as pending regardless of body,
matching that version's status-first handling. Other relay responses and management
readbacks require JSON objects; empty or malformed bodies produce sanitized errors.
Sign-in relay requests contain no account selector,
`$TOKEN$`, Authorization, cookies or account headers. Start performs a one-time preflight that refuses a non-empty gateway `proxy-url`.
A later external configuration change is outside that check; device polling does
not repeatedly fetch configuration.

Before any credential write, the backend validates the ID token from its own token
exchange: issuer, client audience, expiry, exact saved email and ChatGPT account
ID, and saved subject/user IDs when present. Missing account identity, a wrong
email/workspace or an invalid claim fails closed and writes nothing. This follows
upstream's token introspection model: token provenance comes from the backend's
fixed HTTPS exchange; it does not add independent JWT signature verification.

### Saving the sign-in (CLIProxyAPI 8.0.30)

Since upstream v8.0.25 the Codex credential file is the source of truth. Every
save without creation intent restores all token fields from disk, in memory too
(`sdk/cliproxy/auth/codex_persist.go:106-125`, `MergeCodexCredentialsFromDisk`
in `internal/auth/codex/token.go:182-220`). A `PATCH /auth-files/fields` with new
tokens therefore returns 200 and changes nothing. Two real sign-ins were dropped
this way on 2026-10-16 before this design.

Completion now writes the tokens to disk itself, under the gateway's own lock:

1. Without the lifecycle lock (the wait in step 3 can take 90 seconds; routing
   switches and polls carry on meanwhile), completion re-resolves the account by
   `auth_index` plus identity, validates the new ID token against a private
   download, and requires a tested gateway build (below).
2. `dashboard/credential_replace.py`, the only code that touches the auth dir,
   takes the target from the `/auth-files` listing. The name must match
   `codex-….json` (a plain basename, no separators or `..`), the listed `path`
   must be present and equal `/var/lib/cliproxyapi/auth/<name>`, and the record
   must explicitly be a file credential (`source: file`, not `runtime_only`). The
   auth dir must be owned by the dashboard user. The file is opened with
   `O_NOFOLLOW` and must be a regular file owned by the dashboard user, at most
   1 MiB. Open errors are classified by errno: a symlink (`ELOOP`) or socket
   (`ENXIO`) is `not_regular`, `EACCES`/`EPERM` is `no_access`, a larger file is
   `too_large`, all lasting; a file deleted since the listing (`ENOENT`) and other
   errors are a transient `unreadable`.
3. It takes `flock(LOCK_EX)` on `<abs path>.lock`, the same sidecar gateway Codex
   saves and refreshes hold (`internal/auth/codex/filelock_unix.go:11-24`),
   polling for up to 90 seconds. The lock file must be a regular file owned by the
   dashboard user with owner read and write; an existing lock that passes those
   checks but still cannot be opened is refused as `lock_untrusted` too. A refresh
   holds that lock across its network call, so the write
   waits for it to finish. A timeout writes nothing: **Couldn't save the new
   sign-in**, and Try again starts a new sign-in. So do the other refusals that
   can pass on another attempt (`changed`, `unreadable`, `invalid_json`,
   `invalid_tokens`, or an I/O error).
4. Before taking the lock, it removes temp files of every Codex credential left by
   a writer that died before renaming (`codex-….json.tmp.<24 hex>`, regular, owned
   by the dashboard user, untouched for ten minutes); the start preflight does the
   same, so a crash's leftover copy does not wait for that credential's next save.
   The nightly backup skips `*.tmp.<hex>` names too. Holding the lock, it reads the
   current JSON and
   refuses anything that is not `type: codex`, has `siwc`/`use_siwc`, or is
   invalid JSON (duplicate keys, NaN and non-finite numbers such as `1e999`
   included). It re-validates the new ID token against these bytes (email,
   account ID, user IDs and subject must match) and re-checks that the session is
   still active. It overlays exactly
   `access_token`, `refresh_token`, `id_token`, `expired`, `last_refresh` and
   `plan_type`, keeping every other key and its type (priority, prefix, disabled,
   proxy and unknown keys). Timestamps are UTC `%Y-%m-%dT%H:%M:%SZ`. It writes
   `<name>.tmp.<random>` (never ending in `.json`, which the watcher would load as
   an account) exclusively with mode 0600 and fsyncs it. Management upload and
   delete do not take this lock, so immediately before the rename it compares the
   file's inode, size, mtime and ctime with the read; any change, or a deleted
   file, refuses the write. Only the instant between that check and the rename
   stays open. It then renames over the file, fsyncs the directory and unlocks.
   Any failure before the rename removes the temp file and leaves the original
   intact. Once the rename has happened, a failed unlock or close is ignored: a
   returned `replace()` always means written, an error always means not written.
5. No management call is ever made while that lock is held. Gateway saves wait
   for it, and the file store's global mutex would stall every account behind
   them. `replace()` takes no management client, and `credential_replace.holding()`
   lets tests assert the rule.

The unit grants `ReadWritePaths=-/var/lib/cliproxyapi/auth` (with `UMask=0077`).
The `-` makes it optional: a missing auth dir does not stop the dashboard, and
sign-in then fails closed at start with `save_unavailable`. The mount is made when
the dashboard starts: if `/var/lib/cliproxyapi/auth` is removed or recreated while
it runs, the dashboard's view of it goes stale, sign-in fails closed with
`save_unavailable`, and the dashboard needs a restart. The dashboard already
holds the management key, which can upload, patch or delete these files, so
direct access adds no real capability.

**Start preflight.** Before the device flow begins, start checks every reason
step 2 or 4 would refuse the save: the auth dir must be writable, and
`credential_replace.preflight()` applies the same listing, name, path, owner,
regular-file, size, `type` and `siwc` checks that `replace()` uses (they share one
function). An existing `<name>.lock` must pass the same check as in step 3, a
regular file (not a symlink) owned by the dashboard user with owner read and
write; a missing one is fine. The preflight opens the file with `O_NOFOLLOW`, only
`lstat`s the lock, never takes it or creates it, and never writes the credential;
its only change is the stale temp-file sweep from step 4. A lasting refusal
(`credential_replace.LASTING`: `invalid_name`, `path_mismatch`, `not_file`,
`not_regular`, `wrong_owner`, `no_access`, `too_large`, `not_codex`,
`siwc_present` or `lock_untrusted`) or an unwritable auth dir ends the sign-in as
`save_unavailable` (**Dashboard can't save sign-ins.**), with its reason in the
journal (`auth_dir_unwritable` for the auth dir), before any request to OpenAI;
the card offers no button for it until the outcome expires, because the
cause does not clear by itself. A transient refusal (`unreadable` or
`invalid_json`, such as a file caught mid-replacement) ends as `save_failed`
(**Couldn't save the new sign-in.**) with Try again, because the next click can
pass. A lasting refusal that first appears at the save, such as a lock file that
turns untrusted after start, ends as `save_unavailable` too.

Start also downloads the credential and compares it with the listing as the save
does: its ID token must decode to this account's email and, when the listing has
one, its workspace. A mismatch ends as `identity_unavailable` before the device
flow, with no button until it expires, since a sign-in could only be refused.

**Sync and verify.** With the credential lock released, completion gets one
120-second deadline for everything after the save: waiting for the lifecycle
lock, every management call (each at most 18 seconds), `controller.lock`, the
echo and the enable. A hung gateway therefore cannot hold the lifecycle lock or
`controller.lock` for minutes. Running out of time ends as `saved_unconfirmed`
(journal `timed_out: true`), never as a rollback; if the sign-in was already
confirmed and only the enable ran out, it is `signed_in` with routing left off.
Completion takes the lifecycle lock and (a) downloads the credential and requires exactly the written
token fields. Only then does it take `controller.lock` (at most 10 seconds, or
less if completion has less time left), and:

- sends the old fields PATCH (`name` plus the six fields). Disk already holds the
  tokens, so the gateway's merge-from-disk makes it a synchronous memory update
  that can never regress disk (`conductor_persistence.go:225-232`). The watcher
  reloads too (a rename-over arrives as CREATE), but the flow does not rely on
  its timing;
- (b) re-resolves the identity and requires the listed workspace and plan;
- (c) runs a runtime-token echo. A one-shot listener on 127.0.0.1 with an
  ephemeral port and a random 256-bit path receives `/api-call` (`GET`,
  `Authorization: Bearer $TOKEN$`, `proxy_url: direct`). `/api-call` substitutes
  the **in-memory** token without refreshing Codex (`api_tools.go:255-294,769-778`),
  allows loopback URLs on 8.0.30, and a request-level `direct` proxy outranks
  credential and global proxies (`api_tools.go:837-869`). Only the SHA-256 of
  the received header is compared with that of the new access token; the header
  is never stored or logged. Because the echo itself uses `direct`, a configured
  proxy cannot affect it; only the start preflight checks `proxy-url`. The
  listener accepts until `/api-call` returns, with a one-second timeout per
  connection, so a stray local connection cannot stall it. If the call fails on
  the dashboard's side, the port stays bound in the background until the
  gateway's own 60-second `/api-call` timeout (`api_tools.go:24`) has passed,
  plus five seconds, so another local process cannot bind it and catch a late
  token. That linger is also arranged when a request deadline runs out during the
  echo, before the timeout propagates. An echo call that fails or hits its own
  management timeout with request time left counts as a failed echo.

If (b) or (c) fails, completion re-sends the PATCH once and checks again.
Confirmation requires the exact tokens written by this sign-in. Changed tokens,
including a gateway refresh or an independent replacement of the same account,
and incomplete verification end as `saved_unconfirmed`. Nothing is rolled back
or retried, and routing is not automatically enabled now or later. The gateway's
file watcher reloads the saved credential on its own; normal polling determines
whether the account authenticates and whether a fresh device sign-in is needed.

Approval enables routing through `enable_after_signin` only while its session
still wants enable. It re-checks that the session is active and not superseded by
explicit off immediately before the enable PATCH inside the lifecycle/flock
boundary, then verifies the final disabled flag on disk too. A failed enable
after a confirmed save reports **Signed in** with routing left off, not a failed
sign-in. The outcome carries `enable_failed` only while enable was still wanted
and the listing or disk still shows the account disabled; an explicit routing
change in between, or an enable that took effect despite the error, records
none. The card adds **Couldn't turn on routing** only while the account shows as
disabled, and health reports `enable_failed` instead of `intentionally_disabled`
while the outcome lasts. A later deliberate routing change that succeeds, on or
off, clears `enable_failed` from the outcome. A disabled account whose enable
failed still gets the **Needs a new sign-in** desktop notification when a check newer than the
sign-in reports that it needs one. Once approval enters `finishing`, cancel returns a fixed
`already_finishing` conflict; pending sessions remain cancellable.

**Version gate.** `signin.TESTED_GATEWAYS` (`8.0.30`, `8.0.43` and `8.0.47` in this checkout) lists the
builds validated with `tests/gateway_replace_integration.py`. The version comes
from the `X-CPA-VERSION` header on the start preflight's `GET /config`. An
untested build fails closed at start, before the user approves anything, and
again just before the disk write: **Gateway version not validated for sign-in.**
Start also checks that the dashboard can write the auth dir, failing with
`save_unavailable` (**Dashboard can't save sign-ins.**) before the device flow.
Legacy 8.0.7 is excluded on purpose: it has no Codex file lock, and
the harness showed an in-flight refresh overwriting the new tokens (12 of 12
in-flight races). After any gateway upgrade, re-run the harness against the new
binary and extend the allowlist only if it passes. The [8.0.47 preparation](gateway-upgrade-8.0.47-2026-10-29.md) passed this gate; 8.0.30 remains live, and the updated dashboard has not been deployed by that preparation.

8.0.30 is the rollback target for the staged 8.0.47 cutover; 8.0.7 is historical.

**Journal.** Each outcome writes one `codex_signin` JSON line to stdout (the
journal): kind, account email, elapsed seconds and `checks`. Every `signed_in`
line adds `enabled`. `checks` holds which sync checks passed (`disk`, `listing`,
`runtime`, `repatched`), `enable_failed`, `timed_out` when completion's deadline
ran out, or a fixed `reason`. Reasons include a save refusal's code (`unreadable`,
`no_access`, `lock_untrusted` and so on), `auth_dir_unwritable`,
`identity_unavailable`, `session_ended` (the session ended after the token exchange;
nothing was saved), `gateway_untested` (with the refused `version`), and for
`start_failed` the step that failed: `proxy_configured`, `download_failed`,
`config_failed`, `usercode_failed` (the OpenAI relay) or `preflight_failed`;
for `provider_error`, `device_poll_failed`, `device_response_invalid` or
`token_exchange_failed`. `checks.error` means a sync step raised before the
checks completed. `recorded: false` means the session had already ended through
cancel, close or an identity change, so the outcome was not kept. A failure to
write the line never changes the outcome. Tokens, codes and headers are never logged.

Outcome kinds: `signed_in` (with `enabled`, and `enable_failed` when the enable
failed), `saved_unconfirmed`, `save_failed`, `save_unavailable`,
`gateway_untested`, `identity_unavailable`, `invalid_identity`, `wrong_account`,
`wrong_workspace`, `start_failed`, `provider_error`, `declined` and `expired`.

**Checks.** `tests/test_credential_replace.py` covers the writer, refusals, lock
contention from a second process, failure cleanup, the state machine and secret
hygiene. `tests/gateway_replace_integration.py` runs the real dashboard code
against an unmodified gateway binary in a scratch dir on 127.0.0.1:18417-18419
with synthetic credentials. A loopback CONNECT proxy with a scratch CA
(`SSL_CERT_FILE`) fakes the token endpoint and refuses every other host.
`tests/sandbox_lock_check.py` runs the writer as `cliproxyapi` under the unit's
sandbox properties against a scratch dir. Results are in
[the 8.0.30 upgrade notes](gateway-upgrade-8.0.30-2026-10-15.md#codex-sign-in-replacement).

Device identifiers, verifiers, authorization codes and tokens remain in backend
memory. The overview allowlists only the opaque session handle, user code, fixed
verification link, expected email, state, expiry and sanitized outcome. Start,
cancel and routing changes are bounded JSON POSTs with the existing same-origin,
Host, cross-site, read-only and `X-Router-Request: 1` checks and no-store responses.
Each account POST (sign-in start, cancel, and routing on/off) has one monotonic
45-second backend deadline, created before reading the request body. Every
management call gets `min(18 seconds, remaining)`, and both in-process and
controller-flock waits get `min(10 seconds, remaining)`. Waiting consumes the
same deadline; it is never renewed between calls or lock boundaries. Expired
requests stop before further management calls or a new sign-in worker and use
the sanitized unavailable response. A transport timeout can leave an already
sent write unconfirmed; no automatic retry, rollback or token replay is added.
`dashboard/request_deadline.py` defines `ACCOUNT_MUTATION_SECONDS`,
`MANAGEMENT_CALL_SECONDS`, and `LOCK_WAIT_SECONDS`. The proxy and browser mirror
the 45,000ms base as `ACCOUNT_MUTATION_DEADLINE_MS`, with parity tests guarding
against drift. `ACCOUNT_PROXY_TIMEOUT_MS` is that base plus 5,000ms (50 seconds);
`ACCOUNT_BROWSER_TIMEOUT_MS` is the proxy timeout plus 5,000ms (55 seconds).
All three account POSTs share this ordering, including cancel. The existing
`/api/routing` preference selector uses the same 45/50/55-second chain too,
preserving its smaller `min(15 seconds, remaining)` management-call timeout.
The proxy has an
absolute timer as well as a socket-inactivity timeout; response closure clears
the timer. Start still makes at most four calls, cancel one, and the two-account
routing switch at most seven including fresh quota checks, but no longer has
an additive 146-second worst-case request budget.
Provider polling and import completion run independently of those requests.
The switch's post-change quota refresh and routing reconciliation also run in a
background worker, rather than extending the mutation response's timeout chain.
Explicit off advances the account's routing-intent generation even if a start
has not registered its session yet. Start captures that generation before its
initial lookup and approval rechecks it immediately before enabling. An accepted
cancel also invalidates older starts still awaiting their initial lookup, so a
stale join cannot recreate the canceled session. A later new sign-in start is a
new intent; explicit routing on remains an independently verified change and
does not re-arm an older suppressed sign-in.
The Tailscale proxy permits exactly `/api/accounts/signin/start`,
`/api/accounts/signin/cancel` and `/api/accounts/routing` for POST. Status is folded
into the overview; raw management endpoints are never forwarded.

## Model errors and failover

Live policy verified 2026-10-27 on CLIProxyAPI 8.0.30:
`routing.retry.request-retry: 5`, `max-retry-credentials: 2`,
`max-retry-interval: 45` seconds. Management model listings confirm one Pro and
one Free Codex account. The Free listing excludes `gpt-6.1-sol`, `gpt-6-astra`,
`gpt-6-sol` and `gpt-5.6-sol`; see [model routes](standard-management-setup.md#model-routes).

Source-only research against v8.0.7 (revision `97f244b8ddb9cbf564b6e6faab0159102cca8617`) confirms that
credential fallback is broader than quota/429. No gateway or OpenAI request was
sent for this research. The historical policy recorded in
`docs/quota-failover-verification.json:22-24` was `request-retry: 0`,
`max-retry-credentials: 2`, `max-retry-interval: 0`; live
configuration was not read then. The source details below are pinned to 8.0.7.

- `sdk/cliproxy/auth/conductor_execution.go:483-526,630-690` tracks attempted
  credentials and picks another after an upstream error, unless it is classified
  as a request fault or an explicit configured stop. The credential limit applies
  inside that attempt, independently of additional request retry rounds. Streaming
  uses the same outer credential loop at lines 911-1045 and 1186-1221;
  `conductor_stream.go:290-317,396-405` stops request faults before fallback.
- `sdk/cliproxy/auth/conductor_cooldown.go:1762-1805` recognizes model-support
  wording such as `model_not_supported`, `unsupported model`, and
  `not available for your plan/account` for HTTP 400/422/404, plus structured
  `model_not_found` errors (`1980-2012`). `isRequestInvalidError` explicitly
  permits those model failures to rotate (`2172-2205`).
- Such model failures get model-level unavailability, normally a 12-hour cooldown
  (or the supplied retry interval), at `conductor_cooldown.go:803-835`.
  Ordinary 401/402/403 failures normally get 30 minutes, 404 gets 12 hours,
  and 429 has its separate quota handling (`856-887`). An alternate must still
  be enabled, authenticated, support the model and be available for selection.
- HTTP status alone is insufficient. Generic 400 errors and bodies classified as
  `invalid_request_error` stop rotation, as do stale response/item references
  (`internal/clienterror/client_error.go:71-115`). A plain permission 403 can
  rotate, but the wording-based model-support exception does not include 403:
  a 403 carrying a generic invalid-request body can therefore stop, unless it
  has the recognized structured `model_not_found` signal. Arbitrary 400/403/404
  plan-denial payloads are not all guaranteed to fail over.
- Codex forwards non-success HTTP responses through its status classifier
  (`internal/runtime/executor/codex_executor_execute.go:116-125`,
  `codex_executor_stream.go:120-136`); the classifier does not rewrite arbitrary
  plan-denial messages into model-support errors
  (`codex_executor_terminal.go:317-379`). Fallback cannot replay a stream after
  output has already been delivered.

No dashboard plan guard is needed for the recognized model errors, and restoring
one would defeat user-controlled Free routing. If guaranteed fallback for every
plan-denial shape is required, the next decision should be a narrow gateway
model-capability classification change (including relevant 403 bodies), backed
by synthetic gateway tests. That change is not part of this implementation.

Pinned source links:
[execution](https://github.com/router-for-me/CLIProxyAPI/blob/97f244b8ddb9cbf564b6e6faab0159102cca8617/sdk/cliproxy/auth/conductor_execution.go#L483),
[streaming](https://github.com/router-for-me/CLIProxyAPI/blob/97f244b8ddb9cbf564b6e6faab0159102cca8617/sdk/cliproxy/auth/conductor_stream.go#L290),
[classification and cooldown](https://github.com/router-for-me/CLIProxyAPI/blob/97f244b8ddb9cbf564b6e6faab0159102cca8617/sdk/cliproxy/auth/conductor_cooldown.go#L1762),
[request faults](https://github.com/router-for-me/CLIProxyAPI/blob/97f244b8ddb9cbf564b6e6faab0159102cca8617/internal/clienterror/client_error.go#L71),
[Codex status classification](https://github.com/router-for-me/CLIProxyAPI/blob/97f244b8ddb9cbf564b6e6faab0159102cca8617/internal/runtime/executor/codex_executor_terminal.go#L317).

## Deployment and isolated preview

Preview the explicit account-access deployment manifest from the task worktree:

```sh
python3 scripts/install-operations.py --account-access-only
sudo -n python3 scripts/install-operations.py --account-access-only --apply
```

Both scoped manifests (`--account-access-only` and `--claude-watch-only`) include
the sign-in credential writer, dashboard backend and static assets, dashboard
unit, preview proxy and `router-backup.py`. The backup script is installed at
`/opt/personal-codex-router/router-backup.py`, mode `0755`, so scheduled backups
skip the writer's unfinished `*.tmp.<hex>` files.

Apply backs up replaced files, installs the dashboard unit (its
`ReadWritePaths=-/var/lib/cliproxyapi/auth` lets sign-in replace Codex tokens),
runs `systemctl daemon-reload` and restarts only
`personal-codex-router-dashboard.service` and
`personal-codex-router-preview.service`. It does not restart the gateway or Claude services or change firewall rules. Deployment success alone
does not verify live account status; inspect the next dashboard poll afterward.

For isolated visual checks (synthetic credentials and provider responses only):

```sh
python3 tests/signin_preview.py --port 18429 --state-dir ~/.cache/agent-scratch/personal-codex-router/signin-fixture-state
```

The fixture uses one-second device polling; production uses the provider interval.
Both verify imports immediately and never replay credentials. Only the fixture controls,
sign-in start/cancel and account switches are handled;
every unmocked mutation (including both Claude reset routes) is rejected. The
production management HTTP transport, routing HTTP transport and Claude reset
transport raise if called. This is synthetic transport isolation, not a sandbox
or a claim to exercise live provider acceptance. Stop the fixture when finished.
