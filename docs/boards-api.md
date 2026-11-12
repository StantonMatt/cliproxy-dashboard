# Boards API

Widget data uses the same loopback, Host, same-origin and `X-Router-Request: 1`
checks as `/api/requests`. All four data endpoints open SQLite with `mode=ro`
and pin their reads to one transaction. They never collect usage or call the
management usage queue. Invalid parameters return HTTP 400 `{"error":"…"}`;
SQLite failures return HTTP 503. Repeated and unknown query parameters are
rejected. JSON responses contain no credential material.

Time ranges are **[since, until)**, with offset-bearing ISO 8601 dates. `since`
defaults to `1970-01-01T00:00:00Z`; `until` defaults to now. `tz` defaults to
`UTC` and accepts an IANA time-zone name. Naive dates and empty ranges are
invalid. The request list retains its existing **inclusive** `until` behavior.

## `GET /api/series`

Parameters:

| Parameter | Values / default |
| --- | --- |
| `metric` | `requests` (default), `failures`, `success_rate`, `tokens`, `input_tokens`, `output_tokens`, `cached_tokens`, `cache_hit_rate`, `avg_duration` |
| `group` | `none` (default), `provider`, `account`, `model`, `outcome` |
| `bucket` | `minute`, `hour`, `day` (default), `week` |
| `since`, `until`, `tz` | Range and local time zone |
| `compare` | `0` (default), `1` |
| `provider`, `account` | Optional provider group and upstream `auth_index` filters |

`outcome` is allowed only for `requests`. Minute ranges are at most six hours;
hour ranges at most 100 days; at most 1,000 buckets are returned. Longer-than-
history ranges start on the first recorded local day. `first_recorded` is the
first hourly rollup timestamp, or the first raw timestamp on an older database.
A database without recorded history returns an empty bucket array.

```json
{
  "metric": "requests", "group": "account", "bucket": "hour", "tz": "America/Santiago",
  "since": "2026-11-01T03:00:00Z", "until": "2026-11-01T05:00:00Z", "unit": "count",
  "first_recorded": "2026-09-22T16:00:00Z", "archived_before": null,
  "buckets": ["2026-11-01T00:00:00-03:00", "2026-11-01T01:00:00-03:00"],
  "current": null,
  "series": [{"key":"account:codex:abc", "label":"person@example.test", "provider":"codex",
    "auth_index":"abc", "email":"person@example.test", "account_key":"account.json",
    "values":[12,3], "total":15}],
  "previous": null
}
```

Entity keys are stable and independent of rank:

| Group | Key |
| --- | --- |
| none | `total` |
| provider | `provider:<upstream-provider>` |
| account | `account:<upstream-provider>:<auth_index>` (empty suffix for unknown account) |
| model | `model:<upstream-provider>:<model>` (`unknown` for an empty model) |
| outcome | `outcome:ok`, `outcome:failed` |

Every series carries `provider`, `auth_index`, `email`, `account_key`; irrelevant
fields are null. Account identity uses the same inventory lookup as existing
usage account/hour rows. Labels fall back from email to account key to auth
index to “Unknown account”. Provider and account filters use the request list's
API-key-channel attribution rules; returned provider identities remain the
upstream identities used by existing usage rows.

Count/token gaps are zero; rate and duration gaps are null. Series are sorted
by current-period total descending, then key. `current` indexes the real bucket
containing now, or is null for historical ranges. Local days and Monday weeks
follow wall time. A repeated hour has two ISO labels with different offsets;
a skipped hour is absent. A skipped local midnight starts at its first real
instant, and the following day returns to midnight. Fractional-offset zones
split retained raw edge hours at their actual local boundaries.

`compare=1` adds `{since, until, buckets, series}` in `previous`. Its series use
the same ordered keys as the current period; absent keys receive zero values
(or null rates). A partial local day starting at midnight compares with the
same wall-clock hours yesterday. Other ranges compare with the immediately
preceding equally long period. Comparisons use the effective range after
history clamping. DST can change the number of real hour buckets in a local-day
comparison: match values to their supplied bucket timestamps, and do not assume
all days have 24 hours.

Metrics:

- `tokens` sums `total_tokens`; other token metrics sum the named field.
- `success_rate` is `100 * (requests - failed) / requests`.
- `avg_duration` is `sum(latency_ms) / sum(latency_ms_count)`, in milliseconds.
- `cache_hit_rate` exactly follows the dashboard's `promptUsage` / `cachedShare`
  rules: Codex and other providers use input as prompt and cached tokens as
  cached prompt. Claude and `claude-api-key` use input + read + creation when
  both split counts cover all requests. Otherwise prompt is
  `max(input, total - output)` when those totals exist, or the available split
  sum. Cached prompt uses read tokens only, with recorded read coverage.
  Accounts are combined after calculating their prompt contributions. No
  positive prompt or no cached coverage yields null; percentages clamp to
  0–100. Period totals are calculated from period counters, never by averaging
  displayed bucket percentages.

