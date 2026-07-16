# Z-Gap reachability and ownership audit

**Status:** Active (Phase 0 evidence)  
**Method:** Code and scripts as primary evidence; docs checked for contradictions  
**Date:** 2026-07-16  
**Branch inspected:** `pm_event_driven`

## 1. Entry points

| Entry | Path | Invokes | Z-Gap trading? |
|-------|------|---------|----------------|
| `tyrex-pm run` | `src/tyrex_pm/runtime/app.py` → `cmd_run` → `run_once.execute_run` | Full Path A bootstrap | Yes |
| `tyrex-pm record` | `runtime/record_run.cmd_record` | Market/event recording | No (PTB/calibration adjacent) |
| `tyrex-pm run_continue` | `runtime/run_continue.cmd_run_continue` | Paired Binary only | No |
| `scripts/go_z_gap_tiny_live.py` | → `ZGapSessionOrchestrator.run_session` | Path B orchestrator | Yes (operator path) |
| `scripts/run_z_gap_observe_batch.py` | Subprocess `python -m tyrex_pm.runtime.app run` | Path A per window | Yes (ops) |
| `scripts/run_z_gap_shadow_e2e.py` | `ShadowHarness` | In-process shadow | Test / shadow |
| `scripts/preflight_z_gap_live_scenario.py` | `validate_z_gap_live_scenario` | Gates only | Ops |
| Preflights | `preflight_binance_connectivity.py`, `preflight_clock_sanity.py`, `preflight_ptb_attestation.py` | Artifacts under `var/reporting/z_gap/` | Ops |

Wired strategy kinds: `_RUNTIME_WIRED_STRATEGY_KINDS` in `runtime/app.py` includes `STRATEGY_KIND_Z_GAP`.

## 2. Call graphs

### Path A — `tyrex-pm run` (complete bootstrap)

```
execute_run
  → load_app_config (+ validate_z_gap_live_config when enforce)
  → resolve_and_apply_btc_5m_metadata          # runtime/btc_5m_metadata.py
  → RuntimeCoordinator + OrderStore + WalletStore + AllocationLedger
  → TimeAuthority.sample_offset                # runtime/time_authority.py
  → market WS/REST bootstrap                   # market_data_runtime, run_market_ws_ingest
  → start_signal_feeds                         # signal_feed_runtime.py
       → SignalStateStore, Binance, RTDS, PriceToBeatTracker
       → select_ptb_source / persist_ptb_selection
  → run_z_gap_observe_loop  OR  run_z_gap_enforce_loop
       → run_observe_tick → evaluate_z_gap_entry
       → [enforce] entry_plan → process_intent_work_unit → OMS
       → [enforce] evaluate_exit_triggers → exit_plan → OMS
```

### Path B — `go_z_gap_tiny_live` (session orchestrator)

```
ZGapSessionOrchestrator.run_session
  → discover_next_window / apply_btc_5m_metadata_to_app
  → prepare_static_artifacts + approvals
  → bootstrap_session_runtime                  # z_gap_session_runtime.py
  → start_production_feeds → start_signal_feeds ONLY
  → warm_sigma_before_boundary
  → capture_ptb_at_boundary / boundary gate
  → run_post_boundary_runtime
       → run_z_gap_observe_loop OR run_z_gap_enforce_loop
```

### Shared decision core (verified)

`runtime/z_gap_run.run_observe_tick` → `strategies/z_gap/entry_eval.evaluate_z_gap_entry`  
(+ `quant/*` for σ, FV, fees, edge)

### Critical Path A vs Path B divergence (verified)

In `runtime/z_gap_session_runtime.py`:

- `start_production_feeds` docstring and body start **Binance + RTDS signal feeds only**.
- No `run_market_ws_ingest`, no market REST refresh loop, no user WS.
- `_build_coordinator` creates wallet/orders/OMS but **does not** attach `AllocationLedger`.
- `ensure_market_state_store` may create an empty store; books are not populated by this path.

