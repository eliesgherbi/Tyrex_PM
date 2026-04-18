# Module reference

**Hub:** [../README.md](../README.md) · **Architecture:** [../Architecture.md](../Architecture.md) · **Developer guide:** [../developer_guide.md](../developer_guide.md)

One short README per implemented package under `src/tyrex_pm/`. Read these alongside the source — they explain *why* the module exists and *what its boundaries are*; the source explains *how*.

| Module | What it owns | Doc |
|--------|--------------|-----|
| `core/` | Shared dataclasses, enums, ids, time, errors, reason codes | [core/](core/README.md) |
| `ingestion/` | Data API polling (guru), market & user WebSocket loops, fixture replay | [ingestion/](ingestion/README.md) |
| `signals/` | Pluggable signal building blocks (currently `GuruCopySignal` adapter) | [signals/](signals/README.md) |
| `strategies/` | Composition layer that turns signals into intents (currently `guru_follow`) | [strategies/](strategies/README.md) |
| `risk/` | `RiskEngine` + per-policy modules (deny/approve, evidence, in-flight reservations) | [risk/](risk/README.md) |
| `execution/` | Single-writer OMS, order builder/lifecycle, shadow + live backends | [execution/](execution/README.md) |
| `state/` | `WalletStore`, `OrderStore`, `MarketStateStore`, `StrategyStore`, reconcile state machine | [state/](state/README.md) |
| `runtime/` | App entrypoint, config loader, coordinator, supervisors, pipeline | [runtime/](runtime/README.md) |
| `reporting/` | Fact schema, sinks, summarizer | [reporting/](reporting/README.md) |
| `venue/polymarket/` | CLOB bridge, REST clients (Data API, Gamma), market & user WS, normalizers, auth | [venue/](venue/README.md) |

Modules in the original rebuild plan that were intentionally **not** implemented in this milestone:

- `features/` — feature pipelines. Folded into `signals/` as needed.
- `protection/` — virtual exits / stops. Out of scope for the copy-only strategy.
- `portfolio/` — PnL / attribution. The `wallet_sync` fact carries the operator-relevant data; deeper analytics are deferred.

If those land later, recreate the corresponding folder under `Docs/modules/`.
