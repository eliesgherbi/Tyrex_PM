# Tyrex_PM

Polymarket-focused **event-driven** trading framework (clean reset on `rest_project`).

## Status

| Checkpoint | Commit | Notes |
|------------|--------|-------|
| R5 | `5cc1a30` | Shadow OMS + portfolio + lifecycle |
| R5.1 | `6b03cc3` | Unified TradingHost + retry/escalation |
| R6A/B | *uncommitted* | LiveOMS + reconcile + read-only probe |

**R6 does not submit or cancel real orders.** R7 requires explicit authorization.

NautilusTrader is **not** a dependency. See [`Docs/06_architecture_references.md`](Docs/06_architecture_references.md).

## Quick start

```bash
pip install -e ".[dev]"
tyrex-pm version
tyrex-pm observe --config config/observe_fixture_r3.json
tyrex-pm shadow --config config/observe_shadow_r5.json --btc-window next
pytest
# R6B read-only (no mutations):
python scripts/r6b_readonly_probe.py
```

Secrets: copy [`.env.example`](.env.example) to `.env` (never commit `.env`).

## Documentation

Start at [`Docs/00_objective.md`](Docs/00_objective.md).  
Venue semantics: [`Docs/Implementation/r6_venue_semantics.md`](Docs/Implementation/r6_venue_semantics.md).  
Validation: [`Docs/08_validation_report.md`](Docs/08_validation_report.md).

## Historical reference

Pre-reset code lives under `old/` and is **not** imported by the active package.
