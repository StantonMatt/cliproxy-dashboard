# Hill climb: widget board performance

Status: **climb finished 2026-11-03. Rounds 1–4 kept; rounds 5–7 reverted; stopped by the stop rule.** Measurement detail lives in
[perf-hill-climb-log.md](../perf-hill-climb-log.md).
Scratch area: `~/.cache/agent-scratch/personal-codex-router/hill-climb/widget-board-perf/`, holding round patches, JSON, snapshots and raw results.

## Contract

### Goal

User report: the board lags most when **resizing widgets**, then at drag pickup/drop. Motion while dragging is already about 60 fps. All numbers below are at 4× CDP CPU throttle, 1440×900, synthetic fixture.

| Goal metric (lower is better) | Baseline median | Goal |
| --- | ---: | ---: |
| Resize motion frame p95, S5a / S5b / S5c | 83.3 / 200.0 / 133.3 ms | ≤ 50 ms each, which is about 12.5 ms on the real machine |
| S1 drop-to-settled | ~780–880 ms | ≤ 400 ms |

### Scores

**Primary score:** the sum of the S5a–d motion `workMs` medians.
- Baseline is about 33,700 ms: S5a 6,802, S5b 8,811, S5c 11,046, S5d 6,201.
- A/A per-scenario arm range is 8–13%.

For a round aimed at drag or drop rather than resize, the primary score is S1 `dragWorkMs` + `release` work, and the S5 sum becomes a secondary metric.

**Minimum useful effect:** a 10% median reduction of the round's primary score.

**Mechanism indicators.** Each round names one in advance. Candidates, deterministic or near-deterministic in A/A:
- motion forced layouts and forced style recalcs
- chart SVG replacements
- natural probes
- chart ResizeObserver callbacks

### Secondary bounds

A keep requires all of these:
- S2 first full board, S3 poll refresh and S4 customize toggle: median no more than 15% worse than A. This is about their A/A range.
- S1 motion frame p95 no worse than A by more than one frame (16.7 ms).
- No goal metric worse than A beyond its A/A range.

### Gates

The harness has 247 gates and all must pass. They include:
- final order, span and height
- persistence
- no console errors
- slide and settle animations actually progress
- charts still render at their final size

Removing animations, chart content, timers, polling or deferred work to lower the score is a failure.

### Paths

**Editable:** `dashboard/static/boards.js`, `charts.js`, `widgets.js`, `boards.css` and `charts.css`. Board-related parts of `app.js` and `style.css` are editable only when the trace points there.

**Frozen:**
- `scripts/perf/*` and `tests/test_perf_board_*`
- the fixture `scripts/perf/fake-dashboard.py`
- `dashboard/server.py` and `dashboard/static/index.html`
- all existing tests
- fonts and assets

Harness SHA-256 values are recorded in `~/.cache/agent-scratch/personal-codex-router/perf/resize-aa-eb0f6c6/harness-sha256.json`.

**Never touched:** live services on 127.0.0.1 ports 8320, 8317 and 8400–8409, `/etc`, `/var/lib`, and systemd.

**Focused checks every round**, run directly:
- `node tests/test_reorder_ui.cjs`
- `python3 tests/test_widgets_ui.py`
- `python3 tests/test_boards_ui.py`
- `python3 tests/test_dashboard_layout.py`
- the two harness self-tests

### Measurement

**Baseline identity:** main `eb0f6c67af5467b5aa3b0e5e73fccd6c4b7a5da2`, no dashboard source changes. The inherited untracked files are harness and docs only.

**Environment:** i9-13900K, Chromium headless-shell 156.0.8078.4, through `PLAYWRIGHT_CHROMIUM`, run via `~/.local/bin/heavy`. This is a shared desktop; other tasks are not stopped.

