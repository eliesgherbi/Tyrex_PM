# Z-Gap Strategy v0 — Phase A: First Tiny Live Run

**Status:** Implementation-ready (no code yet)  
**Date:** 2026-07-09 (revised)  
**Parent reference:** [`z_gap_strategy_v0.md`](z_gap_strategy_v0.md)  
**Next phase:** [`z_gap_strategy_v0_pahseB.md`](z_gap_strategy_v0_pahseB.md)

---

## 1. Goal

Phase A is split into **two sub-stages**. The **first structural live target** is not enforced trading — it is:

```text
Live observe_only Z-Gap
```

meaning:

```text
live feeds + PTB + sigma + fair value + fee model + edge + facts
but no OMS submit
```

This proves the riskiest new surface — Binance + RTDS + PTB + model computation — **without** putting order-side code in the loop.

### Phase A1 — Observe-only live model path (first target)

Run live windows continuously in `observe_only`. Every window emits model/edge/skip facts and becomes a **free calibration sample**. No position state. No exits. No OMS.

### Phase A2 — Enforced tiny order path (only after A1)

Single-window (or few-window) tiny FAK path: model-capped BUY, activation, thesis stop, lifecycle flatten, kill switch.

Phase A success is **operational, not PnL**.

**Explicit rule:** Do not tune `theta_take`, `z_band`, or `tau_band` from five live windows. Phase A validates **operations**, not statistical edge.

---

## 2. Scope

### Phase A1 (observe_only) — in scope

| Area | In scope |
|------|----------|
| A0.0 external dependency spikes | ✓ |
| Strategy kind `z_gap` + minimal YAML | ✓ |
| PTB pre-attestation (before enforce) | ✓ |
| Clock sanity preflight | ✓ |
| Live `SignalStateStore` + Binance + RTDS + PTB | ✓ |
| EWMA σ + fair value `z`, `p_UP`, `p_DOWN` | ✓ |
| Calibration-lite on recordings | ✓ |
| Dynamic fee curve `φ(price)` | ✓ |
| Edge calculator with `φ`, slippage | ✓ |
| Full skip-reason catalog (evaluated, not submitted) | ✓ |
| Facts + terminal summary + calibration sample capture | ✓ |
| `entry_mode: observe_only` | ✓ |

**Not in A1:** OMS submit, position state, exits, sizing enforcement.

### Phase A2 (enforce) — in scope (after A1)

| Area | In scope |
|------|----------|
| Fee/slippage-aware model-capped FAK BUY | ✓ |
| Fixed USD sizing | ✓ |
| Pipeline / OMS / activation | ✓ |
| Minimal exits: thesis stop + lifecycle flatten + kill switch | ✓ |
| `fee_validation` on real fills | ✓ |

**Runtime pattern:** Dedicated loop (`z_gap_run.py`), mirroring `paired_binary_run.py`.

---

## 3. Out of Scope (Phase A)

Defer to Phase B or C — **do not build in Phase A:**

| Item | Phase |
|------|-------|
| Hold-to-resolution | B |
| Settlement / redeem / resolution PnL | B |
| Rich / edge discretionary exit | B |
| `run_continue` multi-window | B |
| Recovery after interrupted run | B |
| Advanced basis-lag model | B/C |
| `z_neutral_timeout` | C / late B |
| Kelly / dynamic sizing | C |
| Maker / post-only | C |
| Full frictioned backtest / shadow mode | C |
| Long-lived feed process | C |

Phase A2 **must flatten before resolution** — no settlement path required.

---

## 4. Architecture Mapping

### A1 — Observe-only pipeline

```text
[A0.0] Fee-curve spike + Binance reachability spike
     ↓
[Preflight] PTB attestation + clock_sanity_check
     ↓
[Background] Binance WS + RTDS WS + PM CLOB WS → SignalStateStore + MarketStateStore
     ↓
[Tick] σ → fair value → φ-based edge → entry gates (evaluate only)
     ↓
[Facts] model_state_snapshot, edge_evaluated, z_gap_entry_skip, terminal summary
     ↓
[Post-window] resolved_outcome when available → calibration sample row
```

**No arrow to OMS.**

### A2 — Enforced pipeline (after A1 pass)

