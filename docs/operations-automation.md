# Usage retention, release notices and nightly tests

These jobs are included in the full `scripts/install-operations.py` manifest.
Its default invocation only previews changes. For installation and updates,
including the final fast-mode plugin step, follow the
[README procedure](../README.md#update-an-existing-installation). Scoped dashboard/watch installers
do not install or enable these jobs. Nothing automatically upgrades the gateway.

## Raw usage retention

`personal-codex-router-retention.timer` runs daily at 03:30 local time, with up to
10 minutes of jitter. The oneshot runs as `cliproxyapi`, with access to its own
state directory. `scripts/retain-usage.py --dry-run` reports the eligible count
without changing the database or creating archives. It accepts `--db`,
`--archive-dir`, and `--batch-size` for isolated validation; the batch size defaults
to 500 and is limited to 1–1000.

The live defaults are `/var/lib/personal-codex-router/usage.sqlite3` and
`/var/lib/personal-codex-router/usage-archive/`. The archive directory is mode
0700; compressed files and the single-instance lock are 0600, owned by
`cliproxyapi`. Keep these private files and their backups outside the repository.
Archives have no expiry: include them in an off-machine backup policy if raw
request detail must survive disk failure.

Retention keeps at least 90 days by rounding the cutoff down to a complete UTC
day. Valid timestamps strictly before that boundary are eligible. Invalid or
unparseable timestamps stay in the database. Rare fractional-second records
whose SQLite rounded hour differs from their `strftime(timestamp)` hour also
stay: the existing summary query needs these raw records to correct rounding at
an hour boundary. These exceptions preserve exact historic totals.

For each batch, the job:

1. Reads at most 500 raw rows without a write transaction, bounded by the initial
   maximum row ID so ongoing collection cannot prolong the run indefinitely.
2. Writes a uniquely named gzip JSON Lines archive. The first line records format
   version 1, the `usage` column names in order, and the cutoff. Subsequent lines
   are complete row arrays, including IDs and NULLs. The last line records the
   row count and SHA-256 of the exact uncompressed header and row lines,
   including newlines.
3. Fsyncs the file, reopens and decompresses every byte, checks every source row,
   count and checksum, then renames the completed file and fsyncs its directory.
   The archive directory's parent is also fsynced before any deletion.
4. Rereads the published archive, then acquires `BEGIN IMMEDIATE` with a 250 ms
   lock timeout. It checks that the source rows are unchanged, records the
   retention boundary and cumulative deleted count in `usage_retention`, deletes
   just those IDs, and commits. Compression and filesystem syncs take place
   outside this short transaction.

The job never updates `usage_hourly` or `usage_batches`. It refuses missing or
incomplete hourly history. Collection can run between batches; a busy database,
failed verification, changed source row or full disk stops the run safely.
Already committed batches remain archived. A crash can leave an unused complete
archive or `.partial` file; a later run may produce a duplicate archive but never
deletes a row on the strength of an old/unverified file. Archive names are unique;
offline restoration must deduplicate row IDs across duplicate archives.

`gzip -t /private/path/usage-....jsonl.gz` checks compression integrity. A complete
checksum/count verification can be done offline without opening the live DB:

```python
import gzip, hashlib, json
with gzip.open(archive_path, 'rb') as stream:
    lines = stream.readlines()
header = json.loads(lines[0])
footer = json.loads(lines[-1])
assert header['format'] == 1 and header['table'] == 'usage'
assert footer['rows'] == len(lines) - 2
assert footer['sha256'] == hashlib.sha256(b''.join(lines[:-1])).hexdigest()
assert all(len(json.loads(line)) == len(header['columns']) for line in lines[1:-1])
```

Do not restore directly into a running collector database. Restore/test a private
copy first, and restore the matching hourly table from a verified backup if it
is lost. After any raw pruning, the collector refuses to recreate a missing
hourly table from incomplete raw rows. The retention marker is committed in the
same transaction as deletion and must be preserved with the database.

Dashboard history starts at each provider's earliest rollup hour. Whole-hour
summary ranges, including All recorded, retain complete hourly totals after raw
pruning. Recent non-hour ranges still use exact raw records. A non-hour range
that starts in archived history rounds its start down to the containing UTC
rollup hour, including the full first hour rather than returning an error or
silently losing requests. Such summaries include `effective_since` with that
rounded ISO start; they may include up to one additional hour of usage. Starts
at whole UTC hours remain exact. Today and Last 7 days use recent raw detail
even in timezones with fractional-hour offsets; All recorded uses the UTC epoch
and complete hourly history in every timezone.
Missing hourly history after pruning also reports an error rather than partial
raw totals. `GET /api/requests` returns retained detail with HTTP 200 for ranges
reaching into archived history, clamping `since` to the raw retention boundary
when needed. Every page includes `archived_before`: the ISO retention cutoff,
or null until raw
rows have actually been archived. Pagination ends at that boundary. A jump
with `until` before the boundary returns an empty page with the same metadata.
The Requests list shows one quiet "Older requests archived" line at its end
when the selected range crosses that boundary, with the cutoff in its tooltip;
an empty archived jump shows that line in place of a generic empty/error state.
Recent ranges keep their usual display. Summary totals continue to include
archived requests, so their counts can exceed the retained detail list.

No routine VACUUM or explicit blocking checkpoint runs. SQLite reuses freed
pages for new records; WAL readers and the collector avoid the long writer lock,
temporary disk demand and filesystem churn of VACUUM. If reclaiming disk space
becomes necessary, plan an explicit maintenance window and verified backup.

## Gateway releases

`personal-codex-router-release-check.timer` runs weekly on Monday at 10:00 local
time, with up to 30 minutes of jitter. The `cliproxyapi` oneshot anonymously reads
`https://api.github.com/repos/router-for-me/CLIProxyAPI/releases?per_page=30`.
The upstream slug is also recorded in the gateway-upgrade documents. No GitHub
sign-in, PAT, gateway management request, download or installation is involved.

The response has a 10-second timeout and 2 MB limit. The checker takes the highest
stable numeric version among the most recent 30 releases, excluding drafts,
prereleases and non-release tags. If no stable release is present, it records a
failed attempt and preserves the previous observation. ETags avoid refetching
unchanged metadata. HTTP 403/429 records rate limiting and honors numeric
`Retry-After` and `X-RateLimit-Reset` headers with a bounded backoff; no immediate
retry loop runs. Timeouts, malformed responses and other failures retain the
last successful version and timestamp, with a separate attempt/error status.

Atomic, fsynced state is stored at
`/var/lib/personal-codex-router/gateway-release.json` (0600). Run
`scripts/check-gateway-release.py --state /private/scratch/gateway-release.json`
to check without modifying live state. Errors are allowlisted and never contain
raw response bodies. A single-instance file lock prevents overlapping checks.

The dashboard compares that observation with the current `X-CPA-VERSION` value
already in the gateway snapshot. A fresh, online gateway running an older
numeric release creates the normal-urgency notice
`Gateway <new> is available (running <current>)`. Unknown/dev versions do not
invent an update. Existing notices survive gateway outages and failed release
checks, clear on confirmed catch-up, and are superseded by a newer release.
Notice IDs and desktop-delivery deduplication survive restarts and are retained
per version beyond the usual 90-day notice cleanup, so one release causes one
desktop notification. Normal `router-ops.py` delivery handles it.

## Nightly tests

Run all checks once with:

```sh
~/.local/bin/heavy python3 scripts/run-tests.py
```

The runner uses unittest discovery for every Python test, then every
`tests/*.cjs` file, in sorted order. `test_billing_dates.cjs` runs in both
`America/Fortaleza` and `America/New_York`. It also syntax-checks the dashboard
JavaScript and preview proxy. It continues after failures, timeouts and missing
commands, prints one result per check and a final pass/fail count, and exits
nonzero if anything failed. Python discovery is limited to 30 minutes and each
Node command to two minutes. Timed-out checks and their inherited child processes
are killed as a dedicated process group. There is no CI dependency.

`personal-codex-router-nightly-tests.timer` is a desktop user's systemd **user**
timer. It runs at 02:00 local time with up to 15 minutes of jitter. Its installed
helper reads `site.json`'s `source_checkout`, requires the source runner and
tests directory, then runs that checkout's runner through the user's
`~/.local/bin/heavy`. It never tests the installed runtime in place of source.
The full installer writes its source path to site.json; deploy from a durable
checkout, not a disposable worktree. Scoped installers do not enable this timer.

The job waits normally for the heavy scheduler. Its unit has a six-hour timeout
including queue time, and a file lock avoids duplicate nightly runs. Logs live
under `~/.cache/personal-codex-router/test-runs/` (directory 0700, files 0600).
The last 14 task-generated `tests-*.log` files are kept; unrelated files and
symlinks are left alone. Success is quiet. Test or startup failure sends a
normal desktop notification linking the log path; if the session bus is
unavailable, the failure remains in the log and user journal. A hard systemd
timeout or process termination can precede helper notification; inspect the
failed user unit/journal in that case.
