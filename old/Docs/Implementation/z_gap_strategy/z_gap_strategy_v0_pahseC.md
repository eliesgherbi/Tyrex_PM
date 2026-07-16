# Z-Gap Strategy v0 — Phase C: Maturity, Calibration, and Scaling

**Status:** Implementation-ready (no code yet)  
**Date:** 2026-07-09  
**Parent reference:** [`z_gap_strategy_v0.md`](z_gap_strategy_v0.md)  
**Prerequisite:** [`z_gap_strategy_v0_pahseB.md`](z_gap_strategy_v0_pahseB.md) stable v0 pass

---

## 1. Goal

Phase C prepares Z-Gap for **serious capital readiness** and **research maturity**.

This phase answers:

- Does Z-Gap have a **real statistical edge** after friction?
- Can it **scale** beyond tiny live tests?
- Is the model **stable** out-of-sample?

**Do not mix Phase C with Phase A or B implementation.** Phase C begins only after stable v0 (Phase B §12) is satisfied.

---

## 2. Entry Criteria From Phase B

| Gate | Required |
|------|----------|
| ≥5 `run_continue` windows without crash | ✓ |
| Settlement/redeem reconciled on held window | ✓ |
| Sell vs resolution PnL attribution correct | ✓ |
| Recovery proven | ✓ |
| Fee model validated on ≥10 fills | ✓ |
| Scheduler warm-up acceptable | ✓ |
| Operator confidence in facts pipeline | ✓ |

---

## 3. Research Questions

Phase C must answer these with data, not intuition:

| # | Question |
|---|----------|
| R1 | Is `Φ(z)` calibrated vs realized outcomes by decile/regime? |
| R2 | Does model Brier beat PM mid after fees? |
| R3 | What is friction-adjusted expectancy per entry after latency + FAK rejects? |
| R4 | How much edge decays between signal and fill? |
| R5 | When does basis/lag help vs hurt? |
| R6 | What is optimal `theta_take`, `z_band`, `tau_band` under walk-forward constraints? |
| R7 | Does hold-to-resolution beat sell-exit by regime? |
| R8 | Is maker mode viable given adverse selection? |
| R9 | What sizing (fractional Kelly) maximizes growth subject to drawdown cap? |
| R10 | Does long-lived feed architecture materially improve entry rate vs subprocess cold-start? |

---

## 4. Calibration Plan

Move beyond Phase A calibration-lite.

### Calibration data sources (do not start cold)

Phase C C0.2 ingests **three** sources — accumulation begins in Phase A/B:

| # | Source | When collected | Path |
|---|--------|----------------|------|
| 1 | Historical recordings / normalized data lake | Pre-existing + ongoing record | `research/normalize/`, `var/recordings/` |
| 2 | Phase A `observe_only` live windows | A0.5+ | `var/reporting/z_gap/calibration_samples.jsonl` |
| 3 | Phase B fixed-checkpoint terminal rollups | B0.1+ | `var/reporting/z_gap/checkpoint_rollups.jsonl` |

Phase C must merge these into a single calibration dataset before walk-forward analysis.

### C0.2 — Full calibration

**Primary tables:** `reference_prices`, `btc_ticks`, `price_to_beat`, `best_bid_ask`, `markets`, `market_resolutions` plus live checkpoint rollups.

**Deliverables:**

| Artifact | Content |
|----------|---------|
| Reliability curves | Predicted p* decile → outcome frequency |
| Brier score | Model vs PM mid vs outcome |
| Regime breakdown | By z band, τ bucket, σ quartile, basis bucket |
| Live vs recording comparison | Drift between offline and observe_only samples |
| Reliability diagram plots | `research/output/z_gap/calibration/` |
| Out-of-sample split | Train days 1–N, validate N+1–M |
| Walk-forward | Rolling 7-day train, 1-day test |

**Gate for parameter changes:**

```text
No production parameter change without walk-forward improvement
and operator sign-off.
```

**Module:** `research/z_gap/calibration.py` (extends `calibration_lite.py`; reads checkpoint rollups)

---

## 5. Frictioned Backtest Plan

### C0.3 — Replay with friction

**Prerequisite:** M2B.5 replay engine or `research/z_gap/replay/`.

### Entry economics (reference)

Entry edge at decision:

```text
edge_entry = p_L - ask - φ(ask) - slippage_entry
```

Correct for entry decision and especially for **hold-to-resolution** paths (no exit-side cost if held to $1/$0 payout).

### Exit-side cost (required in C0.3)

If a position exits via **thesis stop** or **discretionary sell** (rich exit, lifecycle flatten), it pays exit-side friction:

```text
exit_cost = φ(bid) + spread_slippage_exit
```

**C0.3 must decompose full round-trip economics:**

