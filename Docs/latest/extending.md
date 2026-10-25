# Adding a strategy (or a market, or a host enhancement)

A strategy supplies only strategy-specific behaviour. One host owns feeds, readiness, execution, protection, persistence and reporting, and the same host components serve live, paper, observation and replay. Do not copy the host. Do not add `if strategy_kind == ...` anywhere outside `strategies/<name>/`.

Read this page before writing code. Ownership and the v1 pipeline: [architecture.md](architecture.md). Facts and adapters: [strategy_inputs.md](strategy_inputs.md). YAML roles: [config_guide.md](config_guide.md). Research workspace: [research_workspace.md](research_workspace.md).

The acceptance test `tests/integration/test_v1_new_strategy.py` proves the claim of this page: a strategy defined only in `tests/support/demo_strategy.py`, registered at test time, runs in the decision harness, is offered by the research workspace and replays a recorded window, with no framework edit.

## The contract (what every strategy implements)

| Piece | Where | What |
|---|---|---|
| Plugin | `strategies/<name>/plugin.py` | `StrategyPlugin` (`strategies/registry.py`): `kind`, `input_contract`, `load_config`, `create_driver`, `entry_tau_bounds`, `max_clock_uncertainty_ms`. Optional: `requires_protection`, `validate_run_config`, `indicator_specs`, `lifecycle_modes`, `decision_record_schema`, `max_entry_debit`, `required_depth_levels`, `research`. |
| Driver | `strategies/<name>/driver.py` | A dataclass inheriting `strategies/base.py:BaseDriver`, satisfying `strategies/protocol.py:StrategyDriver`. |
| Config | `strategies/<name>/schema.py` | `load_config(parameters, file)`; unknown keys fail. |
| Inputs | `facts/mappings.py` | The `InputContract` (facts read, indicators, what wakes `evaluate`). |
| Research | `ResearchSchema` on the plugin | How the workspace offers it (defaults, form layout, policy fields). |

Driver attributes the hosts read: `strategy_id`, `config`, `time_authority`, `target_notional`, `window_id`, `fee_curve` (None when the strategy has no fee estimate).

```text
evaluate(
    market_snapshot,   # books, quotes, freshness, optional trading reference
    context,           # DecisionContext: position, capabilities, now, tau_s, references
    features,          # FeatureBundle of the subscribed indicators
    trigger,           # EvaluationTrigger: feed | timer | replay | observe
    causation_id,
    correlation_id,
) -> StrategyEvaluation   # decision + generic intents + EligibilityFacts
```

Feedback methods (defaults in `BaseDriver`; override what the strategy uses). Hosts call them directly, never through `getattr`:

| Method | Called when |
|---|---|
| `on_entry_outcome(EntryOutcome)` / `on_exit_outcome(ExitOutcome)` | Every entry or exit intent got an outcome (not sent, zero fill, partial, filled, uncertain). |
| `configure_ptb(PtbSnapshot)` | The window's price to beat was sealed. |
| `on_hold_committed()` | The host committed the position to resolution. |
| `restore_window_usage(**usage)` | Restart inside a window already traded (attempt counters from the ledger). |
| `value_exit_refusal(...) -> str | None` | Final-gate recheck of a value exit. |
| `episode_view() -> EpisodeView | None` | Host reports and retry clocks read the current entry episode. |
| `persistence_slice()` / `restore_persistence_slice(data)` | Captured runs and reconstruction. |

Rules:

