# Research → live integration: status after P0–P7 (2026-10-19)

The plan is in `research_live_feature_integration_plan_2026-10-19.md`. Detailed documents:

- `recording_framework_2026-10-19.md` (P4/P5);
- `case_runs_migration_2026-10-19.md` (P6);
- `redemption_discovery_2026-10-19.md`;
- `capture_replay_and_explorer_2026-10-19.md`.

Out of scope and unchanged:

- no real-money trading;
- no live template enables a new setting;
- no redemption was submitted.

## Implemented versus still incomplete

| Area | Implemented | Still incomplete |
|---|---|---|
| Strategy features (P1–P3) | Variance time policy and the disagreement veto as strategy settings, both defaulting to legacy/off. Replay execution contract v2 (live readiness gate). | — |
| Recording (P4/P5) | Decisions and explain recording for live, paper, observation and replay. Raw capture with fidelity labels. `raw_on_fault`. Prune and benchmark commands. | Raw capture's per-message cost on the event loop (see the performance section). |
| Notebook 06 (P6, item 2) | Production-path case runs. Migration verified: 42 of 42 trading-equal under v1 and v2. Study hooks removed. Shared explorer over case runs, v3 runs and study bundles, with exact policies and labels saying whether a trace is saved or regenerated. | — |
| Explanations (item 3) | 24 runs, 12,605 records, browsable in the explorer. | The live loss windows are not in any local recording, so they can't be replayed. |
| Capture → replay (item 4) | Warehouse RTDS normalizer, SDK adapters, live clock snapshots, evaluation instants, observe-only reconstruction. **919 of 919 decisions** on a 16-minute capture. | Reconstructing **trading-run** decisions needs the execution journal's position timeline. Binance `@depth20` is not normalized (not used). |
| UI redemptions (item 1) | Automatic, read-only, proven and idempotent discovery. Applied to the live ledger (+10.25532 pUSD credited). | The `reconcile` and `confirm-redemption` CLI commands still don't take the state lock (unchanged). |
| Book DESYNC (item 6) | Root cause established and impact quantified (below). | Fix proposed, **not applied**. |
| P7 qualification (item 5) | Development, freeze and one untouched evaluation: done. Paper performance runs A–D: done. | See the qualification and performance sections. |

## Research-qualified versus merely available

- **Available, not qualified for live:**
  - `variance_time_policy: elapsed_variance_v1`;
  - `disagreement_veto` (any threshold);
  - `recording.raw_inputs` in live trading.
- **Qualified as tooling:**
  - decision and explain recording: no measurable overhead in paper runs;
  - capture → reconstruction for observation runs;
  - redemption discovery: proven on the real UI transaction.
- **Not qualified:** the 9 pp veto. On development evidence it removes mostly winners, so it was not tested on the holdout and is not recommended.

## P7: qualification

The identity is `var/research/p7_qualification/p7-4863e0595c/qualification.json`. Its record holds the candidates, policies, execution policy, scenarios and both window sets, plus the freeze (rationale, holdout check, development digest) and the stage statuses.

- **Execution:** `replay_execution_contract_v2`, `reference_availability_v1`.
- **Candidates:** the two frozen entry configurations, unchanged.
- **Policies:**
  - `legacy_volatility` (`per_sample_v0`);
  - `corrected_volatility` (`elapsed_variance_v1`).
- **Development:** 94 frozen development windows × 2 candidates × 2 policies × {friction, favorable} = 752 replays (3,269 s, 6 workers).
- **Untouched check before the freeze:** no saved result on disk (summaries, decisions, timeline caches, notes or other research files) refers to any of the 39 holdout windows. Unsaved interactive inspection in earlier sessions cannot be ruled out. The split itself records one window moved to development after inspection.

### Development results

| Candidate | Policy | Scenario | Fills | Wins/losses | P&L | Per fill | Without top-3 windows |
|---|---|---|---|---|---|---|---|
| default | legacy | friction | 36 | 29/7 | +57.81 | 1.61 | +12.06 |
| default | corrected | friction | 43 | 34/9 | +57.45 | 1.34 | +5.52 |
| default | legacy | favorable | 44 | 35/9 | +73.22 | 1.66 | +23.92 |
| default | corrected | favorable | 58 | 46/12 | +68.59 | 1.18 | +10.43 |
| comparison | legacy | friction | 32 | 25/7 | +77.42 | 2.42 | +8.14 |
| comparison | corrected | friction | 39 | 31/8 | +20.32 | 0.52 | +2.12 |
| comparison | legacy | favorable | 39 | 30/9 | +99.65 | 2.56 | +23.14 |
| comparison | corrected | favorable | 54 | 41/13 | +28.41 | 0.53 | +3.96 |

