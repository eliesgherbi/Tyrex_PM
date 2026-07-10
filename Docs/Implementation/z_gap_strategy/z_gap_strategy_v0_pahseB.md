# Z-Gap Strategy v0 — Phase B: Stable v0 After First Live Proof

**Status:** Implementation-ready (no code yet)  
**Date:** 2026-07-09  
**Parent reference:** [`z_gap_strategy_v0.md`](z_gap_strategy_v0.md)  
**Prerequisite:** [`z_gap_strategy_v0_pahseA.md`](z_gap_strategy_v0_pahseA.md) operational pass  
**Next phase:** [`z_gap_strategy_v0_pahseC.md`](z_gap_strategy_v0_pahseC.md)

---

## 1. Goal

After Phase A proves **observe_only live model path (A1)** and **one tiny enforced window (A2)** safely and explainably, Phase B stabilizes v0 into a **repeatable live strategy** across multiple windows.

Phase B adds:

- settlement / redemption / resolution reconciliation,
- hold-to-resolution (only after settlement path exists),
- corrected rich-exit logic,
- improved basis gate,
- `run_continue` with scheduler lead-time,
- recovery after interruption,
- richer reconciliation and reporting.

Phase B success: **repeatable multi-window live operation** with full PnL attribution (sell-exit vs resolution), no manual intervention in normal windows, and documented cold-start mitigations.

---

## 2. Entry Criteria From Phase A

Phase B starts only when **both** Phase A sub-stages pass:

### From Phase A1 (observe_only)

| Gate | Required |
|------|----------|
| A0.0 spikes complete (fee curve + Binance reachability) | ✓ |
| ≥3 observe_only live windows with usable facts | ✓ |
| `calibration_samples.jsonl` accumulating | ✓ |
| Distinct basis skip codes verified in live facts | ✓ |
| Feed freshness + model snapshots reliable | ✓ |

### From Phase A2 (enforce)

| Gate | Required |
|------|----------|
| PTB attestation on **fresh** windows ≤ **0.5 bps** | ✓ |
| Clock sanity ≤ 500ms drift | ✓ |
| Calibration-lite reviewed | ✓ |
| Dynamic φ fee model resolved (or empirically validated) | ✓ |
| `max_fill_price_for_edge_floor` validated on ≥1 fill | ✓ |
| Thesis stop + lifecycle flatten proven | ✓ |
| `fee_validation` on real fill within tolerance | ✓ |

**Do not enable Phase B hold-to-resolution** until settlement milestone (B0.3) is complete.

---

## 3. Scope

| Area | In scope |
|------|----------|
| Settlement detection + resolution outcome | ✓ |
| Token redemption / claiming path | ✓ |
| Resolution payout reconciliation | ✓ |
| Hold-to-resolution policy | ✓ (after B0.3) |
| Merged fee-adjusted rich-exit | ✓ |
| Basis gate improvement | ✓ |
| `run_continue` for `z_gap` | ✓ |
| Scheduler lead-time / warm-up assertions | ✓ |
| Recovery (position, exit, settlement-pending) | ✓ |
| Extended cashflow reconciliation scripts | ✓ |
| Kill switch extensions (`max_trades_per_run`, etc.) | ✓ |
| Integration tests (hold, settlement, run_continue) | ✓ |

### Out of scope (Phase C)

- Full calibration / walk-forward
- Frictioned replay backtest
- Shadow mode
- Kelly / fractional sizing
- Maker mode
- Long-lived feed process (noted; full solution in C)
- `z_neutral_timeout` (optional late B; default C)

---

## 4. Stable v0 Architecture Changes

Phase B extends Phase A architecture without replacing it:

```text
Phase A loop (z_gap_run.py)
  + settlement/resolution watcher
  + redemption executor (if required)
  + hold_policy (gated on settlement support)
  + rich_exit evaluator
  + recovery on startup
  + run_continue subprocess orchestration
```

### New / extended modules

