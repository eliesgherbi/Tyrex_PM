# Architecture enhancement program

**Status:** Phases 1, 2, 3, 3.5, 4, and **4.5** are **implemented** (package + pipeline + unit tests). **Phase 4.6** is **implemented · shadow-validated** (paired binary strategy + `PairedBinaryMonitor` + persistence/recovery); live PB-Level ladder **not yet run**. Phases 5 (portfolio foundation) and 6 (hard kill switch) remain planning-only.

---

## 1. Purpose

Tyrex_PM is already strong on the hard live-trading parts — `RiskEngine`, reconcile state machines, user-WS truth, `AllocationLedger`, `SingleWriterOMS`, and `facts.jsonl`. It is weak on **architectural extensibility** and **execution intelligence**: there is no first-class execution planner, market data is not yet a shared component, TP/SL is only a test harness (`tp_sl_test`), and strategy plumbing is still shaped around guru copy.

This program **freezes the current safe spine** and adds the missing production components in **one clean direction**, phase by phase. It is not a rebuild.

---

## 2. Locked target architecture

Current (safe) spine — stays:

```text
Ingestion → Signal → Strategy → Intent → RiskEngine → SingleWriterOMS → Venue
```

Target spine — adds the planner:

```text
Ingestion
  → Signal
  → Strategy
  → Intent
  → RiskEngine pre-check
  → ExecutionPlanner
  → final planned-order validation
  → SingleWriterOMS
  → Venue
```

Live-truth spine — stays:

```text
User WS / REST
  → WalletStore / OrderStore
  → Reconcile
  → facts.jsonl
```

Market-data spine — becomes real:

```text
Market WS / REST snapshots
  → MarketStateStore
  → ExecutionPlanner
  → ProtectionEngine
  → Risk marks
```

Protection spine — becomes a real overlay:

```text
Position / Allocation / MarketState
  → ProtectionEngine
  → ExitIntent
  → RiskEngine
  → ExecutionPlanner
  → SingleWriterOMS
```

---

## 3. Phases

| Phase | File | Status | Outcome |
|-------|------|--------|---------|
| **0** | [phase_0_architecture_contract_freeze.md](phase_0_architecture_contract_freeze.md) | docs | Docs frozen around the generic spine; guru demoted; planner/market-store/protection declared as roadmap; fill-finality + kill-switch semantics written down. |
| **1** | [phase_1_generic_signal_strategy_dispatch.md](phase_1_generic_signal_strategy_dispatch.md) | **implemented · CLI-runnable** | Generic `Signal` + `Strategy.on_signal` + `process_signals()`; guru runs through it; `simple_signal_test` non-guru harness wired via `fixture_signal_run.py`. |
| **2** | [phase_2_market_state_store.md](phase_2_market_state_store.md) | **implemented · CLI-runnable (P4.5)** | `MarketStateStore` + ingest parsers + REST bootstrap; wired in `app.py` when `market_data.enabled` (REST refresh loop; WS ingest still optional). |
| **3** | [phase_3_execution_planner.md](phase_3_execution_planner.md) | **implemented · shadow CLI (entry) · harness (urgent/stale)** | `ExecutionPlanner` in pipeline when enabled; entry live-validated via `simple_signal_test`; urgent/stale via `validation_harness`. |
| **3.5** | [phase_3_5_fill_finality_helper.md](phase_3_5_fill_finality_helper.md) | **implemented · unit-tested · live indirect** | `fill_state.py` + user_stream refactor; protection registration wired via `maybe_register_protection_after_buy`. |
| **4** | [phase_4_protection_engine.md](phase_4_protection_engine.md) | **implemented · harness-runnable · unit-tested** | `protection/` package; registration + tick routing wired for harness modes; periodic live tick supervisor deferred. |
| **4.5** | [phase_4_5_live_validation_harness.md](phase_4_5_live_validation_harness.md) | **implemented · L1–2 live-validated · L3/5/6 live-ready** | `validation_harness` + `FinalityWaiter` + `ProtectionSupervisor`; [wiring plan](phase_4_5_live_validation_wiring_plan.md) approved and implemented. |
| **4.6** | [phase_4_6_paired_binary_strategy_production_protection.md](phase_4_6_paired_binary_strategy_production_protection.md) | **implemented · shadow-validated · live-ready** | `paired_binary` strategy + `PairedBinaryMonitor` + persistence/recovery; first production long-running monitor loop outside harness. |
| **5** | [phase_5_portfolio_foundation.md](phase_5_portfolio_foundation.md) | planning (read-only work may proceed; production protection on guru blocked until P4.6) | Minimal `portfolio/` foundation: read-only `positions_view`, attribution scaffold. Full PnL deferred. |
| **6** | [phase_6_hard_kill_switch.md](phase_6_hard_kill_switch.md) | planning | Soft vs hard kill split; hard kill cancels all open orders through `SingleWriterOMS`; kill-switch facts + runbook. Does **not** depend on Phase 5. |

**Live/shadow verification:** [live_validation_matrix.md](live_validation_matrix.md) — what is CLI-runnable, live-runnable, unit-only, and runtime-unwired.

---

## 4. Global non-negotiable constraints

These hold in **every** phase. Any plan that violates one is wrong.