```text
... same model path ...
     ↓
[Entry] max_fill_price_for_edge_floor → model-capped FAK BUY → pipeline → OMS
     ↓
[Activation] Allocation ledger → ACTIVE
     ↓
[Monitor] Thesis stop OR lifecycle flatten → FAK SELL
     ↓
[Facts] + fee_validation + operational metrics
```

### Reuse from existing codebase

| Stage | Existing module |
|-------|-----------------|
| PM books | `ingestion/market_ws_ingest.py`, `state/market_store.py` |
| Binance ingest | `ingestion/external_btc.py`, `venue/binance_data/` |
| RTDS ingest | `ingestion/reference_prices.py`, `venue/polymarket_rtds/` |
| PTB derivation | `ingestion/price_to_beat_tracker.py` |
| Metadata | `ingestion/market_discovery.py`, `paired_binary_metadata.py` pattern |
| Execution (A2 only) | `runtime/pipeline.py`, `execution/planner.py`, `execution/oms.py` |
| Facts | `reporting/facts.py`, `reporting/schema_v2.py` |

### New in Phase A

| Module | A1 | A2 | Generic? |
|--------|----|----|----------|
| `state/signal_state_store.py` | ✓ | ✓ | Yes |
| `runtime/signal_feed_runtime.py` | ✓ | ✓ | Yes |
| `runtime/btc_5m_metadata.py` | ✓ | ✓ | Yes |
| `quant/volatility.py` | ✓ | ✓ | Yes |
| `quant/binary_fair_value.py` | ✓ | ✓ | Yes |
| `quant/fees.py` | ✓ | ✓ | Yes |
| `quant/edge.py` | ✓ | ✓ | Yes |
| `quant/entry_cap.py` — `max_fill_price_for_edge_floor` | — | ✓ | Yes |
| `strategies/z_gap/*` | ✓ | ✓ | Z-Gap |
| `runtime/z_gap_run.py` | ✓ | ✓ | Z-Gap loop |
| `exit_policy/thesis_stop.py` | — | ✓ | Yes |
| `scripts/preflight_ptb_attestation.py` | ✓ | ✓ | Ops |
| `scripts/preflight_clock_sanity.py` | ✓ | ✓ | Ops |
| `scripts/preflight_binance_connectivity.py` | ✓ (A0.0) | ✓ | Ops |
| `research/z_gap/calibration_lite.py` | ✓ | ✓ | Research |

**Spike deliverable:** [`fee_curve_spike_notes.md`](fee_curve_spike_notes.md) (created in A0.0)

---

## 5. Required Components

### A0.0 — External dependency spikes (before A0.1)

Mandatory gate before implementation begins.

#### A0.0.a — Fee-curve availability

Confirm whether `MarketInfo.raw` or the CLOB market info endpoint exposes dynamic fee-curve parameters for `φ(price)`.

**Inspect:**

- `venue/polymarket/market_info.py` — `fee_rate_bps` only today
- `/clob-markets/<condition_id>` raw JSON in `MarketInfo.raw`
- `get_fee_rate_bps(token_id)` SDK path

**Deliverable:** `Docs/Implementation/z_gap_strategy/fee_curve_spike_notes.md`

Must answer:

| Question | Required answer |
|----------|-----------------|
| Does live market info expose the dynamic fee curve? | yes/no + evidence |
| What fields are available? | field names |
| Is the curve directly computable? | formula or blocker |
| If not, is only `fee_rate_bps` available? | yes/no |
| Fallback if only flat/effective fee? | documented path |
| Block `entry_mode: enforce` until empirical validation? | yes/no |

**Fallback path if real curve unavailable:**

```text
observe_only first
→ first tiny fills only for empirical fee validation
→ enforce only after predicted vs observed fee is trusted
```

#### A0.0.b — Binance WS reachability from trading host

Record mode on one machine ≠ live trading host path.

**Deliverable:** `scripts/preflight_binance_connectivity.py` result artifact:

- connectivity pass/fail,
- latency sample (ms),
- reconnect behavior notes,
- fallback decision if Binance blocked.

**Gate:** If Binance WS is not reachable from the live trading host, Phase A must **not** proceed to `entry_mode: enforce` until an approved substitute feed path is defined.

