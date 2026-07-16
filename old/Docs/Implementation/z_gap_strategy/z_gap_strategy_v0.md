# Z-Gap Strategy v0 — Live-First Implementation Proposal

**Status:** Broad reference specification (no code yet)  
**Date:** 2026-07-09  
**Audience:** Implementation agent, operator review before live tiny-size runs

### Implementation split (active documents)

This file remains the **broad architecture reference**. Implementation is split into three focused phases:

| Phase | Document | Goal |
|-------|----------|------|
| **A** | [`z_gap_strategy_v0_pahseA.md`](z_gap_strategy_v0_pahseA.md) | **Observe-only live first** (A0.5), then tiny enforced window |
| **B** | [`z_gap_strategy_v0_pahseB.md`](z_gap_strategy_v0_pahseB.md) | Stable v0 after Phase A — settlement, hold, run_continue |
| **C** | [`z_gap_strategy_v0_pahseC.md`](z_gap_strategy_v0_pahseC.md) | Maturity — calibration, backtest, shadow, scaling |

**Start implementation with Phase A only.** Do not implement Phase B/C scope in Phase A milestones.

### Quant feedback → phase mapping

| Quant feedback | Phase |
|--------------|-------|
| Real dynamic fee curve `φ(price)`, not flat bps | **A** |
| Fee validation against observed fill | **A** first live validation; **B** reporting |
| Model-capped FAK limit (`max_fill_price_for_edge_floor`) | **A** |
| PTB tolerance <= 0.5 bps (no $ absolute) | **A** |
| observe_only as structural first live target | **A** (A0.5) |
| A0.0 fee-curve + Binance connectivity spikes | **A** |
| Clock sanity preflight | **A** |
| Distinct basis reject codes (no collapse) | **A** |
| Phase B checkpoint calibration rollups | **B** |
| PTB comparison **before** first live entry | **A** |
| Calibration-lite reliability/Brier check | **A** |
| Operational success criteria, not PnL | **A** |
| Basis gate nuance: lag vs divergence | **A** note; **B** improvement |
| Correct/merge edge-exit/rich-exit logic | **B** |
| `z_neutral_timeout` | **C** or late **B** optional |
| Hold-to-resolution requires settlement/redeem/reconcile | **B** |
| Hold instead of flatten when z strong near close | **B**, after settlement support |
| Sigma unit correctness tests | **A** |
| `run_continue` cold-start / reconnect issue | **B** note; **C** long-lived feed |
| Scheduler lead-time assertion | **B** |
| Long-lived feed process / sigma carry-over | **C** |

---

## 1. Executive Summary

### What Z-Gap is

Z-Gap is a **single-leg directional** strategy for Polymarket BTC 5-minute binary markets. For each window it asks: *will BTC finish at or above the official price-to-beat (K)?* It compares a fast BTC reference price (Binance `S`) and the Polymarket RTDS Chainlink price (`S_CL`) against `K`, estimates short-horizon volatility `σ`, and computes a digital fair value:

```text
z     = ln(S / K) / (σ * sqrt(τ))
p_UP  = Φ(z)
p_DOWN = 1 - p_UP
```

It enters **only one leg** (UP or DOWN) when the model edge after fees, slippage, and latency risk exceeds a threshold and all quality gates pass. Exits are **model-based** (thesis reversal, rich bid vs model, hold-to-resolution), not entry-price stop/TP.

### Why it is the next step after the pair strategy

`paired_binary` validated the execution spine: WS-primary books, FAK orders, allocation ledger, position activation, survival enforce path, `run_continue`, facts, reconciliation, and run reports. Strategically, buying both legs forced the bot to overcome structural friction before any survivor management could help.

Z-Gap removes that handicap: one taker entry only when the model says Polymarket is mispriced enough. The infrastructure built for paired-binary maps directly; the **decision layer** changes from pair-cost gates to model/edge gates.

### What live-first v0 means

We explicitly **do not** gate v0 on a long research phase (calibration notebooks, frictioned replay, shadow mode). v0 ships a **safe, minimal, inspectable live integration** at tiny fixed size to learn:

- execution behavior and fill quality,
- Binance + RTDS + PTB integration under live latency,
- signal/model computation stability,
- model-based exit behavior,
- post-run attribution via facts.

Research maturity (M2B.4/M2B.5 replay, feature builder, parameter resolver) is **deferred** but the v0 design places reusable modules where those tools will plug in later.

### Explicitly out of scope for v0

| Out of scope | Notes |
|--------------|-------|
| Kelly / dynamic sizing | Fixed USD only; document hook point only |
| Maker / post-only | Taker FAK only |
| Dual-leg entry | One position per window |
| Re-entry after exit in same window | Hard policy + state machine |
| RTDS-Binance topic | Direct Binance WS only (already implemented for record) |
| Gamma `priceToBeat` post-settlement backfill | Use derived PTB from Chainlink at window start |
| Full M2B.5 replay / shadow validation gate | Post-v0 maturity path |
| `target_policy` / `stall_exit` / `reachability` survival modules | Paired-binary legacy; not used |
| GTC entry or managed-rest entry | FAK taker entry |
| Multi-market concurrent positions | One active window position |
| Serious capital / profit optimization | Safety + observability first |

---

## 2. Current Project State Review

### 2.1 Existing relevant modules

#### Strategy abstractions

| Module | Path | Relevance |
|--------|------|-----------|
| Generic contract | `src/tyrex_pm/strategies/base.py` — `Strategy`, `StrategyContext`, `StrategyResult` | Guru/harness path; Z-Gap will **not** use `on_signal` as primary loop |
| Production loop pattern | `src/tyrex_pm/runtime/paired_binary_run.py` | **Primary template** for Z-Gap dedicated loop |
| Entry evaluation pattern | `src/tyrex_pm/strategies/paired_binary/entry_eval.py` — `read_leg_book`, `evaluate_entry` | Template for pure `evaluate_entry` + book reads |
| State machine + persistence | `src/tyrex_pm/strategies/paired_binary/state.py` | Template for phases, disk persist, recovery |
| Monitor / exit emission | `src/tyrex_pm/strategies/paired_binary/monitor.py` | Emits `IntentWorkUnit`, never calls OMS directly |
| Facts with dedup | `src/tyrex_pm/strategies/paired_binary/facts.py` | Pattern for material decision facts |
| Sizing | `src/tyrex_pm/strategies/paired_binary/sizing.py` | Exit clamp via allocation ledger |
| Config kinds | `src/tyrex_pm/runtime/config.py` — `STRATEGY_KIND_*`, `AppConfig` | Add `z_gap` kind + config block |

**Wired strategy kinds today:** `guru_follow`, `sell_test`, `allocation_test`, `tp_sl_test`, `simple_signal_test`, `validation_harness`, `paired_binary` (`runtime/app.py`, `runtime/run_once.py`).

#### Runtime / run loop

