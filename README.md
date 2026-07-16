# Tyrex_PM

Polymarket-focused **event-driven** trading framework (clean reset on `rest_project`).

Strategies receive normalized data, compute signals, emit typed intents, and rely on shared modules for risk, execution, portfolio truth, lifecycle, recovery, scheduling, and observability.

## Status

**R1 complete:** historical implementation archived under [`old/`](old/) (checkpoint `630bac2`).  
**R2 complete:** `core/` + `engine/` event contracts and in-process dispatcher (`tyrex-pm` 0.3.0).  
Next: **R3** read-only market-data and observe strategy (not started).

NautilusTrader is **not** a dependency or engine candidate. See [`Docs/06_architecture_references.md`](Docs/06_architecture_references.md).

## Quick start

```bash
pip install -e ".[dev]"
tyrex-pm version
tyrex-pm help
pytest
```

Secrets: copy [`.env.example`](.env.example) to `.env` (never commit `.env`). The R1 skeleton does not require venue credentials.

## Documentation

Start at [`Docs/00_objective.md`](Docs/00_objective.md). Implementation plan: [`Docs/07_implementation_plan.md`](Docs/07_implementation_plan.md).

## Historical reference

Pre-reset code, tests, configs, and docs live under `old/`. That tree is **not** installed or imported by the active package. See `old/HISTORICAL_ENVIRONMENT.md`.