## `GET /api/breakdown`

Parameters: `metric` (the six count/token metrics, default `requests`), `by`
(`model` default, `provider`, `account`, `failure_reason`), `since`, `until`,
`top` (1–20, default 8), optional `provider` and `account`.

Response:

```json
{"metric":"requests", "by":"failure_reason", "unit":"count",
 "since":"2026-10-01T00:00:00Z", "until":"2026-11-01T00:00:00Z", "total":10,
 "rows":[{"key":"failure_reason:timeout", "label":"Timed out", "provider":null,
   "auth_index":null, "email":null, "account_key":null, "value":6, "share":60}],
 "other":{"count":2,"value":4,"share":40}, "archived_before":null}
```

Shares are percentages (zero for a zero total). Rows sort descending by value,
then key. `other.count` is the number of folded entities; `other` is null when
none are folded. Failure reasons include failed records only and use
`failure_reason:<kind>`, with the existing request labels: `rate_limit`,
`capacity`, `auth`, `server_error`, `timeout`, `cancelled`, `other`, `unknown`.
Missing or unrecognized legacy reasons map to `unknown`.

## `GET /api/heatmap`

Parameters: `mode` (`weekhour` default or `calendar`), `metric` (`requests`
default, `failures`, `tokens`), `since`, `until`, `tz`.

Weekhour returns `{mode, metric, tz, cells, max, first_recorded}`. `cells` is
seven rows (Monday through Sunday), each with 24 wall-clock hours. Each cell
is the sum for that hour divided by the number of that weekday's calendar days
in the range with recorded history. Zero-activity days after history starts
count in the denominator. A day with a repeated DST hour contributes both
occurrences to its one hour cell; a spring gap contributes zero. An unobserved
weekday has null cells.

Calendar returns `{mode, metric, tz, days:[{date:"YYYY-MM-DD",value}], max,
first_recorded}`. Values are null for days wholly before recorded history,
and zero for recorded days without requests. It includes requested days before
history so the client can draw outlined cells. Calendar output is bounded at
1,000 days. `max` is zero when no numeric observations exist.

## `GET /api/retry`

Parameters: `since`, `until`. Returns the existing usage summary's
`retry_recovery` object directly:

```json
{"source":"journal", "available":true, "evidence_since":"…",
 "evidence_through":"…", "last_scanned_at":"…",
 "recovered_requests":5, "recovered_upstream_429s":8}
```

Recovery is bounded by the successful completion timestamp. Evidence metadata
retains its original meaning (coverage of the journal, rather than the query
range). Unavailable journal evidence returns `available:false` and null counts.

## Retention and query strategy

Full UTC hours use `usage_hourly` through `(provider,hour)` seeks. Recent
partial boundary hours and the current hour use raw records, so lagging current
rollups neither lose nor duplicate requests. Minute series and failure reasons
use indexed raw detail. Older schemas without rollups remain readable.

`archived_before` has the same meaning as `/api/requests`: raw detail before
that timestamp has been pruned. Minute ranges clamp to it. Hour/day/week counts
and token totals continue through the rollup; archived partial UTC hours are
included whole, and effective `since`/`until` disclose the expanded boundary.
For fractional-offset zones, retained edge hours split exactly; archived hours
cannot be split and are assigned to the bucket containing the UTC hour's start.
This is the unavoidable precision limit of the stored hourly archive.

Archived failure counts appear under `unknown`. Token metrics split by failure
reason clamp to retained history (and disclose that effective `since`), because
the archive contains no failed-request token counters. A missing hourly table
after retention returns 503 rather than incomplete totals.

## Request list account filter

`GET /api/requests?account=<auth_index>` combines with all existing filters and
paging. Identifiers contain 1–255 ASCII letters, digits, `_` or `-`. Empty,
malformed and repeated values return 400 `{"error":"Invalid account"}`.
Account identifiers can occur under more than one provider; add `provider` to
select a single provider's account.

## Saved layout version 2

`GET /api/overview` returns:

```json
{"layout":{"version":2,"revision":0,"providers":["codex","claude"],
 "accounts":{"codex":["account.json"]},"inventory":["codex"],"boards":null}}
```

A v1 file upgrades on read with revision 0 and `boards:null`, without modifying
the file. The client creates default boards from the normalized legacy provider
order. Missing/invalid files use the same empty migration state and log one
warning. The server never invents widgets. Card order filters removed keys and
appends existing keys omitted from the saved order exactly as before.

