# Phase 1 — Generic signal/strategy dispatch

**Program:** [README.md](README.md) · **Prev:** [phase_0_architecture_contract_freeze.md](phase_0_architecture_contract_freeze.md) · **Next:** [phase_2_market_state_store.md](phase_2_market_state_store.md)

> **Procedural now, event-ready later.** Generalize the contract; do **not** add a bus.

---

## 1. Goal

Replace the guru-shaped strategy contract with a generic one so that adding a strategy or overlay never requires a new loop in `runtime/app.py`. The target is:

```python
Strategy.on_signal(signal: Signal, ctx: StrategyContext) -> StrategyResult
```

with one generic dispatch function:

```python
runtime/pipeline.py::process_signals(signals, strategy, coord, sink, ...) -> None
```

Guru copy continues to work, but only as the **first consumer** of this generic path.

---

## 2. Why this phase exists

Today every new strategy type duplicates runtime wiring:

- `runtime/pipeline.py::process_new_guru_signals` is guru-specific.
- `runtime/app.py` has bespoke loops: `_run_sell_test_loop`, `_run_tp_sl_test_loop`, and the guru poll loop, each repeating readiness gating, pricing, and dispatch.
- `strategies/base.py` defines the de-facto contract as `on_guru_signal(sig: GuruCopySignal, coord)`.
- `signals/base.py` only knows `GuruCopySignal`.

This means Phase 4 (protection) and any future signal source would each add **another** loop. Generalizing now stops that growth and gives protection a clean `ExitIntent` entry point.

---

## 3. Current state

```text
signals/base.py        → GuruCopySignal (wraps GuruTradeSignal)
strategies/base.py     → Strategy Protocol: on_guru_signal(sig, coord) -> (intents, skip, meta)
runtime/pipeline.py    → process_new_guru_signals(...)  # guru-only entry
                       → process_intent_work_unit(...)  # SHARED, already generic per-intent
runtime/app.py         → guru poll loop + _run_sell_test_loop + _run_tp_sl_test_loop
state/strategy_store.py→ guru watermark + guru dedup set only
```

Key asset already in place: **`process_intent_work_unit`** is already strategy-agnostic at the intent level (risk → OMS → allocation hooks → facts). Phase 1 only needs to generalize the **signal → intents** step that feeds it.

---

## 4. Target state

```text
signals/base.py
  Signal           # base/union: a strategy input record
  GuruCopySignal   # one concrete Signal (unchanged shape)

strategies/base.py
  StrategyContext  # read-only handles: coord, market_state (Phase 2), config slice
  StrategyResult   # (intents: list[Intent], skip_reason: str|None, meta: dict|None)
  Strategy.on_signal(signal: Signal, ctx: StrategyContext) -> StrategyResult

runtime/pipeline.py
  process_signals(signals, strategy, coord, sink, *, app, run_id, oms, ...) -> None
      for each signal:
          emit signal fact
          result = strategy.on_signal(signal, ctx)
          emit strategy_skip fact if result.skip_reason
          for intent in result.intents:
              process_intent_work_unit(IntentWorkUnit(intent, ...), ...)

runtime/app.py
  guru poll loop builds Signal records, calls process_signals(...)  # no guru-specific dispatch fn
```

Backward-compatibility bridge (internal, temporary): `process_new_guru_signals` may become a thin wrapper that adapts `GuruTradeSignal` → `GuruCopySignal` (`Signal`) and calls `process_signals`. `GuruFollowStrategy.on_guru_signal` may be kept as an alias that delegates to `on_signal`, removed once callers are migrated.

### Scope boundary (locked)

Phase 1 is **deliberately minimal**. It migrates **only the guru path** to the generic contract and adds **one** new non-guru harness (`simple_signal_test`). It does **not** force `sell_test`, `allocation_test`, or `tp_sl_test` onto `on_signal`.

