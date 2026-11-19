# Widget board performance hill climb

Phases 1 and 1b establish synthetic input, drag/resize benchmarks and a diagnosis.
No dashboard JavaScript or production server behavior changes are part of either phase.

## Run the harness

Run from the implementation checkout. `PLAYWRIGHT_MODULE` can point to an
installed Playwright package, as in the existing browser tests. The harness also
looks in the Codex runtime dependency directory. Choose a compatible installed
browser with `PLAYWRIGHT_CHROMIUM`; no browser installation is performed.

```sh
env PLAYWRIGHT_CHROMIUM=/home/user/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell \
  ~/.local/bin/heavy node scripts/perf/bench-board.mjs \
  --baseline-ref eb0f6c67af5467b5aa3b0e5e73fccd6c4b7a5da2 --aa \
  --iterations 5 --warmups 1 \
  --output ~/.cache/agent-scratch/personal-codex-router/perf/baseline-aa-eb0f6c6-verified
```

For a candidate, omit `--aa`; the default candidate is the checkout containing
the harness. `--candidate /absolute/checkout` selects another checkout. Use
`--baseline /absolute/checkout` instead of `--baseline-ref` to reuse a baseline.
`--baseline-ref main` resolves main at run time; pin a full SHA for comparisons.
The harness creates a detached baseline under
`~/.worktrees/personal-codex-router/perf-baseline-<PID>`, then removes it and
prunes the worktree registry. It creates no branches or commits. Uncommitted
candidate changes are used and `git status --short` is recorded.

The separate two-checkout integration smoke command was:

```sh
env PLAYWRIGHT_CHROMIUM=/home/user/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell \
  ~/.local/bin/heavy node scripts/perf/bench-board.mjs \
  --baseline-ref eb0f6c67af5467b5aa3b0e5e73fccd6c4b7a5da2 \
  --candidate /home/user/.worktrees/personal-codex-router/perf-widget-board-hill-climb \
  --iterations 1 --warmups 0 \
  --output ~/.cache/agent-scratch/personal-codex-router/perf/two-checkout-smoke-eb0f6c6
```

Defaults: five scored runs per arm, one discarded warmup per arm, A/B/A/B order,
1440×900, device scale 1, UTC, en-US, light theme, normal animation preference,
headless Chromium, 4× CDP CPU throttling. `--cpu`, `--iterations`, `--warmups`,
`--port-a` and `--port-b` are configurable. Mobile is deferred: this board disables
pointer dragging at widths ≤760px and uses move controls instead.

Outputs are `results.json`, `summary.md`, one JSON per run, gzip-compressed CDP
traces per scenario and a separate diagnostic `.cpuprofile`. Output must be below
`~/.cache/agent-scratch/personal-codex-router/perf/`. Gzip traces can be
decompressed outside the repo and loaded in Chrome DevTools Performance or Perfetto.

```sh
node scripts/perf/trace.mjs /absolute/path/run-1-A-S1.trace.json.gz S1-motion
```

`--skip-tests` is only a harness debugging option: it makes the overall result
non-passing. Any browser gate failure also makes the command exit nonzero. On
normal completion, exceptions, SIGINT and SIGTERM, the harness closes its browser,
terminates its two synthetic servers and removes its temporary baseline worktree.
SIGKILL cannot run cleanup; the recorded PIDs/path identify task-owned resources.

## Synthetic mode and boundaries

```sh
python3 -B scripts/perf/fake-dashboard.py --fake-data \
  --checkout /absolute/checkout --port 8390
```

This is a separate opt-in fixture server, not a flag on the production entrypoint.
`--fake-data`, `--checkout` and `--port` are required. CLI ports are restricted to
8390–8399; the bind address is always 127.0.0.1. Host, same-origin and
`X-Router-Request` checks protect the API; static paths stay inside the selected
checkout's `dashboard/static`. All state and layout writes are in memory.
Unknown mutation endpoints fail closed. The fixture imports only Python stdlib,
never the production server or its auth/usage modules. A subprocess audit-hook
self-test rejects outgoing connects and production/auth/usage file access.

The fixed board has 15 widgets: four usage charts, heatmap, timeline, pulse,
health, four individual account cards (two Codex, two Claude), model breakdown,
request stat and retry recovery. Accounts use `example.test` identities.
The fixture's epoch is 2026-11-02 12:00 UTC; the browser's Date follows that origin
plus monotonic elapsed time. Request counts and time-series values advance by a
deterministic API poll counter; each run resets the counter and layout. All
overview/series/breakdown/heatmap/retry/request endpoints stay synthetic.
The page uses the real app, board engine, widgets, styles and native Customize/
Cancel/Done controls. No static JavaScript is patched or substituted.

Browser requests are restricted to that arm's fixture origin. No requests are
sent to 8320, 8317, 8400–8409, upstream providers, real databases or credentials.
No production services, files, units or user browser tabs are controlled.

## Scenarios and metrics

| Scenario | Procedure and measurement window |
|---|---|
| S1 | Enter Customize outside the measured window; press the first chart's grip for 400ms, then sweep it over the next two slots in 120 scheduled small mouse moves, targeting 60 moves/sec over 2s, with a 14px vertical arc; release and wait for settling. Expected order is `[perf-01, perf-02, perf-00, perf-03, …, perf-14]`. Mouse lift uses the real >4px movement threshold; the production 350ms hold timer applies to touch, not mouse. |
| S2 | Fresh browser context, empty browser cache/storage, navigate and wait for all 15 widgets' real render completion (including four chart SVGs, heatmap, timeline and account cards), then settling. OS disk cache remains warm. |
| S3 | Wait for the actual 15s application interval to invoke `load()`. Mark immediately before that callback, then measure through the resulting data/render/settling. The idle wait before the poll is excluded. The widget API's 15s cache is allowed to expire naturally. |
| S4 | Click the real Customize button, wait for settling, click Cancel with no edits and wait for settling. Report the combined entry/exit duration. |
| S5a | Fresh default fixture, enter Customize, resize chart `perf-00` with `.resize-grip`: 4→8→12→8→4→6 columns, ending at a fractional pointer position 6.22 columns to exercise the snap. 240 linear pointer steps in five segments, scheduled at 60/sec against an absolute 4s target. Record actual delivery time; 4× CPU throttling backpressures CDP dispatch. Release, settle, Done, verify persisted dimensions. |
| S5b | Fresh default fixture, `.height-grip` on `perf-00`; natural height (7 rounded rows)→+2→+4→+1→0→+2.22 row pitches; expected final 9 rows, width stays 4. Same 240 points/4s target. |
| S5c | Fresh default fixture, `.corner-grip` on `perf-00`; combine S5a width and S5b height paths; expected final 6 columns ×9 rows. Same 240 points/4s target. |
| S5d | Fresh `resize-non-chart` fixture arrangement: move health `perf-07` before chart `perf-00`, `perf-01`, `perf-02`; retain all 15 widgets. Same width path as S5a, expected final 6 columns/Auto. Health has no chart host, so its SVG/ResizeObserver chart counts are zero; chart work belongs to neighbours. |

Settled means 150ms without board DOM/text/attribute mutations or grid resizing,
no pending fetches, no active board glide, no lifted widget, and no `.resizing` or
`.resize-settling` frame. Resize uses custom rAF tweens rather than WAAPI glides. S3 additionally
waits for the actual `load()` promise to finish; its correctness gate requires
the synthetic overview generation to advance. Infinite decorative animations and
the 1s text timers remain enabled; they do not permanently prevent settling.
This is a reproducible quiet-window proxy, not proof of compositor presentation.

The primary scalar score is **S1 drag main-thread work in milliseconds, lower is
better**: exclusive script + layout + style recalculation + paint/prepaint time
between movement start and pointer release. Compare it alongside movement p95,
long tasks, pointer-handler time and drop settling; do not keep a change that
regresses feel/correctness because its scalar score improved. A/A noise is the
minimum evidence threshold, not statistical proof. The summary labels a median
difference within either arm's observed run range as inconclusive.

| Metric | Definition |
|---|---|
| Frames | rAF timestamp intervals during S1 movement or S5 motion only; nearest-rank median/p95 and max. Other scenarios also retain interval distributions. Counts and duration are retained so different-length sweeps are visible. |
| >16.7ms / >33ms | Number of observed rAF intervals strictly over those thresholds. These are slow intervals, not an estimate of the exact number of missing presented frames. 60Hz timestamp quantization can put ordinary 16.7ms frames just over the first threshold; >33ms is more useful. |
| Long tasks | PerformanceObserver entries >50ms; count and total duration. Full S1 includes hold, movement and drop; `duringDrag.longTasks` isolates lifted motion. |
| Script/layout/style/paint | Renderer-main-thread CDP timeline slices, with direct child durations subtracted to avoid counting layout inside script twice. Paint includes PrePaint; excludes GPU/raster/compositor worker time. S1 retains whole-scenario, motion-only and drop-only breakdowns; S5 retains whole-scenario, motion-only and release-only breakdowns. |
| Forced synchronous layouts | `Layout` events nested inside script execution on the renderer main thread. `UpdateLayoutTree` under script is counted separately as forced style recalculation. These are observed browser recalculations, not all geometry getter calls or DevTools' >30ms warning count. Initiator stacks are retained. |
| Pointermove | Wall time inside the registered boards.js pointermove listener; median/p95/max, count and total. Includes work forced synchronously by the listener; excludes unrelated pointer listeners. |
| Drop-to-settled | From capture-phase pointerup, before the real drop handler, until the settled check succeeds. Includes glides, deferred renders and the 150ms quiet interval. |
| First full board | Navigation-relative `performance.now()` when all fixture widgets are drawn and the first settled window is reached. It is a conservative stable-board measurement, not FCP or the first pixel of each widget. |
| Chart/timer activity | Constructor-source-tagged ResizeObserver and interval callbacks plus chart SVG insertions observed by MutationObserver; full-S1 and movement-only counts are separate. Callback activity is not itself proof of a redraw; SVG replacement is stronger evidence. |
| Resize phases | Start scenario before pointerdown; motion starts after resize setup and immediately before the first scheduled move. Capture-phase pointerup ends motion and starts release, before the app release handler. Finish after settling. Timeline events crossing a boundary are clipped. Motion frames require both timestamps in the motion window. |
| Resize long tasks | Count original >50ms PerformanceObserver entries overlapping each phase; sum only their overlap duration in that phase. An entry crossing pointerup can appear in both counts; its duration is split and never added twice. This prevents charging the release handler to motion merely because the task began just before capture-phase pointerup. |
| Release-to-settled | Capture-phase pointerup to completion of the resize-aware settled check, including the 270ms live-box tween and 150ms quiet window. This differs from trace release duration by instrumentation/collection overhead. |
| Resize chart activity | Each direct SVG insertion MutationRecord counts once (multiple replacements of one widget in one batch remain separate). IDs distinguish target from neighbours; `svgByWidget` and `resizeCallbacksByWidget` retain detail. Constructor-tagged chart ResizeObserver callbacks include entry target widget IDs; callbacks on detached/unmapped hosts remain in total but not target/neighbour counts. SVG insertion counts include chart, pulse and timeline graphics; stat spark callbacks are counted by chart observer instrumentation, while the phase-1 SVG scope excludes stat sparks. These are observed insertions/callbacks, not presented frames. |
| Natural probes / packing | A live `#board-grid > .w` deep clone marks one successful `measureNatural` probe (boards.js:167); early returns do not clone and are excluded. One `--cols` setProperty call marks entry into `applyPacking` (boards.js:208). No geometry read or stack capture is added on these paths. Full function-entry counts, including early returns, are retained as CDP precise coverage in the unscored S5a/S5c diagnostic only. |
| Resize rAF | Constructor-free callback tagging recognizes the boards.js rAF containing `updateResize(r)`; reports callback count and duration distribution. Sampling profiles plus timeline stacks cover nested private functions. Input points, rAF updates, packing transitions and chart draws are distinct counts. |
| Resize animation gate | Observe the target's actual `__resizeSettle`, `.resize-settling` class, at least two rAF samples, and changed inline width/height/translate between samples; wait until settling clears. Merely scheduling a callback cannot pass. Model and DOM span/height must match expected values; Done must persist them normally. Omitted serialized height normalizes to Auto. |
| Spread | Per-arm median [min,max] over five scored runs; full range max−min and relative range are in JSON. Warmups and the detailed sampling-profiler run are excluded. No confidence interval is implied. |