| Module | Purpose |
|--------|---------|
| `settlement/resolution_watcher.py` | Detect market resolved, outcome, payout |
| `settlement/redemption.py` | Claim/redeem winning tokens if Polymarket requires explicit action |
| `settlement/reconciliation.py` | Match $1 payout vs position |
| `exit_policy/hold_policy.py` | Hold-to-resolution when z strong |
| `exit_policy/rich_exit.py` | Merged fee-adjusted rich exit |
| `exit_policy/basis_gate.py` | Improved basis / lag handling |
| `strategies/z_gap/recovery.py` | Resume interrupted runs |
| `runtime/run_continue.py` | Allow `strategy_kind == z_gap` |
| `scripts/reconcile_run_cashflows.py` | Z-Gap + resolution paths |

### Reuse

- Phase A: `SignalStateStore`, quant model, φ fees, model-capped entry, thesis stop
- Existing: `paired_binary_resolution_exit_accounting` pattern, `market_resolved` WS events, wallet reconcile

---

## 5. Settlement and Redemption Path

**Blocker:** Z-Gap may hold a winning token through settlement. Paired-binary mostly sold before resolution; settlement cashflow was secondary (`paired_binary_resolution_exit_accounting`).

### B0.3 — Settlement / redemption / resolution-payout reconciliation

**Investigate current repo:**

| Asset | Path |
|-------|------|
| Resolution events | `core/events.py` — `RESOLUTION_OBSERVED`, `MARKET_RESOLVED` |
| WS parser | `ingestion/event_factory.py` |
| Resolution normalize | `research/normalize/tables/market_resolutions.py` |
| PnL accounting fact | `FACT_TYPE_PAIRED_BINARY_RESOLUTION_EXIT_ACCOUNTING` |
| Wallet refresh | `runtime/pipeline.py` reconcile path |

**Implement:**

1. **Settlement detection** — Subscribe to `market_resolved` / poll Gamma post-close; map winning outcome to held leg.
2. **Resolution outcome** — UP won if final reference ≥ K (or venue outcome label).
3. **Redemption path** — Document Polymarket mechanics:
   - If tokens auto-settle to USDC via CLOB/CTF contract → wallet sync sufficient.
   - If explicit `redeem` API required → implement in `settlement/redemption.py` using existing `PyClobBridge` / CTF patterns (inspect `venue/polymarket/`).
4. **Wallet update** — Post-resolution `refresh_wallet_from_clob` + position zeroing.
5. **Reconciliation** — `expected_payout = shares * $1` (winner) vs wallet delta; emit `z_gap_resolution_reconciled`.
6. **Facts:**
   - `z_gap_settlement_observed`
   - `z_gap_redemption_submitted` / `z_gap_redemption_done`
   - `z_gap_resolution_pnl`
   - `z_gap_resolution_reconciled`

**Tests:**

- Fixture: held UP, UP wins → $1/share payout reconciled
- Fixture: held UP, DOWN wins → $0 payout
- Fixture: sell-exit before resolution → no resolution payout

**Gate:** `hold_to_resolution.enabled` may only be `true` after B0.3 tests pass.

---

## 6. Hold-to-Resolution Policy

### Rule (quant feedback)

Do **not** sell merely because lifecycle flatten is near if hold conditions are met:

```text
If abs(z) >= z_hold continuously for hold_confirm_s
   AND tau <= tau_hold_s:
    hold to resolution (suppress discretionary exits)
Else:
    normal flatten / thesis stop / rich exit apply
```

**Suggested config:**

```yaml
hold_to_resolution:
  enabled: true   # only after B0.3
  z_hold: "1.5"
  tau_hold_s: 45
  require_continuous_hold: true
  hold_confirm_s: 5
```

**Interaction with lifecycle:**

| Condition | Behavior |
|-----------|----------|
| Hold active | Suppress rich exit and discretionary flatten |
| Thesis stop | **Still fires** — model reversal overrides hold |
| Kill switch | Always fires |
| Resolution | Natural exit at settlement |

