# Current documentation (Docs/latest)

Concise docs for the **accepted framework** at commit `fb9d0d8`.  
This layer describes **what the system is now**, not the history of how it was built.

Formal baselines: [`../specifications/`](../specifications/).  
Chronological evidence: [`../implementation/`](../implementation/).

## Contents

### Getting started

| Document | Purpose |
|----------|---------|
| [installation.md](getting_started/installation.md) | Python env, editable install, tests, CLI check |
| [quickstart.md](getting_started/quickstart.md) | Smallest safe offline / read-only path |

### Concepts

| Document | Purpose |
|----------|---------|
| [architecture.md](concepts/architecture.md) | Objective, ports/adapters, ownership, diagrams |
| [events_signals_intents.md](concepts/events_signals_intents.md) | Event ≠ signal ≠ intent ≠ plan ≠ command |
| [strategy_execution_flow.md](concepts/strategy_execution_flow.md) | Full pipeline + settlement ladder |
| [state_lifecycle_recovery.md](concepts/state_lifecycle_recovery.md) | Authoritative state, flatness, `var/state` vs reports |
| [operating_modes.md](concepts/operating_modes.md) | OBSERVE / SHADOW / LIVE_TINY |

### Modules

| Document | Purpose |
|----------|---------|
| [overview.md](modules/overview.md) | Source map + responsibility matrix |
| [market_data_and_signals.md](modules/market_data_and_signals.md) | Adapters, books, freshness, indicators, signals |
| [strategy_risk_planning.md](modules/strategy_risk_planning.md) | Strategy, risk, sizing, entry/exit planning |
| [execution_and_portfolio.md](modules/execution_and_portfolio.md) | OMS, settlement, portfolio, residuals |
| [reporting_and_operations.md](modules/reporting_and_operations.md) | Facts, reports, readiness, evidence |

### Integrations

| Document | Purpose |
|----------|---------|
| [polymarket.md](integrations/polymarket.md) | Supported Polymarket capabilities and limits |
| [binance.md](integrations/binance.md) | Reference-data-only BTC feed |

### How-to

| Document | Purpose |
|----------|---------|
| [configuration.md](how_to/configuration.md) | Config sources, flags, paths, secrets |
| [run_modes.md](how_to/run_modes.md) | Observe, shadow, dry, read-only recon recipes |
| [reconciliation_and_recovery.md](how_to/reconciliation_and_recovery.md) | Startup checks, residuals, incident checklist |

### Developer guide

| Document | Purpose |
|----------|---------|
| [development.md](developer_guide/development.md) | Tree, tests, firewalls, commit hygiene |
| [extending_the_framework.md](developer_guide/extending_the_framework.md) | Add indicator/signal/strategy/risk/adapter/planner |
| [documentation_style.md](developer_guide/documentation_style.md) | How to keep docs honest |

## Suggested reading paths

**New user:** installation → quickstart → operating_modes → architecture.

**Strategy developer:** strategy_execution_flow → events_signals_intents → strategy_risk_planning → extending_the_framework.

**Framework developer:** architecture → modules/overview → development → documentation_style.

**Operator:** configuration → run_modes → reconciliation_and_recovery → polymarket.

**Incident investigator:** state_lifecycle_recovery → reconciliation_and_recovery → [`../implementation/r8_framework_acceptance.md`](../implementation/r8_framework_acceptance.md) → relevant incident report under [`../implementation/`](../implementation/).