Every run reports order in DOM and layout model, persisted order after Done,
console/page errors, failed HTTP responses, board readiness, and observed running
`board-reorder` animations with positive time/progress. Calling `animate()` alone
does not pass the animation gate. Existing reorder/widgets/boards tests run in
each arm; fixture and trace-analysis self-tests run from the harness checkout.

The CPU profiler runs only in one additional unscored baseline pass (S1, S5a and S5c).
S5a/S5c also enable precise coverage in that diagnostic; coverage disables V8
optimization and sampling adds overhead, so diagnostic timings never enter A/A scores. Native
geometry getter costs often appear without JavaScript file locations; use trace
initiator stacks to attribute them. Samples, timeline wall durations and handler
measurements overlap and must not be added together.

## Baseline record


### 2026-11-02: final main/main baseline

Both arms: `eb0f6c67af5467b5aa3b0e5e73fccd6c4b7a5da2`; clean detached checkouts.
Started `2026-11-02T21:56:45.521Z`; Linux x86_64, 13th Gen Intel(R) Core(TM) i9-13900K; Chromium `156.0.8078.4`.
Five scored runs per arm, one discarded warmup each, plus one unscored CPU profile.
All 91 browser gate checks passed across 13 passes.
Each scored sweep delivered 120 board pointermove callbacks over 2128.80–2224.51ms (target 2000ms); 126–131 measured frame intervals.

Results: `/home/user/.cache/agent-scratch/personal-codex-router/perf/baseline-aa-eb0f6c6-verified/results.json`

Summary: `/home/user/.cache/agent-scratch/personal-codex-router/perf/baseline-aa-eb0f6c6-verified/summary.md`

Source fingerprints: `/home/user/.cache/agent-scratch/personal-codex-router/perf/baseline-aa-eb0f6c6-verified/harness-sha256.json`

| Metric (ms unless a count) | A median [min,max] | B median [min,max] | B − A | Delta |
|---|---:|---:|---:|---:|
| S1.frames.median | 16.70 [16.70, 16.70] | 16.70 [16.70, 16.70] | +0.00 | +0.00% |
| S1.frames.p95 | 16.80 [16.80, 16.80] | 16.80 [16.80, 16.80] | +0.00 | +0.00% |
| S1.frames.max | 33.40 [33.30, 49.90] | 33.40 [33.30, 49.90] | +0.00 | +0.00% |
| S1.frameIntervalsOver16_7 | 62.00 [56.00, 68.00] | 65.00 [60.00, 69.00] | +3.00 | +4.84% |
| S1.frameIntervalsOver33 | 2.00 [2.00, 3.00] | 2.00 [1.00, 3.00] | +0.00 | +0.00% |
| S1.longTasks | 3.00 [3.00, 3.00] | 3.00 [3.00, 4.00] | +0.00 | +0.00% |
| S1.longTaskTotalMs | 652.00 [582.00, 696.00] | 631.00 [565.00, 699.00] | -21.00 | -3.22% |
| S1.mainThreadMs.script | 304.03 [292.56, 335.88] | 313.99 [284.00, 331.87] | +9.97 | +3.28% |
| S1.mainThreadMs.layout | 158.59 [139.89, 172.27] | 148.30 [143.25, 160.33] | -10.29 | -6.49% |
| S1.mainThreadMs.recalcStyle | 577.25 [547.76, 608.39] | 530.59 [517.15, 618.03] | -46.65 | -8.08% |
| S1.mainThreadMs.paint | 166.20 [159.71, 184.91] | 179.44 [167.75, 188.13] | +13.25 | +7.97% |
| S1.forcedLayouts | 91.00 [89.00, 92.00] | 92.00 [91.00, 95.00] | +1.00 | +1.10% |
| S1.pointermoveTotalMs | 232.80 [207.00, 244.40] | 247.30 [236.40, 278.50] | +14.50 | +6.23% |
| S1.dropToSettledMs | 878.80 [812.90, 929.00] | 846.90 [764.20, 896.90] | -31.90 | -3.63% |
| S1.dragWorkMs | 489.69 [455.72, 518.56] | 516.63 [465.87, 550.45] | +26.94 | +5.50% |
| S1.dragMainThreadMs.script | 130.94 [127.01, 150.48] | 142.54 [123.01, 156.70] | +11.60 | +8.86% |
| S1.dragMainThreadMs.layout | 34.19 [28.96, 37.43] | 41.67 [26.98, 44.18] | +7.48 | +21.89% |
| S1.dragMainThreadMs.recalcStyle | 195.76 [177.72, 205.05] | 200.52 [173.61, 210.71] | +4.76 | +2.43% |
| S1.dragMainThreadMs.paint | 124.02 [122.04, 140.01] | 134.04 [122.41, 141.38] | +10.02 | +8.08% |
| S1.dragForcedLayouts | 15.00 [13.00, 16.00] | 22.00 [15.00, 24.00] | +7.00 | +46.67% |
| S2.firstFullBoardMs | 1654.90 [1633.20, 1886.50] | 1676.30 [1621.80, 1710.50] | +21.40 | +1.29% |
| S3.durationMs | 842.00 [787.30, 899.40] | 809.10 [789.30, 929.50] | -32.90 | -3.91% |
| S4.durationMs | 2799.50 [2474.60, 2840.20] | 2715.70 [2506.90, 2916.40] | -83.80 | -2.99% |

Identical-code score delta: **+26.944ms (+5.50%)**.
A spread: **62.838ms (12.83%)**; B spread: **84.581ms (16.37%)**.
Interpretation: **inconclusive within observed run spread**.
Use roughly **16.4% / 84.6ms** as an initial signal threshold for this workload, then repeat and check secondary metrics; this is not a significance test.
Pointer-total relative spread: 17.02%; drop settling: 15.67%; first full board: 15.31%; idle poll: 17.33%; Customize: 15.08%.
The preliminary five-pair A/A (before correcting phase-boundary trace accounting) gave a false 7.21% score improvement, with an 80.554ms / 14.92% A spread. Those artifacts remain at `~/.cache/agent-scratch/personal-codex-router/perf/baseline-aa-eb0f6c6/`; use the final run above for comparisons. This independent run reinforces the noise warning.

### Detailed S1 profile and diagnosis

Trace: `/home/user/.cache/agent-scratch/personal-codex-router/perf/baseline-aa-eb0f6c6-verified/diagnostic-1-A-S1.trace.json.gz`

CPU profile: `/home/user/.cache/agent-scratch/personal-codex-router/perf/baseline-aa-eb0f6c6-verified/diagnostic-1-A-S1.cpuprofile`

Detailed movement: 2177.743ms; frame median/p95/max 16.70/16.80/33.40ms; 129 intervals, 61 >16.7ms and 2 >33ms.
Movement work: script 142.509ms; style 185.051ms; layout 42.147ms; paint/prepaint 126.202ms. 22 forced Layout events and 149 forced style recalculations.
Whole S1: 3 long tasks totaling 607ms; 91 forced Layout and 223 forced style events.
Pointer-handler median/p95/max: 1.20/1.60/39.10ms; total 214.50ms. Drop-to-settled: 797.50ms.

Top located JavaScript self-time functions (sampling estimates from the separate diagnostic):

| Function | File:line:column | Self time (ms) |
|---|---|---:|
| chartModel | `dashboard/static/widgets.js:226:20` | 37.319 |
| follow | `dashboard/static/boards.js:622:16` | 27.576 |
| hourName | `dashboard/static/charts.js:550:18` | 16.945 |
| render | `dashboard/static/boards.js:274:16` | 14.350 |
| levelSplitCards | `dashboard/static/app.js:806:25` | 13.204 |
| context | `dashboard/static/boards.js:96:17` | 7.349 |
| measure | `dashboard/static/boards.js:194:17` | 2.669 |
| providerAccount | `dashboard/static/app.js:345:25` | 1.301 |

Native self time (no JS file location):
- `getBoundingClientRect`: 420.499ms.
- `get clientWidth`: 77.706ms.
- `remove`: 51.702ms.
- `set innerHTML`: 31.128ms.
- `append`: 23.400ms.
- `requestAnimationFrame`: 19.812ms.

Use trace initiator stacks to locate geometry calls. Sampling self times, timeline durations and handler timings overlap; do not add them. `(idle)` and `(program)` are not ranked as app functions.

| Phase and initiator | Forced Layout | Forced style | Combined time (ms) |
|---|---:|---:|---:|
| Motion: `app.js:2577` (layoutRects) | 3 | 3 | 61.921 |
| Motion: `boards.js:623` (follow) | 0 | 120 | 59.852 |
| Motion: `boards.js:169` (measureNatural) | 6 | 13 | 7.639 |
| Motion: `boards.js:172` (measureNatural) | 13 | 13 | 29.131 |
| Drop: `boards.js:169` (measureNatural) | 24 | 26 | 107.279 |
| Drop: `boards.js:172` (measureNatural) | 27 | 27 | 163.872 |
| Drop: `charts.js:71` (size) | 7 | 7 | 63.932 |