**Interpretation:** Decision code is shared; bootstrap authorities for books, fills (user WS), and allocations are **not** equivalent. This violates the intended single-control-path invariant today.

## 3. Runtime modules with strategy decisions vs wiring

### Decision ownership (prefer keep under strategy)

| Symbol | File |
|--------|------|
| `evaluate_z_gap_entry` | `strategies/z_gap/entry_eval.py` |
| `build_z_gap_entry_plan`, `validate_z_gap_pre_submit` | `strategies/z_gap/entry_plan.py` |
| `evaluate_exit_triggers`, kill/flatten helpers | `strategies/z_gap/exit_eval.py` |
| `evaluate_thesis_stop` | `exit_policy/thesis_stop.py` |
| `build_z_gap_exit_work_unit` | `strategies/z_gap/exit_plan.py` |
| Lifecycle transitions | `strategies/z_gap/lifecycle.py` |
| PTB source policy | `strategies/z_gap/ptb_policy.py` |
| Model math | `quant/*` |

### Runtime decision wiring (should become thin host)

| Symbol | File |
|--------|------|
| `run_observe_tick`, `run_z_gap_observe_loop` | `runtime/z_gap_run.py` |
| `run_enforce_tick`, `run_z_gap_enforce_loop` | `runtime/z_gap_enforce.py` |
| Session/boundary orchestration | `runtime/z_gap_session_orchestrator.py` |
| Boundary PTB gate/capture | `runtime/z_gap_boundary_gate.py`, `z_gap_ptb_capture.py` |
| Live preflight validators | `runtime/z_gap_live.py`, `z_gap_live_preflight.py`, `z_gap_preflight.py` |

## 4. Sources of truth

| State | Authoritative owner today | Competing / incomplete writers |
|-------|---------------------------|--------------------------------|
| Instruments / tokens | `btc_5m_metadata.apply_btc_5m_metadata_to_app` → `ZGapStrategyConfig` | Scenario placeholders before resolve |
| Market books | `MarketStateStore` via WS/REST on Path A | Path B: store may exist without ingest |
| Signal / Binance / RTDS | `SignalStateStore` via `signal_feed_runtime` | Single writer (good) |
| PTB live | `SignalStateStore` + `PriceToBeatTracker` + `ptb_policy` | Lock file `ZGapPtbStore` (`var/state/z_gap_ptb_lock.json`); attestation JSON; chainlink tick log |
| Clock | `RuntimeCoordinator.time_authority` (`TimeAuthority`) | Separate `clock_sanity.json` artifact (ops, not runtime clock) |
| Orders | `OrderStore` + OMS / pipeline | Path A user WS + REST reconcile; Path B lacks user WS |
| Fills | OrderStore matched qty + pipeline callbacks; lifecycle `reconcile_*_fill` | Shadow local fill flag |
| Positions | `AllocationLedger` (Path A) + `WalletStore` + `ZGapLifecycleState.active_quantity` | Path B missing ledger; lifecycle JSON is not full restart restore |
| Strategy lifecycle | In-memory `ZGapLifecycleState`; persist `var/reporting/z_gap/lifecycle_state.json` | Startup asserts terminal; does not restore open position |

## 5. Exit paths (enforce)

Precedence in `strategies/z_gap/exit_eval.evaluate_exit_triggers`:

1. `evaluate_kill_switch` — manual intervention, market-data critical, lifecycle failure, max loss (when wired)
2. `evaluate_thesis_stop` — z-stop with confirmation (`exit_policy/thesis_stop.py`)
3. `evaluate_lifecycle_flatten` — `tau_s <= flatten_before_event_end_s`

All submit via `build_z_gap_exit_work_unit` → `process_intent_work_unit`.  
Observe-only: no exits.

**Out of Z-Gap active architecture:** `protection/` TP/SL and `survival/` paired-survivor frameworks. Do not import them into the new Z-Gap exit interface.

