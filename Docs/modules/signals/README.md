# `signals/`

Reusable adapters that turn raw venue / external rows into the shape strategies want to consume. Thin by design — strategies, not signals, decide what to do with them.

## Files

| File | Purpose |
|------|---------|
| `base.py` | The generic `Signal` protocol (`source` / `token_id` / `dedup_key`) every signal conforms to (P1 architecture_enhance), plus `GuruCopySignal` which implements it |
| `guru_copy_signal.py` | `to_copy_signal(GuruTradeSignal) -> GuruCopySignal` adapter |
| `simple_signal.py` | `SimpleSignal` — minimal non-guru signal used by the `simple_signal_test` harness to prove the generic dispatch path |

All signals satisfy the `Signal` protocol so `pipeline.process_signals` can dispatch any source through one path. `guru` keeps its `guru_signal` fact for back-compat; non-guru sources emit a generic `signal_received` fact.

**CLI wiring.** For `kind: simple_signal_test`, `runtime/fixture_signal_run.py` builds a `SimpleSignal` from config and passes it to `process_signals` via `SimpleSignalTestStrategy`. This path never polls the guru Data API.

## When to add a new signal vs. a new strategy

- **Add a signal** when multiple strategies would want the same derived view (e.g. a microprice-derived edge, a regime classifier).
- **Add a strategy** when the logic decides what to *do* (filter, size, exit). Strategies live under `strategies/`.

## What signals must not do

- Talk to the venue.
- Read or write any store.
- Carry mutable state across calls (use a feature pipeline + store if you need that).

A signal is essentially a pure function from raw inputs to a typed dataclass.