`layout.inventory` is a read-only list of provider IDs in the saved account ledger,
including disconnected providers. Unlike normalized `accounts`, it excludes newly
connected providers until the next layout save. Clients use it to add their first
Accounts widget while preserving deliberate removals. Send inventory keys as empty
account lists when disconnected; `inventory` itself is not a POST body field.

The board header's range picker is browse-only: it changes the current page's
range without changing the stored board or posting a layout. A saved default
range is edited through Board options → Heading & time range in customize mode.
Unrelated Notes or account-order saves preserve that stored default.

`POST /api/layout` accepts:

```json
{"version":2,"base_revision":0,"accounts":{"codex":["account.json"]},
 "boards":[{"id":"overview","name":"Overview","builtin":true,"heading":"",
   "range":"today","showRange":false,
   "widgets":[{"id":"note1","type":"notes","span":4,"height":6,"title":"",
               "options":{"text":"Hello"}}]}]}
```

Success returns `{"layout":<normalized v2 view>}` with revision incremented by
one. A stale `base_revision` returns HTTP 409
`{"error":"conflict","layout":<current normalized v2 view>}` without writing.
`force:true` overwrites the current revision and increments it. Compare and
replace happen under the same writer lock. Writes use a 0600 sibling file,
fsync, atomic replacement and directory fsync.

Legacy `{providers, accounts}` POST bodies remain accepted: they change card
and provider order, preserve stored boards and increment the revision. Optional
`providers` in a v2 body updates the migration order; otherwise it is preserved.

Validation: body at most 262,144 bytes; at most 12 boards and 40 widgets per
board; board/widget IDs are globally unique `[A-Za-z0-9_-]{1,64}`; name 1–24
characters; heading/title at most 60; range 1–16; `builtin`/`showRange` booleans;
widget type `[a-z][a-z0-9-]{0,31}`; span integer 1–12 or `"auto"`. Unknown widget
types and scalar options survive storage for forward compatibility. Options
have at most 24 keys, key lengths 1–32, string values at most 80 characters
(`text` at most 4,000; `account` at most 255), and other values limited to booleans, finite numbers or
null. Arrays/nested objects are rejected. Existing provider/card ID bounds
(255 characters, 64 entries) remain in effect. Invalid bodies return 400
`{"error":"invalid_request"}`; storage/deadline failures return 503.

Widgets accept optional `height`: `"auto"` (also the missing-key default) or an
integer from 3 through 20. Booleans, fractional numbers and numeric strings are
invalid. The client clamps fixed heights to each widget type's registry limits;
Accounts allows 6 to 20 rows; an individual Account allows 4 to 18 rows.
Heading and Accounts with `hideCards:true` always use Auto. Fixed boxes are `57 * height - 34` pixels
on desktop and tablet. Accounts and individual Account widgets never scroll or clip: in a box too short for its
full cards (a fixed height, or Auto on a line with a fixed widget) the cards
condense (snug, then compact), and the box can't be shorter than its compact
cards at the current width, including the Customize bar while customizing. That
measured minimum limits resizing and the height presets (Short is the compact
height) and grows a box or line whose saved height is now too short; it never
rewrites the saved height. Phone layouts ignore heights and retain natural sizing.
The saved list order determines packing; no coordinates are stored. Auto boxes
stretch to their line's bottom and block stacking below them. All-Auto lines
retain content-sized tracks.

Legacy chart `options.height` (`short`/`medium`/`tall`) is kept and only sets
an Auto-height chart's natural plot height, so existing boards render unchanged;
a fixed widget height sizes the plot to the box instead. Charts no longer offer
that option. Deploy the server validation change with or before this client.
Before rolling back to a server without height support, strip `height` from
stored widgets; the older validator rejects layouts containing that key.

`accounts` options are `provider`, `hideRouting` (default false), and `hideCards`
(default false). The picker calls the latter form Header. Its width is Auto
(3 columns per linked account, clamped to 3–12) unless explicitly resized;
its height is always Auto. `hideRouting` applies to both forms.

`account-card` options are `provider`, `account` (the linked account key, up to
255 characters), and `label` (a saved display name for removed accounts). It
defaults to span 3 and Auto height, with a measured compact minimum and measured
Short/Medium presets, like Accounts. A board can contain one Accounts widget
per provider and one individual card per account. An individual card leaves
that board's Accounts widget, returns when removed or reassigned, and does not
change the saved `accounts` order. Accounts Auto width counts remaining cards;
when none remain, it counts all linked accounts. Missing individual accounts
remain saved, hide in view mode, and offer Choose another in Customize.
