# Phase 2 M0 — File-by-file audit checklist

**Audit date:** 2026-06-29  
**Scope:** Every production/test path touching `market_state`, `fetch_order_book`, `is_stale`, `read_leg_book`, `MarketStateStore`, `market_data_rest_refresh_loop`, `paired_binary_run`, `ExecutionPlanner`, freshness/quality, reporting/latency facts.

**Status legend:** `audit_done` = reviewed, no Phase 2 code change in this file yet; `change_planned` = milestone assigned; `implemented` = not used in M0.

---

## Market state core

| File | Current behavior | Why it matters for WS-authoritative Phase 2 | Milestone | Risk | Test | DoD | Status |
|------|------------------|-----------------------------------------------|-----------|------|------|-----|--------|
| `src/tyrex_pm/state/market_store.py` | In-memory per-token books; `apply_snapshot` single-writer; `ts = utc_now()` at apply; `is_stale(max_age_s)` fail-closed; VWAP via `estimate_fill_price` | WS must supply exchange/received timestamps, source, depth metadata; wall-clock apply time hides WS lag | M2 v2 APIs; M1 shadow uses separate instance | Break all readers if API changes abruptly | `tests/test_market_state_store.py` | v2 snapshot capture + source fields; backward compat reads | change_planned |
| `src/tyrex_pm/runtime/market_data_runtime.py` | `ensure_market_state_store`; REST `bootstrap_market_state`; **`market_data_rest_refresh_loop` default 5s**; fixture inject; health payload with `is_stale` | Primary live refresh is REST poll — root cause of multi-second age | M1 shadow WS task; M8 disable poll when WS healthy | Poll/WS race on same store | `tests/test_market_data_runtime.py` | Shadow loop writes to shadow store only; M8 poll gated | change_planned |
| `src/tyrex_pm/venue/polymarket/book_snapshot.py` | `bootstrap_market_store_from_rest` → `fetch_order_book` per token; fail-soft per token | Correct for bootstrap/recovery; must not remain sole live source | M2 tag `REST_BOOTSTRAP`/`REST_RECOVERY` | Blocking event loop if sync fetch misused | integration with REST mock | Source tagged on every apply | change_planned |
| `src/tyrex_pm/ingestion/market_stream.py` | Parsers `parse_book_message`, `apply_price_change`, `apply_market_message`; **`ts=utc_now()`**; **no WS transport wired** | Ready for M1 wiring; timestamps must become event-time | M1 `run_market_ws_ingest` → shadow store | Wrong payload schema | `tests/test_market_stream_ingest.py` | Live fixture captured; shadow compare REST | change_planned |
| `src/tyrex_pm/ingestion/user_stream.py` | Live user WS; order/trade events → wallet/OrderStore | Already event-driven; M7 hooks user-fill latency | M7 latency facts | Low | existing user WS tests | `ack_to_user_fill_ms` in decision snapshots | audit_done |
| `src/tyrex_pm/strategies/sell_test/pricing.py` | `fetch_order_book` threaded SDK wrapper | Shared REST fetch primitive | Keep for bootstrap only | Direct strategy fetch anti-pattern | existing | Not used for paired-binary live decisions | audit_done |
| `src/tyrex_pm/strategies/tp_sl_test/strategy.py` | Direct `fetch_order_book` in strategy loop | Violates store-only reads; out of Phase 2 paired-binary scope | M10 / separate | Scope creep | — | Document exclusion from M9 | audit_done |

---

## Runtime / scheduler