**Command:**
```sh
env PLAYWRIGHT_CHROMIUM=/home/user/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell \
  ~/.local/bin/heavy node scripts/perf/bench-board.mjs --baseline <A snapshot> --candidate <B snapshot> \
  --iterations 5 --warmups 1 --output ~/.cache/agent-scratch/personal-codex-router/perf/hill-climb/rNN
# --output must be under ~/.cache/agent-scratch/personal-codex-router/perf/ (the harness rejects other paths).
# --baseline must be a git checkout (use a detached worktree of the base plus the last kept state patch).
```

**Calibration:** done. Two A/A runs were made (drag and resize), both with 5 scored runs and 1 warmup per arm, alternating. Results are in the log.

**Per-round A/B:** A is the last kept snapshot and B is the candidate. Each arm gets 5 scored runs plus 1 warmup, alternating.

**Keep rule, fixed before any candidate is scored.** Keep only if all of the following hold:
1. The mechanism indicator moved in the predicted direction.
2. The primary score's median fell by at least 10%, AND the ranges don't overlap (B max < A min).
3. All gates, secondary bounds and focused checks pass.

If the median gain is at least 10% but the ranges overlap, run **one** extra 5+5 batch and decide on all 10+10 runs: the combined median must still be at least 10% better, with B's median below A's minimum. No further re-rolls.

**Confirmation:**
- Every 3 kept rounds: the kept state vs the original baseline, 5+5.
- Final: 10 scored runs per arm plus 1 warmup, vs the original baseline, on all scenarios.

The method is ranges and medians; it is not a confidence interval.

### Budget and stop rule

- **Budget:** at most 8 rounds or about 10 hours of wall time, whichever comes first. About 1.5 hours is reserved for confirmation.
- **Stop when:**
  - both goals are met
  - 2 consecutive valid rounds stay under 10%
  - the budget runs out
  - a decision is needed, such as a frozen-path repair or a scope change

### Mode and handback

- **Mode:** performance, with no council.
- **Hand back:**
  - the score table and the confirmed delta
  - the user tries it on the synthetic board, served from the worktree on 127.0.0.1:8390
  - the user tries it on the real dashboard after deploy; this is outside this contract
  - `review-fix-loop` on the final diff
- **Publication:** a local commit on branch `perf/widget-board-hill-climb` containing this log, the harness and the source change. **No push or PR while GitHub is suspended.** Deploying to the live dashboard is a separate decision.

## Rounds

Round scores are exploratory until confirmed.

| Round | Hypothesis | Mechanism | A → B score | Spread, n | Guards/secondary | Keep/revert/invalid; why | Patch/evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Caching/batching natural-height probes and deferring them during drag/resize should cut resize motion work, because probes forced 25–31% of motion layout/style | natural probes S5a 111→0, S5c 195→0; forced layouts S5a 699→485, S5c 1184→808 | 35,812 → 27,823 ms (−22.3%) | A 32,373–36,507; B 26,748–29,481; no overlap; 5+5 | 247/247 gates pass. p95 S5a 100→83, S5b 217→83, S5c 133→117. S1 drop 798→681. S2/S3/S4 all faster. Release-to-settled worse (S5c 475→689) | **Keep**: met the rule. The release regression is the next target | scratch rounds/01.patch, 01.json; perf/hill-climb/r01 |
| 2 | Coalescing chart redraws to about 120 ms during an active resize (exact redraw after release) should cut motion work, because the resized chart was rebuilt on 239 of 240 frames | SVG replacements S5a 297→73, S5b 440→119, S5c 377→119; forced layouts S5a 484→295, S5c 821→569 | 27,611 → 24,831 ms (−10.07%) | A 25,815–29,783; B 20,777–25,565; no overlap; 5+5 | 247/247 gates pass. Frame p95 unchanged (83/83/117). Release and S1–S4 within bounds | **Keep**: just met the rule. p95 didn't move, so the stalls lie elsewhere | rounds/02.patch, 02.json; perf/hill-climb/r02 |
| 3 | Updating only the resized widget plus packing and glide at each step crossing, instead of a full `render(true)`, should cut the multi-frame stalls | forced layouts S5a 280→167, S5c 559→323; render no longer on the step path | 24,434 → 18,979 ms (−22.3%) | A 24,256–25,805; B 18,121–19,646; no overlap; 5+5 | 247/247 gates pass. **p95 S5a/b/c/d 83/83/117/83 → 33/33/50/33: resize goal met.** S1 drop 731→730 (goal 400 not met). S2 +4.9%, S4 +4.3%, within bounds | **Keep** | rounds/03.patch, 03.json; perf/hill-climb/r03 |

