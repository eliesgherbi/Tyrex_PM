# Module reference

**Hub:** [../README.md](../README.md) · **Architecture:** [../Architecture.md](../Architecture.md) · **Developer guide:** [../developer_guide.md](../developer_guide.md)

One short README per implemented package under `src/tyrex_pm/`. Read these alongside the source — they explain *why* the module exists and *what its boundaries are*; the source explains *how*.

| Module | What it owns | Doc |
|--------|--------------|-----|
| `core/` | Shared dataclasses, enums, ids, time, errors, reason codes | [core/](core/README.md) |
| `ingestion/` | Data API polling (guru), market & user WebSocket loops, fixture replay | [ingestion/](ingestion/README.md) |
| `signals/` | Pluggable signal building blocks: generic `Signal` protocol + adapters (`GuruCopySignal`, `SimpleSignal`) | [signals/](signals/README.md) |
| `strategies/` | Composition layer that turns signals into intents via `Strategy.on_signal` (`guru_follow`, `simple_signal_test`, test harnesses) | [strategies/](strategies/README.md) |
| `risk/` | `RiskEngine` + per-policy modules + `planned_order.validate_planned_order` (final planned-order gate) | [risk/](risk/README.md) |
| `execution/` | `ExecutionPlanner`, single-writer OMS, order builder/lifecycle, shadow + live backends | [execution/](execution/README.md) |
| `protection/` | Reusable TP/SL overlay attached by `owner_id`; emits urgent `ExitIntent`s after `allocation_buy_applied` | [protection/](protection/README.md) |
| `market_data/` | WS-authoritative book quality, executable depth, decision snapshots, readiness (Phase 2) | [market_data/](market_data/README.md) |
| `survival/` | Phase 1 survivor damage control: floor, trailing, enforce dispatch, order policy (default off) | [survival/](survival/README.md) |
| `state/` | `WalletStore`, `OrderStore`, `MarketStateStore`, `AllocationLedger`, `fill_state` finality helper, reconcile state machine | [state/](state/README.md) |
| `runtime/` | App entrypoint, config loader, coordinator, supervisors, pipeline | [runtime/](runtime/README.md) |
| `reporting/` | Fact schema, sinks, summarizer | [reporting/](reporting/README.md) |
| `venue/polymarket/` | CLOB bridge, REST clients (Data API, Gamma), market & user WS, normalizers, auth | [venue/](venue/README.md) |

Modules in the original rebuild plan that were intentionally **not** implemented in this milestone:

- `features/` — feature pipelines. Folded into `signals/` as needed.
- `portfolio/` — PnL / attribution (Phase 5, deferred). The `wallet_sync` fact carries the operator-relevant data; the `state/fill_state` finality helper documents the rules a future portfolio will build on.

`protection/` (TP/SL overlay) **was** implemented in the architecture-enhancement program (Phase 4). See [protection/](protection/README.md).