| File | Current behavior | Why it matters | Milestone | Risk | Test | DoD | Status |
|------|------------------|----------------|-----------|------|------|-----|--------|
| `src/tyrex_pm/runtime/app.py` | Spawns REST bootstrap + **`market_data_rest_refresh_loop`** + user WS; `ensure_market_state_store` for paired_binary | Must spawn market WS shadow (M1) then cut over (M8) | M1, M8 | Task lifecycle / double refresh | `tests/test_live_harness_wiring.py` | Feature-flagged WS task | change_planned |
| `src/tyrex_pm/runtime/paired_binary_run.py` | Hybrid **event wake + timer** loop; `_paired_binary_tick_body`; activation/entry `decision_snapshot`; coordinator integration | Poll + stale REST drives stop/TP; needs event wake (M6) + quality gate (M3) | M3, M6, M7 | Reentrancy / missed triggers | `tests/test_paired_binary_runtime.py`, `test_paired_binary_event_wake.py`, `test_tick_non_reentrancy.py` | Debounced wake; no concurrent ticks | **implemented (M6, C1)** |
| `src/tyrex_pm/runtime/coordinator.py` | `market_state: object \| None` on coordinator | Injection point for store v2 / shadow | M1, M2 | Typing loose | — | Typed `MarketStateStore` reference | audit_done |
| `src/tyrex_pm/runtime/pipeline.py` | `ExecutionPlanner.plan(..., market_state=coord.market_state)`; exit book evidence via `build_exit_book_evidence_for_intent` | Planner must receive quality-gated executable view | M3, M4, M7 | Stale plan on retry | `tests/test_execution_planner.py` | Fresh snapshot per FAK retry | change_planned |
| `src/tyrex_pm/runtime/pair_entry_saga.py` | Entry planning with `coord.market_state` | FAK entry uses same stale store | M3, M4 | Entry on stale book | `tests/test_pair_entry_saga.py` | ENTRY requires DataQuality PASS | change_planned |
| `src/tyrex_pm/runtime/paired_binary_recovery.py` | Market-aware recovery: token-pair identity, terminal reset, wallet bootstrap independent of lifecycle | Prevents 0-tick no-op on new market after DONE | Phase 2 hardening | Wrong resume on crash | `test_paired_binary_state_recovery_*.py`, `test_paired_binary_terminal_state_reset.py` | Recovery facts + IDLE reset default | **implemented** |
| `src/tyrex_pm/runtime/validation_harness_run.py` | Harness bootstrap + readonly market loop | Pattern for M1 smoke | M1 | — | `tests/test_live_harness_wiring.py` | Shadow ingest smoke | change_planned |
| `src/tyrex_pm/runtime/protection_runtime.py` | Protection tick wiring (no direct store logic) | Shares poll cadence with main loop | M6 | — | `tests/test_protection_runtime_wiring.py` | Event wake includes protection | audit_done |
| `src/tyrex_pm/runtime/config.py` | `market_data.max_book_age_s`, planner `max_book_age_s`, `urgent_exit_max_book_age_s` | M3 `crypto_5m` profile binds here | M3 | Config drift | config tests | Profile documented | change_planned |

---

## Paired-binary strategy (logic unchanged in Phase 2)

| File | Current behavior | Why it matters | Milestone | Risk | Test | DoD | Status |
|------|------------------|----------------|-----------|------|------|-----|--------|
| `src/tyrex_pm/strategies/paired_binary/entry_eval.py` | **`read_leg_book`** computes `book_age_ms`, `stale` via `is_stale`; touch-only bid/ask | Entry blocks on stale; age logged but source unknown | M3 gate before eval (no logic change) | False PASS if REST refresh masks age | entry eval tests | WS_PRIMARY + PASS required | change_planned |
| `src/tyrex_pm/strategies/paired_binary/monitor.py` | Event/tick monitor; stop/TP triggers emit **`decision_snapshot`** before dispatch; FAK retry snapshots; exit work carries `decision_id` | Stop on stale touch — bad_stop run evidence | M4 executable view; M6 wake; M7 facts | Slippage / FAK rejects | `tests/test_paired_binary_monitor*.py`, `test_paired_binary_stop_tp_decision_facts.py` | Depth-aware exit evidence + decision snapshots | **implemented (C0)** |
| `src/tyrex_pm/strategies/paired_binary/strategy.py` | Entry via `read_leg_book` + `evaluate_entry` | Same store dependency | M3 | — | `tests/test_paired_binary_runtime.py` | Unchanged strategy params M0–M9 | audit_done |
| `src/tyrex_pm/strategies/paired_binary/latency.py` | **`LatencyTracker`**: activation + material paths; null fields with reason when ack/fill missing | Stop/exit chain instrumented on paired-binary paths | M7 | — | `test_paired_binary_activation_latency_facts.py`, `test_latency_facts.py` | Full trigger→submit chain on material decisions | **implemented (C0)** |
| `src/tyrex_pm/strategies/paired_binary/facts.py` | Emits `book_capture_quality`, `latency_sample`, stop/exit payloads; **`book_age_ms` often null on exit** | Baseline gap visible in M0 metrics | M7 | — | fact schema tests | Every decision snapshot has age/source | change_planned |
| `src/tyrex_pm/strategies/base.py` | `StrategyContext.market_state` | Contract for all strategies | M2 | — | — | Document WS-primary policy | audit_done |

