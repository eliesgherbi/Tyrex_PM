# `protection/`

Production take-profit / stop-loss as a **reusable overlay** (P4 architecture_enhance). Protection is strategy-agnostic: it attaches to an `owner_id`, watches `MarketStateStore`, and emits `ExitIntent`s through the same generic pipeline as any other intent. It is **not** a strategy and is **not** wired inside `guru_follow`.

## Invariants

- Attaches by `owner_id` (works for guru and any non-guru strategy).
- Reads `AllocationLedger` + `WalletStore` + `MarketStateStore`; never mutates them.
- Registers **only after allocation-final evidence** — `fill_state.is_allocation_final(status)` (CONFIRMED). `allocation_buy_applied` from MATCHED-only evidence is **not** sufficient. Never on submit-ack, resting BUY, or MATCHED/MINED-only evidence.
- Emits `ExitIntent` only (`urgency="urgent"`); never submits/cancels directly.
- Every exit clamps to `min(planned, owner_allocation, venue_available_to_sell)`.
- Exits flow through RiskEngine → ExecutionPlanner (FAK) → `validate_planned_order` → `SingleWriterOMS`.

## Files

| File | Purpose |
|------|---------|
| `config.py` | `ProtectionPolicy` — TP/SL as percentages or absolute prices, size mode (`full`/`percent`/`fixed`), exit order style (default FAK), optional fallback limit, `max_book_age_s` |
| `trigger_eval.py` | Pure functions: `resolve_thresholds(policy, entry_price)` and `evaluate_trigger(observed, tp, sl)` |
| `sizing.py` | `compute_exit_sizing(...)` — enforces the SELL clamp invariant; returns operator evidence |
| `registry.py` | `ProtectionRegistry` (entries keyed by `owner_id|token`) + `register_if_allocation_final(...)` gating on `state.fill_state.is_allocation_final` |
| `lifecycle.py` | `build_exit_work_unit(entry, sizing, ...)` — builds the urgent `ExitIntent` work unit with owner provenance |
| `monitor.py` | `ProtectionMonitor.register(...)` / `.tick(coord)` — emits facts, dedups ticks, marks entries `triggered` before emitting to prevent double-sell |

## Flow

```
BUY CONFIRMED (fill_state.is_allocation_final)
   → register_if_allocation_final → ProtectionRegistry           → protection_register
MarketStateStore update / timer
   → ProtectionMonitor.tick                                      → protection_tick (deduped)
      trigger fired?
         → mark entry.triggered, compute clamped size            → protection_trigger
         → build urgent ExitIntent → process_intent_work_unit
            → RiskEngine → ExecutionPlanner (FAK) → validate_planned_order → OMS
               → intent_created / risk_decision / execution_plan / oms_submit / exit_lifecycle / allocation_ledger
```

## Stale / missing book

`tick` never fabricates a trigger from a stale or missing book: if `MarketStateStore.snapshot(token)` is missing or `is_stale(...)` is true, the entry is skipped (one deduped `protection_tick` noting the stale state). This keeps protection fail-safe when market data is unavailable.

## Facts

`protection_register`, `protection_tick` (deduped on observed price), `protection_trigger`. The actual exit reuses existing facts (`intent_created`, `risk_decision`, `execution_plan`, `oms_submit`, `exit_lifecycle`, `allocation_ledger`).

## Relationship to `tp_sl_test`

`strategies/tp_sl_test` remains a standalone regression harness for the TP/SL exit lifecycle. The production architecture is this `protection/` overlay; new production TP/SL must use it, not the harness or `guru_follow`.

## Runtime wiring (P4.5)

| Path | Status |
|------|--------|
| Register after allocation-final evidence | **wired** — `pipeline` → `maybe_register_protection_after_buy` (gated on `is_allocation_final`) |
| Live finality wait + register | **wired** — `FinalityWaiter` → `register_protection_after_finality` (Level 5 harness) |
| Harness trigger modes (shadow) | **wired** — `validation_harness_run` → `run_protection_tick` → `process_intent_work_unit` |
| Live supervisor tick loop (validation) | **wired** — `ProtectionSupervisor` → `run_protection_tick` (Level 6 harness) |
| Periodic live tick supervisor (production guru) | **not wired** — deferred; paired binary proves production monitor loop (P4.6) |
| `PairedBinaryMonitor` (strategy-specific) | **implemented** — `paired_binary_run` loop; reads `MarketStateStore`; SELL sized via allocation (repaired from finality) + venue clamp |