| Component | Notes |
|-----------|-------|
| Entry fee | `φ(ask)` or `φ(fill_price)` |
| Entry slippage | Model vs actual fill |
| Exit fee | `φ(bid)` at exit |
| Exit slippage | Spread + FAK sweep |
| Resolution payout path | $1/$0 if held — no exit fee |
| Edge decay | Signal edge − fill edge |

### Probability-weighted expected exit cost

For each entry scenario, estimate:

```text
E[exit_cost] = P(thesis_stop) * cost_stop
             + P(rich_exit) * cost_rich
             + P(lifecycle_flatten) * cost_flatten
             + P(hold_to_resolution) * 0   # payout path, not sell friction
```

Use Phase B checkpoint rollups + labeled windows to estimate exit-path probabilities by regime (z, τ, basis).

**Do not add this complexity to Phase A entry enforcement** — document and simulate in C only.

### Simulate in replay

| Friction | Method |
|----------|--------|
| Real order books | Recorded `book_snapshot` / `book_delta` |
| Dynamic φ fees | `quant/fees.py` on entry and exit marks |
| Latency injection | Phase A/B measured decision→fill distribution |
| Depth constraints | `ExecutableBookView` at decision size |
| Partial fills | FAK fill model from recorded depth |
| FAK rejects | Historical reject rate |
| Model-capped limits | `max_fill_price_for_edge_floor(...)` — fee + slippage aware |
| Basis gate | Phase A/B rules with distinct skip codes |
| Exit path branching | Thesis / rich / flatten / hold per policy |

**Outputs per parameter grid point:**

- Expectancy (gross and net of entry + exit friction)
- Hit rate, CVaR-5%
- Entry count, skip histogram
- Mean edge decay
- **Fee drag vs slippage drag** — entry vs exit split
- Resolution vs sell-exit PnL mix

**Keystone test:** Replay one known live window → fact sequence matches live decisions (fills excepted).

---

## 6. Shadow Mode Plan

### C0.4 — Live shadow (no OMS submit)

**Config:**

```yaml
execution_mode: shadow
z_gap:
  entry_mode: shadow_evaluate   # compute + fact, no submit
```

**Log:**

- Hypothetical entries / exits
- Hypothetical model-capped fill price
- Observed edge decay vs real book moves
- Skip histogram vs live-enforce windows
- Calibration drift (rolling Brier on shadow decisions)

**Promotion ladder:**

```text
shadow → tiny live enforce → scaled live
```

Each step requires reconciled PnL + stable calibration metrics.

---

## 7. Advanced Model Upgrades

### C0.5 — Volatility and basis diagnostics

Optional upgrades (evaluate via backtest before live):

| Upgrade | Module | Purpose |
|---------|--------|---------|
| Jump-robust σ | `quant/volatility_robust.py` | Reduce σ collapse after spikes |
| HAR-lite | `quant/volatility_har.py` | Multi-horizon σ blend |
| Event-clock multiplier | `quant/volatility.py` | Scale σ as τ→0 |
| Market-implied vol | `quant/implied_vol.py` | PM mid vs digital BS inversion |
| Implied vs forecast diagnostic | facts | Detect when PM disagrees with σ |
| Basis-lag model | `exit_policy/basis_gate.py` | Binance-led entry when Chainlink lagging |
| `z_neutral_timeout` | `exit_policy/neutral_timeout.py` | Exit coin-flip positions |

**Feature builder integration:** Extend `market_data/features.py` or M2B.6 `FeatureVector` with `d_sigma`, `dislocation`, `basis_lag_ms` — parity test live vs replay.

---

## 8. Maker Mode

### C0.6 — Post-only / maker (only after taker understood)

**Scope:**

- Post-only order support in planner
- Maker fee / rebate in `quant/fees.py` (if applicable)
- Queue-position risk metrics
- Cancel/replace logic on signal move
- Adverse selection measurement (filled maker when z moved against)

**Gate:** Positive taker v0 expectancy in frictioned backtest over meaningful sample.

**Not required for Z-Gap v0 maturity** if taker edge is sufficient.

---

## 9. Sizing and Capital Scaling

### C0.7 — Dynamic sizing

**Only after:** calibration credible + reconciliation trustworthy.

| Mode | Description |
|------|-------------|
| `fixed_usd` | Phase A/B default |
| `fractional_kelly` | `f* = edge / variance` with haircut |
| Hard caps | `max_usd`, `max_participation`, `per_trade_max_notional` |
| Daily loss budget | `max_daily_loss_usd` |
| Drawdown kill | Session + rolling drawdown |

**Module:** `quant/sizing.py` — consumed by `strategies/z_gap/sizing.py`

**Risk engine:** Unchanged caps as final authority.

---

