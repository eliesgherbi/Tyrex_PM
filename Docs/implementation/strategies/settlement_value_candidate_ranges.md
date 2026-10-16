# settlement_value — provisional candidate ranges (S2)

Date: 11 October 2026.

**Data:** 45 development windows from two collections (1 October and 6 October 2026, three recording sessions). 18 later windows are held out and **have not been evaluated**.

**Labels:**

- outcomes are `proxy_outcome` (payout unit unknown);
- K is PROVISIONAL;
- the reference is the PolyBolt TWAP;
- fees use the scenario curve;
- execution follows the uncalibrated `favorable` / `friction` scenarios.

**Not proof of edge.** These are starting points for signal-only observation, not trading settings.

## Evidence used

| Bundle (`var/research/strategy_runs/s2/`) | Code state | Use |
|---|---|---|
| `validation_seed/` — 2 backends × 2 scenarios × 45 windows (180 runs, both backends recorded) | Current: recorded clock correction, limit buffer 1 | **Primary** |
| `vol/` — 10 volatility settings × 45 windows | Before the clock correction; endpoint backend only | Relative volatility sensitivity only |
| `grid/summaries_partial.parquet` — 24 configurations × 2 scenarios × 9 windows | Before the clock correction; no limit buffer; grid stopped early | Smoke result only |

## Findings

1. **Probability quality.**

   | Source | Brier (pooled) | Log loss (pooled) |
   |---|---|---|
   | Book midpoint | 0.1227 | 0.378 |
   | TWAP proxy model | 0.1229 | 0.382 |
   | Endpoint model | 0.184 | 0.551 |

   Values are on common rows at 240, 180, 120, 60 and 30 s to close; 16–41 windows per checkpoint (3 at 30 s).

   - The averaging-aware proxy is as good as the book.
   - The endpoint model is clearly worse; it should not drive decisions.
   - Matching the book is not beating it: an edge needs the model to be *right where it disagrees*.

2. **Selected-trade economics.**

   | Backend | Scenario | Entries | Wins | Debit | P&L |
   |---|---|---|---|---|---|
   | TWAP proxy | favorable | 15 | 8 | $71.61 | −$11.50 |
   | TWAP proxy | friction | 16 | 8 | $66.20 | −$8.91 |
   | Endpoint | favorable | 29 | 10 | $138.95 | −$12.05 |
   | Endpoint | friction | 30 | 9 | $128.95 | −$7.28 |

   - **TWAP proxy:** the friction run reached 16 entries with 14 fills.
   - **Endpoint:** the friction run had 26 fills.
   - **Uncertainty:** every 95% window-bootstrap interval straddles zero widely (TWAP proxy about −$45 to +$25).
   - **Side selection:** 58% of fills bought the book's underdog side; the win rate was 42%.

3. **Volatility.** The shared EWMA estimator with the old 1e-4 floor inflated uncertainty about 4×. A 1e-5 floor, half-life 30–60 s and `sigma_scale` 1.0–1.25 gave the lowest model Brier in `vol/`. That run predates the clock correction, so treat this as relative guidance only.

4. **Execution.**
   - With no limit buffer, `friction`'s adverse tick left most taker orders unfilled.
   - A one-tick buffer restores fills while never paying beyond the limit.
   - A $2 debit cap is often infeasible against the assumed 5-share minimum (`BELOW_MINIMUM_SIZE`).

5. **Clock.** These figures come from replay v1, whose clock mixed whole-second `clob_time` samples into the correction: 36–60% of evaluations were host-skipped (`future_binance_rejected`). Replay v2 (15 October 2026) uses Binance time only and removes those skips (S0, collector clock section). **The ranges above must be re-derived from v2 reruns before use.**

## Provisional ranges (for observation, not trading)

| Parameter | Range | Basis |
|---|---|---|
| `model.backend` | `twap_proxy_diffusion` | Probability quality equal to the book; endpoint clearly worse |
| `volatility.sigma_floor` | 1e-5 | Old 1e-4 always binding |
| `volatility.half_life_s` | 30–60 | `vol/` (pre-correction, relative) |
| `volatility.sigma_scale` | 1.0–1.25 | `vol/` (pre-correction, relative) |
| `entry.min_edge_per_share` | ≥ 0.05 | Higher edge thresholds reduce underdog buying; not separable from noise here |
| `entry.tau_max_s` / `tau_min_s` | 150–240 / 30 | Partial grid only |
| `entry.limit_buffer_ticks` | 1 | Restores fills under friction |
| `sizing.max_debit_usdc` | 5 (`cap_only`) | 2 is often below the minimum size |

## Status and next evidence

- **No shortlist and no holdout evaluation:** neither was produced; no configuration has enough fills to rank.
- **Before any trial:**
  - record longer (the 24-hour collection in the runbook);
  - rerun `s2_experiments vol` and `grid` on current code, which records both backends and includes the clock correction;
  - run signal-only observation;
  - only then fix a shortlist from development data and evaluate it once on the holdout.
