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
| `execution/` | `ExecutionPort`, **`NoOpExecutionPort`**, **`PolymarketExecutionPolicy`**, **`NautilusGuruExecutionPort`** |
| `strategy/` | `BaseComposableStrategy`, **`CopyStrategy`** (subscribes to guru topic → intent) |
| `config/` | Typed YAML: **`StrategySettings`**, **`RiskSettings`**, **`RuntimeSettings`** + loaders |
| `runtime/` | **`guru_compose`**, **`state_readers`**, **`guru_instrument_dynamic`**, **`clob_factory`**, etc. |
| `reporting/` | Placeholder |

**Data → strategy contract:** `GuruMonitorActor` polls **`GET /activity`** (TRADE only) with a **timestamp watermark** (`guru_state_path`), publishes `GuruTradeSignal` on `tyrex_pm.guru.GuruTradeSignal`. `CopyStrategy` subscribes via `msgbus.subscribe`. Full `/trades` history crawling is **not** part of the follower path.

## `run_guru.py` composition

1. Load `.env` (optional `TYREX_PM_DOTENV`), then the three YAML files via `tyrex_pm.config.loaders`.
2. `build_guru_trading_node(strategy, risk, runtime)` (`runtime/guru_compose.py`):
   - `TradingNode` — **empty** or **Polymarket live** clients per `polymarket_nautilus_live` + `execution_mode`.
   - State readers constructed and injected into **`ConfiguredRiskPolicy`**.
   - `GuruMonitorActor` (poll + dedup + bus publish).
   - `CopyStrategy` + risk + execution port:
     - **Shadow** → `NoOpExecutionPort`
     - **Live + framework submit** → `NautilusGuruExecutionPort`
     - **Live legacy** → `PolymarketExecutionPolicy` with `on_submit_ok=risk.note_fill_assumption`
3. `scripts/run_guru.py` runs `node.build()` then `node.run()` (optional Phase A boot line for framework mode).

**Logging:** Persistence and “where to log” rules — [logging_system_guide.md](logging_system_guide.md); operator validation playbook — [log_validation_playbook.md](log_validation_playbook.md).

**Status hub:** `Docs/Implementation/current_state.md`.

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

**Config / compose:** `tests/test_split_config_loaders.py`, `tests/test_guru_compose_build.py`, `tests/unit/test_configured_risk.py`, `tests/test_phase_a_risk.py`.

## Design choices (v1)

- **Shadow / live continuity:** `CopyStrategy` always produces `OrderIntent`; shadow uses `NoOpExecutionPort`; live uses **`PolymarketExecutionPolicy`** or **`NautilusGuruExecutionPort`** per runtime flags (`submit_intent` no-ops unless `mode=="live"`).
- **Token filter:** optional via `token_filter.enabled` in strategy YAML (`false` = all tokens at strategy gate; `true` + list = `not_allowlisted` for others). Not implicit from an empty list.
- **Risk:** `ConfiguredRiskPolicy` on the operational path; fail-closed with explicit `ReasonCode` strings. `ShadowAllPassRisk` remains for unit tests / minimal harnesses.
- **Execution:** No Nautilus `Order` types inside `CopyStrategy` — venue translation stays in **`execution/`** ports.
- **Latency:** Guru polling in `GuruMonitorActor`; live submit latency depends on py-clob vs framework path — see execution module docstrings.

## Assumptions / limits

- Guru polling is HTTP-only; one wallet; dedup file under `var/` (gitignored) by default.
- `OrderIntent` is an internal bridge to `ExecutionPort` / `PolymarketExecutionPolicy`.
- Backtest/live parity of **`CopyStrategy` class** is a goal; historical guru replay is not fully wired yet.