1. Declare facts; the host starts only that source closure.
2. Subscribe indicators with parameters; the host owns one `IndicatorGraph`. Mark an indicator `gates_entry=True` when no entry may be sent before it is ready (`ewma_vol` in z_gap and settlement_value).
3. Read time from the host: `context.now`, `context.tau_s` (seconds to the market's end). Never the wall clock.
4. Emit generic intents only. No SDK, no account reads, no fill inference in `strategies/`.
5. Accept every `EvaluationTrigger` value; do not branch behaviour on the trigger.
6. Shared helpers: `strategies/common.py` (entry price cap, outcome instrument), `indicators/binary_fair_value.py` (fair value), `execution/fill_model.py` (fills and fees, host side).

## Checklist (new strategy)

1. **Inputs** — reuse ids in `facts/ids.py`; put the `InputContract` in `facts/mappings.py`. A new source needs an id, a live adapter (or it stays in `UNIMPLEMENTED_ADAPTERS`) and a replay route in `research/strategy_replay/feed.py`.
2. **Package** — `strategies/<name>/` with `config.py`, `schema.py`, `reasons.py`, `strategy.py`, `driver.py`, `plugin.py`.
3. **Driver** — inherit `BaseDriver`; define `strategy_id`; implement `evaluate`; override the feedback methods you use.
4. **Plugin** — fill the required fields; add `research=ResearchSchema(...)` to offer it in the workspace (start with `default_parameters` taken from your run YAML, `essentials` for the first form section, `default_protection` if `requires_protection`).
5. **Register** — import the plugin in `ensure_strategies_registered()` (`strategies/registry.py`). That is the only host line.
6. **YAML** — `config/runs/<name>_….yaml` with `strategy.kind`; a full `protection:` block if required.
7. **Tests** — add the kind to `tests/support/strategy_configs.py`. The contract test (`tests/unit/strategies/test_driver_contract.py`) and V1-a (`tests/integration/test_v1_all_strategies_replay.py`) then cover it automatically. Add a lab fixture and unit tests for the decision logic.
8. **Docs** — this page's reference list, [strategy_inputs.md](strategy_inputs.md), [config_guide.md](config_guide.md).

## Proving a strategy, from cheapest to closest to live

| Step | Command | Proves |
|---|---|---|
| Decision harness | `tyrex-pm eval --config <yaml> --fixture tests/fixtures/lab/<pack>.json` | `evaluate()` logic on hand-written books, depth, trades. No execution. |
| Replay | Research workspace, or `research.strategy_replay.run_window(window, strategy_kind=..., ...)` | The full host pipeline on recorded windows: scheduler, indicators, planner, final gate, simulated fills, outcomes back to the driver. |
| Observe | `tyrex-pm observe --config <yaml> --connect-public-feeds` | Live public feeds, signal only. |
| Paper | `tyrex-pm run --config <paper yaml> --paper` | The live host with the simulated venue (same fill rule as replay). |
| Live | `tyrex-pm run --config <live yaml> --live` | Real orders under the run's debit cap. |

### Lab fixture (`lab_evaluate_v1`)

Copy `tests/fixtures/lab/ask70_books.json` or `feature_probe_l2.json`. Steps are applied in order; `evaluate: true` calls the driver after that step's ingest.

| Step field | Use when |
|---|---|
| `books` | The plugin reads Polymarket asks/bids |
| `depth` | L2 indicators (`feature_probe` imbalance) |
| `trade` | Trade indicators (EWMA) |
| `reference_price` | Trading reference on `DecisionSnapshot` |

Set `event_end` so the remaining time sits inside the plugin's entry band. Pair the pack with a run YAML: `eval` loads `strategy.kind` and parameters from `--config`.

## Intents the host understands

| Intent | Host behaviour |
|---|---|
| `EnterIntent` + `TAKER` | `MarketBuyOrderSpec` FAK through the lifecycle (replay: simulated amount BUY). |
| `EnterIntent` + `MAKER` | Post-only `LimitOrderSpec` GTC; stays working until fill, cancel or expire (live only). |
| `ExitIntent` / `FlattenIntent` | Cancel session-owned orders, then `MarketSellOrderSpec`; bounded FAK retries. |
| `CancelIntent` | Cancel the named venue id; reconcile before any following sell. |
| `HoldToResolutionIntent` | Hold the position to resolution (settlement lifecycle: ledger, payout tracking, redemption worklist). |

## Adding a market family

A recurring up/down market (asset + window length) is configuration: add `config/markets/<family_id>.yaml`.

```yaml
schema: window_family_v1
family_id: btc_updown_15m
asset: BTC
slug_prefix: btc-updown-15m
window_duration_s: 900
resolution_stream: https://data.chain.link/streams/btc-usd
binance_symbol: BTCUSDT
outcomes: [UP, DOWN]
enabled: false
```

Discovery, the runtime family registry, run configs (`market.family`), the warehouse collector profile and replay read it (`domain/polymarket/window_families.py`). Acceptance: `tests/unit/runtime/test_market_family_config.py`.

- `enabled: false` keeps a family out of implicit discovery and out of the normalizer. **Enabling** a family changes what the normalizer extracts from raw records: raise `PARSER_VERSION` in the same change.
- Only BTC reference feeds are wired. A family for another asset is refused until the collector's Binance and Chainlink streams and the RTDS reference symbol take the family's symbols (`data_warehouse/supervisor.py`, `data_warehouse/connectors/streams.py`, `adapters/polymarket/rtds_normalize.py`).
- Multi-outcome or windowless markets need a new market/session abstraction (`BinaryMarket` is two-outcome); not in v1.

## Module by module

Touch the smallest owner. Strategy math belongs in `strategies/<name>/`.

- **`facts/`** — new fact id, derived producer or contract. Tests: `tests/unit/facts/test_input_contracts.py`.
- **`indicators/`** — reusable producers and `IndicatorSpec` parameters. Strategies read `FeatureBundle` keys; they do not keep private tapes.
- **`runtime/composition.py`** — a new live adapter goes into `LIVE_ADAPTERS`; until then it fails closed.
- **`runtime/run_config.py`, `protection/spec.py`** — shared YAML keys only; strategy leaves go in the strategy's `schema.py`.
- **`runtime/host/`** — host components of the trading runtime (see [architecture.md](architecture.md)). Branch on `input_contract` or plugin fields, never on kind.
- **`execution/`, `persistence/`, `reporting/`** — intents in, planner, coordinator, gateway; the journal is the authority; reports are projections.
- **`research/strategy_replay/`** — the replay engine. A new source fact needs a route in `feed.py` (live semantics).
- **`research/workflows/`** — the workspace API. It reads `StrategyPlugin.research`; do not add strategy-specific code here.

## What not to do

- Do not start Binance/Chainlink "because other strategies do".
- Do not wake `evaluate()` from every ingest; the `EvaluationScheduler` does.
- Do not read `driver.state` or probe drivers with `getattr` from a host: add a contract method.
- Do not compute time to close or wall-clock time in a strategy.
- Do not hardcode slugs, window lengths or symbols: use the market family.
- Do not treat `q_edge` as runnable (guide contract only).

## Reference

- Plugins: `strategies/ask70/plugin.py`, `strategies/feature_probe/plugin.py`, `strategies/settlement_value/plugin.py`, `strategies/z_gap/plugin.py`.
- Contract and defaults: `strategies/protocol.py`, `strategies/base.py`, `strategies/triggers.py`, `strategies/research.py`.
- Test-only example strategy: `tests/support/demo_strategy.py`.
- Decision harness: `src/tyrex_pm/lab/evaluate_replay.py`. Replay: `src/tyrex_pm/research/strategy_replay/run.py`.