| C1 | **Confirmation after 3 kept rounds:** kept r03 vs original eb0f6c6, 5+5 | SVG S5a 296→69; forced layouts S5a 696→163, S5c 1148→306; probes 110→0 | 35,187 → 18,486 ms (**−47.5%**) | A 33,289–41,228; B 17,934–20,416; no overlap | 247/247 gates pass ×12. p95 S5a/b/c/d 100/217/133/83 → 33/33/50/33. S1 drop 781→647. S2 −12%, S3 −28%, S4 −25%. S5 release-to-settled +3–9% (e.g. S5a 512→541) | **Confirmed** | perf/hill-climb/c03 |

| 4 | On drop, reconciling only order, strip controls, packing and glide, instead of a full `render()` of every widget (re-render, re-fetch, chart rebuild, natural probes), should cut S1 drag+release work and drop-to-settled, because each post-drop mutation restarts the 150 ms quiet window. Primary for this round: S1 `dragWorkMs` + drop main-thread work | drop forced layouts 21–24 → 4–7; forced style 26–31 → 8–12; post-release SVG 6 → 0; ordinary drop does 0 `renderWidget` calls | 1,016 → 667 ms (−34.3%) | Batch 1 overlapped: one B run hit load avg ~15 and every scenario doubled. Extra batch run; 10+10 A 854–1,164, B 569–1,361; B median < A min | All scored-run gates pass. One unscored A-arm (r03) warmup S5a animation gate failed under load. **S1 drop-to-settled 730 → 358 ms: drop goal met.** S2 +1.2%, S3 +2.8%, S4 +3.1%. S1 p95 16.8 → 16.8. Resize code untouched | **Keep**. Both goals met, so stop and run the final confirm | rounds/04.patch, 04.json; perf/hill-climb/r04, r04b |

| F1 | **Final confirm attempt:** kept r04 vs original eb0f6c6, 10+10 | SVG S5a 297→70; forced layouts S5a 707→162; S1 drop 938→351 | 42,277 → 20,249 ms (−52.1%) | A 33,199–55,758; B 18,104–33,367; one B outlier overlaps A min | **Gates FAIL:** S5 settle-animation gate failed in 2 of 40 B scenario runs (S5b r6, S5d r5; 2 samples) and 1 of 40 A runs (under load). Cause: B release does 2 long tasks (~170–470 ms) vs A's 1 (~105–160 ms); S5c release probes 12 vs 2; release-to-settled S5d 483→547. Review H2: no gate checks final chart size | **Not confirmed**. Round 5 targets release jank. A chart final-size gate is being added | perf/hill-climb/final |