---

## Execution / risk / protection

| File | Current behavior | Why it matters | Milestone | Risk | Test | DoD | Status |
|------|------------------|----------------|-----------|------|------|-----|--------|
| `src/tyrex_pm/execution/planner.py` | **`ExecutionPlanner`**: urgent exit uses `is_stale` + `estimate_fill_price` (VWAP walk); no retry snapshot policy | FAK limit from stale VWAP → rejects | M4 ExecutableBookView + retry policy | FAK rejects (bad_stop) | `tests/test_execution_planner.py` | Fresh snapshot each retry | change_planned |
| `src/tyrex_pm/execution/models.py` | `ExecutionPlan.book_evidence` dict | Must carry quality verdict + depth | M4, M7 | — | — | Schema extended | change_planned |
| `src/tyrex_pm/risk/exits.py` | **`build_exit_book_evidence_for_intent`**: touch bid + `book_age_ms` + stale flag | Risk uses same store truth | M3, M4 | Mark fallback on stale if misconfigured | robustness tests | Gate before fallback | change_planned |
| `src/tyrex_pm/protection/monitor.py` | **`is_stale` + best_bid** observe; no trigger if stale | Same freshness model | M3, M6 | — | `tests/test_protection_supervisor.py` | Quality gate integration | change_planned |
| `src/tyrex_pm/protection/lifecycle.py` | Builds urgent ExitIntent for ExecutionPlanner | — | M4 | — | — | Planner evidence | audit_done |
| `src/tyrex_pm/core/models.py` | Intent / ApprovedIntent types | — | — | — | — | — | audit_done |

---

## Reporting / observability

| File | Current behavior | Why it matters | Milestone | Risk | Test | DoD | Status |
|------|------------------|----------------|-----------|------|------|-----|--------|
| `src/tyrex_pm/reporting/schema_v2.py` | Fact types incl. `paired_binary_latency_sample`, `paired_binary_book_capture_quality` | M7 adds decision_snapshot facts | M7 | Schema churn | — | v2 fields documented | change_planned |
| `src/tyrex_pm/reporting/summarize.py` | Run summary heuristics | M9 report input | M9 | — | — | Baseline compare script | audit_done |

---

## New modules (not present — planned)

