# `signals/`

Reusable adapters that turn raw venue / external rows into the shape strategies want to consume. Thin by design — strategies, not signals, decide what to do with them.

## Files

| File | Purpose |
|------|---------|
| `base.py` | `GuruCopySignal` dataclass — the standard input to copy-style strategies (wraps a `GuruTradeSignal` with derived fields like normalized notional) |
| `guru_copy_signal.py` | `to_copy_signal(GuruTradeSignal) -> GuruCopySignal` adapter |

## When to add a new signal vs. a new strategy

- **Add a signal** when multiple strategies would want the same derived view (e.g. a microprice-derived edge, a regime classifier).
- **Add a strategy** when the logic decides what to *do* (filter, size, exit). Strategies live under `strategies/`.

## What signals must not do

- Talk to the venue.
- Read or write any store.
- Carry mutable state across calls (use a feature pipeline + store if you need that).

A signal is essentially a pure function from raw inputs to a typed dataclass.