| Module | Path | Role |
|--------|------|------|
| CLI entry | `src/tyrex_pm/runtime/app.py` | `run`, `run_continue`, `record` |
| Single window | `src/tyrex_pm/runtime/run_once.py` — `execute_run()` | Instantiates strategy, starts background tasks, dispatches main loop |
| Multi-window | `src/tyrex_pm/runtime/run_continue.py` | BTC 5m scheduler + per-window `execute_run()` subprocess |
| Pipeline spine | `src/tyrex_pm/runtime/pipeline.py` — `process_intent_work_unit()` | Intent → risk → planner → OMS |
| Coordinator | `src/tyrex_pm/runtime/coordinator.py` — `RuntimeCoordinator` | `market_state`, `allocation_ledger`, `market_info_cache` |
| Market data bootstrap | `src/tyrex_pm/runtime/market_data_runtime.py` | `MarketStateStore` setup, WS bootstrap |
| WS event wake | `src/tyrex_pm/runtime/market_update_coordinator.py` | Debounced ticks on book updates |
| Metadata resolution | `src/tyrex_pm/runtime/paired_binary_metadata.py` | Gamma `--event-url` → token IDs, window bounds |
| Lifecycle | `src/tyrex_pm/runtime/strategy_lifecycle.py` — `MarketLifecycleGuard`, `StrategyRuntimePolicy` | Near-close entry block, flatten-before-close |
| Record mode (reference) | `src/tyrex_pm/runtime/record_run.py` | Shows how to wire Binance + RTDS background tasks |

#### Market data ingestion

| Capability | Path | Live trading today? |
|------------|------|---------------------|
| CLOB WS books | `ingestion/market_ws_ingest.py`, `ingestion/market_stream.py`, `ingestion/sequencer.py` | **Yes** — WS-primary in live paired-binary |
| Binance BTC | `ingestion/external_btc.py`, `venue/binance_data/` | **Record only** — wired in `record_run.py` |
| RTDS Chainlink | `ingestion/reference_prices.py`, `venue/polymarket_rtds/` | **Record only** |
| Price-to-beat | `ingestion/price_to_beat_tracker.py` | **Record only** — derives K from Chainlink at `event_start_ts` |
| Market discovery | `ingestion/market_discovery.py` — `discover_btc_5m_by_slug()` | **Yes** — `run_continue` |
| Window scheduler | `ingestion/btc_5m_window_scheduler.py` | **Yes** — `run_continue` |
| Book store | `state/market_store.py` — `MarketStateStore` | **Yes** |
| Quality gate | `market_data/quality.py` — `DataQualityGate`, `DecisionContext` | **Yes** — entry/stop/urgent_exit contexts |
| Executable depth | `market_data/executable_book.py` | **Yes** — planner + survival |
| Feature builder (book-only) | `market_data/features.py` — `FeatureBuilder` | Reporting only; no external prices yet |

#### Execution / OMS

| Module | Path | Z-Gap reuse |
|--------|------|-------------|
| OMS | `execution/oms.py` — `SingleWriterOMS` | Full reuse |
| Live adapter | `execution/live_oms.py` | Full reuse |
| Planner | `execution/planner.py` — `ExecutionPlanner` | FAK at book worst price for urgent exits; GTC/FAK entry per intent |
| Fill reconciliation | `execution/fill_reconciliation.py` | Extend for single-leg PnL attribution |
| Order builder | `execution/order_builder.py` | Tick quantization |

#### Allocation / position activation

| Module | Path | Z-Gap reuse |
|--------|------|-------------|
| Ledger | `state/allocation_ledger.py` | Per-owner BUY credit on fill, SELL reservation |
| Runtime hooks | `runtime/allocation_runtime.py` | `maybe_apply_allocation_buy`, `maybe_reserve_exit_allocation` |
| Exit lifecycle | `runtime/allocation_exit_lifecycle.py` | Release reservation on fill/reject |
| Entry fill lifecycle | `state/entry_fill_lifecycle.py` | Order vs fill vs owned qty |

Z-Gap v0 simplifies activation: **one leg filled → ACTIVE** (no pair saga, no activation recheck against pair loss budget). Optional post-fill book freshness recheck mirrors `activation_flow.py` pattern.

#### Risk

| Module | Path | Z-Gap reuse |
|--------|------|-------------|
| Engine | `risk/engine.py` — `evaluate_intent()` | Full reuse |
| Planned order validation | `risk/planned_order.py` | Full reuse |
| Venue min size | `risk/venue_min_size.py` + `venue/polymarket/market_info.py` | Fee + min size from `MarketInfoCache` |
| Kill switch (operator) | `risk/kill_switch.py` | Full reuse |

#### Survival / exit machinery (paired-binary oriented)

| Module | Path | Z-Gap v0 use |
|--------|------|--------------|
| Advisory orchestrator | `survival/advisory.py` | **Not reused as-is** — survivor-leg / entry-price oriented |
| Hard floor / trailing | `survival/survivor_floor.py`, `survival/trailing_stop.py` | **Not used** — entry-price triggers |
| Economics | `survival/economics.py` | **Pattern reuse** — fee/slippage net evaluation |
| Enforcement dispatch | `survival/enforcement_dispatch.py` | **Pattern reuse** — FAK exit dispatch, but coupled to `PairedBinaryRuntimeState` |
| Order policy | `survival/order_policy.py` | **Reuse** — FAK retry repricing |
| Kill switches | `survival/kill_switches.py` — `KillSwitchManager` | **Extend** — add trade-count / consecutive-loss counters |
| Exit planning | `survival/exit_planning.py` — `SurvivalExitPlanner` | Reuse quality context for exits |

Current exits are **entry-price or pair-cashflow oriented** for trigger detection; order submission is book-oriented (FAK at executable worst price). Z-Gap needs a **generic model-state exit evaluator** decoupled from survivor entry price.

#### Reporting / facts

| Module | Path | Role |
|--------|------|------|
| Fact envelope | `reporting/facts.py` — `make_fact()` | Reuse |
| Schema | `reporting/schema_v2.py` — ~100 `FACT_TYPE_*` | Extend with Z-Gap + generic model facts |
| Facts sink | `reporting/sinks/jsonl.py` | Reuse |
| Summarize | `reporting/summarize.py` | Extend operator view |
| Decision evidence | `FACT_TYPE_DECISION_SNAPSHOT`, `FACT_TYPE_LATENCY_CHAIN` | Reuse for entry/exit decisions |
| Docs | `Docs/reporting_fact_model.md` | Update after fact types land |

#### Tests / fixtures

| Area | Paths |
|------|-------|
| Paired-binary runtime | `tests/test_paired_binary_*.py` (30+ files) |
| WS / ingest | `tests/test_market_ws_ingest.py`, `tests/test_ws_primary_*.py` |
| External BTC | `tests/test_external_btc_events.py` |
| RTDS / PTB | `tests/test_reference_price_events.py` |
| Discovery / scheduler | `tests/test_market_discovery.py`, `tests/test_btc_5m_window_scheduler.py` |
| Survival | `tests/test_survival_*.py`, `tests/test_survivor_*.py` |
| Golden recordings | `tests/fixtures/recordings/golden_day/` — PM books + Binance + Chainlink |
| Analysis scripts | `scripts/analyze_run_timeline.py`, `scripts/reconcile_run_cashflows.py` |

#### Research (future calibration, not v0 blocker)

| Module | Path | Notes |
|--------|------|-------|
| M2B.4 pipeline | `research/m2b4/pipeline.py` | `btc_short_vol`, lead-lag — informs later σ calibration |
| Normalized tables | `research/normalize/tables/reference_prices.py`, `btc_ticks.py` | Offline PTB/Binance/Chainlink joins |
| Phase 2B plan | `Docs/Implementation/Phase 2 completion/tyrex_pm_phase2b_plan_rev2.md` | Defines `d_sigma` feature vector for M2B.6 — aligns with Z-Gap `z` |

