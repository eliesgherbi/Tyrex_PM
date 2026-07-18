# Tyrex_PM

Polymarket-first **event-driven** trading framework (active tree on `rest_project`).

Strategies emit intents; shared modules own market state, risk, execution, portfolio, lifecycle, persistence, and facts. Binance is **reference data only**.

## Status

| Item | State |
|------|-------|
| Accepted checkpoint | `fb9d0d8` — **R8 PASS** (framework validation complete) |
| Engine | Minimal in-process Tyrex dispatcher |
| NautilusTrader | **Not a dependency** |
| Z-Gap | **Not implemented** (future design) |
| Legacy code | Isolated under `old/` — never imported |

Three tiny-live validations completed (operator-run); third run automatic BUY+SELL success. Generic long-running live trading is **not** productized.

## Install and test

```bash
pip install -e ".[dev]"
tyrex-pm version
pytest
```

Python ≥ 3.11. Secrets: copy [`.env.example`](.env.example) → `.env` (never commit `.env`).

## Safe quickstart

```bash
tyrex-pm observe --config config/observe_fixture_r3.json
```

Shadow (paper OMS, no real orders):

```bash
tyrex-pm shadow --config config/observe_shadow_r5.json --btc-window next
```

## Architecture (high level)

```text
Adapters → Events → State → Indicators → Signals → Strategy
  → Intents → Risk → Execution plan → OMS
  → Execution events → Orders/Fills/Portfolio → Facts
```

## Documentation

Start at **[`Docs/README.md`](Docs/README.md)**.

- Current guides: [`Docs/latest/`](Docs/latest/README.md)
- Specifications: [`Docs/specifications/`](Docs/specifications/)
- Implementation evidence: [`Docs/implementation/`](Docs/implementation/)
