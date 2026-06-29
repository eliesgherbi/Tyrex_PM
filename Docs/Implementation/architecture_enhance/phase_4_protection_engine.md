# Phase 4 — ProtectionEngine

**Program:** [README.md](README.md) · **Prev:** [phase_3_5_fill_finality_helper.md](phase_3_5_fill_finality_helper.md) · **Next:** [phase_4_5_live_validation_harness.md](phase_4_5_live_validation_harness.md)

> **Procedural now, event-ready later.** `on_market_update(...) -> list[ExitIntent]`; no bus.

---

## 1. Goal

Promote TP/SL from the `tp_sl_test` harness to a reusable **production `protection/` overlay** that attaches by `owner_id`, reads allocation + venue position + `MarketStateStore`, and emits `ExitIntent` only. Protection rides the same pipeline as every other exit: `RiskEngine → ExecutionPlanner → SingleWriterOMS`.

---

## 2. Why this phase exists

The review and `tp_sl_overlay_plan.md` agree: `tp_sl_test` is a correct **validation harness** but the wrong home for production TP/SL. Production protection must:

- protect **any** owner's allocation (not be guru-specific, not be a separate run mode),
- not double-sell on repeated triggers,
- never bypass risk or allocation,
- reuse the planner (Phase 3) for correct urgent-exit execution.

Without a dedicated overlay, every strategy that wants stops would fork exit logic.

---

## 3. Current state

```text
strategies/tp_sl_test/strategy.py   → one BUY → monitor price → allocation-aware ExitIntent on trigger
                                       has its own _run_tp_sl_test_loop in runtime/app.py
strategies/sell_test/pricing.py     → SELL pricing reused by tp_sl_test
runtime/exit_lifecycle.py           → emit_exit_lifecycle, matched-status parsing (shared)
runtime/allocation_exit_lifecycle.py→ allocation reservation/exit linkage
state/allocation_ledger.py          → owner allocation truth (get_available_allocated, reservations)
Docs/Implementation/sell_feature/tp_sl_overlay_plan.md → approved design, deferred (P6)
```

The pieces exist but are tied to a test strategy and a bespoke loop. Phase 1 (generic dispatch), Phase 2 (market store), and Phase 3 (planner) provide the seams to lift this out.

---

## 4. Target state

```text
protection/
  config.py        # ProtectionConfig: owner_id, trigger rules (abs/pct), price source, exit sizing
  registry.py      # active protections keyed by (owner_id, token_id); register/unregister; dedup
  monitor.py       # ProtectionEngine.on_market_update(snapshot, ctx) -> list[ExitIntent]
  trigger_eval.py  # pure trigger math (TP/SL/trailing), absolute & percentage
  sizing.py        # exit size modes, clamped to allocation + available_to_sell
  lifecycle.py     # registration/trigger/exit/terminal fact emission + dedup state
```

Flow:

```text
BUY → CONFIRMED (fill-finality FINAL) → allocation_buy_applied (existing runtime hook)
  → ProtectionEngine.register(owner_id, token_id, rules, entry_ref)
  → market tick updates MarketStateStore
  → ProtectionEngine.on_market_update(snapshot, ctx)
       trigger_eval crosses threshold
       sizing clamps to min(planned, owner_allocation, available_to_sell)
       emit ExitIntent (urgency="urgent")
  → process_intent_work_unit  (generic pipeline)
  → RiskEngine → ExecutionPlanner (FAK worst price) → validate_planned_order → SingleWriterOMS
```

`tp_sl_test` remains a **regression harness** validating this overlay, not the production code path.

### Registration boundary (locked)

Protection registers **only after actual allocation credit**, using the Phase 3.5 fill-finality helper. It must **not** register on weaker evidence:

| Event | Register protection? |
|-------|:--:|
| OMS submit ack | **No** |
| Resting BUY (live, no fill) | **No** |
| MATCHED-only evidence | **No** (unless the allocation policy explicitly credits MATCHED, which it does not today) |
| `allocation_buy_applied` (CONFIRMED ⇒ `FillFinality.FINAL`) | **Yes** |

> Preferred safe rule: **protection registers after `allocation_buy_applied`.** This keeps protection aligned with `AllocationLedger` and the fill-finality table, so a stop can never fire against phantom inventory.

---

## 5. Non-goals