- The corrected policy changes the outcome in 85 of 188 window × candidate pairs (friction).
- It gives more fills. For the default candidate the P&L is about equal. For the comparison candidate it is lower, driven by a few side flips on cheap fills; `…1792087500` went from +41.01 to −4.89.
- 73–90% of each configuration's P&L sits in its top 3 windows, and median P&L per fill is 0.7–1.2. **Development does not rank the policies reliably.**

### Veto evidence (development, v3 verification, gap at the first entry)

| Candidate, scenario | Threshold | Fills vetoed | Winners / losers removed | P&L removed |
|---|---|---|---|---|
| default, friction | 9 pp | 34 of 36 | 27 / 7 | +57.17 |
| comparison, friction | 9 pp | 32 of 32 | 25 / 7 | +77.42 |
| default, friction | 20 pp | 7 of 36 | 6 / 1 | +8.44 |
| comparison, friction | 20 pp | 2 of 32 | 1 / 1 | −3.79 |

Every filled entry already has a large gap, so no threshold isolates losers. No veto variant was tested.

### Freeze (2026-10-19 17:41 UTC)

Both policies, unchanged, with the rationale above. The holdout check at the freeze passed: untouched, 39 windows.

### Untouched evaluation

39 holdout windows × 2 candidates × 2 policies × 2 scenarios = 312 replays (765 s). It ran once and was not repeated.

| Candidate | Policy | Scenario | Fills | Wins/losses | P&L | Per fill | Without top-3 windows | Median per fill |
|---|---|---|---|---|---|---|---|---|
| default | legacy | friction | 16 | 9/7 | **+38.60** | 2.41 | −26.26 | 0.14 |
| default | corrected | friction | 23 | 18/5 | **−1.48** | −0.06 | −14.23 | 0.62 |
| default | legacy | favorable | 18 | 10/8 | +31.66 | 1.76 | −36.20 | 0.19 |
| default | corrected | favorable | 27 | 22/5 | +1.28 | 0.05 | −12.05 | 0.62 |
| comparison | legacy | friction | 14 | 10/4 | **+72.68** | 5.19 | −4.28 | 0.65 |
| comparison | corrected | friction | 18 | 14/4 | **+9.60** | 0.53 | −6.37 | 0.59 |
| comparison | legacy | favorable | 13 | 9/4 | +71.79 | 5.52 | −14.26 | 0.74 |
| comparison | corrected | favorable | 27 | 23/4 | +21.17 | 0.78 | +4.50 | 1.07 |

The corrected policy changed 55 of 78 window × candidate pairs (friction). Its total P&L delta was −40.08 for default and −63.08 for comparison.

**Reading.**

- Legacy volatility has the higher total on the holdout, as it had in development. But **all** of its holdout profit comes from its top 3 windows: without them it is negative for both candidates. These are large payouts on cheap fills.
- Corrected volatility fills more often and wins more often (default: 18 wins against 5 losses, versus 9 against 7), with a higher median per fill. But it does not take those few large payouts.
- With 39 windows and P&L this concentrated, the difference cannot be separated from luck in the tails.

**Result of P7.**

- **Neither policy is qualified as an improvement to enable.** `elapsed_variance_v1` stays available but off.
- The legacy result does not establish robust profitability either. Its edge rests on a handful of windows, in development and holdout alike.
- The holdout has now been used and must not drive further selection. The next untouched evidence must come from new recordings: the 43 windows of today's collection are not yet in the split, and frozen extensions can add them.

## Performance: recording off versus on (bounded paper runs)

The four paper runs are listed below. All used live public data, a simulated account, no signing, the baseline 2c/150s strategy and continuous hold, with about 25–30 minutes and 5–6 windows each:

| Run | Recording | When (local) | Notes |
|---|---|---|---|
| A | off (`essential`) | 17:30–18:00 | re-run with the corrected sampler and timer |
| B | explain + raw | 17:01–17:30 | |
| C | explain + raw, writer forced to fail, `raw_on_fault: stop_entries` | 18:00–18:15 | |
| D | explain, no raw | 18:15–18:45 | |

A separate 6-hour data collection ran on the same machine until 18:33. It shares the CPU, and D overlaps its shutdown.

| | A, off | D, explain | B, explain + raw |
|---|---|---|---|
| Process CPU mean (one core = 100) | 38.6% | 44.9%* | 36.3% |
| Memory p50 | 164 MiB | 151 MiB | 175 MiB |
| Event-loop lag, median of per-minute p50 | 0.87 ms | 0.39 ms | **3.7 ms** |
| Event-loop lag, median of per-minute p99 | 90 ms | 209 ms* | 92 ms |
| Event-loop lag max; reports > 250 ms | 660 ms; 35 | 415 ms; 44* | 724 ms; 37 |
| Order request → POST (journal, n = 4 each) | ≤ 16 ms | ≤ 15 ms | 31 ms |
| Recording writer CPU | — | 0.56 s | 16.3 s |
| Capture cost per evaluation p50 / p99 | — | 90 / 154 µs | 89 / 169 µs |
| Records written / dropped | — | decisions + explain / 0 | 665,535 raw (651 MB uncompressed) + decisions + explain / 0 |
| Peak raw queue | — | — | 528 records / 16 MB (bound 50,000 / 64 MiB) |