---

### A1. Config and wiring

- `STRATEGY_KIND_Z_GAP`, `ZGapStrategyConfig` with `entry_mode: observe_only | enforce`
- `config/strategies/z_gap.yaml`
- `config/scenarios/live_z_gap_observe.yaml` (A1 default)
- `config/scenarios/live_z_gap_tiny.yaml` (A2 enforce)
- Fail-closed if kind unknown

```yaml
z_gap:
  entry_mode: observe_only   # default until A1 + preflight pass
```

A2 constraints (when `entry_mode: enforce`):

```yaml
sizing:
  mode: fixed_usd
  max_usd: "5"
  min_shares: "5"
entry_order_style: FAK
one_position_per_window: true
no_reentry_after_exit: true
```

---

### A2. Metadata and PTB pre-validation (hard blocker)

**Policy:**

```text
No K        → no trade / no hypothetical entry in enforce
Uncertain K → no trade
K too late  → no trade
K inconsistent with reference beyond tolerance → no trade
```

PTB attestation runs **before** `entry_mode: enforce`. Not alongside first live order.

#### PTB tolerance (revised)

**Only:**

```text
PTB mismatch tolerance <= 0.5 bps
```

**Drop** the absolute-dollar alternative (`$2`). It is inconsistent with bps semantics ($2 on ~$100k BTC ≈ 0.2 bps, not 2 bps).

**Why 0.5 bps:** K error biases z directly:

```text
z_error ≈ K_error_bps * 1e-4 / (sigma * sqrt(tau))
```

With `sigma ≈ 1.5e-4 / sqrt(second)` and `tau = 120s`, 1 bps K error ≈ 0.06 z; 2 bps ≈ 0.12 z — too large vs `z_stop = 0.25`.

**Log on attestation:**

- `K_derived`, `K_reference`, `ptb_error_bps`, `reference_source`, `attestation_pass` / `attestation_fail`

**Attestation data rules:**

| Data source | Use |
|-------------|-----|
| `golden_day` fixtures | **Tests only** — not sufficient for enforce |
| Fresh live-recorded windows | **Required** before `entry_mode: enforce` |
| No fresh attestation | **Only `observe_only` allowed** |

**Pre-live script:** `scripts/preflight_ptb_attestation.py`

**Skip reasons:** `z_gap_ptb_missing`, `z_gap_ptb_unverified`, `z_gap_ptb_late`, `z_gap_ptb_mismatch`

---

### A2b. Clock sanity check (preflight)

`tau` uses local time vs `event_end_ts`. Wrong host clock breaks `z` and `tau_band`.

**Script:** `scripts/preflight_clock_sanity.py` (or section in `preflight_z_gap_live_scenario.py`)

Compare local UTC against trusted reference(s):

- Binance server/event time from WS or REST (if available),
- Polymarket/RTDS payload timestamp,
- system NTP status (if easy to inspect).

**Emit:**

- `local_time_utc`, `reference_time_utc`, `clock_drift_ms`, `pass` / `fail`

**Threshold:**

```text
abs(clock_drift_ms) > 500  =>  block entry_mode: enforce
```

`observe_only` may run with warning; enforce fails closed.

**Skip reason (enforce):** `z_gap_clock_drift_exceeded`

---

### A3. Live SignalStateStore and feeds

Same as prior spec — `SignalStateStore` + `signal_feed_runtime.py` for Binance, RTDS, PTB tracker; PM books via `MarketStateStore`.

---

### A4. EWMA sigma and fair-value model

Unchanged formulas; **σ units pinned:**

- `sigma_units: "per_sqrt_second"` when `sample_interval_s: 1`
- Golden tests in `tests/fixtures/z_gap/fair_value_golden.json`

---

### A5. Dynamic fee model

Per A0.0 spike outcome. Implement `quant/fees.py` with `phi_taker_fee(price, market_info)`.

**Enforce gate:** If curve unavailable per spike → `entry_mode: enforce` blocked; observe_only continues.

---

### A6. Edge calculator

```text
edge_UP   = p_UP   - ask_UP   - φ(ask_UP)   - expected_slippage_UP
edge_DOWN = p_DOWN - ask_DOWN - φ(ask_DOWN) - expected_slippage_DOWN
```