1. **Natural-height probing dominates the measured drop geometry cost.** `boards.js:165–175` clones complete widget/SVG subtrees, appends a probe, reads its height and removes it. The live-width and clone-height getters account for 51 forced Layout plus 53 forced style events after release: **271.151ms**, 63.46% of drop layout+style. Across S1 they cause 70 of 91 forced Layout events and 307.921ms of synchronous work. Confidence: high; trace stacks identify both getter lines.
2. **The movement loop forces style resolution per point.** `boards.js:622` writes translate, then line 623 reads the drawn rectangle. Its 120 forced style recalculations cost **59.852ms**; this source causes 0 Layout events. Do not call these 120 forced layouts. `pointerMove` at 605–606 calls follow synchronously; `dragItems` at 608 queries the DOM each time. Confidence: high for flushes; gains from coalescing high-rate input need measurement beyond this cadence.
3. **Slot changes create larger synchronous flushes.** `boards.js:626–628` reads FLIP rectangles, cancels glides, moves the slot, packs and immediately reads settled boxes in `app.js:2576–2581`. Its 3 Layout plus 3 style events cost **61.921ms** during movement (58.447ms style). The profiled handler max is 39.10ms. Confidence: high for this packing path; other spans/travel can differ.
4. **Release rerenders more than the moved widget.** `boards.js:646` calls render; line 311 renders and queues natural measurement for every active widget. The diagnostic records 6 chart SVG replacements after release and 0 while moving (the raw events locate replacements after pointerup). Repeated chartModel (`widgets.js:226`), SVG construction and date-label work also contribute. This explains part of the drop/S2/S4 tail. Confidence: high for work observed; any skipping must preserve Customize state, async generations, ranges and fresh data.
5. **Observers/timers fire, but chart redraws are not the motion bottleneck here.** There is 1 chart ResizeObserver callback (`charts.js:55`), plus 8 board observer callbacks (`boards.js:298–301`) and 6 widget timer callbacks. Chart SVG replacements during movement: **0**. Pulse and health callbacks at `widgets.js:978`/`1048` have small direct costs; text mutations can still queue natural probes via the board MutationObserver. Blanket chart/timer disabling is not supported by this trace. Confidence: high for this sweep; timer-to-probe causality beyond the observed callback chain remains advisory.

Timer at `widgets.js:978` during motion: 3 ticks, 0.900ms direct callback time.
Timer at `widgets.js:1048` during motion: 3 ticks, 0.200ms direct callback time.
All 3 recorded 260ms glides: 3 observed running with positive progress. Real polling, 1s timers and decorative animations remain active.

### Ranked phase 2 candidates (none implemented)

| Rank | Candidate and location | Expected impact / evidence | Risk to feel or correctness |
|---|---|---|---|
| 1 | Batch/cache natural size by content, width and edit state; `boards.js:165–175`, `298–301` | Largest measured drop opportunity: 271.151ms synchronous probe budget, plus clone/remove work; 36.770ms during movement. These are addressable budgets, not promised savings. | High: stale natural heights can clip content, change packing, break account density/minima or loop observers. Invalidate for fonts, width, content and Customize; keep height/account tests. |
| 2 | Derive the drawn rect from captured rect + pointer delta; cache drag items/gaps safely; `boards.js:608–628` | Target 120 forced style flushes / 59.852ms plus selectors/allocations. | Medium: preserve scroll, transforms, viewport changes, gap landings and directional hysteresis. |
| 3 | Batch FLIP/packing geometry and reuse home boxes until topology changes; `boards.js:626–628`, `app.js:2576–2581` | Target 61.921ms slot-change flush budget and worst handlers. | High: stale boxes can destabilize overlap/gaps, reverse travel, glide retargeting and final order. Retain visible glides. |
| 4 | Skip unchanged content on drop/Customize transitions; `boards.js:311`, `646`, `widgets.js:226` | Reduce 6 post-drop SVG replacements and chart/date work; measure separately from probe caching. | Medium/high: stale controls, data, ranges, focus, pending renders or async generations. |
| 5 | Coalesce pointermove to one pending rAF; `boards.js:605–606` (resize already does this at 605) | Likely helps 120–1000Hz input; this 60-moves/sec schedule does not establish a large gain. | Medium: adds up to one frame of latency; flush last position on pointerup/cancel and preserve autoscroll/capture. |

For each: one behavior change, focused checks, identical fixture/browser/throttle, improvement beyond A/A noise, then all correctness gates and secondary metrics. Keep only measured improvements.

### Process notes and limitations

- The installed Playwright module expected a missing browser revision 1234. An explicit
  existing headless-shell revision 1248 worked; its actual reported version is 156.0.8078.4.
  Pin both package and executable for future comparisons; do not silently download or switch browsers.
- Infinite pulse animation makes a naive “all animations finished” check hang. Track board
  glide objects and a quiet window while keeping decorative animation/timers enabled.
- `document.getAnimations()` in every instrumentation rAF caused style resolution itself.
  That draft was rejected. Final instrumentation tracks returned animation objects instead.
  A first rAF often sees animation time zero; keep sampling until progress is positive.
- Mouse does not use the 350ms touch timer. The benchmark still waits 400ms, then crosses
  the real mouse movement threshold. Touch long-press remains covered by the reorder tests.
- A manually invoked early `load()` would hit the widget API cache. S3 waits for the actual
  15s callback and measures from callback entry. This adds about 15s to each browser pass.
- Naive per-step sleeps double-count CDP dispatch overhead. Moves are scheduled against an
  absolute target cadence; actual delivered duration is still recorded (about 2.15–2.23s).
- Native geometry costs dominate JS self time. Retain timeline initiator stacks and separate
  style from Layout; do not infer a layout from every getter or add overlapping metrics.
- rAF p95 stays near 16.8ms despite substantial total work and a long drop tail. Keep the
  scalar work budget and drop metric, not just FPS. Strict >16.7ms counts are quantization-sensitive.
- Shared-machine CPU scheduling, CPU throttling and trace instrumentation introduce noise.
  `heavy` controls finite-job/RAM concurrency, not a dedicated CPU. The preliminary 7.21% false A/A gain
  demonstrates why a single pass cannot choose a winner. No traces from rejected drafts enter the baseline.
- The fixture avoids the existing production-server import/patch machinery. It serves unchanged
  static files from either checkout and has no production transport or storage dependencies.
  API response shapes must stay compatible with both refs; HTTP/readiness gates expose gaps.
- This is a synthetic frontend benchmark with warm OS caches, local in-memory APIs, 15 mostly
  Auto-height widgets and a same-row desktop sweep. It does not measure production backend I/O,
  touch/mobile pointer drag (disabled by the board), mixed-span cross-row travel, autoscroll,
  hardware compositing, or subjective feel. Resizing was added in phase 1b below. Extend scenarios before generalizing.
- Whole-repo suites were not run: the requested focused suites and harness integration gates
  cover this phase; no production JavaScript/server code changed.

- A trace event can enclose a scenario marker (pointerup EventDispatch begins just before the drop marker). The analyzer must clip overlapping parent events to the window, not discard them. The added regression failed before the fix (script/style 0 instead of 68/3ms), then passed. Reanalysis of the first trace corrected drop script 91.175→168.385ms and forced Layout attribution 63→69; final A/A was rerun with the corrected analyzer.
- A rejected calibration had two S3 samples ending in 6–7ms before the response rendered. Final instrumentation waits for the actual load promise, pending requests and a 150ms quiet window, and gates on a changed synthetic overview generation. Do not accept an idle-looking frame as refresh completion.
- Both A/A calibrations are noisy; no optimization win is claimed. The separate two-checkout smoke verified detached main versus this implementation worktree at the same static revision; its one sample per arm is integration evidence, not a noise estimate.

### Validation and cleanup

- Final A/A: 91/91 browser gate checks PASS across 13 passes (10 scored, 2 warmups, 1 unscored diagnostic). DOM/model/expected order and saved order match; console and HTTP errors absent; full board ready; glides observed progressing; the actual poll finishes, pending requests drain and the synthetic overview generation advances.
- Two-checkout smoke: 18/18 browser gate checks PASS across 3 passes. Results: `~/.cache/agent-scratch/personal-codex-router/perf/two-checkout-smoke-eb0f6c6/results.json`. It used detached baseline `perf-baseline-2578417` versus the assigned worktree, then stopped ports 8390/PID 2581088 and 8391/PID 2581090 and removed/pruned the baseline.
- `node tests/test_reorder_ui.cjs`: PASS in each arm (drag/drop/cancel, touch long-press, fixed-height gaps, refresh deferral, keyboard moves, refused saves).
- `python3 -B tests/test_widgets_ui.py`: 16/16 PASS in each arm.
- `python3 -B tests/test_boards_ui.py`: 65/65 PASS in each arm.
- These three requested focused commands also passed directly in the assigned implementation worktree after the final benchmark: reorder PASS, widgets 16/16, boards 65/65.
- `python3 -B tests/test_perf_board_fixture.py`: 6/6 PASS (opt-in, ports/bind, deterministic data, Host/origin/header, static containment, forbidden mutations, memory persistence/conflicts/reset, audit-hook isolation). Production server tests were not touched.
- `node tests/test_perf_board_trace.mjs`: 13 assertions PASS, including the regression for events crossing scenario markers. The original log message understated assertion count; the final message is corrected.
- Final A/A servers: 8390/PID 2620459 and 8391/PID 2620460 stopped; detached `/home/user/.worktrees/personal-codex-router/perf-baseline-2617910` removed and registry pruned.
- No production JS/server code, main checkout contents, live services, real databases or credentials changed. No commit/push/stash/reset/rebase/branch creation, delegation or review loop. Temporary browser processes closed; no persistent task servers.


## Phase 1b: resize extension (2026-11-02)

Run command (5 scored samples per arm, 1 warmup each, 1 separate diagnostic):

```sh
env PLAYWRIGHT_CHROMIUM=/home/user/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell \
  ~/.local/bin/heavy node scripts/perf/bench-board.mjs \
  --baseline-ref eb0f6c67af5467b5aa3b0e5e73fccd6c4b7a5da2 --aa \
  --iterations 5 --warmups 1 \
  --output ~/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6
node scripts/perf/report-resize.mjs \
  ~/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6
```

### Resize control flow and measurement boundaries

- `pointerStart`, `boards.js:573–579`: capture live frame/body/grid geometry,
  insert a slot, mark `.resizing`, pack, then build readout/cell guides. This
  pointerdown setup belongs to whole-S5 totals and is excluded from motion.
- `pointerMove`, `boards.js:605`: keep the latest coordinates and queue one
  `updateResize` rAF. Adding another rAF throttle is not an established fix.
- `updateResize`, `boards.js:598–603`: every delivered rAF writes live pixel
  width and/or height. On integer span/row changes it reads every frame via
  `rects`, holds neighbours, updates the model, invokes full `render(true)`,
  glides neighbours and rebuilds the readout/guides. Continuous pixel updates
  and quantized topology changes therefore have different costs.
- `render`, `boards.js:274–317`: full chrome/tabs/bar/inventory reconciliation,
  `applyPacking`, `fitAccounts`, and `renderWidget`/`queueNatural` for every
  widget. Active resize/settle guards suppress some work, but they do not
  suppress the chart host's separate observer in `charts.js:55–60`.
