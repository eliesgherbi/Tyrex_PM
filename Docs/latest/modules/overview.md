# Modules overview

**Purpose:** map active packages to responsibilities and dependencies.  
**Contracts baseline:** [`../../specifications/03_module_contracts.md`](../../specifications/03_module_contracts.md).

## Source-tree map

```text
src/tyrex_pm/
├── application/     CLI entrypoints
├── engine/          in-process event dispatcher
├── core/            ids, intents, commands, modes, snapshots
├── adapters/        polymarket/, binance/
├── domain/          polymarket market/instrument types
├── market_data/     books, freshness, decision snapshots, executable quotes
├── indicators/      reusable transforms
├── signals/         signal packaging
├── strategies/      protocol + framework_validation/
├── risk/            engine, dedup, policies, reasons
├── planning/        entry/exit planners
├── execution/       OMS protocol, shadow_oms, order_store, fill_ledger, polymarket/
├── portfolio/       positions
├── lifecycle/       trade phase machine
├── persistence/     snapshots
├── reporting/       facts JSONL
├── runtime/         hosts, R7 gates, one-shot live, residuals/ack
└── operations/      small ops helpers
```

## Responsibility matrix

| Area | Owns | Detail page |
|------|------|-------------|
| Market data & signals | Feeds, books, freshness, indicators, signals | [market_data_and_signals](market_data_and_signals.md) |
| Strategy / risk / planning | Intents, authorization, sizing, exit plans | [strategy_risk_planning](strategy_risk_planning.md) |
| Execution & portfolio | OMS, settlement, fills, residuals | [execution_and_portfolio](execution_and_portfolio.md) |
| Reporting & operations | Facts, state paths, recon | [reporting_and_operations](reporting_and_operations.md) |

## Dependency matrix (allowed)

| From \ To | Adapters | State | Strategy | Risk | Plan | OMS | Portfolio |
|-----------|----------|-------|----------|------|------|-----|-----------|
| Adapters | — | emit | no | no | no | no | no |
| Strategy | no | read via context | — | intents out | no | **no** | no |
| Risk | no | read ctx | consume intents | — | approve | no | read exposure |
| Planner | no | books | — | approved | — | commands | no |
| OMS | transport | — | — | — | commands | — | events out |
| Portfolio | no | — | — | — | — | consume events | — |

Strategies **must not** import `tyrex_pm.execution.polymarket.*`.

## Tests (architecture gates)

- `tests/test_import_firewall.py` — no `old/` imports
- `tests/test_r8_architecture_gates.py` — strategy boundaries, no default network hosts in unit tests
- Module suites under `tests/test_r*.py`