---

### A7. Model-capped FAK entry — fee and slippage aware (A2 only)

**Wrong (do not use):**

```text
limit_price = min(ask_seen, p_L - theta_fill_floor)
```

This ignores fee and slippage at the fill price.

**Correct constraint:** submitted limit must satisfy:

```text
p_L - limit_price - φ(limit_price) - expected_slippage >= theta_fill_floor
```

**Helper** (`quant/entry_cap.py`):

```python
def max_fill_price_for_edge_floor(
    p_L: Decimal,
    theta_fill_floor: Decimal,
    fee_model: FeeModel,
    expected_slippage: Decimal,
    tick_size: Decimal,
) -> Decimal:
    """Highest price still meeting post-fill edge floor after φ and slippage."""
```

Then:

```text
limit_price = min(ask_seen, max_fill_price_for_edge_floor(...))
```

Quantize **down** to tick so final submitted limit cannot violate the floor after rounding.

```yaml
entry:
  theta_take: "0.05"
  theta_fill_floor: "0.02"
```

**Tests must cover:**

1. Zero-fee case
2. Dynamic-fee case
3. Cap below ask → blocks entry (`z_gap_entry_blocked_model_cap`)
4. Quantization does not raise price above valid cap
5. Predicted post-fill edge at submitted limit is always `>= theta_fill_floor`

**Facts:** `ask_seen`, `p_L`, `theta_fill_floor`, `max_fill_price`, `phi_at_limit`, `expected_slippage`, `final_limit_price`, `predicted_edge_at_limit`

---

### A8. Entry gates and skip-reason catalog

Full catalog unchanged; **implementation hygiene:**

**Do not collapse** basis reject codes:

| Code | Meaning |
|------|---------|
| `z_gap_basis_exceeded_fresh_chainlink` | Chainlink fresh AND basis over limit |
| `z_gap_chainlink_stale_basis_untrusted` | Chainlink stale — basis not trusted |

**Never** map both to generic `basis_failed` or `feed_stale`.

Phase B basis relaxation **depends** on this separation (see Phase B doc).

**Test assertion:** `test_z_gap_entry_eval.py` must prove both codes emit distinctly under controlled inputs.

In `observe_only`, gates still run; skips emit facts; no OMS.

---

### A9. Basis gate nuance

Same as prior — fresh Chainlink → strict basis; stale Chainlink → reject on staleness, not basis divergence. Advanced treatment → Phase B.

---

### A10. Minimal exits (A2 only)

Thesis stop, lifecycle flatten, kill switch. Flatten before resolution. Not in A1.

---

### A11. Calibration-lite + observe-only samples

**Calibration-lite** (`research/z_gap/calibration_lite.py`) on recordings — gates enforce if obviously broken.

**Observe-only live windows (A1):** Every window appends a calibration sample row:

```json
{
  "market_id": "btc_5m_...",
  "entry_mode": "observe_only",
  "would_have_entered": false,
  "skip_reason_top": "z_gap_tau_out_of_band",
  "p_up_at_decision": "0.63",
  "z_at_decision": "0.33",
  "edge_up": "0.04",
  "edge_down": "-0.02",
  "ptb_error_bps": "0.1",
  "resolved_outcome": "UP"
}
```

Stored in `var/reporting/z_gap/calibration_samples.jsonl` — feeds Phase B/C calibration.

---

## 6. Correctness Fixes Applied

| Issue | Phase A fix |
|-------|-------------|
| Flat fee bps | Dynamic `φ(price)`; A0.0 spike first |
| Book-only / naive model cap | `max_fill_price_for_edge_floor` with φ + slippage |
| PTB 2 bps / $2 tolerance | **0.5 bps only** |
| observe_only as afterthought | **A1 structural first target** |
| PTB alongside first live | Pre-attestation + fresh recordings for enforce |
| No calibration | Calibration-lite + observe_only samples |
| Basis codes collapsed | **Two distinct codes** — tested |
| Clock drift | `clock_sanity_check` preflight |
| Sigma units | Pinned per √second + golden tests |

---

## 7. Milestones

### Implementation order

