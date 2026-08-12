# Tyrex_PM documentation

**Tyrex_PM** is a Polymarket-first, event-driven trading framework: strategies declare an `InputContract` and emit intents; shared modules own market state, risk, execution, protection, persistence, and facts.

**Engine:** in-process asyncio host (`TradingRuntime`). NautilusTrader is not a dependency. Package version `0.3.0`.

## Maturity

| Area | Status |
|------|--------|
| Unified live runtime (one YAML, `--live`) | Implemented |
| `z_gap` plugin (PTB + spot trades + books) | Implemented |
| `ask70` plugin (books-only entry harness + protection overlay) | Implemented |
| InputContract / plugin registry / evaluation scheduler | Implemented |
| Live Polymarket OMS (tiny debit cap) | Implemented |
| Position protection (reactive FAK SL/TP/trailing) | Implemented |
| Maker GTC live POST | Planned only (`MAKER_LIMIT_PLANNED`) |
| `q_edge` / Binance L2 / perp adapters | Catalogued, not built |
| On-chain redeem / merge / cleanup | Unsupported |
| Legacy archive under `old/` | Reference only — never imported |

## Documentation layers

| Layer | Path | Role |
|-------|------|------|
| **Latest** | [`latest/`](latest/README.md) | Current accepted behavior — **start here** |
| **Specifications** | [`specifications/`](specifications/) | Historical objectives and design baselines (R-series). Superseded where they disagree with `latest/` or code. |
| **Implementation** | [`implementation/`](implementation/) | Chronological evidence (phase reports, incidents). Not current API. Some older R-series reports were removed from the tree. |

### Precedence

1. **Active code and executable tests** define actual behavior.
2. **`Docs/latest/`** explains current accepted behavior.
3. **`Docs/specifications/`** records historical objectives. Do not implement from them without checking `latest/`.
4. **`Docs/implementation/`** records chronological evidence.

If documents disagree, trust code/tests, correct `latest/`, and leave historical notes unmarked as current.

## Who should read what

| Reader | Start |
|--------|-------|
| New user / operator | [`latest/running.md`](latest/running.md) |
| Strategy developer | [`latest/extending.md`](latest/extending.md) → [`latest/strategy_inputs.md`](latest/strategy_inputs.md) |
| Framework developer | [`latest/architecture.md`](latest/architecture.md) → [`latest/execution_lifecycle.md`](latest/execution_lifecycle.md) |
| Protection / exits | [`latest/protection.md`](latest/protection.md) |

## Navigation

- Current docs TOC → [`latest/README.md`](latest/README.md)
- Formal specs index (historical) → [`specifications/README.md`](specifications/README.md)
