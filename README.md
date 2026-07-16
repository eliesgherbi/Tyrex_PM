# Tyrex_PM

Polymarket-focused **event-driven** trading framework (clean reset on `rest_project`).

Strategies receive normalized data, compute signals, emit typed intents, and rely on shared modules for risk, execution, portfolio truth, lifecycle, recovery, scheduling, and observability.

## Status

**R1–R4 complete** (observe → intent → risk → dry plan).  
**R5 complete:** ShadowOMS + order/fill/portfolio lifecycle + exits + persistence. Stop before R6 (live venue OMS).

NautilusTrader is **not** a dependency or engine candidate. See [`Docs/06_architecture_references.md`](Docs/06_architecture_references.md).

## Quick start

```bash
pip install -e ".[dev]"
tyrex-pm version
tyrex-pm observe --config config/observe_fixture_r3.json
tyrex-pm shadow --config config/observe_shadow_r5.json --btc-window next
tyrex-pm discover-btc-window --which next
pytest
```

`observe` = R3/R4 dry path (no OMS).  
`shadow` = R5 ShadowOMS on public market data (no real orders).

Secrets: copy [`.env.example`](.env.example) to `.env` (never commit `.env`). Public observe/shadow does not require private keys.

## Documentation

Start at [`Docs/00_objective.md`](Docs/00_objective.md). Implementation plan: [`Docs/07_implementation_plan.md`](Docs/07_implementation_plan.md). Validation: [`Docs/08_validation_report.md`](Docs/08_validation_report.md).

## Historical reference

Pre-reset code, tests, configs, and docs live under `old/`. That tree is **not** installed or imported by the active package. See `old/HISTORICAL_ENVIRONMENT.md`.