### 2.2 Architecture spine (unchanged)

Z-Gap v0 stays on the official spine documented in `Docs/Architecture.md`:

```text
Tick (books + signal state) → Strategy decision → Intent → RiskEngine → ExecutionPlanner → OMS → Venue
```

Every decision emits facts to `var/reporting/runs/<run_id>/facts.jsonl`.

### 2.3 Gap summary: record vs live

```text
RECORD MODE (record_run.py)          LIVE MODE (run_once.py) today
├── market_ws_ingest                 ├── market_ws_ingest  ✓
├── external_btc_ingest              ├── (missing)
├── reference_prices_ingest          ├── (missing)
└── price_to_beat_tracker          └── (missing)
```

**Primary v0 engineering work:** promote external feeds from record-only to a live **`SignalStateStore`** on `RuntimeCoordinator`, without coupling ingest loops to OMS.

---

## 3. Strategy-to-Architecture Mapping

Full pipeline with concrete module assignments:

```text
Data Sources
  → Signal State
  → Fair-Value Model
  → Edge Calculation
  → Entry Decision
  → Risk / Quality Gates
  → Sizing
  → Execution Intent
  → OMS / FAK Order
  → Position Activation
  → Model-Based Exit Rules
  → Exit Execution
  → Reconciliation
  → Facts / Reports
```

### 3.1 Data source layer

| Need | Reuse | Extension | Location | Generic? |
|------|-------|-----------|----------|----------|
| Polymarket CLOB books | `run_market_ws_ingest`, `MarketStateStore` | Subscribe UP/DOWN token IDs from metadata | Existing | Yes |
| Binance BTC `S` | `run_external_btc_ingest`, `venue/binance_data/` | Live wiring + `SignalStateStore` update callback | `runtime/signal_feed_runtime.py` (new) | Yes |
| RTDS Chainlink `S_CL` | `run_reference_prices_ingest`, `venue/polymarket_rtds/` | Live wiring | Same | Yes |
| Price-to-beat `K` | `PriceToBeatTracker` | Register window on metadata resolve; expose `K` to store | `ingestion/price_to_beat_tracker.py` (extend) + store adapter | Yes |
| Window / token mapping | `discover_btc_5m_by_slug`, `paired_binary_metadata` | Generalize to `btc_5m_metadata.py` | `runtime/btc_5m_metadata.py` (new) | Yes |
| Timestamps / freshness | `MarketEvent` recv_ts / source_ts | Per-feed staleness policy | `state/signal_state_store.py` (new) | Yes |

**Tests:** `test_signal_state_store.py`, extend `test_reference_price_events.py`, `test_external_btc_events.py` with live-runtime wiring smoke.

**Facts:** `signal_feed_tick`, `signal_feed_stale`, `price_to_beat_observed` (live fact mirror of event), `basis_computed`.

### 3.2 Signal state layer

| Component | Proposed module | Generic? |
|-----------|-----------------|----------|
| `SignalStateStore` | `src/tyrex_pm/state/signal_state_store.py` | **Yes** |
| Feed health aggregator | `src/tyrex_pm/market_data/signal_feed_health.py` | **Yes** |
| Coordinator field | `RuntimeCoordinator.signal_state` | Yes |

**`SignalStateStore` snapshot (per tick):**

```python
@dataclass(frozen=True)
class SignalSnapshot:
    binance_price: Decimal | None      # S
    binance_source_ts: datetime | None
    binance_recv_ts: datetime | None
    chainlink_price: Decimal | None    # S_CL
    chainlink_source_ts: datetime | None
    chainlink_recv_ts: datetime | None
    price_to_beat: Decimal | None      # K
    ptb_status: str                    # pending | observed | missing
    ptb_lag_ms: float | None
    basis_bps: Decimal | None          # ln(S/S_CL)*10000 or (S-S_CL)/S_CL*10000
    tau_s: float | None                # seconds to event_end_ts
    feed_reject_reason: str | None
```

Updated by background ingest tasks; read synchronously by strategy loop (no async in decision path).

### 3.3 Fair-value model layer

| Component | Proposed module | Generic? |
|-----------|-----------------|----------|
| EWMA σ on log returns | `src/tyrex_pm/quant/volatility.py` — `EwmaVolatilityEstimator` | **Yes** |
| Digital fair value | `src/tyrex_pm/quant/binary_fair_value.py` — `compute_z`, `compute_digital_probs` | **Yes** |
| Model output dataclass | `src/tyrex_pm/quant/models.py` — `FairValueSnapshot` | **Yes** |

**Formulas (v0):**

```text
z      = ln(S / K) / (σ * sqrt(τ))     # τ in seconds; use τ_min floor (e.g. 1s) to avoid div-by-zero
p_UP   = Φ(z)                          # standard normal CDF
p_DOWN = 1 - p_UP
```

Use `S` (Binance) as primary spot; `S_CL` for basis gate only in v0.

**Tests:** `tests/test_ewma_volatility.py`, `tests/test_binary_fair_value.py` — golden vectors for known S, K, σ, τ.

**Facts:** `model_state_snapshot` (generic) with z, σ, p_UP, p_DOWN, τ, estimator status.

### 3.4 Edge calculation layer

| Component | Proposed module | Generic? |
|-----------|-----------------|----------|
| Fee estimate | `src/tyrex_pm/quant/fees.py` — wraps `MarketInfoCache.get_fee_rate_bps` | **Yes** |
| Slippage estimate | `src/tyrex_pm/quant/slippage.py` — tick-based + optional depth from `ExecutableBookView` | **Yes** |
| Edge calculator | `src/tyrex_pm/quant/edge.py` — `compute_edge_up/down` | **Yes** |

**Edge (v0):**

```text
fee(px) = px * fee_rate_bps / 10000        # from venue market_info per token
edge_UP   = p_UP   - ask_UP   - fee(ask_UP)   - slippage_UP
edge_DOWN = p_DOWN - ask_DOWN - fee(ask_DOWN) - slippage_DOWN
```

Slippage v0: `expected_slippage_ticks * tick_size` (config), with optional cap from sweep VWAP at planned size.

**Tests:** `tests/test_edge_calculator.py`, `tests/test_fee_lookup.py`.

**Facts:** `edge_evaluated` with both legs, selected leg, fee_bps, slippage est, edges.

### 3.5 Entry decision layer

| Component | Proposed module | Generic? |
|-----------|-----------------|----------|
| Pure entry eval | `src/tyrex_pm/strategies/z_gap/entry_eval.py` | Z-Gap orchestration; calls generic quant + gates |
| Book read | Reuse `paired_binary/entry_eval.read_leg_book` or move to `market_data/book_read.py` | **Yes** (extract) |
| Gate reason codes | `src/tyrex_pm/core/reason_codes.py` — add `Z_GAP_*` codes | Shared |

**Entry gates (v0 config):**

| Gate | Rule |
|------|------|
| `theta_take` | `max(edge_UP, edge_DOWN) >= 0.05` |
| `z_band` | `0.8 <= abs(z) <= 2.2` |
| `tau_band_s` | `60 <= tau <= 210` |
| `basis_max_bps` | `abs(basis_bps) <= 3` |
| Feed freshness | Binance + Chainlink + K observed, ages within profile |
| Book quality | `DataQualityGate` context `ENTRY` |
| `one_position_per_window` | State machine enforces |
| `no_reentry_after_exit` | State flag `exited_this_window` |
| Lifecycle | `MarketLifecycleGuard` blocks near-close entry |