\* D overlaps the data collection's shutdown.

**Failing writer (C):**

- The raw stream failed when it opened its first segment.
- `RECORDING_FAILED` was raised ("trading continues"), then `RAW_CAPTURE_FAILED` with `stop_entries`.
- Raw capture stopped visibly: 5 records were dropped and counted, and no further capture was attempted.
- The decisions and explain streams kept writing (816 / 457 records, 0 drops).
- The entry-only blocker `RAW_CAPTURE_FAILED` was raised, so there were no new entries, while evaluation continued (593 decisions).
- Event-loop lag was unaffected (p50 1.2 ms).

**Conclusions:**

- Decision and explain recording has no measurable cost on the loop, the order path or CPU.
- Raw capture adds about 3 ms of typical loop delay. It also shows a possible ~15 ms on request → POST: one or two 16 ms timer steps on 4 orders, which is an indication, not a measurement.
- Recommendation, unchanged from decision 6: raw capture for observation and diagnostic runs; not in live trading without a dedicated measurement.

**Limitations:**

- Order timings come from the execution journal's monotonic stamps, which have about 16 ms resolution on Windows.
- 4–5 orders per run.
- The runs were sequential, not interleaved, with market conditions differing between them.
- There was CPU contention from the data collection.
- The event-loop monitor was switched to `perf_counter` after the first run A, because `time.monotonic` steps about every 15.6 ms on Windows. That first attempt is archived and was not used.

Raw data is in `var/research/p7_performance/` (`performance_report.json`, process samples, journals).

## Book DESYNC (item 6)

**Cause.** It is a reconstruction artefact, not a genuine crossed venue state. Polymarket sends one matching step as several websocket messages:

1. a `price_change` adding the marketable remainder through the opposite best;
2. size-0 removals of the opposite levels;
3. fresh `book` snapshots.

The store checks for a cross after each single message (`book_store.py` → `snapshots.py:88`). The venue's own best bid and ask on those messages were never crossed: 0 of all price changes.

**Measured on the probe capture (3.5 min):**

- 14 token-episodes from 7 venue messages;
- each episode ended 0.5–3.5 ms later by the next `book` message;
- total desynced time 1.5–13 ms per token;
- one wasted REST resync, correctly rejected by the version guard;
- 0 evaluations affected, but that probe had no in-band evaluations.

**Locked or stale top while READY.** In 146 of 257,842 checks (0.057%) our top differed from the venue-reported best for a few milliseconds, e.g. 0.93/0.93 against the venue's 0.93/0.94. The strategy can see these, because the readiness check allows bid == ask.

**Replay data quality.** The warehouse reconstruction's crossed check uses `>=` and records about 5,040 `crossed_book` invalidations in 3.2 h from the same cause. Replay is therefore affected more often than live.

**In-band entries affected.**

- The 16-minute capture shows the same warnings.
- Reconstruction there reproduced all 919 decisions, including the in-band ones, so no decision differed between live and reconstruction.
- By time fraction, about 0.006% per token, roughly 1 evaluation in 10⁴ could meet a desync.

**Proposed fix, not applied.** Carry each change's venue `best_bid`/`best_ask` into the delta. When a cross or lock appears and the venue best is uncrossed, remove the opposite levels better than the venue best (exactly what the following size-0 messages do), then require our top to equal the venue best, or DESYNC as today. Mirror the rule in `reconstruction/books.py` so live and replay stay consistent.

The following protections are kept: the strict `BookSnapshot` cross check, DESYNC on any remaining inconsistency, delta rejection while desynced, the epoch and token filters, the REST version guard, and the log rate limit.

It is not applied in this pass because it changes live book state and every replay baseline. It needs its own review and a before/after replay comparison.

The investigation scripts are in the session scratchpad (`desync/`). The findings above are what they produced.

## Simple instructions

- **Notebook 06:**
  1. Run All. Nothing launches.
  2. Section 7 holds the explorer. Pick a dataset (case run, v3 run or study bundle), then a configuration, then a window.
  3. *Generate timeline* replays only that one run.
  4. To launch new case runs, set `RUN_CASE_REPLAYS = True` in the first cell.
- **UI redemptions:** redeem in the Polymarket UI. A running hold runner records it within `redemption_discovery.interval_s` (default 300 s). Without a runner, run:

  ```
  tyrex-pm settlement --config <hold run yaml> discover-redemptions --connect-chain [--apply]
  ```

- **Reconstructing an observation:**

  ```
  tyrex-pm recording reconstruct --capture <observation folder>
  ```
