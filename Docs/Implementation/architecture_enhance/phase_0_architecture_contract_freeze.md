# Phase 0 — Architecture contract freeze

**Program:** [README.md](README.md) · **Next:** [phase_1_generic_signal_strategy_dispatch.md](phase_1_generic_signal_strategy_dispatch.md)

> **Procedural now, event-ready later.** This phase changes **documentation only**. No runtime behavior changes.

---

## 1. Goal

Freeze the architecture decisions in the top-level docs so that every later phase has one unambiguous contract to implement against. After this phase, the docs describe a **generic procedural spine** with guru as one strategy, the event bus as deferred, and the planner / market store / protection engine / fill-finality / kill-switch semantics as written-down roadmap.

---

## 2. Why this phase exists

Today there are two competing architecture stories:

- `Docs/Architecture.md` and the actual code → **procedural** runtime centered on guru copy.
- `Docs/Implementation/native_pm_rebuild/ARCHITECTURE.md` → **bus-centric**, event-driven vision.

The review called this a "split brain". Writing code before resolving it would bake the ambiguity into new modules. Phase 0 picks **procedural + generic spine** and rewrites the docs so a new developer cannot mistake guru copy for the architecture, or the `EventBus` for the active runtime.

---

## 3. Current state

| Doc | Guru / bus coupling today |
|-----|---------------------------|
| `Docs/Architecture.md` | §2 "Strategies never touch the venue" example is `GuruFollowStrategy`; §3 runtime diagram routes guru poll → `GuruCopySignal` → `GuruFollowStrategy` as the main path; §6 lists `GuruTradeSignal` first. |
| `Docs/LIVE_ARCHITECTURE.md` | Strong on venue/local truth; **no** explicit trade-finality (MATCHED/MINED/CONFIRMED) section. |
| `Docs/developer_guide.md` | §3 strategy contract is `on_guru_signal`. |
| `Docs/modules/strategies/README.md` | Opens with `guru_follow`; others framed as harnesses. |
| `Docs/modules/signals/README.md` | Only `GuruCopySignal` documented. |
| `Docs/Implementation/native_pm_rebuild/ARCHITECTURE.md` | Presents a bus-centric runtime as the "final" target. |
| `core/bus.py` | `EventBus` defined but **never subscribed** anywhere in `src/`. |
| Code reality | `runtime/pipeline.py::process_new_guru_signals` + `process_intent_work_unit`; per-strategy loops in `runtime/app.py` (`_run_sell_test_loop`, `_run_tp_sl_test_loop`, guru poll loop). |

---

## 4. Target state

After Phase 0 the docs assert, consistently:

1. The **official runtime is procedural**:
   `Ingestion → Signal → Strategy → Intent → RiskEngine → ExecutionPlanner → SingleWriterOMS → Venue`.
2. `EventBus` is **deferred**; `facts.jsonl` is the audit/event surface.
3. Guru is **one strategy**:
   `guru_follow = one strategy`, `GuruCopySignal = one signal`, `guru_stream = one ingestion adapter`.
4. `ExecutionPlanner` **will become a mandatory layer** (Phase 3).
5. `MarketStateStore` **will become a central shared component** (Phase 2).
6. **Trade-finality rules** are documented (MATCHED/MINED/CONFIRMED/RETRYING/FAILED).
7. **Soft vs hard kill-switch** semantics are documented (implementation later, Phase 6).

---

## 5. Non-goals

- No code changes, no new modules, no test changes.
- No deletion of `native_pm_rebuild/` docs — they are re-labeled as historical/aspirational, not active.
- No event-bus wiring or design beyond "deferred".
- No renaming of existing code symbols (`on_guru_signal` etc.) — that is Phase 1.

---

## 6. Files likely to change

```text
Docs/Architecture.md                                  # reframe spine; demote guru; add roadmap section
Docs/LIVE_ARCHITECTURE.md                             # add trade-finality section
Docs/developer_guide.md                               # generic strategy contract note + "guru is one strategy"
Docs/modules/strategies/README.md                     # lead with generic contract; guru as example
Docs/modules/signals/README.md                        # signal is generic; GuruCopySignal is one type
Docs/CONFIG_MODEL.md                                  # (if needed) note future planner/protection/kill keys
Docs/OPERATIONS.md                                    # (if needed) note future hard-kill runbook stub
Docs/reporting_fact_model.md                          # (if needed) reserve future fact names
Docs/Implementation/native_pm_rebuild/README.md       # add banner: historical/aspirational, superseded by architecture_enhance
Docs/Implementation/current_state.md                  # link to this program as the active roadmap
```