**Tests:** `tests/test_z_gap_entry_eval.py` — table-driven pass/fail per gate.

**Facts:** `z_gap_entry_eval`, `z_gap_entry_skip` (mirror paired_binary naming).

### 3.6 Risk / quality gates

| Gate | Module | Notes |
|------|--------|-------|
| Notional / capital / inventory | `risk/engine.py` | Unchanged |
| Venue min size (5 shares) | `risk/venue_min_size.py` | `min_shares: 5` in sizing |
| Book quality | `market_data/quality.py` | Add `DecisionContext.MODEL_ENTRY` alias or reuse `ENTRY` |
| External feed requirement | `market_data/quality.py` — `require_external_price` on profile | Wire for Z-Gap `crypto_5m` profile |
| Participation cap | New check in `strategies/z_gap/sizing.py` | `filled_size / depth_at_size <= max_participation` |
| Survival kill switches | `survival/kill_switches.py` | Extended counters (see §8) |

### 3.7 Sizing

| Component | Proposed module | Generic? |
|-----------|-----------------|----------|
| Fixed USD sizing | `src/tyrex_pm/strategies/z_gap/sizing.py` | Z-Gap v0; pattern reusable |
| Formula | `shares = floor(max_usd / ask)` clamped to `[min_shares, depth * max_participation]` | |

**v0 config:**

```yaml
sizing:
  mode: fixed_usd
  max_usd: "5"
  min_shares: "5"
  max_participation: "0.15"
```

**Kelly hook (later):** `quant/sizing.py` with `mode: kelly` reading edge and σ; risk caps still enforced by `RiskEngine`.

### 3.8 Execution

| Step | Module |
|------|--------|
| Build `EnterIntent` | `strategies/z_gap/entry_eval.py` — FAK, limit at worst acceptable from planner |
| Pipeline | `runtime/pipeline.py` — `process_intent_work_unit` |
| Planner | `execution/planner.py` — marketable FAK for entry when style=FAK |
| OMS | `execution/oms.py` |

Mirror paired-binary entry: `entry_order_style: FAK` in strategy config.

**Facts:** Reuse `intent_created`, `execution_plan`, `oms_submit`, `oms_result`; extend with `z_gap_entry_submitted`, `latency_chain` per decision_id.

### 3.9 Position activation

| Phase | Behavior | Module |
|-------|----------|--------|
| `ENTRY_PENDING` | Wait for fill via user WS / reconcile | `state/entry_fill_lifecycle.py` |
| `FILLED` → `ACTIVE` | `allocation_ledger.apply_buy`; record entry model snapshot | `strategies/z_gap/lifecycle.py` |
| Optional recheck | Skip in v0 unless book stale at fill | Defer |

**State machine phases (Z-Gap):**

```text
IDLE → ENTRY_PENDING → ACTIVE → EXIT_PENDING → DONE
                  ↘ FAILED (unwind if partial — unlikely with FAK)
```

Persist: `var/state/z_gap_{owner}_{market_id}.json`

### 3.10 Model-based exit rules

| Rule | Trigger | Module |
|------|---------|--------|
| Thesis stop | UP: `z <= -z_stop`; DOWN: `z >= z_stop` | `exit_policy/thesis_stop.py` (new, generic) |
| Edge / rich exit | `bid >= p_leg + theta_rich` or edge at bid `<= -theta_exit` | `exit_policy/edge_exit.py` (new, generic) |
| Hold to resolution | `abs(z) >= z_hold` and `tau <= tau_hold_s` → suppress discretionary exit | `exit_policy/hold_policy.py` (new, generic) |
| Pre-close flatten | Lifecycle guard | Reuse `strategy_lifecycle.py` |
| Kill switch flatten | `KillSwitchManager` | Extended `survival/kill_switches.py` |

**Orchestrator:** `exit_policy/evaluator.py` — `evaluate_model_exits(ctx) -> ModelExitDecision`

**Z-Gap adapter:** `strategies/z_gap/monitor.py` — calls evaluator each tick; emits `IntentWorkUnit` like `PairedBinaryMonitor`.

**Execution path:** Reuse `survival/order_policy.select_survival_exit_order` + new thin `exit_policy/dispatch.py` (decoupled from `PairedBinaryRuntimeState`).

### 3.11 Reconciliation

| Component | Path | Z-Gap |
|-----------|------|-------|
| Wallet / orders | `runtime/pipeline.py` — `reconcile_coordinator` | Reuse |
| Cashflow PnL | `execution/fill_reconciliation.py` | Add `reconcile_z_gap_cashflows()` for single-leg |
| Script | `scripts/reconcile_run_cashflows.py` | Extend for z_gap runs |

### 3.12 Facts / reporting

See §9 for full fact list. Terminal summary: `z_gap_terminal_summary` + extend `scripts/analyze_run_timeline.py`.

---

## 4. Required New or Extended Components

### 4.1 `SignalStateStore` (generic)

| Field | Value |
|-------|-------|
| **Purpose** | Live in-memory authoritative snapshot of external prices, PTB, basis, freshness |
| **Generic vs specific** | Generic |
| **Module** | `src/tyrex_pm/state/signal_state_store.py` |
| **Input** | `MarketEvent` callbacks from Binance + RTDS ingest; PTB tracker events; window bounds |
| **Output** | `SignalSnapshot` via `snapshot()` / `is_fresh(profile)` |
| **Tests** | Unit: tick ordering, staleness, basis; integration: golden_day fixture replay |
| **Facts** | `signal_feed_health`, `basis_computed` |

### 4.2 `signal_feed_runtime` (generic)

| Field | Value |
|-------|-------|
| **Purpose** | Start/stop Binance + RTDS background tasks in **live** `run_once.py` (mirror `record_run.py`) |
| **Generic vs specific** | Generic |
| **Module** | `src/tyrex_pm/runtime/signal_feed_runtime.py` |
| **Input** | `AppConfig.runtime.external_btc`, `reference_prices`; `stop` event |
| **Output** | Updates `coord.signal_state`; optional facts on stale/reconnect |
| **Tests** | `tests/test_signal_feed_runtime.py` — mock WS, verify store updates |
| **Facts** | `signal_feed_connected`, `signal_feed_disconnected` |

**Config change:** Remove "record mode only" docstring constraint from `ExternalBtcConfig` / `ReferencePricesConfig`; add `live_enabled` guard via strategy kind or `runtime.signal_feeds.enabled`.

### 4.3 `btc_5m_metadata` (generic)

| Field | Value |
|-------|-------|
| **Purpose** | Resolve Gamma event URL / slug → market_id, condition_id, yes/no tokens, event_start/end |
| **Generic vs specific** | Generic (extract from `paired_binary_metadata.py`) |
| **Module** | `src/tyrex_pm/runtime/btc_5m_metadata.py` |
| **Input** | `--event-url`, placeholders in strategy YAML |
| **Output** | `Btc5mMarketMetadata` applied to `AppConfig` |
| **Tests** | Reuse `test_market_discovery.py` patterns |

### 4.4 `EwmaVolatilityEstimator` (generic)

| Field | Value |
|-------|-------|
| **Purpose** | σ from Binance 1s log returns |
| **Generic vs specific** | Generic |
| **Module** | `src/tyrex_pm/quant/volatility.py` |
| **Input** | Binance tick stream (1s resample or per-tick with time decay) |
| **Output** | `sigma`, `sample_count`, `jump_guard_tripped` |
| **Tests** | Known series golden; jump guard rejects >Nσ moves |
| **Facts** | Included in `model_state_snapshot` |