- In following rendering cycles chart observers read size, queue a chart rAF,
  build a replacement SVG (`charts.js:106–220`), and cause board content
  observation. Neighbour tween ticks (`boards.js:410–414`) also change real
  dimensions, so observers can redraw neighbours throughout a glide.
- Neighbour landing (`boards.js:404`) reads client dimensions, renders content
  and queues natural measurement. Auto widgets can then deep-clone their
  complete content/SVG (`boards.js:165–175`), append, read geometry and remove.
- Release (`boards.js:633–645`) flushes the final update, captures free/grid
  boxes, removes resize UI, renders/measures/packs, and runs the 270ms custom
  size tween. Landing renders again and queues natural measurement. The
  release metric includes this work and the final quiet window.

### New process notes

- Calibration artifacts are retained at
  `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-calibration-1/`.
  The calibration was terminated after one complete A pass and part of B when
  the persistence assertion incorrectly required explicit `height: auto`.
  Normal serialization omits Auto; the corrected gate normalizes omitted
  heights. No production bug or optimization was involved; rejected calibration
  data is excluded from the accepted A/A.
- Calibration servers 8390/PID 2913507 and 8391/PID 2913509 stopped;
  `/home/user/.worktrees/personal-codex-router/perf-baseline-2910901`
  was removed/pruned by the harness SIGTERM cleanup.
- A long task containing pointerup starts before the capture marker. Resize
  phase metrics split its duration by overlap; classifying only by task start
  would charge release work to motion. A regression covers this boundary.
- Phase-1 SVG counting deduplicated widgets within one MutationObserver batch;
  phase 1b counts each insertion record. Do not compare exact redraw counts
  across those instrumentation revisions as optimization evidence.
- Motion is a 240-input spatial sweep with a 4s target, not a guaranteed 4s
  wall-clock window. CDP dispatch is backpressured by page work at 4× throttle.
  Report actual motion duration, delivered callbacks and total work together.
  This does not model a hardware input stream delivering 240 independent
  events while the renderer is blocked; subjective feel remains unmeasured.
- The quiet-window check now also waits for custom resize tweens. Sampling
  their inline style values does not invoke layout or `getAnimations()`.
- Lightweight probes wrap native clone/setProperty/rAF calls equally in both
  arms. They do not expose or replace private app functions. Precise coverage
  counts all private function entries only in the separate diagnostic, and
  its timings are not used as A/A performance scores.
- The report command is offline postprocessing: no fixtures, browser, live
  services or credentials. It writes a complete metric-spread table, diagnostic
  source attribution JSON and a comparison chart beside the retained traces.

- Real 15s application polling and 1s pulse/health timers remain active during
  resize. Height/corner sweeps can cross the next poll after navigation;
  `resize-diagnosis.json` retains per-run motion/release poll counts. Their
  contribution belongs to the measured production-like workload and can add
  timing variance, especially when a poll lands near release. No manually
  invoked early poll or timer suppression was introduced.

### Accepted main/main resize A/A and baseline

Started `2026-11-02T23:18:27.071Z`; both arms `eb0f6c67af5467b5aa3b0e5e73fccd6c4b7a5da2`.
linux x64, 13th Gen Intel(R) Core(TM) i9-13900K; Chromium `156.0.8078.4`.
Five scored samples per arm, one discarded warmup per arm, plus one unscored diagnostic; 1440×900, 4× CPU. All 247/247 gates passed across 13 passes.

- `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/results.json`
- `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/summary.md`
- `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/resize-summary.md`
- `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/resize-diagnosis.json`
- `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/resize-summary.html`
- `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/harness-sha256.json`
- `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/analysis-sha256.json`

Combined headline cells below are median [min,max] across all 10 scored identical-code runs (warmups and profiles excluded). All times ms; nearest-rank distributions. Each sweep delivered exactly 240 board pointermove callbacks and 240 resize-rAF updates. These combined cells describe the workload; the per-arm table below defines A/A noise.

| Scenario | Actual motion | Motion work | Frame p95 | Frame max | >33ms | Release-to-settled |
|---|---:|---:|---:|---:|---:|---:|
| S5a | 9863.609 [9401.544, 10281.154] | 6801.519 [6533.879, 7199.868] | 83.300 [83.200, 100.000] | 283.300 [233.400, 333.300] | 56.000 [48.000, 59.000] | 511.300 [507.100, 693.700] |
| S5b | 11981.442 [11831.714, 13282.076] | 8811.374 [8689.492, 9961.201] | 200.000 [200.000, 216.700] | 266.700 [249.900, 299.900] | 62.000 [58.000, 68.000] | 475.000 [457.700, 1209.400] |
| S5c | 14739.844 [14406.889, 15665.808] | 11046.321 [10785.143, 11855.279] | 133.300 [116.700, 183.400] | 316.700 [283.300, 366.700] | 106.000 [94.000, 118.000] | 489.200 [474.600, 543.000] |
| S5d | 9596.126 [9255.233, 10478.034] | 6201.200 [5959.956, 6946.486] | 66.700 [66.700, 83.400] | 283.300 [250.100, 333.400] | 55.000 [48.000, 65.000] | 477.500 [459.400, 694.700] |

| Scenario | Motion target SVG | Motion neighbour SVG | Motion natural probes | Motion applyPacking |
|---|---:|---:|---:|---:|
| S5a | 239.000 [239.000, 240.000] | 56.000 [52.000, 59.000] | 103.000 [97.000, 110.000] | 18.000 [18.000, 18.000] |
| S5b | 239.000 [239.000, 239.000] | 199.000 [193.000, 201.000] | 193.000 [182.000, 214.000] | 11.000 [11.000, 11.000] |
| S5c | 239.000 [239.000, 239.000] | 130.000 [125.000, 144.000] | 181.000 [161.000, 208.000] | 27.000 [27.000, 27.000] |
| S5d | 0.000 [0.000, 0.000] | 103.000 [95.000, 107.000] | 91.000 [88.000, 99.000] | 18.000 [18.000, 18.000] |

All four motion frame medians were 16.7ms (exact or within floating-point precision). This hides p95 66.7–200ms and worst intervals up to 366.7ms in the scored corner workload. Strict >16.7ms counts are retained below but are quantization-sensitive.

| Motion-work A/A noise | A median [min,max] | B median [min,max] | A range (%) | B range (%) | B−A (%) |
|---|---:|---:|---:|---:|---:|
| S5a | 6801.519 [6638.698, 7199.868] | 6932.985 [6533.879, 7108.404] | 561.170 (8.251%) | 574.525 (8.287%) | 131.466 (1.933%) |
| S5b | 8970.104 [8780.032, 9961.201] | 8794.539 [8689.492, 9731.526] | 1181.169 (13.168%) | 1042.034 (11.849%) | -175.565 (-1.957%) |
| S5c | 11637.623 [10871.066, 11855.279] | 10951.871 [10785.143, 11706.783] | 984.213 (8.457%) | 921.640 (8.415%) | -685.752 (-5.893%) |
| S5d | 6429.990 [6195.706, 6946.486] | 6169.868 [5959.956, 6299.283] | 750.780 (11.676%) | 339.327 (5.500%) | -260.122 (-4.045%) |

All four identical-code work deltas are within at least one arm's observed range; no improvement is claimed. Treat the larger per-arm range as an initial signal floor and repeat. There is no confidence interval.

**Good repeatable operation metrics:** target chart ResizeObserver callbacks are exactly 240 during motion in S5a/b/c; target SVG insertions are exactly 239 in S5b/c and near-deterministic 239–240 in S5a. Total motion SVG insertions in S5a/b/c are near-deterministic (each arm range ≤5%). S5a forced Layout/style counts and S5d forced Layout/style counts also stay within 5% per arm. These are useful primary operation-count diagnostics alongside work time.

**Workload controls:** exactly 240 input/rAF updates; exactly 18/11/27/18 motion pack calls for S5a/b/c/d, and 2 release pack calls in every scenario. These establish matched input/topology; reducing them alone is not a performance win. S5d target SVG/observer zero is a negative control. Frame medians are repeatable but insensitive; release timing, natural-probe counts and many neighbour metrics are variable. Do not use them as sole primary scores.

Height release outliers have an observable confound: the 3 runs with a 15s poll during release took 1058.9/1108.4/1209.4ms, performed 31/31/33 release probes and 751.111/733.954/765.008ms release work. Other scored height runs had 0–4 release probes and 457.7–624.1ms release-to-settled. This is correlation and source evidence, not an isolated causal experiment.

### Detailed S5a/S5c diagnostic

Timings here are from the separate unscored profiler/precise-coverage pass. They are diagnostic budgets, not A/A scores; sampling/coverage and poll alignment differ from the scored runs. Native and JavaScript self times overlap timeline work and must not be added to it.

- S5a trace: `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/diagnostic-1-A-S5a.trace.json.gz`
- S5a raw CPU profile: `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/diagnostic-1-A-S5a.cpuprofile`
- S5c trace: `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/diagnostic-1-A-S5c.trace.json.gz`
- S5c raw CPU profile: `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/diagnostic-1-A-S5c.cpuprofile`

| Diagnostic phase | Script | Layout | Style | Paint/prepaint | Work | Forced Layout | Forced style | Long tasks | Long-task overlap ms | Probes | Packs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S5a motion | 1772.113 | 1377.851 | 2694.648 | 946.155 | 6790.767 | 687.000 | 760.000 | 29.000 | 3931.400 | 97.000 | 18.000 |
| S5a release | 62.358 | 37.722 | 124.626 | 26.901 | 251.607 | 18.000 | 21.000 | 1.000 | 121.600 | 2.000 | 2.000 |
| S5c motion | 3024.282 | 2374.235 | 5530.736 | 1287.120 | 12216.373 | 1231.000 | 1346.000 | 53.000 | 8534.800 | 218.000 | 27.000 |
| S5c release | 63.700 | 42.276 | 111.016 | 36.341 | 253.333 | 23.000 | 27.000 | 1.000 | 112.200 | 4.000 | 2.000 |

Top located JavaScript **motion self time**, after chronological sample repair (ms):

| Function | File:line:column | S5a | S5c |
|---|---|---:|---:|
| render | `dashboard/static/boards.js:274:16` | 273.238 | 425.160 |
| context | `dashboard/static/boards.js:96:17` | 227.743 | 409.411 |
| (anonymous) | `dashboard/static/widgets.js:264:29` | 94.634 | 198.751 |
| hourName | `dashboard/static/charts.js:550:18` | 84.551 | 185.096 |
| hourName | `dashboard/static/widgets.js:82:18` | 79.174 | 183.698 |
| measure | `dashboard/static/boards.js:194:17` | 40.712 | 73.248 |
| drawTime | `dashboard/static/charts.js:102:18` | 35.397 | 47.061 |
| draw | `dashboard/static/boards.js:408:16` | 11.129 | 22.489 |
| measureNatural | `dashboard/static/boards.js:165:24` | 8.570 | 17.298 |