```text
Minimum useful Phase 1:
  1. Add generic Signal / StrategyContext / StrategyResult.
  2. Add process_signals().
  3. Adapt the guru path through the generic wrapper.
  4. Add simple_signal_test as the non-guru architecture harness.
  5. Keep existing sell_test / allocation_test / tp_sl_test green (unchanged loops).
```

Governing rule:

> **Existing harnesses may keep their current loops temporarily. No new production feature may add a new bespoke runtime loop.**

`sell_test` / `allocation_test` / `tp_sl_test` remain **legacy harnesses** on their existing `app.py` loops until the phase that replaces their logic (e.g. Phase 4 absorbs `tp_sl_test`'s production role). Migrating them "for architecture purity" is explicitly out of scope and would risk breaking validated code.

---

## 5. Non-goals

- No event bus, no subscriptions, no async fan-out.
- No change to `RiskEngine`, `SingleWriterOMS`, `AllocationLedger`, or reconcile behavior.
- No change to guru filtering/sizing/exit logic itself — only the call shape.
- No new market data (`StrategyContext.market_state` is reserved for Phase 2; may be `None` now).
- **No forced migration** of `sell_test` / `allocation_test` / `tp_sl_test` onto `on_signal` — they stay on their current loops as legacy harnesses.

---

## 6. Files likely to change

```text
src/tyrex_pm/signals/base.py                         # add Signal base/union
src/tyrex_pm/strategies/base.py                      # StrategyContext, StrategyResult, on_signal
src/tyrex_pm/strategies/guru_follow/strategy.py      # implement on_signal (keep on_guru_signal alias)
src/tyrex_pm/runtime/pipeline.py                     # add process_signals(); wrap process_new_guru_signals; emit signal_received
src/tyrex_pm/runtime/app.py                           # guru loop calls process_signals (existing harness loops untouched)
src/tyrex_pm/reporting/schema_v2.py                   # add FACT_TYPE_SIGNAL_RECEIVED
Docs/developer_guide.md                               # update strategy contract section
Docs/modules/strategies/README.md                     # generic contract first
Docs/modules/signals/README.md                        # Signal generic; GuruCopySignal one type
Docs/reporting_fact_model.md                          # document signal_received
```

> `sell_test/strategy.py`, `allocation_test/strategy.py`, and `tp_sl_test/strategy.py` are **intentionally absent** from this list — they are not migrated in Phase 1.

---

## 7. New files likely to be added

```text
src/tyrex_pm/strategies/simple_signal_test/__init__.py
src/tyrex_pm/strategies/simple_signal_test/strategy.py   # minimal non-guru harness strategy
config/strategies/simple_signal_test.yaml                # config for the harness
tests/test_generic_signal_dispatch.py
```

`simple_signal_test` is the **architecture testing strategy** going forward (not guru). It consumes a trivial fixture `Signal` (e.g. one configured BUY, optional later SELL) and emits an `EnterIntent` through `on_signal`, proving the generic path without guru semantics.

---

## 8. Data model changes

`core/models.py`: no change to `Intent` variants. Add (in `signals/base.py` / `strategies/base.py`, not `core`):

```python
# signals/base.py
class Signal(Protocol):
    source: str          # "guru" | "fixture" | "manual" | ...
    token_id: TokenId
    # concrete signals carry their own fields

# strategies/base.py
@dataclass(frozen=True)
class StrategyContext:
    coord: RuntimeCoordinator
    market_state: "MarketStateStore | None" = None   # Phase 2 wires this
    # config slice is passed via the strategy instance, as today

@dataclass(frozen=True)
class StrategyResult:
    intents: list[Intent]
    skip_reason: str | None = None
    meta: dict | None = None
```

`GuruCopySignal` keeps its current shape and gains `source = "guru"`.

---

## 9. Config changes

```text
config/strategies/simple_signal_test.yaml   # new: token_id, buy.notional_usd, buy.limit_price, run_once
```

`runtime/config.py`: add a typed config block for `simple_signal_test` mirroring the minimal `sell_test` shape. No change to existing strategy configs' semantics.

---

## 10. Fact/reporting changes

**Add one** new fact type for generic signal sources; **keep `guru_signal` for backward compatibility.**

```python
# reporting/schema_v2.py
FACT_TYPE_SIGNAL_RECEIVED = "signal_received"
```

Payload (join key: `dedup_key` / `correlation_id`):

```json
{
  "source": "simple_signal_test",
  "signal_type": "simple_entry",
  "token_id": "...",
  "side": "BUY",
  "dedup_key": "...",
  "metadata": {}
}
```

Rules (locked):

- **Keep `guru_signal`** (`FACT_TYPE_GURU_SIGNAL`) exactly as today — do **not** migrate old guru facts now.
- **Emit `signal_received`** for every **non-guru** source (starting with `simple_signal_test`) so new sources are visible *before* `intent_created`. This preserves the "guru is not the center" decision: no source is invisible in the audit trail.
- Guru may optionally *also* emit `signal_received` in a later phase, but Phase 1 does not require it (avoids touching the validated guru path).
- `strategy_skip` (`FACT_TYPE_STRATEGY_SKIP`) still carries the same `reason_codes` constants.
- `intent_created` (`FACT_TYPE_INTENT`) payload for guru intents must be byte-for-byte equivalent.

---

## 11. Tests to add

```text
tests/test_generic_signal_dispatch.py
  test_guru_signal_uses_generic_dispatch
  test_strategy_skip_still_emits_same_reason_codes
  test_intent_created_fact_unchanged_for_guru
  test_no_strategy_imports_venue_or_mutates_state
  test_simple_signal_test_emits_intent_through_process_signals
  test_simple_signal_test_emits_signal_received_fact      # non-guru source visible pre-intent
  test_guru_signal_fact_unchanged                          # guru_signal retained, not migrated
  test_simple_signal_test_sell_clamps_to_allocation       # guards constraint #9
  test_legacy_harnesses_still_run                          # sell_test/allocation_test/tp_sl_test loops unchanged
```

`test_no_strategy_imports_venue_or_mutates_state` should statically assert no `strategies/*` module imports `venue.*` or mutates stores (mirror the spirit of `test_v2_import_isolation.py`).

---

## 12. Acceptance criteria

- A generic `process_signals(...)` exists and is the single dispatch entry for new sources.
- `guru_follow` runs through `process_signals` / `on_signal`; risk + OMS behavior unchanged.
- A `simple_signal_test` non-guru strategy emits an intent through the same path.
- `simple_signal_test` emits a `signal_received` fact before `intent_created`; `guru_signal` is retained unchanged.
- No new **production feature** requires a bespoke loop in `runtime/app.py`.
- `sell_test`, `allocation_test`, `tp_sl_test` remain on their **existing loops** and still pass their tests (not migrated).
- Existing guru facts (`guru_signal`, `strategy_skip`, `intent_created`) are preserved byte-for-byte.

---

## 13. Migration risks

- **Fact regression:** guru `intent_created` payload could shift. Mitigate with `test_intent_created_fact_unchanged_for_guru` (golden compare).
- **Scope creep:** the temptation to migrate `sell_test`/`allocation_test`/`tp_sl_test` "while we're here". Explicitly out of scope — they stay on their loops. Only the guru path moves.
- **Contract leak:** `on_guru_signal` callers elsewhere. Keep an alias until all callers move.
- **Over-generalization:** resist building a `Signal` union so abstract it hides token/side. Keep it minimal.

---

## 14. Rollback strategy

- Keep `process_new_guru_signals` as a working wrapper during migration so reverting `app.py` to call it directly restores the old path.
- Phase 1 is mergeable in two commits: (a) add generic contract + wrapper (no behavior change), (b) migrate `app.py`. Roll back commit (b) alone if the live loop regresses.

---

## 15. Dependencies on previous phases

- **Phase 0** must be merged (docs assert the generic contract and "guru is one strategy").

Provides for later phases: the `on_signal` / `process_signals` entry that Phase 4 protection exits and any future signal source reuse, and the `StrategyContext.market_state` slot Phase 2 fills.

---

## 16. Event-ready design notes

`Strategy.on_signal(signal, ctx) -> StrategyResult` is intentionally a pure function of `(signal, ctx)`: no I/O, no store mutation, deterministic given context. A future event bus can deliver `Signal` events to `on_signal` unchanged; today `process_signals` calls it in a loop. `StrategyContext` is a read-only handle bundle so the same signature works whether invoked procedurally or by a dispatcher. No bus, subscriptions, or queues are added here.

---

## Implementation status

**Status: implemented · CLI-runnable (shadow + tiny live BUY).**

**Verification label:** `CLI-runnable` · `shadow-runnable` · `tiny-live-order-runnable` — see [live_validation_matrix.md](live_validation_matrix.md).

**Implementation summary.** A generic `Signal` protocol (`signals/base.py`) plus `StrategyContext` / `StrategyResult` / `Strategy.on_signal` (`strategies/base.py`) were added. `runtime/pipeline.py::process_signals(...)` is the single dispatch path; `process_new_guru_signals(...)` is now a thin wrapper that runs its guru-specific pre-checks and delegates to `process_signals`. `GuruFollowStrategy.on_signal` delegates to the legacy `on_guru_signal`. A new `simple_signal_test` strategy + `SimpleSignal` prove a non-guru source produces an `EnterIntent` through the same pipeline.

**Runtime integration (post-Phase 1 gap fix).** `runtime/app.py` now routes `kind: simple_signal_test` through `runtime/fixture_signal_run.py::run_fixture_signals_once(...)` — a generic one-shot fixture runner (not a guru-style poll loop). It builds a `SimpleSignal`, instantiates `SimpleSignalTestStrategy`, and calls `process_signals(...)`. Unsupported strategy kinds fail closed with `RuntimeError` instead of falling back to guru polling. Guru polling (`poll_guru_incremental`) runs **only** when `strategy_kind == guru_follow`.

**Files changed.** `signals/base.py`, `signals/simple_signal.py` (new), `strategies/base.py`, `strategies/guru_follow/strategy.py`, `strategies/simple_signal_test/` (new), `runtime/pipeline.py`, `runtime/config.py` (`strategy_kind` on `AppConfig`), `runtime/app.py` (fixture runner path + fail-closed kind routing), `runtime/fixture_signal_run.py` (new), `runtime/allocation_ids.py`, `reporting/schema_v2.py` (`FACT_TYPE_SIGNAL_RECEIVED`), `config/strategies/simple_signal_test.yaml` (new).

**Tests added.** `tests/test_generic_signal_dispatch.py` (guru-unchanged facts, generic dispatch, `signal_received` for non-guru, SELL allocation clamp, no-venue-import invariant, legacy harnesses still run). `tests/test_simple_signal_test_runtime.py` (CLI path does not poll guru, no guru wallet required, uses `process_signals`, emits `signal_received`, exits cleanly, unknown kind does not fall back to guru).

**Known limitations.** `sell_test` / `allocation_test` / `tp_sl_test` deliberately remain on their legacy loops (not force-migrated). `ProtectionMonitor.tick` and market-stream supervisor wiring in `app.py` are still deferred (Phases 2–4 packages exist; orchestration is not unified yet).

**How to run tests.**

```bash
python -m pytest tests/test_generic_signal_dispatch.py
python -m pytest tests/test_simple_signal_test_runtime.py
```

**How to run a safe harness.** `simple_signal_test` is the CLI-safe non-guru reference harness for the generic Signal → Strategy → Intent path. It does **not** use guru polling or require `guru.wallet`. Use it for architecture validation, shadow tests, and controlled smoke tests:

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/simple_signal_test.yaml \
  --run-name simple_strat
```

Expected facts: `signal_received` → `intent_created` (if enabled) → `risk_decision` → OMS/shadow path. With `execution.planner.enabled` in a scenario overlay, also expect `execution_plan` and `risk_decision` with `phase="planned"`.