**v0 config:**

```yaml
sigma:
  estimator: ewma
  half_life_s: 30
  min_samples_s: 20
  jump_guard: true
  jump_threshold_sigma: 4.0   # proposed default
```

### 4.5 `binary_fair_value` + `edge` (generic)

| Field | Value |
|-------|-------|
| **Purpose** | z, p*, all-in edge |
| **Module** | `src/tyrex_pm/quant/binary_fair_value.py`, `quant/edge.py`, `quant/fees.py` |
| **Input** | `FairValueInput(S, K, sigma, tau)` + leg books + fee_bps |
| **Output** | `FairValueSnapshot`, `EdgeSnapshot` |
| **Tests** | Analytical Φ(z) checks; edge with mock fees |

### 4.6 `exit_policy` package (generic)

| Field | Value |
|-------|-------|
| **Purpose** | Model-state exit evaluation decoupled from entry price |
| **Generic vs specific** | Generic framework; Z-Gap supplies config thresholds |
| **Module** | `src/tyrex_pm/exit_policy/` — `context.py`, `thesis_stop.py`, `edge_exit.py`, `hold_policy.py`, `evaluator.py`, `dispatch.py` |
| **Input** | `ModelExitContext` — fair value, books, position leg, hold flags |
| **Output** | `ModelExitDecision(trigger, enforce, evidence)` |
| **Tests** | Per-rule unit tests; combined evaluator precedence tests |
| **Facts** | `model_exit_evaluated`, `model_exit_triggered` |

**Precedence (v0):**

1. Kill switch / lifecycle flatten (highest)
2. Hold-to-resolution suppresses discretionary exits
3. Thesis stop
4. Edge/rich exit

### 4.7 Z-Gap strategy package (strategy-specific, thin)

| File | Purpose |
|------|---------|
| `strategies/z_gap/strategy.py` | Facade, OMS notify callbacks |
| `strategies/z_gap/state.py` | Phase enum, persistence |
| `strategies/z_gap/entry_eval.py` | Gate orchestration |
| `strategies/z_gap/monitor.py` | Tick loop exit evaluation |
| `strategies/z_gap/lifecycle.py` | Phase transitions |
| `strategies/z_gap/sizing.py` | Fixed USD |
| `strategies/z_gap/facts.py` | Z-Gap fact emitters |
| `strategies/z_gap/recovery.py` | Startup state recovery |

### 4.8 `z_gap_run.py` (strategy runtime)

| Field | Value |
|-------|-------|
| **Purpose** | Main loop — mirror `paired_binary_run.py` structure, simplified |
| **Module** | `src/tyrex_pm/runtime/z_gap_run.py` |
| **Loop** | WS wake → read books + signal snapshot → update σ → entry or monitor → pipeline |
| **Tests** | `tests/test_z_gap_runtime.py` with fixture books + injected signal state |

### 4.9 Config / wiring extensions

| File | Change |
|------|--------|
| `runtime/config.py` | `STRATEGY_KIND_Z_GAP`, `ZGapStrategyConfig`, `ZGapModelConfig`, `ZGapSigmaConfig`, `ModelExitConfig` under `runtime.survival` or new `runtime.model_exit` |
| `runtime/app.py` | Wire `z_gap` in `_RUNTIME_WIRED_STRATEGY_KINDS` |
| `runtime/run_once.py` | Instantiate `ZGapStrategy`, dispatch `run_z_gap_loop`, start signal feeds |
| `runtime/run_continue.py` | Allow `strategy_kind == z_gap` |
| `reporting/schema_v2.py` | New fact types |
| `coordinator.py` | `signal_state: SignalStateStore | None` |

### 4.10 `read_leg_book` extraction (generic, optional Z0.2)

Move `read_leg_book` from `paired_binary/entry_eval.py` to `market_data/book_read.py` to avoid cross-strategy import from paired_binary.

---

## 5. Data Source Plan

### 5.1 Polymarket CLOB book data

- **Source:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Reuse:** `run_market_ws_ingest` with WS-primary + quality enforce (same as `live_paired_binary_tiny_ws_primary.yaml`)
- **Tokens:** `yes_token_id` (UP), `no_token_id` (DOWN) from metadata
- **Freshness:** `max_book_age_s: 5`, quality profile `crypto_5m`

### 5.2 Polymarket RTDS Chainlink + price-to-beat

- **Source:** `wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`, symbol `btc/usd`
- **Reuse:** `run_reference_prices_ingest` + `PriceToBeatTracker`
- **K derivation:** First Chainlink tick with `source_ts >= event_start_ts` within `price_to_beat_max_lag_ms` (default 5000ms)
- **v0 policy:** **Do not enter until `ptb_status == observed`**. If `missing`, skip window with fact `z_gap_entry_skip{reason: ptb_missing}`

**Open note:** Gamma `eventMetadata.priceToBeat` is deferred; derived PTB matches record pipeline (`Docs/DATA_LAKE.md`).

### 5.3 Binance BTC feed

- **Source:** `wss://stream.binance.com:9443`, streams `bookTicker` + `aggTrade`
- **Reuse:** `run_external_btc_ingest`
- **S selection (v0):** Mid from `bookTicker` `(bid+ask)/2`; fallback to `aggTrade` price if bookTicker stale > 2s
- **σ input:** 1-second sampled log returns from `S` series

### 5.4 Timestamping and freshness

| Field | Source |
|-------|--------|
| `source_ts` | Venue event time (Binance trade time, RTDS payload ts) |
| `recv_ts` | Local `utc_now()` at WS message receipt |
| `decision_ts` | Monotonic + wall clock at eval (reuse `paired_binary/latency.py` pattern) |

**Freshness policy (proposed v0):**

| Feed | Max age for entry |
|------|-------------------|
| Binance S | 2000 ms |
| Chainlink S_CL | 3000 ms |
| PTB K | Must be observed; age N/A |
| PM book | Quality gate `ENTRY` profile (750ms pass / 1500ms reject) |

### 5.5 Basis monitoring

```text
basis_bps = ((S - S_CL) / S_CL) * 10000
```

Reject entry when `abs(basis_bps) > basis_max_bps` (v0: 3). Log continuously in `basis_computed` facts (deduped on material change).

### 5.6 Failure modes

| Failure | Behavior |
|---------|----------|
| Binance disconnect | Pause entry; if in position, exits allowed with `URGENT_EXIT` quality context; fact `signal_feed_stale` |
| RTDS disconnect | Same; basis gate fails closed |
| PTB missing | No entry entire window |
| WS book gap | `market_readiness_tracker` blocks entry (reuse) |
| σ insufficient samples | No entry until `min_samples_s` elapsed |
| Jump guard trip | Freeze σ update for 5s; no entry during trip |

---

## 6. Z-Gap Model Specification

### 6.1 Variables

| Symbol | Meaning | Source |
|--------|---------|--------|
| `K` | Price-to-beat | `PriceToBeatTracker` |
| `S` | Fast BTC spot | Binance `bookTicker` mid |
| `S_CL` | Chainlink BTC | RTDS |
| `τ` | Time to resolution (seconds) | `event_end_ts - now` |
| `σ` | Short-horizon vol (per √s) | EWMA on log returns |
| `z` | Normalized distance | Model |
| `p_UP`, `p_DOWN` | Digital fair probs | Φ(z) |

