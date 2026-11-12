# Usage failure reasons

The Requests API adds a nullable `failure` object: `kind`, `label`, `status_code`,
`code`, and `source`. Successful rows have `failure: null`. Failed rows without
evidence have `kind: "unknown"`. The same object appears in overview recent rows.
Labels are fixed backend text; raw provider diagnostics never reach the API,
spool, or quarantine.

Kinds are `rate_limit`, `capacity`, `auth`, `server_error`, `timeout`, `cancelled`,
`other`, and `unknown`. The collector classifies the queue's `fail` object before
durable storage, including capacity errors after HTTP 200. `failed` remains
authoritative: diagnostic status can differ from downstream HTTP status and may
be synthesized by the gateway. Cancellation does not establish who cancelled.

`GET /api/requests?kind=capacity` selects one of those eight reasons and implies
failed-only filtering. It combines with `provider` (including API-key groups),
`since`, `until`, `before`, and `limit`; keep the same filters on subsequent pages.
Unknown, empty, or repeated `kind` values return HTTP 400. `kind=unknown` also
includes failed rows with NULL reasons or no reason column before migration.
The query retains the failed-row partial index for indexed time/cursor seeks.

Overview adds `usage.failure_counts` for all providers in the selected range and
`usage.providers.<provider>.failure_counts` for each provider. All eight keys are
always present. The existing Claude compatibility summary also includes these
counts. Existing top-level accounts/models/hours still describe Codex. Counts
retain existing usage-record semantics, including failed upstream attempts that
a retry may subsequently recover.

Collector migration adds nullable `status_code`, `failure_kind`, `failure_code`,
`request_id`, and `execution_id` columns under `BEGIN IMMEDIATE`. Historical failed
rows become unknown because the old allowlist discarded their evidence. It does
not match journal errors to usage rows by timestamp. Existing tokens, rows and
hourly rollups are preserved, and the usage batch/spool transaction is unchanged.

`scripts/collect-request-outcomes.py` separately reads only entries with the trusted
`_SYSTEMD_UNIT=cliproxyapi.service` journal field, without
calling the gateway. It can backfill an authorized destination with `--db <path>
--since '7 days ago'`; subsequent calls resume by cursor. It stores only journal
positions, invocation/request IDs, timestamps and statuses in `usage_retry_events`
and `usage_journal_state`. Events and checkpoint commit together in bounded
batches. It never resets a failed/unseekable cursor automatically.

Overview's `usage.retry_recovery` reports `recovered_requests` and
`recovered_upstream_429s`, selected by successful completion time. It requires an
upstream 429 warning and exactly one later Gin HTTP 200 completion on the same
invocation/request ID. Multiple completions make an ID ambiguous and exclude it;
WebSocket 101s and uncorrelated errors cannot establish recovery. These are
observed lower bounds for HTTP recovery, separate from usage totals, and do not
prove complete stream delivery. `evidence_since`, `evidence_through`, and
`last_scanned_at` expose coverage/freshness. Until the reader runs, `available` is
false and counts/timestamps are null. Zero is returned only with reader evidence.

The full `scripts/install-operations.py` manifest ships the reader beside
`collect-usage.py`, plus `personal-codex-router-request-outcomes.service` and its
60-second timer. A full deployment enables the timer; scoped backend/account/watch
installs leave it alone. No deployment or live migration was performed here.

The oneshot runs as `User=cliproxyapi`, `Group=cliproxyapi`, with
`SupplementaryGroups=systemd-journal` granted only to this service. It shares the
normal `personal-codex-router` state directory, with `UMask=0077` and
`TimeoutStartSec=240` above the reader's 180-second internal limit.
`ProtectSystem=strict` allows state writes only under
`/var/lib/personal-codex-router`; `ProtectHome=true`, `PrivateTmp=true`,
`NoNewPrivileges=true`, an empty capability set, kernel/control-group protections,
and `RestrictAddressFamilies=AF_UNIX` isolate it from home files and network access.
The journal remains readable under `/run/log/journal` and `/var/log/journal`:
filesystem protection permits reads and the supplementary group supplies access.
PrivateTmp does not hide `/run/log/journal`; no writable journal path is needed.
The reader never follows the journal, serializes its invocations with its own
lock, and resumes a committed cursor if the initial seven-day scan needs multiple
runs. Normal usage collection keeps its existing 15-second timer and ownership;
no gateway restart or reconfiguration is needed for the reader.