## 6. Scheduling

| Mechanism | Location | Notes |
|-----------|----------|-------|
| Session next window | `ingestion/btc_5m_window_scheduler` via orchestrator | Path B |
| Observe batch | `scripts/run_z_gap_observe_batch.py` | Spawns Path A |
| Late-start skip | `z_gap_run.is_late_window_start` | Path A; skipped post-boundary on Path B |
| Explicit event URL | `resolve_btc_5m_event_metadata` | Both |
| PB `run_continue` | `runtime/run_continue.py` | **Out of scope** |

No first-class continuous Z-Gap daemon equivalent to `run_continue`.

## 7. Persistence

| Path | Owner |
|------|-------|
| `var/reporting/runs/<run>/facts.jsonl` | `JsonlSink` |
| `var/reporting/z_gap/*.json` | Preflight / orchestrator artifacts |
| `var/reporting/z_gap/lifecycle_state.json` | Enforce lifecycle persist |
| `var/reporting/z_gap/calibration_samples.jsonl` | Calibration append |
| `var/state/z_gap_ptb_lock.json` | `ZGapPtbStore` |
| `var/state/chainlink_ticks.jsonl` | PTB / tick logger |
| `var/state/allocation_ledger.json` | Path A allocation |

`tyrex-pm reset-state` does **not** clear Z-Gap PTB lock or chainlink ticks.

## 8. Configuration

- Models: `ZGapStrategyConfig` and nested configs in `runtime/config.py`
- Modes allowed in code: `observe_only` | `enforce` only
- Primary YAML: `config/strategies/z_gap.yaml`
- Scenarios: `live_z_gap_observe.yaml`, `live_z_gap_tiny.yaml`, `shadow_z_gap_enforce_e2e.yaml`

## 9. Reporting

Fact types in `reporting/schema_v2.py` + emitters in `strategies/z_gap/facts.py` and runtime wrappers.  
Calibration: `strategies/z_gap/calibration_samples.py`; offline `research/z_gap/calibration_lite.py`.

## 10. Documentation / code contradictions

1. Phase C docs mention `shadow_evaluate` entry mode; code allows only `observe_only` | `enforce`.
2. Runbooks treat `go_z_gap_tiny_live` as primary A0.8 path; Path B bootstrap ≠ Path A for books/ledger/user WS.
3. `start_production_feeds` name implies full production stack; implementation is signal feeds only.
4. Root `README.md` / `Docs/Architecture.md` still describe thin `on_signal` / guru-centric layout; Z-Gap bypasses `Strategy.on_signal` (`strategies/z_gap/strategy.py` is OMS callback handle).
5. `Docs/modules/strategies/README.md` historically called paired_binary “design only” while production code exists (doc lag).

## 11. Classification (Z-Gap-touched)

| Component | Class | Evidence |
|-----------|-------|----------|
| `z_gap_run` / `z_gap_enforce` / `entry_eval` / `quant/*` | Live-critical | Both live paths |
| Path A market WS + allocation + user stream | Live-critical | `execute_run` |
| Path B orchestrator + preflight artifacts | Live-critical ops / transitional | `go_z_gap_tiny_live` |
| `shadow_harness` / `scenario_oms` | Test / shadow | scripts + pytest |
| `run_z_gap_placeholder_loop` | Dead/alias | Thin wrapper |
| `research/z_gap/*`, analyze scripts | Research | Offline |
| `protection/`, `survival/` | Out of Z-Gap scope | Not on Z-Gap exit path |

## 12. Coverage gaps

- Path B was not executed live in this audit; book/ledger gaps are static code findings.
- Full `process_intent_work_unit` branch coverage for Z-Gap intents not line-traced.
- Kill-switch fields sometimes default false at call sites; production population not fully mapped.
- Sidecar process does not appear to populate CLOB books (health only).