### 6.2 Formulas

```text
z      = ln(S / K) / (σ * sqrt(max(τ, τ_floor)))
p_UP   = Φ(z)
p_DOWN = 1 - p_UP

edge_UP   = p_UP   - ask_UP   - fee(ask_UP)   - slippage_UP
edge_DOWN = p_DOWN - ask_DOWN - fee(ask_DOWN) - slippage_DOWN
```

### 6.3 Intuition

- `z > 0` → BTC above K → UP favored.
- As `τ → 0`, same log-moneyness becomes more decisive (denominator shrinks).
- `|z| < 0.8` → near coin-flip; fees dominate → no entry.
- `|z| > 2.2` → tail region; PM often already 0.95+; spread/tail risk → no entry.

### 6.4 Volatility estimator

EWMA variance on 1-second log returns `r_t = ln(S_t/S_{t-1})`:

```text
var_t = λ * var_{t-1} + (1-λ) * r_t²
σ_t   = sqrt(var_t / Δt)   # convert to per-√second if Δt=1s
```

With `half_life_s=30`: `λ = exp(-ln(2)/30)`.

**Jump guard:** If `|r_t| > jump_threshold_sigma * σ_{t-1}`, skip update and set `jump_guard_tripped=true`.

### 6.5 Basis gate

Entry requires `abs(basis_bps) <= 3`. Does not block exits (model may be wrong; risk reduction prioritized).

### 6.6 Fee / slippage treatment

**Fees:** `venue/polymarket/market_info.py` → `fee_rate_bps` per token via `coord.market_info_cache` (same path as live attest). Fallback: config `estimated_fee_bps` if cache miss (deny entry if live cache required).

**Slippage (v0):** `expected_slippage_ticks * tick_size` per leg (default 1 tick). Optional v0.1 enhancement: use `ExecutableBookView.sweep_vwap` at planned size for slippage estimate in edge calc.

---

## 7. Entry Specification

### 7.1 Gate summary

```yaml
z_gap:
  entry:
    theta_take: "0.05"
    z_band: ["0.8", "2.2"]
    tau_band_s: [60, 210]
    basis_max_bps: "3"
    expected_slippage_ticks: 1
    one_position_per_window: true
    no_reentry_after_exit: true
```

### 7.2 Leg selection

```text
if edge_UP >= edge_DOWN and edge_UP >= theta_take and gates pass:
    selected_leg = UP (yes_token_id)
elif edge_DOWN > edge_UP and edge_DOWN >= theta_take and gates pass:
    selected_leg = DOWN (no_token_id)
else:
    skip
```

### 7.3 Execution intent

- **Side:** BUY
- **Style:** FAK (`entry_order_style: FAK`)
- **Size:** From fixed USD sizing (§3.7)
- **Limit:** Planner-derived worst acceptable price capped by strategy max entry price (config `max_entry_price: "0.95"` suggested guard)

### 7.4 Rejection reasons (all must be observable)

| Reason code | Condition |
|-------------|-----------|
| `z_gap_ptb_missing` | K not observed |
| `z_gap_feed_stale` | Binance or Chainlink too old |
| `z_gap_basis_exceeded` | Basis bps over limit |
| `z_gap_z_out_of_band` | abs(z) not in band |
| `z_gap_tau_out_of_band` | τ outside window |
| `z_gap_edge_below_theta` | max edge < theta_take |
| `z_gap_sigma_not_ready` | EWMA warming up |
| `z_gap_jump_guard` | Jump guard active |
| `z_gap_book_stale` | PM book stale |
| `z_gap_quality_reject` | DataQualityGate reject |
| `z_gap_already_positioned` | Open position exists |
| `z_gap_no_reentry` | Already exited this window |
| `z_gap_lifecycle_blocked` | Near close / closed |
| `z_gap_kill_switch` | Kill switch deny_entry |

Emit `z_gap_entry_skip` with full model snapshot for every skip (dedupe: material change only).

### 7.5 v0 scenario sketch

**`config/strategies/z_gap.yaml`:**

```yaml
kind: z_gap
enabled: true
z_gap:
  owner_id: z_gap
  market_id: "btc_5m_<YYYYMMDD_HHMM>"
  condition_id: "<required>"
  yes_token_id: "<required>"
  no_token_id: "<required>"
  event_start_ts: null
  event_end_ts: null
  entry_order_style: FAK
  exit_order_style: FAK
  run_once: false
  tick_interval_s: 0.2
  entry:
    theta_take: "0.05"
    z_band: ["0.8", "2.2"]
    tau_band_s: [60, 210]
    basis_max_bps: "3"
    expected_slippage_ticks: 1
  sigma:
    estimator: ewma
    half_life_s: 30
    min_samples_s: 20
    jump_guard: true
  sizing:
    mode: fixed_usd
    max_usd: "5"
    min_shares: "5"
    max_participation: "0.15"
```

**`config/scenarios/live_z_gap_tiny.yaml`:** Overlay WS-primary, quality enforce, signal feeds enabled, model exit enforce, kill switches — mirror `live_paired_binary_tiny_ws_primary.yaml` + `record_btc5m_rich.yaml` feed blocks.

---

## 8. Exit Specification

### 8.1 v0 exit config

```yaml
runtime:
  model_exit:   # new block (or under survival with distinct evaluator)
    enabled: true
    thesis_stop:
      z_stop: "0.25"
      enforcement_mode: enforce
    edge_exit:
      theta_rich: "0.03"
      theta_exit: "0.05"
      enforcement_mode: enforce
    hold_to_resolution:
      z_hold: "1.5"
      tau_hold_s: 45
    kill_switches:
      max_trades_per_run: 10
      max_consecutive_losses: 3
      max_daily_loss_usd: "20"
```

### 8.2 Rule definitions

#### Thesis-reversal stop

For UP position: exit when `z <= -z_stop` (model now favors DOWN vs strike).  
For DOWN position: exit when `z >= +z_stop`.

This is **model-state oriented**, not `entry_price - X`.

#### Edge / rich exit

- **Rich exit:** `bid >= p_leg + theta_rich` — market paying premium vs model; take profit.
- **Edge exit:** Model edge at exit side `p_leg - bid - fee(bid) < -theta_exit` — thesis weakened + market not compensating.

#### Hold-to-resolution

When `abs(z) >= z_hold` AND `tau <= tau_hold_s`, suppress edge/rich discretionary exits. Allow position to resolve if thesis remains strong into the close. Lifecycle flatten still applies at `flatten_before_event_end_s` unless hold policy overrides (v0: hold wins only when z strong; otherwise flatten).

### 8.3 Mapping to existing survival machinery

| Z-Gap rule | Existing analogue | v0 approach |
|------------|-------------------|-------------|
| Thesis stop | `evaluate_dual_stop` (entry-price) | **New** `exit_policy/thesis_stop.py` |
| Rich / edge exit | `survival/economics.py` (survivor net) | **New** `exit_policy/edge_exit.py` using model probs |
| FAK exit execution | `survival/enforcement_dispatch.py` | **New** `exit_policy/dispatch.py` reusing `survival/order_policy.py` |
| Quality on exit | `SurvivalExitPlanner` | Reuse with `DecisionContext.URGENT_EXIT` |
| Kill switches | `survival/kill_switches.py` | **Extend** config + counters |
| Pre-close flatten | `MarketLifecycleGuard` | Reuse |
| Trailing / hard floor | `survival/trailing_stop`, `survivor_floor` | **Do not use** for Z-Gap v0 |