| File | Current behavior | Milestone | Risk | Test | DoD | Status |
|------|------------------|-----------|------|------|-----|--------|
| `src/tyrex_pm/market_data/quality.py` | DataQualityGate + crypto_5m profile | M3 | Wrong ENTRY/EXIT rules | `test_data_quality_gate.py` | Exact v1 rules; observe_only default | implemented |
| `src/tyrex_pm/market_data/readiness.py` | Readiness state machine | M3 | Over-strict startup | `test_market_readiness_states.py` | TRADING_ENABLED requires WS PASS | implemented |
| `src/tyrex_pm/market_data/executable_book.py` | ExecutableBookView + retry helpers | M4 | Wrong FAK floor | `test_executable_book_view.py` | Fresh snapshot per retry | implemented |
| `src/tyrex_pm/market_data/decision_snapshot.py` | DecisionSnapshot builder | M7 | Fact size | `test_decision_snapshot_facts.py`, `test_paired_binary_decision_snapshot_wiring.py` | FeatureBuilder v0 embedded; wired on monitor/activation | **implemented** |
| `src/tyrex_pm/runtime/market_update_coordinator.py` | Debounced wake; authoritative store only; tick lock | M6 | Event storm / shadow wake | `test_market_update_coordinator.py`, `test_paired_binary_event_wake.py` | Coalesce + non-reentrancy | **implemented (C1)** |
| `src/tyrex_pm/strategies/paired_binary/observability.py` | `emit_material_decision` for paired-binary paths | M7 | Duplicate snapshots | C0 wiring tests | observe_only quality on all material paths | **implemented (C0)** |

---

## Tests (grep coverage)

| File | Covers | Milestone updates | Status |
|------|--------|-------------------|--------|
| `tests/test_market_state_store.py` | store, `is_stale`, VWAP | M2 source/age fields | audit_done |
| `tests/test_market_data_runtime.py` | ensure store, bootstrap | M1 shadow loop | audit_done |
| `tests/test_market_stream_ingest.py` | parsers apply to store | M1 WS wire | audit_done |
| `tests/test_execution_planner.py` | stale deny, urgent FAK | M4 ExecutableBookView | audit_done |
| `tests/test_paired_binary_runtime.py` | loop, store wiring | M6 event wake | **implemented** |
| `tests/test_paired_binary_monitor.py` | stop/TP touch triggers | M4 depth | audit_done |
| `tests/test_paired_binary_monitor_lifecycle.py` | phase transitions | M6 | audit_done |
| `tests/test_market_update_coordinator.py` | debounce, coalesce, shadow ignore | M6 | **implemented** |
| `tests/test_paired_binary_event_wake.py` | event_wake tick source | M6 | **implemented** |
| `tests/test_tick_non_reentrancy.py` | tick lock | M6 | **implemented** |
| `tests/test_paired_binary_decision_snapshot_wiring.py` | entry/stop snapshots | M7 | **implemented** |
| `tests/test_paired_binary_activation_latency_facts.py` | activation snapshots | M7 | **implemented** |
| `tests/test_paired_binary_stop_tp_decision_facts.py` | TP/exit planner evidence | M7 | **implemented** |
| `tests/test_paired_binary_robustness.py` | exit book evidence | M3/M4 | audit_done |
| `tests/test_paired_binary_state_recovery_identity.py` | market-aware recovery same-pair | Phase 2 hardening | **implemented** |
| `tests/test_paired_binary_state_recovery_token_mismatch.py` | token mismatch ignores lifecycle | Phase 2 hardening | **implemented** |
| `tests/test_paired_binary_terminal_state_reset.py` | terminal DONE/FAILED reset | Phase 2 hardening | **implemented** |
| `tests/test_paired_binary_loop_health_facts.py` | loop stopped/failed health | Phase 2 hardening | **implemented** |
| `tests/test_paired_binary_open_exposure_force_flatten_validation.py` | BOTH_LEGS_ACTIVE max_runtime flatten | Phase 2 hardening | **implemented** |
| `tests/test_pair_entry_saga.py` | saga + `read_leg_book` | M3 entry gate | audit_done |
| `tests/test_live_harness_wiring.py` | market_state in harness | M1 | audit_done |
| `tests/test_protection_supervisor.py` | protection + store | M3 | audit_done |
| `tests/test_protection_engine.py` | protection engine | — | audit_done |
| `tests/test_protection_runtime_wiring.py` | runtime wiring | M6 | audit_done |
| `tests/test_paired_binary_pnl.py` | PnL facts | M9 compare | audit_done |
| `tests/test_order_allocation_lifecycle.py` | allocation + store | — | audit_done |
| `tests/test_allocation_clamp_grace_and_entry_reconcile.py` | entry reconcile | — | audit_done |