The `widgets.js:264` anonymous callback constructs chart labels; `hourName` performs time/date formatting. Native costs are larger than these located functions. `(idle)` and `(program)` are excluded from app ranking.

| Native motion self time (no JS file location) | S5a | S5c |
|---|---:|---:|
| getBoundingClientRect | 1431.277 | 3419.416 |
| get clientWidth | 1213.482 | 2251.171 |
| set innerHTML | 368.916 | 517.537 |
| get scrollWidth | 292.592 | 437.803 |
| requestAnimationFrame | 139.853 | 241.738 |
| replaceWith | 96.571 | 133.112 |
| remove | 53.111 | 138.359 |
| append | 28.284 | 58.362 |
| (garbage collector) | 52.259 | 67.637 |

Forced synchronous Layout and style events by initiating getter (ms includes those two browser events, not the entire caller):

| Phase/source | S5a Layout/style | S5a ms | S5c Layout/style | S5c ms |
|---|---:|---:|---:|---:|
| motion `app.js:810:120` | 52/70 | 359.092 | 100/127 | 738.992 |
| motion `boards.js:169:44` | 83/92 | 432.659 | 182/204 | 1076.880 |
| motion `boards.js:172:35` | 97/97 | 565.057 | 218/218 | 1393.249 |
| motion `boards.js:191:91` | 41/41 | 352.976 | 61/65 | 570.028 |
| motion `boards.js:194:158` | 52/52 | 48.868 | 100/100 | 104.734 |
| motion `boards.js:287:52` | 18/18 | 268.317 | 27/27 | 415.899 |
| motion `boards.js:324:39` | 18/18 | 296.774 | 27/27 | 440.659 |
| motion `boards.js:404:85` | 72/72 | 407.876 | 157/157 | 880.780 |
| motion `charts.js:71:18` | 218/264 | 506.964 | 255/317 | 832.836 |
| release `app.js:810:120` | 2/3 | 13.383 | 2/3 | 13.620 |
| release `boards.js:169:44` | 1/1 | 0.920 | 3/4 | 16.730 |
| release `boards.js:172:35` | 2/2 | 28.186 | 4/4 | 7.534 |
| release `boards.js:191:91` | 3/3 | 28.167 | 3/3 | 27.937 |
| release `boards.js:194:158` | 2/2 | 1.283 | 2/2 | 1.607 |
| release `boards.js:287:52` | 1/1 | 12.295 | 1/1 | 13.238 |
| release `boards.js:324:39` | 1/1 | 13.073 | 1/1 | 14.854 |
| release `boards.js:638:85` | 1/1 | 1.548 | 1/1 | 1.245 |
| release `boards.js:99:97` | 0/1 | 1.250 | 0/1 | 1.785 |
| release `charts.js:71:18` | 4/5 | 7.053 | 4/5 | 7.657 |

Chart activity by phase; all IDs belong to the synthetic fixture:

| Scenario/phase | SVG by widget | Chart RO by widget | Chart RO total |
|---|---|---|---:|
| S5a motion | `{'perf-00': 239, 'perf-01': 21, 'perf-02': 19, 'perf-03': 5, 'perf-05': 5, 'perf-06': 5}` | `{'perf-00': 240, 'perf-01': 39, 'perf-02': 32, 'perf-13': 5}` | 321 |
| S5a release | `{'perf-00': 6}` | `{'perf-00': 10}` | 10 |
| S5c motion | `{'perf-00': 239, 'perf-01': 59, 'perf-02': 38, 'perf-03': 11, 'perf-05': 11, 'perf-06': 11}` | `{'perf-00': 240, 'perf-01': 108, 'perf-02': 64, 'perf-13': 11}` | 434 |
| S5c release | `{'perf-00': 6}` | `{'perf-00': 10}` | 10 |

S5a motion: target 239 SVG insertions, neighbours 55; S5c: target 239, neighbours 130. Both release phases insert 6 target SVGs and 0 neighbour SVGs. RO totals additionally retain unmapped/detached callbacks; a callback is not proof of a draw. S5a motion has 5 unmapped callbacks and S5c 11.

Private function-entry counts from diagnostic coverage (whole collection interval, including early returns/setup and short collection overhead; use the phase probe/packing metrics above for exact phase counts):

| Function | File:line | S5a calls | S5c calls |
|---|---|---:|---:|
| measureNatural | `dashboard/static/boards.js:165` | 107 | 234 |
| queueNatural | `dashboard/static/boards.js:175` | 1314 | 1977 |
| fitAccounts | `dashboard/static/boards.js:186` | 45 | 69 |
| applyPacking | `dashboard/static/boards.js:205` | 21 | 30 |
| render | `dashboard/static/boards.js:274` | 19 | 29 |
| holdNeighbours | `dashboard/static/boards.js:388` | 19 | 28 |
| tick | `dashboard/static/boards.js:413` | 1239 | 3348 |
| resizeReadout | `dashboard/static/boards.js:590` | 19 | 28 |
| updateResize | `dashboard/static/boards.js:598` | 241 | 241 |
| tick | `dashboard/static/boards.js:642` | 12 | 12 |
| size | `dashboard/static/charts.js:69` | 310 | 397 |
| drawTime | `dashboard/static/charts.js:102` | 295 | 364 |

### Resize diagnosis ranked by evidence

1. **Natural probing is a major resize-motion cost, not only a drop cost.** `boards.js:169/172` causes 180 Layout +189 style events /997.716ms in S5a motion; 400 Layout +422 style /2470.129ms in S5c. These are 24.50% and 31.25% of motion layout+style respectively. The target is guarded while resizing; neighbour content and landing work still generates 97/218 probes. Confidence: high for attribution; cache savings need a measured implementation.

2. **Every integer size transition runs broad board reconciliation.** `boards.js:603` invokes `render(true)`; motion packs are 18/27, full render entries over collection are 19/29. `render` at 274 has 273.238/425.160ms located motion self time; its inventory-height read at 287 and tab scroll-width read at 324 force 565.091/856.558ms combined layout/style. `context` and repeated chart/date-label work add to this path. Confidence: high; the cost of skipping any specific render subset is not isolated.

3. **Continuous SVG replacement is real during resize, including reflow neighbours.** `charts.js:55–60` responds to changing live pixel dimensions, and `213–215` parses an SVG with template.innerHTML then replaces it. Target motion insertions are 239 in both traces; neighbours add 55/130. `charts.js:71` contributes 218 Layout +264 style /506.964ms and 255 Layout +317 style /832.836ms. Native innerHTML self time is 368.916/517.537ms (all callers, not exclusively charts). S5d still costs 6201.200ms median motion work with zero target SVG/RO callbacks and 103 neighbour SVG insertions, so target redraw alone cannot explain resize cost. Confidence: high for observed redraws, medium for isolated redraw savings.

4. **Account fit/header geometry and neighbour landings flush layout repeatedly.** `boards.js:191/194` plus `app.js:810` account for 760.936/1413.754ms forced layout/style during motion. Neighbour landing client-size reads at `boards.js:404` add 407.876/880.780ms. Coverage records 1239/3348 neighbour tween tick entries; holding/retargeting and 270ms size tweens propagate size changes to observers. Confidence: high for source costs; independent causal savings require separating fitting from tween/content changes.

5. **Release and periodic refresh can amplify natural measurement tails.** The separate diagnostic release is 491.2/573.7ms with 251.607/253.333ms work, but scored height releases reach 1209.4ms when polling lands during release, with 33 probes. `boards.js:633–645` also flushes the final update and rerenders at landing. Preserve the actual tween and persistence; do not optimize only motion while hiding this tail. Confidence: high for traces/counts; poll causality remains observational.

### Full per-arm resize metric spread