```text
A0.0 — External dependency spikes
A0.1 — Config + metadata + PTB preflight + clock sanity
A0.2 — SignalStateStore + live Binance/RTDS/PTB
A0.3 — EWMA sigma + fair value + calibration-lite
A0.4 — Dynamic fee curve + edge calculator
A0.5 — Observe-only live run mode + calibration sample capture   ← first live target
A0.6 — Model-capped FAK BUY + fixed sizing                       ← after A0.5
A0.7 — State activation + minimal exits
A0.8 — Enforced tiny live checklist
```

**Hard gates:**

- **A0.0 before A0.1**
- **A0.5 before A0.6 / A0.7 / A0.8**
- **Do not start A0.6** until observe-only live windows produce usable facts
- **`entry_mode: enforce`** only after: A0.5 pass + fresh PTB attestation + clock sanity + fee spike path clear

| ID | Sub-stage | Deliverables |
|----|-----------|--------------|
| **A0.0** | — | `fee_curve_spike_notes.md`, `fee_curve_spike.json`, `preflight_binance_connectivity.py`, `binance_connectivity.json` |
| **A0.1** | A1 | `STRATEGY_KIND_Z_GAP`, YAMLs, `btc_5m_metadata.py`, PTB preflight, clock sanity |
| **A0.2** | A1 | `signal_state_store.py`, `signal_feed_runtime.py`, feed health facts |
| **A0.3** | A1 | `quant/volatility.py`, `quant/binary_fair_value.py`, golden tests, `calibration_lite.py` |
| **A0.4** | A1 | `quant/fees.py`, `quant/edge.py`, `fee_model_resolved` fact |
| **A0.5** | **A1** | `entry_mode: observe_only`, `z_gap_run.py` eval loop, skip facts, `calibration_samples.jsonl`, terminal summary |
| **A0.6** | A2 | `quant/entry_cap.py`, model-capped FAK, `sizing.py` |
| **A0.7** | A2 | `state.py`, activation, `thesis_stop`, lifecycle flatten |
| **A0.8** | A2 | Enforced checklist, `fee_validation`, `preflight_z_gap_live_scenario.py` |

---

## 8. Tests

| Test file | Coverage |
|-----------|----------|
| `test_signal_state_store.py` | Freshness, basis, PTB |
| `test_ewma_volatility.py` | σ units, interval scaling |
| `test_binary_fair_value.py` | Golden vectors |
| `test_fees_phi.py` | Dynamic curve; zero-fee fallback |
| `test_edge_calculator.py` | φ + slippage |
| `test_max_fill_price_for_edge_floor.py` | **5 cases:** zero-fee, dynamic-fee, cap below ask, quantize safety, edge floor invariant |
| `test_z_gap_entry_eval.py` | All skip reasons; **distinct** `basis_exceeded_fresh_chainlink` vs `chainlink_stale_basis_untrusted` |
| `test_z_gap_observe_only_runtime.py` | No OMS calls; facts emitted |
| `test_z_gap_thesis_stop.py` | A2 only |
| `test_calibration_lite.py` | Recording fixture report |
| `test_clock_sanity.py` | Drift threshold |

---

## 9. Facts and Reports

### A1 facts (observe_only)

| Fact | Purpose |
|------|---------|
| `signal_feed_health` | Feed freshness |
| `basis_computed` | S, S_CL, basis_bps, `chainlink_fresh` |
| `model_state_snapshot` | z, p*, σ, units |
| `edge_evaluated` | edges, φ, slippage |
| `fee_model_resolved` | curve id |
| `z_gap_entry_eval` / `z_gap_entry_skip` | Hypothetical entry decision |
| `z_gap_terminal_summary` | Operational rollup |
| `clock_sanity` | Preflight drift |
| `calibration_sample` | Per-window row for rollup |

### A2 additional facts

| Fact | Purpose |
|------|---------|
| `z_gap_entry_submitted` | Model cap evidence |
| `z_gap_position_activated` | Fill state |
| `fee_validation` | Predicted vs observed |
| `model_exit_triggered` | Thesis / flatten |
| `latency_chain` | decision→submit→fill |

### Terminal summary (observe_only example)

