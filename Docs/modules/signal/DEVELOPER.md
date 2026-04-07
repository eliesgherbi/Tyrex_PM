# Developer guide — `tyrex_pm.signal`

[README](README.md) · [Architecture](../../Architecture.md)

## Responsibility

**Pure policy and sizing:** given a `GuruTradeSignal`, compute **accept/reject** for entry or exit and **follower quantity** at strategy baseline — **no** HTTP, **no** `Cache`, **no** risk notional caps (those are **`risk/`**).

## Components

- **`entry.py`** — `GuruFollowEntryPolicy`, `GuruMirrorExitPolicy`, `SignalDecision`.
- **`sizing.py`** — proportional `copy_scale`, optional **conviction** weighting vs rolling average of accepted guru entry sizes.
- **`token_filter_spec.py`** — explicit allowlist semantics (`enabled` + list).

## Contracts

- **`SignalDecision`** carries `accept`, `reason_code`, and optional diagnostics.
- Sizing returns a **float qty**; zero qty → strategy logs `zero_qty` skip (before risk).

## Extension patterns

- **New entry rule:** implement as pure function / small class called from `GuruFollowEntryPolicy`.
- **New sizing curve:** extend `SizingPolicy` or `build_sizing_policy` — unit test without Nautilus.

## Pitfalls

- **Do not** import `tyrex_pm.strategy` or `nautilus_trader` here (package boundary).
- **Per-order USD min/max** belongs in **`risk/configured.py`**, not signal — avoids double floors.

## Tests

`tests/unit/test_copy_strategy_shadow.py`, `tests/unit/test_c2_capital_allocation.py` (conviction paths).