1. Strategies never call venue clients.
2. Strategies never submit/cancel orders directly.
3. Strategies never mutate `WalletStore`, `OrderStore`, or `AllocationLedger`.
4. Protection never submits/cancels directly.
5. Protection never mutates allocation directly.
6. `RiskEngine` remains the mandatory gate.
7. `SingleWriterOMS` remains the only submit/cancel writer.
8. `AllocationLedger` remains mandatory for all SELL paths.
9. Every SELL clamps to `min(planned_size, owner_allocation, venue_available_to_sell)`.
10. User WS remains primary live truth; REST remains bootstrap/repair.
11. `facts.jsonl` remains the operator audit surface.
12. Do not introduce a full event bus now.
13. Do not add multi-venue routing now.
14. Do not build a dashboard now.
15. Do not build ML strategies now.
16. Do not add production TP/SL directly inside `guru_follow`.

---

## 5. Decision: procedural now, event-ready later

The procedural runtime is the **official** architecture for this program. `core/bus.py` (`EventBus`) exists but is **not wired** and stays deferred. The review called the current situation a “split brain” (procedural code vs a bus-centric native-rebuild doc); Phase 0 resolves it in favor of procedural.

`facts.jsonl` is the operational event/audit surface — not an internal pub/sub bus.

Every component added by this program must be **event-ready without an event bus**. That means each one exposes a clean, side-effect-scoped method that a future bus could call unchanged:

```python
Strategy.on_signal(signal, ctx) -> StrategyResult
ProtectionEngine.on_market_update(snapshot, ctx) -> list[ExitIntent]
ExecutionPlanner.plan(approved_intent, ctx) -> ExecutionPlan
RiskEngine.evaluate_intent(intent, ctx) -> RiskDecision
```

Runtime calls these methods directly today. No phase introduces a bus, subscriptions, or multiple async consumers.

> **Principle to repeat in every phase: _Procedural now, event-ready later._**

---

## 6. Decision: guru is not the architecture center

`guru_follow` was the bootstrap strategy. From now on it is **one ordinary strategy among others**.

```text
guru_follow    = one strategy implementation
GuruCopySignal = one signal type
guru_stream    = one ingestion adapter
```

Architecture prose must read `Signal → Strategy → Intent`, never `GuruCopySignal → GuruFollowStrategy → Intent`. Architecture-level tests use a **simple non-guru harness** (suggested names: `simple_signal_test`, `manual_signal_test`, `fixture_signal_test`, `basic_entry_exit_test`) so "strategy" never silently means "guru strategy".

---

## 7. Implementation order

Strictly sequential where dependencies exist:

```text
Phase 0    (docs)            → no code dependency; do first
Phase 1    (dispatch)        → depends on Phase 0 contract
Phase 2    (market store)    → depends on Phase 1 (ctx surface)
Phase 3    (planner)         → depends on Phase 2 (book state)
Phase 3.5  (fill finality)   → can start after Phase 1; MUST land before Phase 4
Phase 4    (protection)      → depends on Phase 2 + Phase 3 + Phase 3.5
Phase 4.5  (validation harness) → depends on Phase 2 + Phase 3 + Phase 4; MUST land before Phase 4.6
Phase 4.6  (paired binary)    → depends on Phase 4.5; MUST land before production protection on long-running strategies
Phase 5    (portfolio)       → depends on Phase 3.5; read-only work may proceed in parallel; production enablement gated on P4.6
Phase 6    (hard kill)       → depends on Phase 3/4 pipeline; does NOT need Phase 5
```

**Why Phase 3.5 before Phase 4:** protection must register only after ownership is *final* (`allocation_buy_applied`), never on submit-ack or MATCHED-only evidence. The finality helper defines that boundary, so it is a hard prerequisite for the protection overlay.

Each phase must keep `guru_follow`, `sell_test`, `allocation_test`, and `tp_sl_test` green before merge.

---

## 8. What not to build now

- No full event bus / subscriptions / multi-consumer runtime.
- No multi-venue routing (the `execution/router.py` stub stays a stub).
- No dashboard / UI.
- No ML strategies.
- No complex cancel/replace execution algos.
- No production TP/SL inside `guru_follow`.
- No strategy or protection code that fetches books or calls venue clients.
- No protection that bypasses `RiskEngine` or `AllocationLedger`.
- No wallet-wide SELL mode.

---

## 9. Definition of done (whole program)

The program is complete when:

1. **Docs** present a generic `Signal → Strategy → Intent → Risk → Planner → OMS` spine; guru is a documented case study, not the spine; the bus is documented as deferred. (Phase 0)
2. **Dispatch** is generic: adding a *new production feature* requires no new loop in `runtime/app.py`. Existing harnesses may remain legacy until their relevant phase replaces them. (Phase 1)
3. **Audit symmetry**: non-guru signal sources are visible before `intent_created` via a generic `signal_received` fact; `guru_signal` is retained for back-compat. (Phase 1)
4. **Market state** is a single shared store; no strategy/test owns book-fetch logic. (Phase 2)
5. **Execution style/price/size** sent to OMS always originates from an `ExecutionPlan` that was validated by a **dedicated planned-order validator** preserving the pre-check `client_order_id`. (Phase 3)
6. **Fill finality** rules are explicit in one helper; MATCHED is never treated as final ownership/PnL; FAILED releases reservations without applying allocation. (Phase 3.5)
7. **TP/SL** is a reusable `protection/` overlay attached by `owner_id`, registered only after `allocation_buy_applied`, allocation-aware, dedup-safe; `tp_sl_test` is only a regression harness. (Phase 4)
8. **Portfolio foundation** (read-only positions view + attribution scaffold) exists; full PnL deferred. (Phase 5)
9. **Hard kill** cancels all open orders through `SingleWriterOMS` with full fact coverage and a runbook; soft kill still allows protective exits; does not depend on Phase 5. (Phase 6)
10. All existing strategies and the live-truth/reconcile guarantees remain intact throughout.