Phase A used `flatten_before_event_end_s: 20` always. Phase B: when hold active, **defer flatten** until settlement path handles exit.

### State machine addition

```text
ACTIVE → HOLD_TO_RESOLUTION → SETTLEMENT_PENDING → DONE
```

Persist `hold_active_since_ts`, `hold_z_peak` in state file.

---

## 7. Improved Exit Logic

### B2 — Correct rich-exit (merge edge-exit / rich-exit)

**Problem:** Separate "rich exit" (`bid >= p + theta_rich`) and "edge exit" (`p - bid - φ(bid) < -theta`) can duplicate the same directional trigger.

**Phase B unified rule — fee-adjusted rich exit:**

```text
rich_edge_at_bid = p_leg - bid - φ(bid) - slippage_exit

Exit if rich_edge_at_bid <= -theta_rich_exit
```

Where `theta_rich_exit` replaces separate `theta_rich` and `theta_exit` (single threshold, default `0.03`).

**Interpretation:** Market bid is "too rich" vs model after fees — take profit.

**Still-uncovered case (document, do not over-engineer in B):**

```text
z decays slowly toward neutral
thesis never fully reverses (no thesis stop)
position becomes coin flip
lifecycle flatten eventually handles it (if hold not active)
```

**Optional late Phase B / Phase C:** `z_neutral_timeout` — exit if `|z| < z_neutral` for `T` seconds. Default: defer to Phase C.

### Exit precedence (Phase B)

1. Kill switch / hard stop
2. Thesis stop (`z_stop`)
3. Fee-adjusted rich exit (if not holding)
4. Lifecycle flatten (if not holding)
5. Hold → settlement

### Module layout

| File | Role |
|------|------|
| `exit_policy/rich_exit.py` | Unified rich-edge exit |
| `exit_policy/evaluator.py` | Orchestrates thesis + rich + hold |
| `exit_policy/dispatch.py` | Extend Phase A dispatch |

---

## 8. run_continue and Scheduler Lead-Time

### Problem (acknowledged)

`run_continue.py` spawns a **subprocess per window** (`execute_run` via subprocess). Each 5-minute window cold-starts:

- Binance WS reconnect
- RTDS reconnect
- σ EWMA from empty
- PTB tracker registration at window boundary

This can miss early `tau_band` entries and produce false `z_gap_sigma_not_ready` / feed stale skips.

### Phase B requirements

**Extend `run_continue.py`:**

- Support `strategy_kind == z_gap`
- Pass `--event-url` / metadata per window (reuse `resolve_window_metadata`)

**Scheduler lead-time assertions** (new preflight checks):

| Assertion | Requirement |
|-----------|-------------|
| RTDS connected | Before `event_start_ts - prestart_seconds` |
| Binance connected | Same |
| PTB tracker registered | Before `event_start_ts` |
| σ warm-up budget | `min_samples_s` elapsed before `tau_band` opens |
| Subprocess start time | `wake_at_ts` early enough for warm-up |

**Config (proposed):**

```yaml
run_continue:
  prestart_seconds: 90        # increase from paired_binary default if needed
  sigma_warmup_grace_s: 25
  require_feeds_ready_at_start: true
```

**Skip reasons:**

- `z_gap_warmup_missed` — σ not ready when tau_band opens
- `z_gap_feeds_not_ready_at_start` — subprocess started too late

**Document:** Full cold-start elimination → Phase C long-lived feed process.

### Multi-window session artifacts

```text
var/reporting/run_continue/<session>/session_manifest.json
var/reporting/run_continue/<session>/window_summaries.jsonl
```

---

## 9. Recovery

**`strategies/z_gap/recovery.py`** — on startup:

| State | Recovery action |
|-------|-----------------|
| `ENTRY_PENDING` | Reconcile order status; resume or reset |
| `ACTIVE` | Resume monitor; reload entry model snapshot |
| `EXIT_PENDING` | Resume exit dispatch |
| `HOLD_TO_RESOLUTION` / `SETTLEMENT_PENDING` | Resume settlement watcher |
| `exited_this_window` | Enforce no-reentry |
| Terminal | Reset for new window (run_continue) or exit |