| — | **Harness change (review H2):** added `S5xChartsFinal` gates. After each resize settles, every chart host's SVG and HTML chart must match its host box within 2 px. Verified passing on original and r04, and failing on a deliberately broken redraw (367 px SVG in a 598 px host). This only tightens correctness; no metric changed. Harness hashes re-recorded | — | — | — | — | Gate added | gate-worker/gate.patch |
| 5 | Resize release without a full `render()`, with deferred natural probes drained after the settle tween, should smooth the tween and cut release work. Primary: sum of S5a–d release `workMs` | release long tasks median 2 → 3; animation samples 7 → 12.5; animation gate fails 5/20 → 0/20 | 1,823 → 1,981 ms (+8.7%) | Overlap. Load average 14–17 | ChartsFinal passes both arms. **S5c release-to-settled 598 → 1,129 ms; S5d 811 → 1,026 ms** | **Revert**: primary worse and settle regressed. Probes after the tween smooth it but delay settle | rounds/05-reverted.patch, 05.json; perf/hill-climb/r05 |
| 6 | r05, plus deferred probes drained synchronously at pointerup, before the tween's first frame | release long tasks median 2 → 1; forced layouts 15 → 8; animation samples 8 → 13 (min 2 → 9); animation gate fails 2/20 → 0/20 | 1,571 → 1,541 ms (−1.9%) | Overlap. Load average 10–12 | **S5c settle regression persists** (B 929–1,278 ms in 4 of 5 runs vs A ~510–660). Shared with r05, so it comes from removing the release full render | **Revert**: primary below 10%, settle regression. Keep the sync-drain idea | rounds/06.json, 06-full-state.patch; perf/hill-climb/r06 |

| 7 | r04 (keeping the release full render), plus deferred probes drained synchronously at pointerup | release long tasks 2 → 1 typical; **animation samples 8 → 6.5; animation gate fails 0/20 → 2/20** | 1,621 → 1,324 ms (−18.4%) | Overlap. Load average 12–20 | S5c settle fine (median 481) | **Revert**: the target mechanism (a smooth settle tween) did not engage. The rest of the tween jank is the full render's async widget refreshes landing mid-tween | rounds/07.json, 07-full-state.patch; perf/hill-climb/r07 |
| F2 | **Final confirm of the reviewed code** (r04 + A1, with the ChartsFinal gate): vs original eb0f6c6, 10+10, load average 5–7 | SVG S5a 296→70; forced layouts S5a 696→163, S5c 1210→295 | 39,703 → 19,574 ms (**−50.7%**) | A 35,695–51,794; B 18,642–30,784; **no overlap** | **All gates pass in all 40 scored scenario runs per arm**, including ChartsFinal. One unscored B warmup failed the settle-animation gate. p95 S5a/b/c/d 100/225/175/83 → 33/50/50/33. **S1 drop-to-settled 946 → 351 ms** (B max 384). S2 −15%, S3 −22%, S4 −26%. S5 release-to-settled +5–15% (e.g. S5b 488→562); release long tasks 1 → 2 | **Confirmed** | perf/hill-climb/final2 |

### Outcome

**Kept:** rounds 1–4, plus review fix A1 (phone natural-height guard restored to Auto-only; no desktop effect).

**Stop reason:** rounds 5 and 6 were two consecutive valid rounds under 10%. Round 7 failed its mechanism.

**Goals** (4× CPU; confirmed by F2, the final 10+10 vs the original on the reviewed code, all scored gates passing; F1 failed a gate and is superseded):
- resize frame p95 S5a/b/c/d is 100/225/175/83 → 33/50/50/33 ms (goal ≤ 50: **met**)
- S1 drop-to-settled is 946 → 351 ms (goal ≤ 400: **met**)
- primary resize motion work is −50.7%, with no overlap
- S2/S3/S4 are 15–26% faster

**Known open item: resize-release smoothness.** The kept state does 2 release long tasks (the full `render()` at pointerup, then a deferred probe batch / async widget refreshes mid-tween) vs 1 in the original. The 270 ms settle tween therefore samples fewer frames, and the settle-animation gate fails occasionally under load: 2 of 40 in F1, vs 1 of 40 for the original. F2 had 0 scored failures, and release-to-settled is 5–15% slower than the original.
- r06 fixed the tween: 0/20 fails, 13 samples, 1 long task. But removing the release full render regressed corner-resize (S5c) settle to about 1 s.
- **Next climb:** start from r06 (`rounds/06-full-state.patch`) and diagnose the S5c settle tail.