---

## 7. New files likely to be added

```text
(none beyond this architecture_enhance/ doc set)
```

This phase only writes docs; the program's own files were created with the README.

---

## 8. Data model changes

None. (No `core/models.py`, `core/enums.py`, or config dataclass changes in Phase 0.)

Document, but do **not** implement, the intended future additions so later phases inherit names:

- `ExecutionPlan` → `execution/models.py` (Phase 3); dedicated `validate_planned_order` → `risk/planned_order.py`.
- `MarketBookSnapshot` / `MarketStateStore` → `state/market_store.py` (Phase 2).
- `Signal` base/union + `StrategyResult` → `signals/base.py` / `strategies/base.py` (Phase 1).
- Fill-finality helper → `state/fill_state.py` (Phase 3.5).

---

## 9. Config changes

None implemented. Document **reserved** future config namespaces so operators are not surprised:

```yaml
# RESERVED — documented in Phase 0, implemented later
execution:
  planner: {}        # Phase 3
protection: {}       # Phase 4
risk:
  kill_switch:
    mode: soft       # soft | hard — Phase 6
```

---

## 10. Fact/reporting changes

None implemented. **Reserve** future fact-type names in `reporting_fact_model.md` so later phases do not collide:

```text
signal_received         # Phase 1 (generic non-guru sources; guru_signal retained)
execution_plan          # Phase 3
protection_register     # Phase 4
protection_tick         # Phase 4
protection_trigger      # Phase 4
fill_finality           # Phase 3.5 (or folded into existing facts)
kill_switch             # Phase 6
```

Existing facts (`guru_signal`, `intent_created`, `risk_decision`, `oms_submit`, `oms_reject`, `oms_cancel`, `oms_result`, `reconcile`, `wallet_sync`, `exit_lifecycle`, `allocation_ledger`, `health`) are unchanged.

---

## 11. Tests to add

None (docs-only). Optionally add a lightweight docs guard later, but it is **not** required for Phase 0:

```text
(optional, deferred) tests/test_docs_no_bus_as_runtime  # greps top-level docs for forbidden phrasing
```

---

## 12. Acceptance criteria

- No top-level doc (`Architecture.md`, `developer_guide.md`, `LIVE_ARCHITECTURE.md`, `modules/*/README.md`) presents `guru_follow` as the architecture spine.
- No doc presents `EventBus` as the active runtime; it is labeled deferred.
- The target architecture is presented as a single linear spine including `ExecutionPlanner`.
- A future event-based migration is mentioned **only** as deferred/future.
- `LIVE_ARCHITECTURE.md` contains an explicit trade-finality section.
- Soft/hard kill semantics are written down (even though unimplemented).
- `native_pm_rebuild/` docs carry a banner marking them historical/superseded by this program.

---

## 13. Migration risks

- **Doc drift vs code:** docs will describe layers (planner, market store) that do not exist yet. Mitigate by clearly tagging those sections "Roadmap — not yet implemented (Phase N)".
- **Broken internal links** when reframing `Architecture.md`. Mitigate by checking all relative links after editing.
- **Confusion with `native_pm_rebuild/`** if not clearly demoted. Mitigate with an explicit banner.

---

## 14. Rollback strategy

Docs-only and version-controlled. Rollback = `git revert` of the Phase 0 doc commit. No runtime impact, so rollback is risk-free.

---

## 15. Dependencies on previous phases

None. Phase 0 is the entry point and a hard prerequisite for Phases 1–6 (they implement against the contract it freezes).

---

## 16. Event-ready design notes

Phase 0 establishes the principle the whole program follows: **procedural now, event-ready later.** The docs must state that every component added later exposes a pure-ish method a future bus could call unchanged:

```python
Strategy.on_signal(signal, ctx) -> StrategyResult
ProtectionEngine.on_market_update(snapshot, ctx) -> list[ExitIntent]
ExecutionPlanner.plan(approved_intent, ctx) -> ExecutionPlan
RiskEngine.evaluate_intent(intent, ctx) -> RiskDecision
```

The docs must **not** propose a bus implementation path. `EventBus` is named only to say: it exists, it is unwired, it is deferred.