Persist recovery facts: `z_gap_state_recovery_applied`.

Pattern: `paired_binary_recovery.py` — adapt for single-leg phases.

---

## 10. Reconciliation and Reporting

### B0.1 — Fixed-checkpoint calibration rollup

Cheap live calibration capture on **every window** (entered or skipped). Do not wait for Phase C to start collecting these fields.

**Terminal summary must include fixed-checkpoint pairs**, captured at nearest evaluation tick to each τ checkpoint:

```json
{
  "p_star_at_tau_120": "0.63",
  "z_at_tau_120": "0.33",
  "basis_bps_at_tau_120": "1.1",
  "sigma_at_tau_120": "0.00015",
  "p_star_at_tau_180": "0.71",
  "z_at_tau_180": "0.48",
  "p_star_at_tau_60": "0.55",
  "z_at_tau_60": "0.12",
  "resolved_outcome": "UP",
  "window_had_entry": false,
  "market_id": "btc_5m_20260709_1705"
}
```

**Implementation:**

- `strategies/z_gap/checkpoint_capture.py` — on each eval tick, if `|tau - checkpoint| <= capture_window_s`, persist snapshot
- Append row to `var/reporting/z_gap/checkpoint_rollups.jsonl` (per window + session rollup)
- Post-resolution: fill `resolved_outcome` from settlement watcher or Gamma poll

**Feeds Phase C C0.2** calibration/Brier without cold-starting research.

Also merge Phase A1 `calibration_samples.jsonl` into B0.1 reporting pipeline.

### Extend `scripts/reconcile_run_cashflows.py`

Single-leg + resolution:

| Cashflow type | Source |
|---------------|--------|
| BUY fill | OMS / WS trade |
| SELL exit fill | OMS / WS trade |
| Resolution payout | Wallet delta post-settlement |
| Fees | `fee_validation` + venue truth |

### Terminal summary extensions (full attribution)

```json
{
  "exit_type": "rich_exit | thesis_stop | lifecycle_flatten | resolution",
  "entry_edge": "0.062",
  "fill_edge": "0.041",
  "edge_decay": "0.021",
  "fee_predicted": "0.012",
  "fee_observed": "0.011",
  "resolution_pnl_usd": "0.00",
  "sell_exit_pnl_usd": "0.15",
  "basis_condition_at_entry": "fresh_chainlink_ok",
  "model_z_at_entry": "1.1",
  "model_z_at_exit": "0.4",
  "p_star_at_tau_120": "0.63",
  "resolved_outcome": "UP",
  "window_had_entry": true
}
```

### Basis gate improvement (B4)

**Prerequisite:** Phase A must emit **separate** skip codes:

- `z_gap_basis_exceeded_fresh_chainlink`
- `z_gap_chainlink_stale_basis_untrusted`

Phase B basis relaxation **requires** this separation to analyze whether skips were true divergence vs Chainlink lag. **Do not collapse codes in implementation.**

Beyond Phase A fail-closed behavior:

| Scenario | Phase B behavior |
|----------|------------------|
| Fresh Chainlink, basis exceeded | Reject — `z_gap_basis_exceeded_fresh_chainlink` |
| Stale Chainlink after Binance move | Reject on staleness — `z_gap_chainlink_stale_basis_untrusted`; do **not** treat basis as divergence signal |
| Binance-led signal | S drives z; S_CL fresh required at PTB boundary + periodic sanity (`chainlink_sanity_interval_s`) |

**Investigation:** Analyze Phase A1/A2 live basis facts by **distinct code**. If stale Chainlink skips dominate, apply relaxation per table. Do not weaken without fact evidence.

### Scripts

- `scripts/validate_z_gap_live_run.py` — Phase B validator
- `scripts/analyze_run_timeline.py` — full attribution columns

---

## 11. Tests

