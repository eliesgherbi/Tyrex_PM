# Developer guide — Tyrex_PM

**Big picture:** [Architecture.md](Architecture.md) · **Per-module:** [modules/README.md](modules/README.md)

Full field reference for strategy / risk / runtime YAML: [CONFIG_MODEL.md](CONFIG_MODEL.md).

## Layout (`src/tyrex_pm/`)

| Area | Role |
|------|------|
| `core/` | Shared types (`GuruTradeSignal`, `OrderIntent`), `ReasonCode`, app YAML (`app_config`), logging helpers |
| `data/` | Market allowlist, resolution, book check, Data API client, `GuruMonitorActor` (poll → bus) |
| `signal/` | Pure-ish policies: guru follow entry, mirror exit, proportional sizing |
| `risk/` | `RiskPolicy` protocol; **`ShadowAllPassRisk`** (tests/default); **`ConfiguredRiskPolicy`** (launcher) |
| `execution/` | `ExecutionPort` / **`NoOpExecutionPort`** (shadow); **`PolymarketExecutionPolicy`** (live CLOB) |
| `strategy/` | `BaseComposableStrategy`, **`CopyStrategy`** (subscribes to guru topic → intent) |
| `config/` | Typed YAML: **`StrategySettings`**, **`RiskSettings`**, **`RuntimeSettings`** + loaders |
| `runtime/` | **`guru_compose.build_guru_trading_node`**, **`clob_factory.build_clob_client_from_env`** |
| `reporting/` | Placeholder |

**Data → strategy contract:** `GuruMonitorActor` publishes `GuruTradeSignal` on topic `tyrex_pm.guru.GuruTradeSignal` (see `data/guru_monitor.py`). `CopyStrategy` subscribes on that string topic via `msgbus.subscribe`.

## `run_guru.py` composition

1. Load `.env` (optional `TYREX_PM_DOTENV`), then the three YAML files via `tyrex_pm.config.loaders`.
2. `build_guru_trading_node(strategy, risk, runtime)` (`runtime/guru_compose.py`):
   - `TradingNode` with empty `data_clients` / `exec_clients` (kernel only; Data API + py-clob are app I/O).
   - `GuruMonitorActor` (poll + dedup + bus publish).
   - `CopyStrategy` + **`ConfiguredRiskPolicy`** + execution port:
     - `execution_mode=shadow` → `NoOpExecutionPort`
     - `execution_mode=live` → `PolymarketExecutionPolicy` with `on_submit_ok=risk.note_fill_assumption`
3. `scripts/run_guru.py` runs `node.build()` then `node.run()`.

Same classes and wiring for shadow and live; only runtime `execution_mode` and execution port implementation change.

## Setup

```bash
pip install -e ".[dev]"
```

Secrets stay in `.env` / environment (see `Docs/Runbooks/polymarket_operator_v1_00.md`). Legacy monolith template: `config/v1.example.yaml`.

## Tests

```bash
pytest tests/ -q
ruff check src tests scripts examples
```

Opt-in live HTTP resolution test:

```bash
set TYREX_NETWORK_TESTS=1   # Windows
pytest tests/test_resolution_network.py -v
```

**Shadow copy:** `tests/unit/test_entry_policy.py`, `tests/unit/test_copy_strategy_shadow.py`, `tests/test_copy_strategy_architecture.py` (guards against `submit_order` / `MarketOrder` in `copy_strategy.py`).

**Config / compose:** `tests/test_split_config_loaders.py`, `tests/test_guru_compose_build.py`, `tests/unit/test_configured_risk.py`.

## Design choices (v1)

- **Shadow / live continuity:** `CopyStrategy` always produces `OrderIntent`; shadow uses `NoOpExecutionPort`, live uses `PolymarketExecutionPolicy` (`submit_intent` no-ops unless `mode=="live"`).
- **Allowlist:** token id filter uses decimal strings matching CLOB `asset` / `token_id` from resolution — `not_allowlisted` if guru signal references another token.
- **Risk:** `ConfiguredRiskPolicy` on the operational path; fail-closed with explicit `ReasonCode` strings. `ShadowAllPassRisk` remains for unit tests / minimal harnesses.
- **Execution:** No Nautilus `Order` types inside `CopyStrategy` — venue translation stays in `PolymarketExecutionPolicy`.
- **Latency:** Guru polling/backoff lives in `GuruMonitorActor` / `PolymarketDataApiClient`; each live submit is a sync CLOB round-trip (~50–200 ms typical) — see `execution/polymarket_policy.py` docstring.

## Assumptions / limits

- Guru polling is HTTP-only; one wallet; dedup file under `var/` (gitignored) by default.
- `OrderIntent` is an internal bridge to `ExecutionPort` / `PolymarketExecutionPolicy`.
- Backtest/live parity of **`CopyStrategy` class** is a goal; historical guru replay is not fully wired yet.
