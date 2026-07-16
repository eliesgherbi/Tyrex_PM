# D1 — Entry-Frequency and Candidate-Wait Analysis

**Status:** COMPLETED (low confidence — supplemental A0.5 evidence included)  
**Artifact:** `var/reporting/z_gap/d1_entry_frequency_analysis.json`  
**Script:** `scripts/analyze_d1_entry_frequency.py`

## Parameters (unchanged — `live_z_gap_tiny.yaml`)

| Parameter | Value |
|-----------|-------|
| `theta_take` | 0.05 |
| `theta_fill_floor` | 0.03 |
| `z_band` | [0.8, 2.2] |
| `tau_band_s` | [60, 210] |
| `basis_max_bps` | 3 |
| `sizing.max_usd` | 5 |

## Local workspace scan

| Metric | Value |
|--------|-------|
| Complete windows (local facts) | **0** |
| Evaluation ticks | **0** |
| `would_enter` count | **0** |
| Confidence | **low** |

No `var/reporting/runs/z_gap_observe_*` facts are present in this workspace. Operator should schedule additional observe-only windows before relying on live candidate timing.

## Supplemental documented evidence (A0.5 report)

From `Docs/Implementation/z_gap_strategy/a0_5_observe_only_live_report.md` (post timing-fix batches):

| Batch | Windows | `would_enter` | Dominant skips |
|-------|---------|---------------|----------------|
| `z_gap_observe_fix_*` | 3 | 0 | basis, feed_stale, jump_guard |
| `z_gap_observe_clocksync_*` | 3 | 0 | basis, feed_stale, chainlink_stale |

**Combined documented windows:** 6  
**Windows with ≥1 candidate:** 0 (0%)  
**Estimated windows until one candidate:** undefined (no candidates observed)

### Skip separation

| Class | Examples | Interpretation |
|-------|----------|----------------|
| Infrastructure / model unavailable | `z_gap_feed_stale`, `z_gap_chainlink_stale_basis_untrusted`, `z_gap_sigma_not_ready` | Feeds, basis trust, or model readiness — not market qualification |
| Market conditions | `z_gap_basis_exceeded_fresh_chainlink`, `z_gap_jump_guard`, `z_gap_tau_out_of_band`, `z_gap_z_out_of_band`, `z_gap_edge_below_theta` | Current parameters did not qualify under observed BTC/PM dynamics |

### Dominant blocking gates (A0.5 histograms)

1. `z_gap_basis_exceeded_fresh_chainlink`
2. `z_gap_feed_stale`
3. `z_gap_chainlink_stale_basis_untrusted`
4. Occasional `z_gap_jump_guard`

## Limitations

- Entry facts dedupe on `(decision_status, reason_code, selected_leg, selected_edge)` — transition counts, not every 1s tick.
- **No profitability or statistical edge claim.**
- Parameters were **not** tuned for this analysis.
- Additional observe-only windows recommended before operator go/no-go on candidate timing.

## Operator action

Run before live window:

```bash
python scripts/run_z_gap_observe_batch.py --windows 3 --prestart-seconds 60 --min-prestart-seconds 20
python scripts/analyze_d1_entry_frequency.py --output-json var/reporting/z_gap/d1_entry_frequency_analysis.json
```