| Test | Coverage |
|------|----------|
| `test_z_gap_settlement_reconcile.py` | Resolution payout $1/share |
| `test_z_gap_redemption.py` | Redeem path mock |
| `test_z_gap_hold_policy.py` | Hold suppresses rich/flatten |
| `test_z_gap_rich_exit.py` | Unified fee-adjusted rich exit |
| `test_z_gap_basis_gate_v2.py` | Stale Chainlink ≠ basis reject |
| `test_z_gap_recovery.py` | ACTIVE / SETTLEMENT_PENDING resume |
| `test_run_continue_z_gap.py` | Subprocess spawn + metadata |
| `test_z_gap_scheduler_leadtime.py` | Warm-up assertion logic |
| `test_z_gap_checkpoint_capture.py` | Fixed τ checkpoints; `p_star_at_tau_120` in terminal summary |
| `test_z_gap_basis_skip_codes_distinct.py` | Regression: two basis codes never collapsed |
| Integration: held-to-resolution fixture | End-to-end settlement PnL |

---

## 12. Stable v0 Success Criteria

Phase B is complete when:

| # | Criterion |
|---|-----------|
| 1 | ≥5 consecutive `run_continue` windows without crash |
| 2 | Settlement/redeem reconciled on ≥1 held-to-resolution window |
| 3 | Sell-exit and resolution PnL attributed separately in reports |
| 4 | Recovery tested: kill mid-window → restart → correct phase |
| 5 | Fee predicted vs observed stable over ≥10 fills |
| 6 | Scheduler lead-time: σ ready before tau_band on ≥80% windows |
| 7 | No manual intervention required for normal windows |
| 8 | Kill switches verified (dry-run trigger) |

PnL may still be negative; criterion is **operational repeatability** and **attribution correctness**.

---

## 13. Risks

| Risk | Mitigation |
|------|------------|
| Redemption API unknown / undocumented | Spike in B0.3 before enabling hold |
| hold + resolution race | `SETTLEMENT_PENDING` phase; facts |
| run_continue cold-start misses entries | Lead-time assertions; document C solution |
| Rich exit fires on noise | theta tuning only after A ops pass; facts |
| Resolution payout mismatch | `z_gap_resolution_reconciled` fail alert |
| Basis relaxation too loose | Evidence-gated; compare skip rates |

---

## Milestones

| ID | Milestone | Deliverables |
|----|-----------|--------------|
| **B0.1** | Reconciliation / reporting + checkpoint rollup | `checkpoint_capture.py`, `checkpoint_rollups.jsonl`, `p_star_at_tau_*`, `resolved_outcome`, cashflow script, timeline analyzer |
| **B0.2** | Unified rich-exit logic | `exit_policy/rich_exit.py`, evaluator precedence |
| **B0.3** | Settlement + redemption + resolution reconcile | `settlement/*`, facts, tests — **hold blocker** |
| **B0.4** | Hold-to-resolution enablement | `hold_policy.py`, state phases, scenario flag |
| **B0.5** | `run_continue` + scheduler lead-time | `run_continue.py` z_gap support, preflight assertions |
| **B0.6** | Recovery | `recovery.py`, persist + tests |
| **B0.7** | Stable v0 integration tests | Multi-window smoke, held-to-resolution fixture |

**Order:** B0.1 → B0.2 → B0.3 → B0.4 → B0.5 → B0.6 → B0.7 (B0.3 before B0.4 is mandatory)

---

## Appendix — Quant feedback in Phase B

| Feedback | Section |
|----------|---------|
| Hold requires settlement/redeem | §5 |
| Hold instead of flatten | §6 |
| Merge edge-exit / rich-exit | §7 |
| Basis gate improvement | §10 |
| Phase A distinct basis codes prerequisite | §10 |
| Checkpoint calibration rollups | §10 B0.1 |
| Fee validation reporting | §10 |
| run_continue cold-start | §8 |
| Scheduler lead-time | §8 |
| z_neutral_timeout (optional) | §7 — defer C |

---

*End of Phase B specification. Requires Phase A operational pass.*
