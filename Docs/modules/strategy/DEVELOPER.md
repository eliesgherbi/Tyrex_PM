# Developer guide — `tyrex_pm.strategy`

[README](README.md) (full flow examples) · [Architecture](../../Architecture.md) · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md)

## Responsibility

Nautilus **Strategy** shell: subscribe to guru topic, orchestrate **`signal/`** policies, call **`risk.evaluate`**, call **`execution.submit_intent`**, structured logging (`copy_skip`, `shadow_order_intent`, `live_order_intent`).

## Hard invariants (enforced by architecture tests)

- **No** direct `Cache`, `Portfolio`, **`VenueState`**, or order-book reads in `CopyStrategy` — **risk** receives injected readers from **`guru_compose`** (Tier A vs B is a runtime concern).
- **No** `py-clob` or HTTP for orders — execution port only.
- **`on_order_event`** may forward to execution port for **limit timeout** cleanup — no business interpretation of fills here.

## Config

`CopyStrategyConfig` merges **strategy YAML** (filter, scale, conviction) with **runtime** `execution_mode` from compose.

## Extension patterns

- **New strategy:** new `Strategy` subclass; reuse **injected** `RiskPolicy` + `ExecutionPort` pattern.
- **Handoff type:** keep using **`OrderIntent`** unless you introduce an explicit v2 type and migrate emit/reporting.

## Pitfalls

- **Conviction `record_accepted_entry_size`:** only on **accepted** entry path — required for rolling average correctness.
- **Risk-adjusted qty:** execution must receive the **post-risk** intent; logging should show both strategy and risk qty where applicable (see reporting `risk_decision`).

## Tests

`tests/test_copy_strategy_architecture.py`, `tests/unit/test_copy_strategy_shadow.py`.