```json
{
  "phase": "A1",
  "entry_mode": "observe_only",
  "ptb_observed": true,
  "ptb_attestation_passed": true,
  "ptb_error_bps": "0.2",
  "feed_uptime_pct": 99.5,
  "sigma_ready_s": 21,
  "skip_reason_histogram": {"z_gap_tau_out_of_band": 412},
  "calibration_sample_written": true,
  "resolved_outcome": "UP",
  "operational_pass": true
}
```

---

## 10. Pre-Live Checklist

### Before A1 (observe_only)

- [ ] **A0.0 complete:** `fee_curve_spike_notes.md` + Binance connectivity from trading host
- [ ] A0.1–A0.4 tests green
- [ ] `preflight_clock_sanity.py` pass (or warn-only for observe)
- [ ] Scenario `live_z_gap_observe.yaml` reviewed

### Before A2 (enforce)

- [ ] **A0.5:** ≥3 observe_only live windows with usable facts
- [ ] `preflight_ptb_attestation.py` on **fresh** recordings (not golden_day alone)
- [ ] PTB error ≤ **0.5 bps** on all attested windows
- [ ] `calibration_lite` reviewed; operator sign-off
- [ ] `clock_sanity`: `abs(drift_ms) <= 500`
- [ ] Fee spike path: curve available OR empirical validation plan acknowledged
- [ ] `live_attest` green
- [ ] `live_z_gap_tiny.yaml`: `max_usd: 5`

---

## 11. First Live Validation Checklist

### A1 — Observe-only (required before A2)

- [ ] Continuous or repeated windows in `observe_only`
- [ ] Model snapshots every evaluation tick (deduped)
- [ ] Skip reasons always populated
- [ ] `calibration_samples.jsonl` growing
- [ ] `resolved_outcome` filled post-resolution when available
- [ ] No `oms_submit` facts

### A2 — Enforced tiny window

- [ ] All A1 checks historically passed
- [ ] Model cap facts on any submit attempt
- [ ] `fee_validation` after fill
- [ ] Thesis stop or flatten; no hold through resolution
- [ ] `operational_pass: true`

---

## 12. Operational Success Criteria

### A1 success (observe_only)

| # | Criterion |
|---|-----------|
| 1 | Feeds live with freshness facts |
| 2 | K observed; `ptb_error_bps` logged when reference available |
| 3 | z, p*, edge computed each eval window |
| 4 | Every hypothetical entry has skip/pass reason |
| 5 | Basis rejects use **correct distinct code** |
| 6 | Calibration sample per window |
| 7 | No OMS submit |
| 8 | Terminal summary complete |

### A2 success (enforce)

| # | Criterion |
|---|-----------|
| 1 | All A1 criteria historically met |
| 2 | PTB attested ≤0.5 bps on fresh windows |
| 3 | Clock drift ≤500ms |
| 4 | Model cap: post-fill edge ≥ theta_fill_floor |
| 5 | Activation + minimal exit proven |
| 6 | `fee_validation` within tolerance |

**Not a success metric:** positive PnL on five windows.

---

## 13. Risks

| Risk | Mitigation |
|------|------------|
| Fee curve unavailable | A0.0 spike; observe_only → empirical validation path |
| Binance blocked on live host | A0.0.b; no enforce without substitute |
| PTB 0.5 bps too strict | Operator review; never widen without quant sign-off |
| Clock drift | Preflight block enforce |
| Basis codes collapsed | Explicit test; blocks Phase B analysis |
| Skipping A1 → enforce | Milestone gate A0.5 before A0.6 |
| Wrong model cap | `max_fill_price_for_edge_floor` tests |

---

## Appendix — Quant feedback in Phase A

| Feedback | Section |
|----------|---------|
| A0.0 fee + Binance spikes | §5 A0.0 |
| observe_only first | §1, §7 A0.5 |
| PTB 0.5 bps | §5 A2 |
| Fee/slippage model cap | §5 A7 |
| Clock sanity | §5 A2b |
| Distinct basis codes | §5 A8 |
| Calibration samples | §5 A11 |
| Operational not PnL | §12 |

---

*End of Phase A specification. Begin with **A0.0 — External dependency spikes**, then A0.1. Do not start A0.6 until A0.5 observe_only live windows produce usable facts.*
