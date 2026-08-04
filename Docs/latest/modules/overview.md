# Modules overview

**Purpose:** map active packages to responsibilities.  
**Contracts baseline:** [`../../specifications/03_module_contracts.md`](../../specifications/03_module_contracts.md).

## Source-tree map

```text
src/tyrex_pm/
├── application/     CLI / composition root
├── engine/          in-process event dispatcher
├── core/            ids, intents, commands, modes, snapshots
├── adapters/        polymarket/, binance/
├── domain/          polymarket market/instrument types (+ resolution/PTB contracts)
├── market_data/     books, freshness, decision snapshots, executable quotes
├── indicators/      reusable transforms (momentum, EWMA vol, binary FV, basis)
├── signals/         signal packaging
├── strategies/      protocol + decisions + framework_validation/ + z_gap/ (pure F2)
├── risk/            engine, dedup, policies, reasons
├── planning/        entry/exit planners (generic)
├── execution/       OMS protocol, shadow_oms, order_store, fill_ledger, polymarket/
├── portfolio/       derived positions
├── lifecycle/       host TradeLifecycle (shadow path)
├── persistence/     snapshots
├── reporting/       facts JSONL
├── runtime/         hosts + R7 gates + N7 Z-Gap one-shot/preflight/PTB policy
└── operations/      small ops helpers
```

## Responsibility matrix

| Area | Owns | Detail page |
|------|------|-------------|
| Market data & signals | Feeds, books, freshness, indicators, signals | [market_data_and_signals](market_data_and_signals.md) |
| Strategy / risk / planning | Intents, authorization, sizing, exit plans | [strategy_risk_planning](strategy_risk_planning.md) |
| Execution & portfolio | OMS, settlement, fills, residuals | [execution_and_portfolio](execution_and_portfolio.md) |
| Reporting & operations | Facts, local state paths, recon | [reporting_and_operations](reporting_and_operations.md) |

## Runtime flow versus static dependencies

**Runtime flow** (processing order): see [architecture](../concepts/architecture.md#runtime-flow-data--control).

**Static dependency rules** (imports):

- `core` → not adapters  
- strategies → contracts/context only (not `execution.polymarket`)  
- risk/planning → not concrete strategies  
- adapters → inward to core/domain  
- `application` / `runtime` hosts may wire concretes  

Do not read the runtime flowchart as an import graph.

## Validated implementation versus generic target

Phase-specific (active, validated, not the forever public API):

- R7: `runtime/r7b_live_once.py`, `r7_lifecycle_policy.py`, ack/residual modules; `config/r7/`, `var/runtime_state/r7/`; CLI `r7b-live-once`, `r7c-recon`, `r7-ack-regenerate`
- N7: `runtime/n7_*.py`, `config/n7_tiny_live.json`, `tools/n7_live/`; CLI `n7-status`, `n7-preflight`, `n7-live`

Generic targets for future strategies: `Strategy` protocol, `RiskEngine`, `OMS`, stores, planners — without importing R7 packages. **Z-Gap must not import `runtime/r7*`.**

## Tests (architecture gates)

- `tests/test_import_firewall.py` — no `old/` imports  
- `tests/test_r8_architecture_gates.py` — strategy boundaries  
- `tests/test_docs_links.py` / `tests/test_docs_consistency.py` — docs vs tree/CLI  