| Metric | A | B | A range | B range | Classification |
|---|---:|---:|---:|---:|---|
| S5a.frames.median | 16.700 [16.700, 16.700] | 16.700 [16.700, 16.700] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.frames.p95 | 83.400 [83.300, 99.900] | 83.300 [83.200, 100.000] | 16.600 | 16.800 | variable |
| S5a.frames.max | 283.400 [233.400, 333.300] | 283.200 [250.000, 283.400] | 99.900 | 33.400 | variable |
| S5a.frameIntervalsOver16_7 | 131.000 [127.000, 144.000] | 134.000 [126.000, 143.000] | 17.000 | 17.000 | variable |
| S5a.frameIntervalsOver33 | 56.000 [48.000, 58.000] | 57.000 [54.000, 59.000] | 10.000 | 5.000 | variable |
| S5a.releaseToSettledMs | 509.400 [507.100, 512.900] | 574.000 [510.700, 693.700] | 5.800 | 183.000 | variable |
| S5a.motion.durationMs | 9863.609 [9458.502, 10281.154] | 9919.482 [9401.544, 10099.646] | 822.652 | 698.102 | variable |
| S5a.motion.workMs | 6801.519 [6638.698, 7199.868] | 6932.985 [6533.879, 7108.404] | 561.170 | 574.525 | variable |
| S5a.motion.longTasks | 30.000 [29.000, 31.000] | 29.000 [29.000, 30.000] | 2.000 | 1.000 | variable |
| S5a.motion.longTaskTotalMs | 3930.100 [3842.200, 4197.400] | 3948.500 [3837.200, 4132.200] | 355.200 | 295.000 | variable |
| S5a.motion.mainThreadMs.script | 1768.806 [1711.074, 1878.393] | 1804.881 [1680.370, 1859.607] | 167.319 | 179.237 | variable |
| S5a.motion.mainThreadMs.layout | 1379.002 [1346.997, 1490.776] | 1397.579 [1315.236, 1479.158] | 143.779 | 163.922 | variable |
| S5a.motion.mainThreadMs.recalcStyle | 2710.794 [2659.671, 2875.053] | 2763.412 [2627.105, 2800.815] | 215.382 | 173.710 | variable |
| S5a.motion.mainThreadMs.paint | 961.976 [920.956, 1003.217] | 962.296 [911.168, 968.824] | 82.261 | 57.656 | variable |
| S5a.motion.forcedLayouts | 700.000 [679.000, 713.000] | 693.000 [686.000, 706.000] | 34.000 | 20.000 | near-deterministic (range <=5%) |
| S5a.motion.forcedStyleRecalcs | 782.000 [769.000, 786.000] | 771.000 [762.000, 777.000] | 17.000 | 15.000 | near-deterministic (range <=5%) |
| S5a.motion.chartSVGReplacements | 296.000 [291.000, 298.000] | 295.000 [293.000, 297.000] | 7.000 | 4.000 | near-deterministic (range <=5%) |
| S5a.motion.resizedWidgetSVG | 239.000 [239.000, 240.000] | 239.000 [239.000, 240.000] | 1.000 | 1.000 | near-deterministic (range <=5%) |
| S5a.motion.neighbourSVG | 56.000 [52.000, 59.000] | 56.000 [54.000, 57.000] | 7.000 | 3.000 | variable |
| S5a.motion.chartResizeCallbacks | 319.000 [314.000, 323.000] | 318.000 [314.000, 321.000] | 9.000 | 7.000 | near-deterministic (range <=5%) |
| S5a.motion.resizedWidgetResizeCallbacks | 240.000 [240.000, 240.000] | 240.000 [240.000, 240.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.motion.neighbourResizeCallbacks | 74.000 [69.000, 78.000] | 73.000 [69.000, 76.000] | 9.000 | 7.000 | variable |
| S5a.motion.boardResizeCallbacks | 900.000 [883.000, 917.000] | 893.000 [877.000, 900.000] | 34.000 | 23.000 | near-deterministic (range <=5%) |
| S5a.motion.naturalProbes | 107.000 [100.000, 110.000] | 102.000 [97.000, 106.000] | 10.000 | 9.000 | variable |
| S5a.motion.applyPacking | 18.000 [18.000, 18.000] | 18.000 [18.000, 18.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.motion.resizeRAF.count | 240.000 [240.000, 240.000] | 240.000 [240.000, 240.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.motion.resizeRAF.p95 | 70.600 [69.700, 72.600] | 71.100 [68.500, 74.000] | 2.900 | 5.500 | variable |
| S5a.release.durationMs | 511.413 [508.940, 515.403] | 575.392 [512.402, 695.944] | 6.463 | 183.542 | variable |
| S5a.release.workMs | 260.828 [244.119, 302.328] | 297.799 [269.968, 345.680] | 58.209 | 75.712 | variable |
| S5a.release.longTasks | 1.000 [1.000, 1.000] | 1.000 [1.000, 2.000] | 0.000 | 1.000 | variable |
| S5a.release.longTaskTotalMs | 124.800 [115.900, 133.800] | 122.800 [118.900, 180.800] | 17.900 | 61.900 | variable |
| S5a.release.mainThreadMs.script | 59.497 [57.431, 70.731] | 67.097 [57.875, 71.916] | 13.300 | 14.041 | variable |
| S5a.release.mainThreadMs.layout | 36.951 [33.909, 49.160] | 45.582 [38.093, 54.097] | 15.251 | 16.004 | variable |
| S5a.release.mainThreadMs.recalcStyle | 139.225 [124.050, 153.047] | 147.304 [139.743, 180.657] | 28.997 | 40.914 | variable |
| S5a.release.mainThreadMs.paint | 27.582 [24.533, 31.508] | 37.816 [28.123, 39.010] | 6.975 | 10.887 | variable |
| S5a.release.forcedLayouts | 18.000 [17.000, 26.000] | 25.000 [25.000, 26.000] | 9.000 | 1.000 | variable |
| S5a.release.forcedStyleRecalcs | 21.000 [21.000, 29.000] | 29.000 [28.000, 29.000] | 8.000 | 1.000 | variable |
| S5a.release.chartSVGReplacements | 6.000 [5.000, 6.000] | 6.000 [5.000, 6.000] | 1.000 | 1.000 | variable |
| S5a.release.resizedWidgetSVG | 6.000 [5.000, 6.000] | 6.000 [5.000, 6.000] | 1.000 | 1.000 | variable |
| S5a.release.neighbourSVG | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.release.chartResizeCallbacks | 9.000 [8.000, 11.000] | 9.000 [9.000, 10.000] | 3.000 | 1.000 | variable |
| S5a.release.resizedWidgetResizeCallbacks | 9.000 [8.000, 11.000] | 9.000 [9.000, 10.000] | 3.000 | 1.000 | variable |
| S5a.release.neighbourResizeCallbacks | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.release.boardResizeCallbacks | 20.000 [20.000, 23.000] | 22.000 [21.000, 24.000] | 3.000 | 3.000 | variable |
| S5a.release.naturalProbes | 2.000 [2.000, 6.000] | 6.000 [6.000, 6.000] | 4.000 | 0.000 | variable |
| S5a.release.applyPacking | 2.000 [2.000, 2.000] | 2.000 [2.000, 2.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.release.resizeRAF.count | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5a.release.resizeRAF.p95 | n/a [n/a, n/a] | n/a [n/a, n/a] | 0.000 | 0.000 | not available |
| S5b.frames.median | 16.700 [16.700, 16.700] | 16.700 [16.700, 16.700] | 0.000 | 0.000 | deterministic in observed runs |
| S5b.frames.p95 | 200.000 [200.000, 216.700] | 200.000 [200.000, 216.600] | 16.700 | 16.600 | variable |
| S5b.frames.max | 283.300 [249.900, 299.900] | 266.700 [250.100, 283.400] | 50.000 | 33.300 | variable |
| S5b.frameIntervalsOver16_7 | 158.000 [152.000, 168.000] | 156.000 [150.000, 185.000] | 16.000 | 35.000 | variable |
| S5b.frameIntervalsOver33 | 63.000 [60.000, 68.000] | 59.000 [58.000, 63.000] | 8.000 | 5.000 | variable |
| S5b.releaseToSettledMs | 559.400 [459.000, 1209.400] | 459.200 [457.700, 1058.900] | 750.400 | 601.200 | variable |
| S5b.motion.durationMs | 12128.841 [11831.714, 13282.076] | 11896.952 [11844.746, 13033.544] | 1450.362 | 1188.798 | variable |
| S5b.motion.workMs | 8970.104 [8780.032, 9961.201] | 8794.539 [8689.492, 9731.526] | 1181.169 | 1042.034 | variable |
| S5b.motion.longTasks | 35.000 [34.000, 37.000] | 34.000 [34.000, 37.000] | 3.000 | 3.000 | variable |
| S5b.motion.longTaskTotalMs | 5959.100 [5878.500, 6647.100] | 5847.400 [5757.100, 6549.900] | 768.600 | 792.800 | variable |
| S5b.motion.mainThreadMs.script | 2383.612 [2309.100, 2609.508] | 2327.888 [2291.347, 2505.248] | 300.408 | 213.901 | variable |
| S5b.motion.mainThreadMs.layout | 1660.996 [1642.180, 1865.907] | 1668.173 [1630.529, 1828.467] | 223.727 | 197.938 | variable |
| S5b.motion.mainThreadMs.recalcStyle | 3973.895 [3874.214, 4462.592] | 3900.538 [3849.182, 4397.714] | 588.378 | 548.532 | variable |
| S5b.motion.mainThreadMs.paint | 951.601 [917.063, 1023.194] | 932.446 [911.385, 1000.097] | 106.131 | 88.712 | variable |
| S5b.motion.forcedLayouts | 948.000 [923.000, 977.000] | 955.000 [919.000, 981.000] | 54.000 | 62.000 | variable |
| S5b.motion.forcedStyleRecalcs | 1126.000 [1116.000, 1166.000] | 1134.000 [1108.000, 1186.000] | 50.000 | 78.000 | variable |
| S5b.motion.chartSVGReplacements | 438.000 [432.000, 440.000] | 438.000 [436.000, 440.000] | 8.000 | 4.000 | near-deterministic (range <=5%) |
| S5b.motion.resizedWidgetSVG | 239.000 [239.000, 239.000] | 239.000 [239.000, 239.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5b.motion.neighbourSVG | 199.000 [193.000, 201.000] | 199.000 [197.000, 201.000] | 8.000 | 4.000 | near-deterministic (range <=5%) |
| S5b.motion.chartResizeCallbacks | 576.000 [564.000, 588.000] | 582.000 [572.000, 590.000] | 24.000 | 18.000 | near-deterministic (range <=5%) |
| S5b.motion.resizedWidgetResizeCallbacks | 240.000 [240.000, 240.000] | 240.000 [240.000, 240.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5b.motion.neighbourResizeCallbacks | 325.000 [313.000, 337.000] | 331.000 [321.000, 339.000] | 24.000 | 18.000 | variable |
| S5b.motion.boardResizeCallbacks | 1316.000 [1312.000, 1343.000] | 1336.000 [1296.000, 1343.000] | 31.000 | 47.000 | near-deterministic (range <=5%) |
| S5b.motion.naturalProbes | 189.000 [185.000, 205.000] | 194.000 [182.000, 214.000] | 20.000 | 32.000 | variable |
| S5b.motion.applyPacking | 11.000 [11.000, 11.000] | 11.000 [11.000, 11.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5b.motion.resizeRAF.count | 240.000 [240.000, 240.000] | 240.000 [240.000, 240.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5b.motion.resizeRAF.p95 | 0.800 [0.800, 0.800] | 0.700 [0.700, 0.800] | 0.000 | 0.100 | variable |
| S5b.release.durationMs | 560.896 [460.757, 1210.853] | 460.098 [459.257, 1060.697] | 750.096 | 601.440 | variable |
| S5b.release.workMs | 239.876 [210.432, 765.008] | 206.666 [192.821, 751.111] | 554.576 | 558.290 | variable |
| S5b.release.longTasks | 1.000 [1.000, 4.000] | 1.000 [1.000, 4.000] | 3.000 | 3.000 | variable |
| S5b.release.longTaskTotalMs | 107.200 [100.500, 792.900] | 102.900 [100.500, 830.900] | 692.400 | 730.400 | variable |
| S5b.release.mainThreadMs.script | 53.630 [52.820, 91.626] | 61.706 [53.564, 81.936] | 38.806 | 28.372 | variable |
| S5b.release.mainThreadMs.layout | 41.419 [30.336, 143.303] | 29.160 [28.038, 144.038] | 112.967 | 116.000 | variable |
| S5b.release.mainThreadMs.recalcStyle | 111.659 [99.710, 486.622] | 95.134 [85.422, 499.676] | 386.912 | 414.254 | variable |
| S5b.release.mainThreadMs.paint | 30.696 [27.566, 43.457] | 26.569 [24.675, 37.347] | 15.891 | 12.672 | variable |
| S5b.release.forcedLayouts | 24.000 [17.000, 90.000] | 17.000 [17.000, 86.000] | 73.000 | 69.000 | variable |
| S5b.release.forcedStyleRecalcs | 26.000 [19.000, 96.000] | 19.000 [19.000, 89.000] | 77.000 | 70.000 | variable |
| S5b.release.chartSVGReplacements | 7.000 [6.000, 13.000] | 7.000 [7.000, 9.000] | 7.000 | 2.000 | variable |
| S5b.release.resizedWidgetSVG | 6.000 [5.000, 8.000] | 7.000 [4.000, 7.000] | 3.000 | 3.000 | variable |
| S5b.release.neighbourSVG | 0.000 [0.000, 5.000] | 0.000 [0.000, 5.000] | 5.000 | 5.000 | variable |
| S5b.release.chartResizeCallbacks | 10.000 [9.000, 13.000] | 11.000 [7.000, 11.000] | 4.000 | 4.000 | variable |
| S5b.release.resizedWidgetResizeCallbacks | 10.000 [7.000, 11.000] | 11.000 [5.000, 11.000] | 4.000 | 6.000 | variable |
| S5b.release.neighbourResizeCallbacks | 0.000 [0.000, 1.000] | 0.000 [0.000, 1.000] | 1.000 | 1.000 | variable |
| S5b.release.boardResizeCallbacks | 24.000 [22.000, 42.000] | 24.000 [24.000, 28.000] | 20.000 | 4.000 | variable |
| S5b.release.naturalProbes | 4.000 [0.000, 33.000] | 0.000 [0.000, 31.000] | 33.000 | 31.000 | variable |
| S5b.release.applyPacking | 2.000 [2.000, 3.000] | 2.000 [2.000, 3.000] | 1.000 | 1.000 | variable |
| S5b.release.resizeRAF.count | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5b.release.resizeRAF.p95 | n/a [n/a, n/a] | n/a [n/a, n/a] | 0.000 | 0.000 | not available |
| S5c.frames.median | 16.700 [16.700, 16.700] | 16.700 [16.700, 16.700] | 0.000 | 0.000 | near-deterministic (range <=5%) |
| S5c.frames.p95 | 133.400 [133.300, 183.400] | 133.300 [116.700, 183.300] | 50.100 | 66.600 | variable |
| S5c.frames.max | 333.300 [300.000, 366.700] | 316.700 [283.300, 333.300] | 66.700 | 50.000 | variable |
| S5c.frameIntervalsOver16_7 | 213.000 [207.000, 231.000] | 218.000 [202.000, 222.000] | 24.000 | 20.000 | variable |
| S5c.frameIntervalsOver33 | 108.000 [100.000, 118.000] | 106.000 [94.000, 117.000] | 18.000 | 23.000 | variable |
| S5c.releaseToSettledMs | 489.500 [474.600, 491.300] | 489.200 [474.600, 543.000] | 16.700 | 68.400 | variable |
| S5c.motion.durationMs | 15336.181 [14406.889, 15665.808] | 14739.844 [14565.171, 15500.020] | 1258.919 | 934.849 | variable |
| S5c.motion.workMs | 11637.623 [10871.066, 11855.279] | 10951.871 [10785.143, 11706.783] | 984.213 | 921.640 | variable |
| S5c.motion.longTasks | 49.000 [46.000, 50.000] | 50.000 [47.000, 50.000] | 4.000 | 3.000 | variable |
| S5c.motion.longTaskTotalMs | 7633.100 [7165.100, 7918.100] | 7172.100 [6834.400, 7821.000] | 753.000 | 986.600 | variable |
| S5c.motion.mainThreadMs.script | 2843.700 [2537.682, 2890.447] | 2630.376 [2555.171, 2783.511] | 352.765 | 228.340 | variable |
| S5c.motion.mainThreadMs.layout | 2302.854 [2209.795, 2352.091] | 2243.698 [2194.317, 2402.229] | 142.296 | 207.912 | variable |
| S5c.motion.mainThreadMs.recalcStyle | 5091.844 [4794.214, 5329.107] | 4742.070 [4672.912, 5216.772] | 534.893 | 543.860 | variable |
| S5c.motion.mainThreadMs.paint | 1347.853 [1299.323, 1394.191] | 1335.976 [1293.585, 1404.885] | 94.868 | 111.300 | variable |
| S5c.motion.forcedLayouts | 1181.000 [1123.000, 1224.000] | 1151.000 [1079.000, 1219.000] | 101.000 | 140.000 | variable |
| S5c.motion.forcedStyleRecalcs | 1276.000 [1228.000, 1347.000] | 1254.000 [1193.000, 1320.000] | 119.000 | 127.000 | variable |
| S5c.motion.chartSVGReplacements | 375.000 [368.000, 383.000] | 369.000 [364.000, 375.000] | 15.000 | 11.000 | near-deterministic (range <=5%) |
| S5c.motion.resizedWidgetSVG | 239.000 [239.000, 239.000] | 239.000 [239.000, 239.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.motion.neighbourSVG | 136.000 [129.000, 144.000] | 130.000 [125.000, 136.000] | 15.000 | 11.000 | variable |
| S5c.motion.chartResizeCallbacks | 486.000 [474.000, 513.000] | 500.000 [471.000, 518.000] | 39.000 | 47.000 | variable |
| S5c.motion.resizedWidgetResizeCallbacks | 240.000 [240.000, 240.000] | 240.000 [240.000, 240.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.motion.neighbourResizeCallbacks | 236.000 [225.000, 263.000] | 251.000 [222.000, 268.000] | 38.000 | 46.000 | variable |
| S5c.motion.boardResizeCallbacks | 1356.000 [1298.000, 1436.000] | 1369.000 [1286.000, 1457.000] | 138.000 | 171.000 | variable |
| S5c.motion.naturalProbes | 189.000 [174.000, 208.000] | 181.000 [161.000, 198.000] | 34.000 | 37.000 | variable |
| S5c.motion.applyPacking | 27.000 [27.000, 27.000] | 27.000 [27.000, 27.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.motion.resizeRAF.count | 240.000 [240.000, 240.000] | 240.000 [240.000, 240.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.motion.resizeRAF.p95 | 88.800 [86.800, 94.000] | 90.700 [89.900, 92.000] | 7.200 | 2.100 | variable |
| S5c.release.durationMs | 492.222 [476.585, 492.691] | 491.215 [476.817, 544.425] | 16.106 | 67.608 | variable |
| S5c.release.workMs | 221.478 [202.343, 257.606] | 222.868 [197.163, 240.584] | 55.263 | 43.421 | variable |
| S5c.release.longTasks | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.release.longTaskTotalMs | 110.900 [103.900, 113.900] | 110.000 [104.800, 120.600] | 10.000 | 15.800 | variable |
| S5c.release.mainThreadMs.script | 54.683 [46.069, 60.409] | 49.339 [45.006, 54.977] | 14.340 | 9.971 | variable |
| S5c.release.mainThreadMs.layout | 32.943 [29.474, 43.037] | 33.169 [27.121, 39.905] | 13.563 | 12.784 | variable |
| S5c.release.mainThreadMs.recalcStyle | 101.508 [100.049, 123.184] | 106.424 [94.930, 114.046] | 23.135 | 19.116 | variable |
| S5c.release.mainThreadMs.paint | 29.202 [26.751, 35.149] | 27.170 [26.017, 32.821] | 8.398 | 6.804 | variable |
| S5c.release.forcedLayouts | 16.000 [16.000, 23.000] | 17.000 [15.000, 24.000] | 7.000 | 9.000 | variable |
| S5c.release.forcedStyleRecalcs | 19.000 [19.000, 26.000] | 20.000 [18.000, 27.000] | 7.000 | 9.000 | variable |
| S5c.release.chartSVGReplacements | 6.000 [5.000, 6.000] | 6.000 [5.000, 7.000] | 1.000 | 2.000 | variable |
| S5c.release.resizedWidgetSVG | 6.000 [5.000, 6.000] | 6.000 [5.000, 7.000] | 1.000 | 2.000 | variable |
| S5c.release.neighbourSVG | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.release.chartResizeCallbacks | 9.000 [7.000, 10.000] | 9.000 [7.000, 11.000] | 3.000 | 4.000 | variable |
| S5c.release.resizedWidgetResizeCallbacks | 9.000 [7.000, 10.000] | 9.000 [7.000, 11.000] | 3.000 | 4.000 | variable |
| S5c.release.neighbourResizeCallbacks | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.release.boardResizeCallbacks | 20.000 [18.000, 22.000] | 22.000 [16.000, 24.000] | 4.000 | 8.000 | variable |
| S5c.release.naturalProbes | 0.000 [0.000, 4.000] | 0.000 [0.000, 4.000] | 4.000 | 4.000 | variable |
| S5c.release.applyPacking | 2.000 [2.000, 2.000] | 2.000 [2.000, 2.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.release.resizeRAF.count | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5c.release.resizeRAF.p95 | n/a [n/a, n/a] | n/a [n/a, n/a] | 0.000 | 0.000 | not available |
| S5d.frames.median | 16.700 [16.700, 16.700] | 16.700 [16.700, 16.700] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.frames.p95 | 66.700 [66.700, 83.400] | 66.700 [66.700, 83.300] | 16.700 | 16.600 | variable |
| S5d.frames.max | 333.300 [266.700, 333.400] | 283.300 [250.100, 300.000] | 66.700 | 49.900 | variable |
| S5d.frameIntervalsOver16_7 | 137.000 [132.000, 150.000] | 127.000 [114.000, 145.000] | 18.000 | 31.000 | variable |
| S5d.frameIntervalsOver33 | 56.000 [53.000, 65.000] | 51.000 [48.000, 63.000] | 12.000 | 15.000 | variable |
| S5d.releaseToSettledMs | 478.900 [473.300, 694.700] | 477.100 [459.400, 478.600] | 221.400 | 19.200 | variable |
| S5d.motion.durationMs | 9919.447 [9594.170, 10478.034] | 9581.474 [9255.233, 9803.540] | 883.864 | 548.307 | variable |
| S5d.motion.workMs | 6429.990 [6195.706, 6946.486] | 6169.868 [5959.956, 6299.283] | 750.780 | 339.327 | variable |
| S5d.motion.longTasks | 29.000 [28.000, 30.000] | 29.000 [29.000, 29.000] | 2.000 | 0.000 | variable |
| S5d.motion.longTaskTotalMs | 3982.400 [3929.300, 4362.000] | 3958.200 [3829.200, 4013.000] | 432.700 | 183.800 | variable |
| S5d.motion.mainThreadMs.script | 1430.654 [1378.231, 1565.317] | 1356.083 [1339.899, 1407.823] | 187.086 | 67.924 | variable |
| S5d.motion.mainThreadMs.layout | 1062.928 [1054.473, 1182.502] | 1060.521 [1014.675, 1072.747] | 128.029 | 58.072 | variable |
| S5d.motion.mainThreadMs.recalcStyle | 3047.563 [2875.190, 3272.402] | 2887.090 [2763.821, 2964.718] | 397.212 | 200.897 | variable |
| S5d.motion.mainThreadMs.paint | 888.845 [857.980, 955.687] | 845.973 [828.260, 866.174] | 97.707 | 37.914 | variable |
| S5d.motion.forcedLayouts | 525.000 [515.000, 537.000] | 525.000 [518.000, 527.000] | 22.000 | 9.000 | near-deterministic (range <=5%) |
| S5d.motion.forcedStyleRecalcs | 556.000 [549.000, 574.000] | 556.000 [545.000, 558.000] | 25.000 | 13.000 | near-deterministic (range <=5%) |
| S5d.motion.chartSVGReplacements | 104.000 [95.000, 107.000] | 103.000 [96.000, 103.000] | 12.000 | 7.000 | variable |
| S5d.motion.resizedWidgetSVG | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.motion.neighbourSVG | 104.000 [95.000, 107.000] | 103.000 [96.000, 103.000] | 12.000 | 7.000 | variable |
| S5d.motion.chartResizeCallbacks | 170.000 [152.000, 176.000] | 166.000 [150.000, 172.000] | 24.000 | 22.000 | variable |
| S5d.motion.resizedWidgetResizeCallbacks | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.motion.neighbourResizeCallbacks | 165.000 [147.000, 171.000] | 161.000 [145.000, 167.000] | 24.000 | 22.000 | variable |
| S5d.motion.boardResizeCallbacks | 953.000 [904.000, 968.000] | 940.000 [904.000, 957.000] | 64.000 | 53.000 | variable |
| S5d.motion.naturalProbes | 91.000 [88.000, 99.000] | 90.000 [88.000, 92.000] | 11.000 | 4.000 | variable |
| S5d.motion.applyPacking | 18.000 [18.000, 18.000] | 18.000 [18.000, 18.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.motion.resizeRAF.count | 240.000 [240.000, 240.000] | 240.000 [240.000, 240.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.motion.resizeRAF.p95 | 69.800 [67.500, 73.500] | 68.000 [66.300, 69.500] | 6.000 | 3.200 | variable |
| S5d.release.durationMs | 480.363 [476.246, 697.415] | 478.144 [460.999, 480.569] | 221.169 | 19.570 | variable |
| S5d.release.workMs | 193.463 [164.926, 243.738] | 179.406 [163.510, 224.148] | 78.812 | 60.638 | variable |
| S5d.release.longTasks | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.longTaskTotalMs | 107.900 [96.700, 118.600] | 102.800 [101.800, 128.100] | 21.900 | 26.300 | variable |
| S5d.release.mainThreadMs.script | 43.113 [38.228, 50.709] | 39.197 [34.994, 52.009] | 12.481 | 17.015 | variable |
| S5d.release.mainThreadMs.layout | 33.554 [29.493, 47.223] | 31.679 [27.749, 43.637] | 17.730 | 15.888 | variable |
| S5d.release.mainThreadMs.recalcStyle | 82.584 [68.430, 108.536] | 76.759 [70.475, 95.608] | 40.106 | 25.133 | variable |
| S5d.release.mainThreadMs.paint | 34.212 [28.775, 37.270] | 27.923 [26.663, 36.231] | 8.495 | 9.568 | variable |
| S5d.release.forcedLayouts | 12.000 [12.000, 18.000] | 12.000 [12.000, 18.000] | 6.000 | 6.000 | variable |
| S5d.release.forcedStyleRecalcs | 16.000 [16.000, 24.000] | 16.000 [16.000, 20.000] | 8.000 | 4.000 | variable |
| S5d.release.chartSVGReplacements | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.resizedWidgetSVG | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.neighbourSVG | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.chartResizeCallbacks | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.resizedWidgetResizeCallbacks | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.neighbourResizeCallbacks | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.boardResizeCallbacks | 23.000 [20.000, 25.000] | 23.000 [18.000, 23.000] | 5.000 | 5.000 | variable |
| S5d.release.naturalProbes | 2.000 [2.000, 6.000] | 2.000 [2.000, 4.000] | 4.000 | 2.000 | variable |
| S5d.release.applyPacking | 2.000 [2.000, 2.000] | 2.000 [2.000, 2.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.resizeRAF.count | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 | 0.000 | deterministic in observed runs |
| S5d.release.resizeRAF.p95 | n/a [n/a, n/a] | n/a [n/a, n/a] | 0.000 | 0.000 | not available |


### Ranked resize fixes (diagnosis only; none implemented)

| Rank | Candidate and file:line | Expected impact | Risk to resize feel/correctness |
|---|---|---|---|
| 1 | Batch/cache natural heights by content generation, width and edit mode; `dashboard/static/boards.js:165–175`, `298–301` | Remove repeated full-subtree clone/append/geometry/remove work on neighbours and at landing. The source-attributed natural-probe budget below is an addressable upper bound, not promised savings. Helps drag drop and resize. | High: width/content/fonts/Customize/density changes must invalidate; stale minima can clip content or destabilize packing. Preserve async generations and observer convergence. |
| 2 | Add a resize-specific reconciliation path at integer size transitions; `dashboard/static/boards.js:603`, `274–317` | Avoid broad tabs/bar/inventory/content work on every row/column step; retain model, packing, strip size label, guides and neighbour retargeting. Can also reduce downstream chart/probe/account work. | High: keep precise free-box feedback, snap thresholds, bounds, row minima, focus, pending refreshes, Cancel and normal persistence. Do not remove live settle animations. |
| 3 | Keep chart SVG structure stable or use a lightweight live-size preview with an exact final draw; `dashboard/static/charts.js:55–60`, `106–220`, especially `213–215` | Target the near-per-input chart SVG replacement stream and redraws caused by neighbour size tweens. Chart observer already schedules one rAF; adding another generic throttle alone does not remove SVG construction/layout. | Medium/high: text/axes/tooltips/focus/accessibility must remain correct; preserve responsive visual feedback and redraw exact pixel geometry when settling finishes. Avoid a visibly frozen or blank chart. |
| 4 | Batch geometry around packing, account fit and neighbour landing; `dashboard/static/boards.js:191–202`, `384–414`, `633–645`; `dashboard/static/app.js:806–813` | Reduce read-after-write flushes in fit key/client-size reads, density trials, header offsets and landing reads. Cache geometry only while topology and content remain valid. | High: stale dimensions can change density, alignment, track height, neighbour trajectories and release landing. Reuse observer sizes where valid; retain all height/account/reorder gates. |
| 5 | Reuse readout/guide DOM and geometry; `dashboard/static/boards.js:562–569`, `590–596` | Smaller local opportunity: avoid repeated readout innerHTML, row-guide removal/recreation and redundant grid/slot reads at transitions. Rank below the broad render/probe/chart/account costs. | Medium: cell highlights, neighbour match lines, max reach and scroll offsets must keep tracking the live landing slot. |

These candidates overlap: removing a render may also remove probes or account
flushes. Do not sum the attributed budgets or claim them as independent savings.
Each candidate needs focused correctness checks, an A/A-calibrated comparison,
and confirmation that the actual motion/release metrics and visible feedback improve.

### Combined drag + resize ranking

1. **Natural-height batching/cache**, `boards.js:165–175`: common measured
   source during resize and drag drop; first shared target.
2. **Resize-specific reconciliation**, `boards.js:603`, `274–317`: larger
   resize-only opportunity than adding pointer rAF coalescing, which resize
   already has. The same unchanged-content discipline can help drop/Customize.
3. **Geometry batching with valid snapshots**, `boards.js:384–414`, `626–645`,
   `app.js:2576–2581`, plus account fit at `boards.js:191–202`: supports resize
   retarget/landing and drag slot changes; preserve glides and correct gaps.
4. **Chart/content reuse**, `charts.js:55–60`, `106–220`, `boards.js:311`:
   highest redraw exposure is resize; drop/Customize also replace charts.
5. **Drag follow rectangle/item caching**, `boards.js:608–628`: phase-1 motion
   style-flush opportunity; resize already uses a different rAF path. Keep this
   separate from resize-specific changes so attribution remains reviewable.

### Profile chronology correction and final validation

- Chromium emitted out-of-order CPU samples: S5a had 3009 negative time deltas
  totaling −1279.932ms; S5c had 4574 totaling −1952.964ms (S1: 604,
  −261.048ms). Weighting positive clipped intervals without restoring chronology
  counted overlapping time twice. The new regression failed before the fix:
  2.5ms self time in a 2ms window. `analyseProfile` now reconstructs absolute
  sample timestamps, sorts them, and weights non-overlapping intervals, with
  optional phase clipping. All 31 assertions pass after the correction.
- The raw `.cpuprofile` and `.trace.json.gz` files are unchanged. Only derived CPU
  self-time arrays in `results.json`, `diagnostic-1-A.json`, and S5a/S5c diagnostic
  JSON were recomputed offline. No scored timeline, frame, observer, probe,
  long-task or correctness value changed. S5a corrected motion self time sums
  to 9627.470ms, exactly its timeline motion window; S5c sums to 15964.346ms,
  exactly its motion window (within floating-point precision).
- Audit copies are retained as
  `/home/user/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/results.before-profile-chronology-fix.json`,
  `diagnostic-1-A.before-profile-chronology-fix.json`,
  `diagnostic-1-A-S5a.before-profile-chronology-fix.json`, and
  `diagnostic-1-A-S5c.before-profile-chronology-fix.json` in that directory.
  They contain superseded CPU self-time arrays; use corrected `results.json`
  and `resize-diagnosis.json`. `harness-sha256.json` fingerprints the measured
  inputs; `analysis-sha256.json` records the later analyzer/report/test hashes,
  correction statistics and post-run checks. The correction affects unscored
  sampling analysis only, so the five-pair A/A was not repeated.
- A/A correctness: **247/247 gates PASS** across 13 passes (10 scored, 2 warmups,
  1 diagnostic). S1 order/model/persistence/glides, S2 ready, S3 actual poll,
  console/HTTP, and all S5 dimensions/DOM/settle progression/persistence pass.
  Final chart spans/heights: S5a 6/Auto, S5b 4/9, S5c 6/9, S5d 6/Auto.
- `python3 -B tests/test_perf_board_fixture.py`: **7/7 PASS**, directly without
  heavy, including resettable health-first arrangement and existing isolation.
- `node tests/test_perf_board_trace.mjs`: **31 assertions PASS**, directly
  without heavy, including phase clipping, long tasks crossing pointerup,
  target/neighbour attribution, repeated SVG insertions, CPU phase clipping
  and negative-delta chronology. The original benchmark test record reports
  29 assertions because the last two were added after inspecting the profiles.
- `env PLAYWRIGHT_CHROMIUM=/home/user/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell ~/.local/bin/heavy node tests/test_reorder_ui.cjs`:
  **PASS** in the assigned worktree after measurement. Reorder, widgets 16/16
  and boards 65/65 also passed in both benchmark arms. No full-repository suite
  was needed for this harness-only task.
- Final A/A servers **8390/PID 2926304** and **8391/PID 2926305** stopped;
  detached `/home/user/.worktrees/personal-codex-router/perf-baseline-2923825`
  removed, registry pruned. `ss` confirms no listeners on 8390–8399. The
  benchmark browser closed; no task-owned persistent server or browser remains.
- Workspace remains on `perf/widget-board-hill-climb` at
  `eb0f6c67af5467b5aa3b0e5e73fccd6c4b7a5da2`, with the four inherited untracked
  items preserved and additions within `scripts/perf/`. `git diff --exit-code --
  dashboard/static` passes. No dashboard optimization, main-checkout edit,
  live service/database/credential access, commit, push, stash, reset, rebase,
  branch creation, subagent, orchestration or review loop was performed.