**Minimal generic extension:** Introduce `ModelExitContext` protocol so future strategies (not only Z-Gap) can plug model-state exits without copying paired-binary survivor logic.

```python
# exit_policy/context.py (conceptual)
@dataclass(frozen=True)
class ModelExitContext:
    leg: Literal["up", "down"]
    z: Decimal
    p_up: Decimal
    p_down: Decimal
    tau_s: float
    bid: Decimal | None
    ask: Decimal | None
    fee_rate_bps: int
    entry_model_snapshot: dict[str, Any]  # z at entry, edge at entry
```

### 8.4 Exit precedence

1. `kill_switch` → `hard_stop` or `force_flatten`
2. `lifecycle_pre_close_flatten` (unless hold-to-resolution active)
3. `thesis_stop` (enforce)
4. `edge_exit` (enforce)
5. Hold policy suppresses 3–4 when conditions met

### 8.5 Exit facts

Emit `model_exit_evaluated` every monitor tick (deduped), `model_exit_triggered` on fire, reuse `survival_exit_order_*` facts from order policy where applicable, `z_gap_exit_done` with realized PnL.

---

## 9. Facts, Reports, and Diagnostics

### 9.1 New fact types (`reporting/schema_v2.py`)

**Generic (reusable):**

| Fact type | Key payload fields |
|-----------|-------------------|
| `signal_feed_health` | feed, connected, last_source_ts, last_recv_ts, age_ms, stale |
| `basis_computed` | S, S_CL, basis_bps |
| `model_state_snapshot` | S, S_CL, K, tau, sigma, z, p_up, p_down, sigma_status, jump_guard |
| `edge_evaluated` | ask/bid per leg, fee_bps, slippage, edge_up, edge_down, selected_leg |
| `model_exit_evaluated` | trigger_candidates, hold_active, z, p_leg, bid |
| `model_exit_triggered` | trigger, z, p_leg, enforcement_mode |

**Z-Gap specific:**

| Fact type | Key payload fields |
|-----------|-------------------|
| `z_gap_entry_eval` | gates, pass/fail, full model + edge snapshot |
| `z_gap_entry_skip` | reason_code, snapshot |
| `z_gap_entry_submitted` | leg, size, limit, decision_id |
| `z_gap_position_activated` | leg, fill_price, shares, entry_z, entry_edge |
| `z_gap_exit_done` | trigger, exit_z, exit_p, fill_price, realized_pnl |
| `z_gap_terminal_summary` | window outcome, entry/exit reasons, PnL, feed stats |
| `z_gap_no_entry_summary` | skip reason histogram for window |

### 9.2 Execution / latency facts (reuse + extend)

| Fact | Content |
|------|---------|
| `decision_snapshot` | decision_id, model snapshot ids, book snapshot ids |
| `latency_chain` | signal_ts, decision_ts, intent_ts, submit_ts, ack_ts, fill_ts, deltas_ms |
| `execution_plan` | planner evidence |
| `intent_created` / `risk_decision` / `oms_*` | Standard spine |

**Post-fill model state:** Attach to `z_gap_position_activated` and fill-correlated `oms_result` extensions.

### 9.3 Run summary additions

Extend terminal artifact (new writer in `z_gap_run.py`):

```json
{
  "strategy_kind": "z_gap",
  "market_id": "...",
  "entered": true,
  "leg": "up",
  "entry_reason": "edge_above_theta",
  "exit_reason": "thesis_stop",
  "realized_pnl_usd": "-0.42",
  "entry_edge": "0.062",
  "fill_slippage_ticks": 1,
  "max_basis_bps": "1.2",
  "ptb_lag_ms": 840,
  "skip_count": 412
}
```

### 9.4 Post-run questions → fact joins

| Question | Fact join |
|----------|-----------|
| Right entry reason? | `z_gap_entry_eval` → `z_gap_entry_submitted` |
| Edge real at signal? | `edge_evaluated` at decision_ts vs `oms_result` fill price |
| Edge gone before fill? | Compare `edge_evaluated` timestamps in latency_chain window |
| Binance vs Chainlink disagree? | `basis_computed` series |
| Thesis stop? | `model_exit_triggered{trigger: thesis_stop}` |
| Model vs noise exit? | `model_exit_evaluated` bid vs z at trigger |
| Loss attribution | `z_gap_exit_done` + `execution_plan` + fee_bps + `reconcile` |

### 9.5 Scripts to extend

| Script | Extension |
|--------|-----------|
| `scripts/analyze_run_timeline.py` | Z-Gap fact types |
| `scripts/reconcile_run_cashflows.py` | Single-leg cashflows |
| `scripts/validate_paired_binary_phase2_live_run.py` | New `validate_z_gap_live_run.py` |

---

## 10. Testing Plan

### 10.1 Unit tests

| Test file | Coverage |
|-----------|----------|
| `test_ewma_volatility.py` | σ convergence, half-life, jump guard |
| `test_binary_fair_value.py` | z, Φ(z) known values |
| `test_edge_calculator.py` | fee + slippage + edge |
| `test_signal_state_store.py` | freshness, basis, PTB status |
| `test_exit_policy_thesis_stop.py` | UP/DOWN symmetry |
| `test_exit_policy_edge_exit.py` | rich vs edge exit |
| `test_exit_policy_hold.py` | suppression logic |
| `test_z_gap_entry_eval.py` | all gate reason codes |
| `test_z_gap_sizing.py` | fixed USD, min 5 shares, participation cap |

### 10.2 Fixture / replay tests

| Test | Method |
|------|--------|
| Feed wiring | Replay `tests/fixtures/recordings/golden_day/external/*` through `SignalStateStore` |
| PTB derivation | Chainlink fixture → K at window start |
| Decision golden | Frozen books + signal snapshot → deterministic entry/exit decision |

### 10.3 Strategy decision tests

Table-driven tests with CSV/json cases:

- (S, K, σ, τ, books, fees) → enter UP / enter DOWN / skip + reason

### 10.4 Data freshness tests

- Stale Binance → `z_gap_feed_stale`
- Stale book → quality reject
- PTB missing → no entry

### 10.5 Exit rule tests

- z crosses `-z_stop` on UP → thesis exit
- bid rich → edge_exit
- hold active → no discretionary exit
- kill switch → flatten

### 10.6 Integration smoke test

```text
shadow mode + fixture books + mock signal_state → one entry + one exit → facts.jsonl assertions
```

Reuse patterns from `test_paired_binary_runtime.py` and `test_validation_harness_runtime.py`.

### 10.7 Live tiny-size validation checklist

- [ ] Preflight script passes (`scripts/preflight_z_gap_live_scenario.py` — new)
- [ ] `live_attest` milestones green
- [ ] WS-primary connected, books fresh
- [ ] Binance + RTDS feeds connected, facts show ages < thresholds
- [ ] PTB observed within 5s of window start
- [ ] One manual window: entry fact → oms fill → position activated → exit → terminal summary
- [ ] `reconcile_run_cashflows.py` PnL within tolerance
- [ ] Kill switch triggers in dry-run config test

---

## 11. Implementation Milestones

Sequential, small increments. Each milestone should leave tests green.

