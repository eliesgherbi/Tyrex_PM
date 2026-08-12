# Adding a strategy

A strategy integration supplies only strategy-specific behavior. Do not copy `TradingRuntime`, `ExecutionLifecycle`, the gateway, or the reducer. Do not add `if strategy_kind == ...` in `market_data_runtime.py` or `run_config.py`.

## Checklist

1. **Fact contract** — add or reuse ids in `facts/ids.py`. If you need a new source, add the id, a producer edge if derived, a live adapter (or leave it in `UNIMPLEMENTED_ADAPTERS` until built), and a mapping in `facts/mappings.py`.
2. **Package** — `strategies/<name>/` with immutable config, schema validation, reasons, strategy, driver, and `plugin.py`.
3. **Plugin** — a `StrategyPlugin` with `kind`, `input_contract`, `load_config`, `create_driver`, `entry_tau_bounds`, `max_clock_uncertainty_ms`, optional `requires_protection` and `validate_run_config`. Register it from `ensure_strategies_registered()` in `strategies/registry.py`.
4. **Driver** — implement `evaluate(...)` and return `StrategyEvaluation` (`strategies/evaluation.py`) with `EligibilityFacts` plus zero or more generic intents.
5. **Intents only** — `EnterIntent` (optional `LiquidityRole.TAKER` default or `MAKER`), `ExitIntent`, `FlattenIntent`, `HoldToResolutionIntent`. No SDK, no account reads, no fill inference.
6. **State** — serialize/restore without venue objects.
7. **YAML** — `strategy.kind` matching the plugin; parameters validated by the plugin schema. If `requires_protection`, include a full `protection:` block.
8. **Tests** — model math, gate precedence, intent semantics, state restore, and that an unknown `strategy.kind` is rejected.

The runtime starts **only** connectors in the contract’s transitive source closure and schedules evaluation **only** on declared triggers.

## Intents the host understands

| Intent | Host behavior today |
|--------|---------------------|
| `EnterIntent` + `TAKER` | Plan `MarketBuyOrderSpec` FAK; POST through the lifecycle. |
| `EnterIntent` + `MAKER` | Plan `LimitOrderSpec` GTC; record `MAKER_LIMIT_PLANNED`; **do not POST**. |
| `ExitIntent` / `FlattenIntent` | Plan `MarketSellOrderSpec`; bounded FAK retries. |
| `HoldToResolutionIntent` | Record `HOLD_TO_RESOLUTION`; no venue mutation. Automatic redeem is not implemented. z_gap rejects `resolution_capability_default: true` at config load. |

## What not to do

- Do not start Binance/Chainlink “because the host always did.” ask70 must keep running if those feeds are down.
- Do not wake `evaluate()` from every ingest. Use `EvaluationScheduler`.
- Do not treat protection as strategy code. Declare `ProtectionSpec`; the overlay arms after fill.
- Do not import Polymarket SDK from `strategies/`.
- Do not add a new market family by hardcoding slugs in the host. Register a `MarketFamily`.

## Reference

- Live plugins: `strategies/ask70/plugin.py`, `strategies/z_gap/plugin.py`.
- Guide contract (not a plugin): `Q_EDGE_INPUT_CONTRACT` in `facts/mappings.py`.
- Inputs and adapters: [strategy_inputs.md](strategy_inputs.md).
- Protection YAML: [protection.md](protection.md).
