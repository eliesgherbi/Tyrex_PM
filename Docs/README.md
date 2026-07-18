# Tyrex_PM documentation

**Tyrex_PM** is a Polymarket-first, event-driven trading framework: strategies emit intents; shared modules own market state, risk, execution, portfolio, lifecycle, persistence, and facts.

**Accepted checkpoint:** commit `fb9d0d8` (R8 PASS — framework validation complete; ready for Z-Gap *design*).  
**Engine:** minimal in-process Tyrex dispatcher. **NautilusTrader is not a dependency.**

## Maturity

| Area | Status |
|------|--------|
| Observe / shadow paths | Implemented and tested |
| Live Polymarket OMS (tiny one-shot) | Implemented; three operator live validations complete |
| Generic long-running live loop | Not productized |
| Z-Gap strategy | Not implemented (future work) |
| On-chain redeem / merge / cleanup | Forbidden / unsupported |
| Legacy archive under `old/` | Reference only — never imported |

## Documentation layers

| Layer | Path | Role |
|-------|------|------|
| **Latest** | [`latest/`](latest/README.md) | Current accepted behavior — start here to use or extend the framework |
| **Specifications** | [`specifications/`](specifications/) | Objectives, requirements, architecture baselines, plans |
| **Implementation** | [`implementation/`](implementation/) | Chronological evidence: phases, incidents, acceptance reports |

### Precedence

1. **Active code and executable tests** define actual behavior.
2. **`Docs/latest/`** explains current accepted behavior.
3. **`Docs/specifications/`** records objectives and design baselines.
4. **`Docs/implementation/`** records chronological evidence and decisions.

If documents disagree, trust code/tests, correct `latest/`, and preserve historical notes under `implementation/` or marked specifications.

## Who should read what

| Reader | Start |
|--------|-------|
| New user | [`latest/getting_started/`](latest/getting_started/installation.md) |
| Strategy developer | [`latest/concepts/strategy_execution_flow.md`](latest/concepts/strategy_execution_flow.md) → [`developer_guide/extending_the_framework.md`](latest/developer_guide/extending_the_framework.md) |
| Framework developer | [`latest/modules/overview.md`](latest/modules/overview.md) → [`developer_guide/development.md`](latest/developer_guide/development.md) |
| Operator | [`latest/how_to/run_modes.md`](latest/how_to/run_modes.md) → [`how_to/reconciliation_and_recovery.md`](latest/how_to/reconciliation_and_recovery.md) |
| Incident investigator | [`implementation/r8_framework_acceptance.md`](implementation/r8_framework_acceptance.md) + live incident reports |

## Implemented vs not

**Implemented:** adapters (Polymarket + Binance reference), indicators/signals, `ReferenceMomentumStrategy` (validation only), risk, planning, ShadowOMS, live Polymarket OMS + settlement, portfolio/lifecycle, durable ack/residual gates, facts/reporting, OBSERVE / SHADOW / LIVE_TINY one-shot.

**Not implemented:** Z-Gap, paired-leg/guru migration, full backtest platform, automatic redemption, generic continuous live trading product.

## Navigation

- Current docs TOC → [`latest/README.md`](latest/README.md)
- Formal specs index → files under [`specifications/`](specifications/)
- Evidence index → files under [`implementation/`](implementation/) (see [`r8_framework_acceptance.md`](implementation/r8_framework_acceptance.md))