- No direct submit/cancel from protection (constraint #4).
- No allocation mutation from protection (constraint #5) — runtime hooks (`allocation_runtime.py`) still own writes.
- No guru-specific coupling: protection works for `simple_signal_test`, `sell_test`, guru, etc.
- No new exit pipeline — reuse `process_intent_work_unit`.
- No `mark` price source (only `fixture` for tests and `best_bid`/book via MarketStateStore for live), matching `tp_sl_overlay_plan.md`.
- No event bus.

---

## 6. Files likely to change

```text
src/tyrex_pm/runtime/app.py                       # start ProtectionEngine monitor tick; remove need for tp_sl loop
src/tyrex_pm/runtime/coordinator.py               # expose protection registry + market_state
src/tyrex_pm/runtime/allocation_runtime.py        # call protection.register at allocation_buy_applied (CONFIRMED only)
src/tyrex_pm/runtime/pipeline.py                  # protection ExitIntents flow through process_intent_work_unit
src/tyrex_pm/strategies/tp_sl_test/strategy.py    # reduce to harness; reuse protection trigger_eval where possible
src/tyrex_pm/runtime/config.py                    # ProtectionConfig parsing
Docs/modules/README.md                            # add protection module
Docs/Implementation/sell_feature/tp_sl_overlay_plan.md  # mark superseded by protection/ (this phase)
Docs/reporting_fact_model.md                      # protection facts
```

---

## 7. New files likely to be added

```text
src/tyrex_pm/protection/__init__.py
src/tyrex_pm/protection/config.py
src/tyrex_pm/protection/registry.py
src/tyrex_pm/protection/monitor.py
src/tyrex_pm/protection/trigger_eval.py
src/tyrex_pm/protection/sizing.py
src/tyrex_pm/protection/lifecycle.py
Docs/modules/protection/README.md
config/protection/default.yaml                    # optional default protection config
tests/test_protection_engine.py
```

---

## 8. Data model changes

In `protection/` (not `core`):

```python
@dataclass(frozen=True)
class ProtectionRule:
    owner_id: str
    token_id: TokenId
    # absolute OR percentage (mutually exclusive, validated at load — mirror tp_sl_overlay_plan §B2)
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    take_profit_pct: Decimal | None
    stop_loss_pct: Decimal | None
    trigger_reference: str            # "entry_price"
    price_source: str                 # "fixture" | "best_bid"
    exit_size_mode: str               # full_allocated | percent_allocated | fixed_size
    exit_size_value: Decimal | None

@dataclass
class ProtectionState:               # registry value; mutable dedup/lifecycle
    rule: ProtectionRule
    entry_price: Decimal
    armed: bool
    triggered: bool                   # dedup guard: a fired trigger cannot fire twice
    exit_reservation_id: str | None
```

The registry's `triggered` flag + allocation reservation are the **double-sell guard**.

---

## 9. Config changes

```yaml
# config/protection/default.yaml (and/or per-strategy protection: block)
protection:
  enabled: false
  rules:
    - owner_id: guru_follow
      price_source: best_bid
      take_profit_pct: "0.20"
      stop_loss_pct: "0.10"
      trigger_reference: entry_price
      exit_size_mode: full_allocated
  monitor_interval_s: 1.0
  exit_urgency: urgent          # feeds ExecutionPlanner → FAK
```

`runtime/config.py`: typed `ProtectionConfig`. Default disabled. Config load rejects mixing absolute + percentage on the same side (reuse `tp_sl_overlay_plan` validation).

---

## 10. Fact/reporting changes

Add fact types in `reporting/schema_v2.py`:

```text
FACT_TYPE_PROTECTION_REGISTER = "protection_register"   # owner_id, token_id, rule, entry_price
FACT_TYPE_PROTECTION_TICK     = "protection_tick"       # observed price vs thresholds (DEDUPED)
FACT_TYPE_PROTECTION_TRIGGER  = "protection_trigger"    # which threshold, planned exit size
```

The actual exit reuses existing `intent_created`, `risk_decision`, `execution_plan`, `oms_submit`, `exit_lifecycle`, `allocation_ledger` facts. `protection_tick` must be **deduped by signature** (like `reconcile`/`wallet_sync`) so monitor ticks do not flood `facts.jsonl`.

---

## 11. Tests to add

```text
tests/test_protection_engine.py
  test_protection_registers_after_allocation_buy_applied
  test_protection_does_not_register_on_submit_ack
  test_protection_does_not_register_on_resting_buy
  test_protection_does_not_register_on_matched_only
  test_take_profit_triggers_exit_intent
  test_stop_loss_triggers_exit_intent
  test_protection_exit_clamps_to_owner_allocation
  test_duplicate_trigger_does_not_double_sell
  test_protection_exit_uses_execution_planner
  test_protection_exit_goes_through_risk_engine
  test_protection_non_guru_owner_supported          # simple_signal_test owner
  test_protection_stale_book_does_not_emit_false_trigger
  test_protection_tick_fact_deduped
```

---

## 12. Acceptance criteria

- A simple non-guru position (`simple_signal_test`) can register TP/SL protection — no guru coupling.
- Protection registers **only** at `allocation_buy_applied` (CONFIRMED); never on submit-ack, resting BUY, or MATCHED-only evidence.
- TP and SL triggers each produce an `ExitIntent`.
- The `ExitIntent` flows through the generic pipeline → risk → planner → OMS.
- Exit size is clamped to `min(planned, owner_allocation, available_to_sell)`.
- Duplicate triggers do not double-sell (registry `triggered` + allocation reservation).
- `guru_follow` can attach protection without a custom run loop.
- `tp_sl_test` still passes as a regression harness.
- Facts show: registration, (deduped) monitor tick, trigger, exit intent, and terminal outcome.

---

## 13. Migration risks

- **Phantom-inventory registration** if protection registers before ownership is final (submit-ack / resting / MATCHED). A stop could fire against a position that does not exist. Mitigate: register **only** at `allocation_buy_applied` per Phase 3.5; `test_protection_does_not_register_on_*`.
- **Double-sell** if dedup/reservation is wrong. Mitigate: reserve allocation at trigger time (`maybe_reserve_exit_allocation`) and set `triggered=True` before emitting; `test_duplicate_trigger_does_not_double_sell`.
- **Race with guru mirror SELL** selling the same allocation. Both go through `AllocationLedger` clamps and `process_intent_work_unit`; the ledger is the arbiter. Test concurrent paths.
- **False triggers on stale book.** Protection must consult `MarketStateStore.is_stale` and skip triggering on stale data (or use the planner's fail-closed path).
- **Lifecycle leaks:** protection not unregistered after full exit/resolution. `lifecycle.py` must unregister on terminal exit and on market resolution.
- **Tp_sl_test divergence:** keep harness behavior identical by sharing `trigger_eval.py`.

---

## 14. Rollback strategy

- Feature flag `protection.enabled=false` → no monitor started, no registrations; runtime identical to pre-Phase-4.
- Protection is additive (new package). Rollback = disable flag or revert commit; guru/sell_test/tp_sl_test unaffected.

---

## 15. Dependencies on previous phases

- **Phase 1** — `ExitIntent` enters via generic `process_intent_work_unit`; protection is a producer of intents, not a strategy loop.
- **Phase 2** — `MarketStateStore` provides trigger price source and staleness.
- **Phase 3** — `ExecutionPlanner` + `validate_planned_order` give urgent exits correct, revalidated FAK worst-price execution.
- **Phase 3.5** — fill-finality helper defines the `allocation_buy_applied` registration boundary protection depends on. **Hard prerequisite.**

Provides for: production stops/exits used by guru and future strategies; exit flows that Phase 5 portfolio attribution observes.

---

## 16. Event-ready design notes

`ProtectionEngine.on_market_update(snapshot, ctx) -> list[ExitIntent]` is a pure function of `(snapshot, registry state, allocation, config)` that **returns** intents rather than submitting them. Today the monitor loop calls it on a timer / after market updates; a future event bus could deliver `MarketBookSnapshot` events to the same method unchanged. Registration is triggered by an existing runtime hook (post-BUY allocation), which a future bus could replace with an `AllocationApplied` event. No bus added.

---

## Implementation status

**Status: implemented · runtime-unwired · unit-tested.**

**Implementation summary.** The `protection/` package implements a strategy-agnostic TP/SL overlay: `config.py`, `trigger_eval.py`, `sizing.py`, `registry.py` (`register_if_allocation_final` gates on CONFIRMED only), `lifecycle.py`, and `monitor.py` (`ProtectionMonitor.tick`). Emitted exits are designed to flow through `process_intent_work_unit` → RiskEngine → ExecutionPlanner → OMS — **but the monitor loop and registration hook are not wired in `app.py`.**

**Files changed.** `protection/*` (new), `reporting/schema_v2.py` (protection facts), `runtime/allocation_ids.py`.

**Tests added.** `tests/test_protection_engine.py` (full chain in pytest only).

**Known limitations.** The legacy `tp_sl_test` harness is retained as-is (a separate regression harness); it was not rewritten on top of `protection/`. Trailing stops are not implemented. **Production TP/SL is not live-ready:** `ProtectionMonitor.tick` is **not** called from `runtime/app.py`; there is no `protection.enabled` config; registration after CONFIRMED is not hooked. The engine is procedural and event-ready for future wiring.

**How to run tests.** `python -m pytest tests/test_protection_engine.py`

**How to run a safe harness.** **Unit tests only** until runtime wiring lands. Manual in-process: shadow + `market_data.enabled` + `execution.planner.enabled`, credit owner via ledger, `register_if_allocation_final(..., status="CONFIRMED")`, push book, `tick()`, feed work unit to `process_intent_work_unit`. **Do not claim production TP/SL is CLI-runnable.**

**Verification label:** `unit-tested` · `IMPLEMENTED-BUT-UNWIRED` for CLI — see [live_validation_matrix.md](live_validation_matrix.md).
