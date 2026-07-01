# Milestone 9 — Baseline Strategy Rerun & Before/After Report

## Objective

Re-run the **current paired-binary strategy unchanged** on the **WS-authoritative backbone** (post-M8) and produce a structured before/after report. **This is the Phase 2 completion gate.**

## Why this milestone exists

Phase 2 success is **not** profitability. M9 measures whether the **same strategy** runs faster with better execution evidence under WS-authoritative data.

**M9 can conclude:** speed, freshness, and execution evidence improved or did not.  
**M9 cannot conclude:** positive expected value, stable edge, or that the strategy is profitable.

## Current codebase state

After M8: WS-primary authoritative store, quality gate, planner, FeatureBuilder v0, scheduler, latency facts. Baseline from M0 frozen control runs.

## Target behavior

### Experiment design

```text
Control:  M0 frozen runs (baseline_runs.yaml)
Treatment: post-M8 runs, identical strategy/runtime params except backbone
```

### Frozen experiment metadata (required in report)

```text
strategy config hash        (sha256 of config/strategies/paired_binary.yaml)
runtime config hash         (sha256 of scenario YAML used)
git commit hash             (treatment runs)
market type                 (e.g. crypto_5m BTC binary)
position_size
pair_stop_loss_pct
pair_take_profit_pct
order style                 (FAK entry)
market selection method
control run IDs + facts paths
treatment run IDs + facts paths
```

### Control runs (from M0 `baseline_runs.yaml`)

```yaml
control_success_run_id: paired_binary_live_1782741234
control_bad_stop_run_id: paired_binary_live_1782742788
control_activation_abort_run_id: paired_binary_live_1782737121
```

Substitute only with documented approval and updated `baseline_runs.yaml`.

### Metrics to compare

| Metric | Source |
|--------|--------|
| book_age_ms at entry / stop / exit planning | decision_snapshot |
| trigger_to_submit_ms, submit_to_ack_ms, trigger_to_fill_ms | latency_chain |
| FAK reject rate | OMS + fak_retry facts |
| expected vs actual slippage | planner evidence vs cashflow |
| activation failures (stale) | quality + activation facts |
| stop slippage | trigger vs exit cash/qty |
| survivor timeout | survivor + max_runtime facts |
| cashflow PnL | paired_binary_realized_pnl |
| PnL variance | across treatment runs |

### Report sections

1. Executive — infrastructure hypothesis: supported / not supported / inconclusive
2. Frozen metadata block (hashes, params)
3. Freshness — p50/p95/p99 book_age_ms before vs after
4. Speed — latency chain before vs after
5. Execution — FAK rejects, slippage, activation
6. PnL — cashflow totals (**not** profitability claim)
7. Verdict — **"Was the strategy bad, or slowed by stale data?"**
8. Recommendation — signal phase vs further infra work

**Explicit disclaimer in report:**

```text
This report measures infrastructure and execution evidence improvement.
It does NOT establish strategy edge or positive expected value.
```

## Files likely touched

- `Docs/Implementation/WebSocket_event_driven_backbone/baseline_vs_ws_primary_report.md` (output)
- `Docs/Implementation/WebSocket_event_driven_backbone/baseline_runs.yaml`
- `scripts/compare_run_latency.py`, `scripts/extract_paired_binary_metrics.py`
- `Docs/OPERATIONS.md`

## New files likely created

- `baseline_vs_ws_primary_report.md`
- `scripts/extract_paired_binary_metrics.py`

## Config changes

Freeze and hash at experiment start — no param changes between treatment runs.

## Facts / observability changes

Consume M7 facts only.

## Tests to add or update

- Script parses sample facts.jsonl → aggregates
- CI optional: fixture before/after JSON

## Acceptance criteria

- [x] ≥3 treatment runs post-M8 vs 3 control runs from M0 (2 RECONSTRUCTED + 1 VERIFIED)
- [x] Report includes frozen metadata block (all fields listed above)
- [x] All metrics in comparison table
- [x] Explicit infrastructure vs strategy-edge verdict
- [x] Disclaimer that edge/profitability not established
- [ ] Team sign-off — **Phase 2 complete** (report ready for review)

**Report:** [baseline_vs_ws_primary_report.md](baseline_vs_ws_primary_report.md)  
**Status:** `PHASE_2_COMPLETE` (2026-06-30)

## Risks

- Small N — document confidence caveat
- Regime change between control/treatment dates — note in report

## Open questions

- Minimum treatment N — recommend ≥3; document if fewer

## Definition of done

Published report with data; OPERATIONS updated; Phase 2 infrastructure objectives declared complete.

## Not in scope

- Strategy parameter tuning
- BTC/RTDS, OBI ([M10](milestone_10_future_scalability_hooks.md))
- Profitability claims