---

## Confirmed current behavior vs planned changes

| Behavior | Current (confirmed) | Planned (Phase 2) | Milestone |
|----------|---------------------|-------------------|-----------|
| Live book source | REST poll 5s + bootstrap (default) | WS_PRIMARY authoritative under `primary_enabled` | M8 **implemented** |
| Book timestamp | Wall clock at `apply_snapshot` | `received_ts` + optional `exchange_ts` | M2 |
| Freshness | `is_stale(max_age_s=5)` binary | DataQualityGate PASS/DEGRADED/EMERGENCY | M3 |
| Strategy reads | `MarketStateStore` via `read_leg_book` | Same interface; gate before decisions | M3, M6 |
| Stop trigger | Touch best bid | Same trigger logic; fresher book + logged age | M6, M7 |
| Exit planning | Planner VWAP from store snapshot | ExecutableBookView at size; fresh per retry | M4 |
| Scheduler | Hybrid event wake + debounce 75ms (`MarketUpdateCoordinator`) | Same; tune debounce in M9 if needed | M6 **done** |
| Latency facts | Full decision snapshot chain on material paired-binary paths | Live ack/fill chain completion | M7 **C0 done** |
| User WS | Live | Unchanged; sync with market WS age in facts | M7 |
| Strategy logic | paired_binary as-is | **Unchanged through M9** | — |

---

## Open questions blocking M1

| # | Question | Resolve in | Owner action |
|---|----------|------------|--------------|
| 1 | Polymarket market WS URL, subscribe payload, event field names | M1 spike + fixture capture | Capture live WS sample |
| 2 | Sequence/hash present in live WS payloads? | M1 live capture | Policy works either way (phase_2.md) |
| 3 | `crypto_5m` profile binding: strategy YAML vs market metadata | M3 | Config decision |
| 4 | `allow_rest_recovery_for_exit: false` for first prod run? | Before M8 | Deployment choice |
| 5 | Store full book or cap at top N on apply? | M2 | Performance vs depth |
| 6 | `exchange_ts` field name in Polymarket payload | M1 fixture | Parser mapping |
| 7 | **Baseline artifacts:** 2/3 control `facts.jsonl` missing locally | **M9 resolved Option C** | RECONSTRUCTED controls documented; optional Option B re-runs |

---

## M0 / Group A sign-off

| Status | Meaning |
|--------|---------|
| **`M9_PHASE_2_COMPLETE`** | Before/after report published; 2/3 controls RECONSTRUCTED with explicit provenance |
| **`READY_FOR_M1_FOUNDATION_WORK`** | Group A (M1/M2/M5) implemented; WS spike done |
| **`READY_FOR_M6_SCHEDULER_WORK`** | Group B (M3/M4/M7) implemented; observability on planner path |
| **`GROUP_C_COMPLETE`** | C0 M7 wiring on monitor/activation/entry; C1 M6 hybrid scheduler; REST authoritative; WS shadow-only |
| **`GROUP_D_IMPLEMENTED`** | M8 WS-primary cutover code + tests; **pending live validation** per m8_validation_plan.md |
| **`GROUP_E4_COMPLETE`** | E4 `m8_validation_002c` — **`M8_VALIDATED`** (strict freshness + latency + lifecycle) |

**M9 complete:** [baseline_vs_ws_primary_report.md](baseline_vs_ws_primary_report.md). Optional: Option B substituted REST controls for stronger quantitative baseline.

**Group B complete (M3/M4/M7):** DataQualityGate (observe_only default), ExecutableBookView, decision snapshots + latency facts on planner path.

**Group C complete (C0/C1):** Monitor/activation/entry material paths emit `decision_snapshot`; M6 hybrid scheduler with authoritative-store wake only.

**Group D (M8):** WS-primary cutover **validated** on E4 run `m8_validation_002c` — [m8_validation_report.md](m8_validation_report.md).