| ID | Milestone | Deliverables | Depends |
|----|-----------|--------------|---------|
| **Z0.1** | Inventory + config scaffold | `STRATEGY_KIND_Z_GAP`, empty `z_gap.yaml`, `live_z_gap_tiny.yaml`, kind wired fail-closed | — |
| **Z0.2** | `SignalStateStore` + feed health | `state/signal_state_store.py`, `market_data/signal_feed_health.py`, unit tests | Z0.1 |
| **Z0.3** | Live signal feed runtime | `runtime/signal_feed_runtime.py`; start Binance+RTDS in `run_once` when configured; facts | Z0.2 |
| **Z0.4** | BTC 5m metadata generalization | `runtime/btc_5m_metadata.py`; `--event-url` for z_gap | Z0.1 |
| **Z0.5** | PTB live integration | Wire `PriceToBeatTracker` to signal feeds; register on metadata; gate on K | Z0.3, Z0.4 |
| **Z0.6** | EWMA σ + fair value | `quant/volatility.py`, `quant/binary_fair_value.py`, tests | Z0.2 |
| **Z0.7** | Fees + edge calculator | `quant/fees.py`, `quant/edge.py`, `quant/slippage.py`, tests | Z0.6 |
| **Z0.8** | Entry eval + gates | `strategies/z_gap/entry_eval.py`, reason codes, `z_gap_entry_*` facts | Z0.5, Z0.7 |
| **Z0.9** | State machine + sizing | `strategies/z_gap/state.py`, `sizing.py`, `lifecycle.py`, persistence | Z0.8 |
| **Z0.10** | `exit_policy` package | thesis/edge/hold + evaluator + dispatch | Z0.7 |
| **Z0.11** | Monitor + main loop | `strategies/z_gap/monitor.py`, `runtime/z_gap_run.py`, pipeline integration | Z0.9, Z0.10 |
| **Z0.12** | Kill switch extensions | `max_trades_per_run`, `max_consecutive_losses`, `max_daily_loss_usd` in config + manager | Z0.11 |
| **Z0.13** | Facts + reporting | `schema_v2` types, `strategies/z_gap/facts.py`, terminal summary, `analyze_run_timeline` | Z0.11 |
| **Z0.14** | Recovery + run_continue | `z_gap/recovery.py`, extend `run_continue.py` for z_gap | Z0.11 |
| **Z0.15** | Integration tests + shadow smoke | `test_z_gap_runtime.py`, fixture golden | Z0.13 |
| **Z0.16** | Live tiny scenario + preflight | `live_z_gap_tiny.yaml`, `preflight_z_gap_live_scenario.py`, ops doc snippet | Z0.15 |

**Optional parallel track after Z0.7:** Extract `read_leg_book` to `market_data/book_read.py` (refactor paired_binary import).

---

## 12. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Latency** — edge gone between signal and fill | High | Log `edge_evaluated` pre/post submit; latency_chain facts; tiny size; FAK only |
| **Stale feeds** — trade on old S | High | Hard freshness gates; no entry if stale; facts on every stale reject |
| **Wrong window/token mapping** | Critical | Gamma slug validation (`validate_btc_5m_reference_url`); metadata merge tests; manual preflight |
| **Binance/Chainlink basis** | Medium | `basis_max_bps` gate; log basis series |
| **Fee regime change** | Medium | Live `fee_rate_bps` from `MarketInfoCache`; deny if unknown |
| **Thin book** — FAK partial / reject | Medium | `max_participation`; quality depth gate; survival order policy retry |
| **Final-second reversals** | Medium | Hold-to-resolution only when z strong; lifecycle flatten default 20s before end |
| **Overconfident model** — σ too low | High | Jump guard; z_band cap; tiny size; post-run attribution before scaling |
| **PTB derivation lag/miss** | High | No entry without K; alert on `ptb_missing` |
| **Missing reconciliation** | Medium | Reuse reconcile spine; extend cashflow script; tentative vs reconciled PnL facts |
| **σ cold-start** | Low | `min_samples_s: 20` — no entry until warm |
| **Record/live code drift** | Medium | Single ingest module for both modes; `SignalStateStore` as shared consumer |

---

## 13. Open Questions

Only true blockers:

1. **PTB authority for live entry:** v0 uses derived Chainlink PTB (same as recorder). If live Gamma PTB differs from first-tick derivation, entries may be systematically wrong. **Blocker resolution:** Run one live window recording (`record_btc5m_rich`) in parallel with first tiny live trade and compare K vs Polymarket UI price-to-beat. If delta > 1 bps routinely, add Gamma PTB fetch before entry (small extension to `venue/polymarket/event_metadata.py`).

2. **Hold-to-resolution vs lifecycle flatten:** When both `z_hold` and `flatten_before_event_end_s` apply in the last 45s, which wins? **Proposed default:** If hold conditions met, defer flatten until `tau <= 10s` (add `hold_flatten_override_tau_s` in v0 config). Confirm with operator before Z0.10.

No other blockers — implementation can proceed with defaults in this document.

---

## Appendix A — Reuse vs Add Quick Reference

| Layer | Reuse as-is | Extend | Add new |
|-------|-------------|--------|---------|
| CLOB WS + books | ✓ | | |
| Binance ingest | ✓ module | Live wiring | `SignalStateStore` |
| RTDS ingest | ✓ module | Live wiring | |
| PTB tracker | ✓ | Live + facts | |
| Window scheduler / discovery | ✓ | `run_continue` for z_gap | |
| Metadata | ✓ pattern | | `btc_5m_metadata` |
| OMS / planner / risk | ✓ | | |
| Allocation ledger | ✓ | | |
| Quality gate | ✓ | `require_external_price` | |
| Survival enforce | Pattern only | Kill switch counters | `exit_policy/*` |
| Facts / pipeline | ✓ | schema types | z_gap facts |
| Main loop | Pattern (`paired_binary_run`) | | `z_gap_run` |
| Strategy logic | | | `strategies/z_gap/*` |
| Quant model | | | `quant/*` |

---

## Appendix B — Config merge example (live tiny)

```yaml
# config/scenarios/live_z_gap_tiny.yaml
execution_mode: live

runtime:
  signal_feeds:
    enabled: true   # new master switch (proposed)
  external_btc:
    enabled: true
    symbol: BTCUSDT
    streams: [bookTicker, aggTrade]
  reference_prices:
    enabled: true
    feeds: [chainlink]
    symbols: [btc/usd]
    emit_price_to_beat: true
    price_to_beat_max_lag_ms: 5000
  market_data:
    enabled: true
    max_book_age_s: 5
    websocket:
      primary_enabled: true
    quality:
      enforcement_mode: enforce
      require_ws_primary_for_entry: true
      market_profile: crypto_5m
  strategy_lifecycle:
    enabled: true
    flatten_before_event_end_s: 20
  model_exit:
    enabled: true
    thesis_stop:
      z_stop: "0.25"
      enforcement_mode: enforce
    edge_exit:
      theta_rich: "0.03"
      theta_exit: "0.05"
      enforcement_mode: enforce
    hold_to_resolution:
      z_hold: "1.5"
      tau_hold_s: 45
    kill_switches:
      enabled: true
      max_trades_per_run: 10
      max_consecutive_losses: 3
      max_daily_loss_usd: "20"

risk:
  per_trade_max_notional_usd: "6"
  readiness:
    require_aggressive_ready: true
```

---

*End of specification. Ready for milestone-by-milestone implementation after operator review.*
