# Allowance forecasts

`GET /api/overview` adds `forecast` to allowance windows in
`providers[].accounts[].windows[]`, the selected provider views for the UI.
Gateway/watch mirrors also receive the field on their display `windows` arrays.
Recovery-only/internal windows are outside this contract.

Each object always contains:

| Field | Meaning |
| --- | --- |
| `state` | `runs_out`, `lasts`, `idle`, `learning`, `out`, `stale`, or `unavailable`. |
| `reason` | Machine-readable qualification; fall back to the state's wording for unknown reasons. |
| `runs_out_at` | Unix seconds UTC or null. Hypothetical exhaustion ignoring reset; populated for both estimated states. |
| `rate_per_hour` | Allowance fraction/hour, e.g. `0.2` means 20 percentage points/hour. Zero for idle, null without an estimate. |
| `basis_minutes` | Whole minutes of observed history used, at most 180. |
| `observed_at` | Source measurement time in Unix seconds UTC or null; never the dashboard poll time. |

Suggested wording: `runs_out`: “At this pace, runs out Thu 14:00”; `lasts`:
“At this pace, lasts until reset”; `idle`: “No recent usage”; `learning`:
“Learning your pace”; `out`: “Allowance used up”; `stale`: “Waiting for updated
usage”; `unavailable`: “Forecast unavailable”. For `superseded_observation`, use
“Waiting for a newer reading”. Show a deadline only for `runs_out`.

The outer window retains `resets_at`. A missing reset allows an exhaustion
estimate but never `lasts`; an invalid reset prevents estimates. Each forecast
applies to one window and makes no claim about routing or whole-account
availability. Idle usage does not guarantee lasting until reset. A predicted
exhaustion passing without a new observation becomes stale, never automatically
out. Even cached exhaustion becomes stale after 15 minutes.

The existing background observer and overview requests sample selected local
measurements. Values are retained on change or every five observed minutes.
Repeated cached values do not create time/history. A reset timestamp change or
usage decrease starts a new series; late/conflicting observations cannot roll it
back. Rate is total percentage change over elapsed time in the last three hours,
requiring at least 15 minutes and one percentage point of increase. Idle requires
30 unchanged observed minutes. Measurements older than 15 minutes cannot train
or produce a forecast. No token-to-allowance conversion or provider call is used.

`operations.AllowanceForecastStore` persists hashed account/window identities and
timestamp/fraction pairs in the existing private service state directory at
`/var/lib/personal-codex-router/allowance-samples.json`. It retains at most 256
series with 256 samples each, expires unseen series after eight days, and rejects
files over 4 MiB. Writes use a private `0600` sibling file, fsync, atomic replace,
and directory fsync. Missing/corrupt state relearns. Failed writes retain samples
in memory and retry after five minutes. The store has one dashboard writer.

`main()` supplies the durable store. Embedded/test `GatewayState` callers inject
`forecasts=AllowanceForecastStore(path)` to enable forecasts; otherwise they have
no forecast side effects. No new Python installation-manifest entry is needed.
Run `python3 -m unittest test_allowance_forecast` from `tests/` for focused checks.