## 10. Long-Lived Feed Architecture

### Problem

Phase B documents `run_continue` subprocess cold-start: Binance/RTDS reconnect and σ reset every 5 minutes.

### C0.8 — Professional solution

**Architecture:**

```text
Long-lived feed daemon (parent or sidecar)
  ├── Binance WS persistent
  ├── RTDS WS persistent
  ├── σ state carry-over
  ├── PTB tracker pre-registered for next window
  └── IPC / shared memory / unix socket → SignalSnapshot

Per-window strategy subprocess (lightweight)
  ├── Reads latest SignalSnapshot
  ├── PM CLOB WS (per-market tokens)
  └── z_gap_run loop
```

**Components:**

| Component | Path |
|-----------|------|
| Feed daemon | `runtime/feed_daemon.py` (new) |
| Snapshot IPC | `state/signal_state_store.py` — shared backend |
| σ persistence | `var/state/sigma_carryover.json` |
| Scheduler integration | `run_continue` starts daemon once per session |

**Success metric:** Entry rate in first 30s of `tau_band` improves vs Phase B baseline; fewer `z_gap_sigma_not_ready` skips.

---

## 11. Serious-Capital Promotion Criteria

Promotion beyond tiny live requires **all**:

| # | Criterion |
|---|-----------|
| 1 | Reconciled live PnL over ≥50 windows (or 7+ days run_continue) |
| 2 | Latency p95 within budget (configurable, e.g. <500ms decision→submit) |
| 3 | Fee model: mean |predicted − observed| < threshold |
| 4 | Calibration stable walk-forward (Brier drift < ε) |
| 5 | No unresolved settlement/redeem issues in last 30 days |
| 6 | No manual intervention in normal windows |
| 7 | Kill switches verified in production drill |
| 8 | Frictioned backtest expectancy > 0 at promoted parameter set |
| 9 | Shadow mode 7-day drift check passed |
| 10 | Operator written go/no-go |

**Capital ladder (suggested):**

```text
$5 → $20 → $50 → $100 per trade
```

Each step requires promotion criteria re-evaluated.

---

## 12. Risks

| Risk | Mitigation |
|------|------------|
| Overfit from walk-forward mining | Holdout days; parameter bounds |
| Backtest ≠ live | Keystone replay test; shadow mode |
| Maker adverse selection | Separate metrics; late enable |
| Kelly oversizing | Fractional Kelly + hard caps |
| Feed daemon SPOF | Health checks; fallback to per-window ingest |
| Scale increases market impact | `max_participation` tighten |
| Model regime change | Rolling calibration monitor |

---

## Milestones

| ID | Milestone | Deliverables |
|----|-----------|--------------|
| **C0.1** | Z-Gap feature table | `research/z_gap/features.py`, normalized parquet partition |
| **C0.2** | Full calibration + Brier | `calibration.py`, reliability reports |
| **C0.3** | Frictioned backtest | `research/z_gap/replay/`, grid outputs |
| **C0.4** | Shadow mode | `entry_mode: shadow_evaluate`, drift dashboard |
| **C0.5** | Advanced vol / basis | Optional model modules + A/B in replay |
| **C0.6** | Maker mode | Planner + fee extensions |
| **C0.7** | Kelly / dynamic sizing | `quant/sizing.py` |
| **C0.8** | Long-lived feed process | `feed_daemon.py`, σ carry-over |
| **C0.9** | Serious-capital readiness review | Promotion checklist doc + operator sign-off |

**Typical order:** C0.1 → C0.2 → C0.3 → C0.4 → (C0.5 parallel) → C0.8 → C0.7 → C0.6 → C0.9

Maker (C0.6) and Kelly (C0.7) are last — only if taker edge confirmed.

---

## Appendix — Quant feedback in Phase C

| Feedback | Section |
|----------|---------|
| Full calibration | §4 |
| Calibration from A observe_only + B rollups | §4 |
| Frictioned backtest | §5 |
| Exit-side cost decomposition | §5 |
| Expected exit cost (thesis/sell paths) | §5 |
| Shadow mode | §6 |
| z_neutral_timeout | §7 |
| Basis-lag model | §7 |
| Long-lived feed / σ carry-over | §10 |
| run_continue cold-start (full fix) | §10 |
| Serious capital criteria | §11 |
| Kelly / maker | §8–9 |

---

## Appendix — Phase summary

```text
Phase A → observe_only model path first (A0.5), then tiny enforced window (A0.6–A0.8)
Phase B → stable repeatable v0 (settlement, hold, run_continue, checkpoint rollups)
Phase C → edge validation + scaling (calibration, backtest w/ exit costs, shadow, capital)
```

---

*End of Phase C specification. Do not start until Phase B stable v0 criteria met.*
