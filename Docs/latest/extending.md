# Adding a strategy (or enhancing the host)

A strategy integration supplies only strategy-specific behavior. One host (`TradingRuntime`) owns feeds, readiness, execution, protection, persistence, and reporting. Do not copy that host. Do not add `if strategy_kind == ...` in `run_config.py`, `market_data_runtime.py`, `trading_runtime.py`, or `market_runtime.py`.

Read this page before writing code. Ownership and live adapters: [architecture.md](architecture.md), [strategy_inputs.md](strategy_inputs.md). YAML field roles: [config_guide.md](config_guide.md). Operator CLI: [running.md](running.md).

## Locked patterns

1. Declare facts; the host starts only that source closure.
2. Subscribe indicators with parameters; the host owns one `IndicatorGraph` and passes a `FeatureBundle`.
3. Implement one `evaluate()` shape for every plugin (below). Private snapshots (z_gap fair value, etc.) stay inside `strategies/<name>/`.
4. Emit generic intents only. No SDK, no account reads, no fill inference from `strategies/`.
5. Prove `evaluate()` on a lab fixture **before** any `--live` run.

```text
evaluate(
    market_snapshot,   # books, quotes, freshness, optional trading reference
    context,           # position, caps, notional, now, references
    features,          # subscribed indicators only
    trigger,           # "feed" or "timer"
    causation_id,
    correlation_id,
) -> StrategyEvaluation
```

Last Chainlink / sealed K, when aligned-eval has them, live on `DecisionContext.references` (`ReferenceFacts`). They are not `evaluate()` kwargs.

## Checklist (new strategy)

1. **Fact contract** — reuse ids in `facts/ids.py`. New source: id + producer edge if derived + live adapter (or leave it in `UNIMPLEMENTED_ADAPTERS`) + mapping in `facts/mappings.py`.
2. **Package** — `strategies/<name>/` with `config.py`, `schema.py` (unknown keys fail), `reasons.py`, `strategy.py`, `driver.py`, `plugin.py`.
3. **Plugin** — `StrategyPlugin` with `kind`, `input_contract`, `load_config`, `create_driver`, `entry_tau_bounds`, `max_clock_uncertainty_ms`. Optional: `requires_protection` (bool or `config -> bool`), `validate_run_config`, `indicator_specs(config)` (default is `input_contract.indicators`). Register from `ensure_strategies_registered()`.
4. **Driver** — `evaluate(...)` → `StrategyEvaluation` with `EligibilityFacts` plus zero or more generic intents.
5. **YAML** — `config/runs/<name>_….yaml` with `strategy.kind` matching the plugin. If protection is required, include a full `protection:` block.
6. **Lab fixture** — `tests/fixtures/lab/<name>_….json` (`lab_evaluate_v1`) and a unit test that calls `evaluate_replay` / `tyrex-pm eval`. See [Lab fixture before live](#lab-fixture-before-live).
7. **Docs** — this page, [strategy_inputs.md](strategy_inputs.md) contract section, [running.md](running.md) config table, [config_guide.md](config_guide.md) parameters subsection. Do not rewrite historical `Docs/implementation/` “current code” sections.

## Module-by-module: how to proceed

Touch the **smallest** owner. If the change is strategy math, it belongs in `strategies/<name>/`, not in the host.

### `facts/`

**When:** a new fact id, derived producer, or `InputContract`.

**How:** add the id to `facts/ids.py` (`SOURCE_FACTS` or derived). If derived, declare upstreams in `facts/producers.py`. Put the contract in `facts/mappings.py` and on the plugin. Tests: `tests/unit/facts/test_input_contracts.py` — what adapters start, what does **not** start, scheduler wake.

**Do not:** start Binance/Chainlink “because other strategies do.” ask70 must still run if those feeds are down.

### `indicators/`

**When:** a reusable producer (OFI, imbalance, EWMA, …) or new `IndicatorSpec` params.

**How:** add or parameterize the producer. `ewma_vol` requires explicit `SigmaConfig` fields on the spec — no silent defaults. The plugin builds specs from YAML (`indicator_specs(config)`). The host constructs one `IndicatorGraph` and calls `on_trade` / `on_depth`. Strategies read `FeatureBundle` keys; they do not ingest tapes.

**Do not:** give the driver a private EWMA/L2 store. Do not import Polymarket `BookSnapshot` into indicator unit tests.

### `strategies/<name>/`

**When:** model, policy, reasons, typed config.

**How:** keep `DecisionSnapshot` generic (books + market). Assemble private snapshots inside the package (see `z_gap/assemble.py`). Map `features` → your types in the **driver**. `schema.py` rejects unknown YAML keys.

**Do not:** import the SDK, gateway, or `TradingRuntime`. Do not add `settlement_reference` kwargs.

### `strategies/registry.py`

**When:** a new kind.

**How:** import the plugin in `ensure_strategies_registered()` and `register_strategy(...)`. That is the only host registration line.

**Do not:** add kind switches in `run_config.py`.

### `runtime/composition.py`

**When:** a new **live** adapter exists.

**How:** add the source fact to `LIVE_ADAPTERS`. Until then it stays in `UNIMPLEMENTED_ADAPTERS` and composition fails closed.

**Do not:** start perp/funding because a guide contract (`q_edge`) names them.

### `runtime/run_config.py` / `protection/spec.py`

**When:** a **shared** YAML key (market, risk, lifecycle, protection).

**How:** add the field, reject-unknown, document in [config_guide.md](config_guide.md) and `config/runs/generic_parameter_catalog.yaml`. Strategy-only leaves go in `strategies/<name>/schema.py`. `requires_protection` may be a callable of loaded config (`feature_probe` + `emit_entry`).

**Do not:** `if strategy_kind == ...`.

### `runtime/market_runtime.py` / `market_data_runtime.py` / `trading_runtime.py`

**When:** a host capability every plugin needs (graph ingest, `ReferenceFacts`, scheduler).

**How:** branch on `input_contract.requires(...)` or `starts_adapter(...)`, never on kind. `evaluate()` stays the generic signature. Lab and live call the same driver method.

**Do not:** copy `TradingRuntime` into a second host, Observe/Shadow path, or Nautilus.

### `runtime/market_family.py`

**When:** a new discovery/window model (not a new strategy).

**How:** register a `MarketFamily`. YAML `market.family` must match.

**Do not:** hardcode slugs in the trading host.

### `protection/`

**When:** overlay trigger styles or YAML fields.

**How:** `ProtectionSpec` is framework-owned. Strategies declare thresholds; they do not POST SL/TP. See [protection.md](protection.md).

**Do not:** fold protection math into ask70 or a new entry harness.

### `execution/` / `persistence/` / `reporting/`

**When:** OMS, journal, or report behavior.

**How:** intents in → planner → coordinator → gateway. Journal is authority; reports are projections. Tick/size come from books, not strategy YAML.

**Do not:** let a strategy POST or infer fills.

### `lab/`

**When:** proving `evaluate()` without venue mutation.

**How:** add a `lab_evaluate_v1` JSON pack and call `evaluate_replay` / `replay_configured_run`. Do not import `execution.polymarket.gateway`.

### `Docs/latest/` and tests

**When:** any of the above ships.

**How:** correct **this folder** (not historical `Docs/implementation/` “current code” sections). Add unit tests next to the owner (`tests/unit/strategies/<name>/`, `tests/unit/facts/`, `tests/unit/lab/`).

## Lab fixture before live

The lab calls the **same** `evaluate()` as production. It does not start adapters, sign, or POST. `--live` is rejected. Use it to prove the door (books + features + intents) before a $5 live run.

### 1. Add `tests/fixtures/lab/<name>_<scenario>.json`

Schema must be `lab_evaluate_v1`. Copy `tests/fixtures/lab/ask70_books.json` or `feature_probe_l2.json`.

```json
{
  "schema": "lab_evaluate_v1",
  "target_notional": "5",
  "window": {
    "market_id": "lab-mykind",
    "condition_id": "lab-mykind-condition",
    "question": "mykind lab",
    "window_id": "lab-mykind",
    "event_start": "2026-09-13T12:00:00+00:00",
    "event_end": "2026-09-13T12:05:00+00:00"
  },
  "steps": [
    {
      "ts": "2026-09-13T12:01:00+00:00",
      "evaluate": true,
      "books": { "up_ask": "0.71", "down_ask": "0.30" },
      "depth": {
        "source": "binance.spot.l2",
        "instrument_id": "btcusdt",
        "bids": [["64000", "4"]],
        "asks": [["64010", "1"]]
      },
      "trade": { "source": "binance.spot.trades", "price": "64000" },
      "reference_price": "64000"
    }
  ]
}
```

| Step field | Use when |
|------------|----------|
| `books` | Plugin reads Polymarket asks/bids (`ask70`, most entries) |
| `depth` | Plugin subscribed L2 indicators (`feature_probe` imbalance) |
| `trade` | Plugin subscribed trade indicators (z_gap EWMA) |
| `reference_price` | Trading reference on `DecisionSnapshot` (z_gap `S`) |
| `evaluate: true` | Call `driver.evaluate()` after applying this step’s ingest |

Omit unused fields. Steps are applied in order; later evaluates reuse the last books / last reference / graph state. Set `event_end` so remaining tau sits inside the plugin’s `tau_min_s` / `tau_max_s`.

Shipped packs: [`tests/fixtures/lab/README.md`](../../tests/fixtures/lab/README.md).

### 2. Pair the fixture with a run YAML

`eval` loads `strategy.kind` + `parameters` from `--config`. The fixture does not replace YAML.

### 3. Add a unit test

`tests/unit/lab/test_evaluate_replay.py` (or `tests/unit/strategies/<name>/`):

```python
from tyrex_pm.lab.evaluate_replay import replay_configured_run

results = replay_configured_run(
    config_path="config/runs/<name>_catalog.yaml",
    fixture_path="tests/fixtures/lab/<name>_….json",
)
assert results  # action, eligibility, intents as appropriate
```

Assert eligibility and intents. Do not assert live-edge economics unless that is the point of the fixture. Do not import the gateway.

### 4. Run locally (no `--live`)

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/<name>_….yaml \
  --validate-config

python -m tyrex_pm.application.cli eval \
  --config config/runs/<name>_….yaml \
  --fixture tests/fixtures/lab/<name>_….json

python -m pytest -q tests/unit/lab tests/unit/strategies/<name> tests/unit/facts/test_input_contracts.py
```

`--validate-config` proves the YAML and kind load. `eval` prints a JSON list: `action`, `reason_code`, `strategy_inputs_eligible`, `blockers`, `intent_kinds`. Optional `--out decisions.json`.

Only after those are green consider `--live` with the $5 cap. Lab success is **not** OMS proof (fills, SL print, dust). That remains a tiny-live operator run. See [running.md](running.md).

## Intents the host understands

| Intent | Host behavior today |
|--------|---------------------|
| `EnterIntent` + `TAKER` | Plan `MarketBuyOrderSpec` FAK; POST through the lifecycle. |
| `EnterIntent` + `MAKER` | Plan post-only `LimitOrderSpec` GTC; POST; stay working until fill / cancel / expire (`MAKER_LIMIT_POSTED`). |
| `ExitIntent` / `FlattenIntent` | Cancel any session-owned live ids, then plan `MarketSellOrderSpec`; bounded FAK retries. |
| `CancelIntent` | Cancel the named venue id; reconcile before any following sell. Never `cancel_all`. |
| `HoldToResolutionIntent` | Record `HOLD_TO_RESOLUTION`; no venue mutation. Automatic redeem is not implemented. z_gap rejects `resolution_capability_default: true` at config load. |

## What not to do

- Do not start Binance/Chainlink “because the host always did.”
- Do not wake `evaluate()` from every ingest. Use `EvaluationScheduler`.
- Do not treat protection as strategy code.
- Do not import Polymarket SDK from `strategies/`.
- Do not add a market family by hardcoding slugs in the host.
- Do not treat `q_edge` as runnable (guide contract; perp adapters unimplemented).
- Do not skip the lab fixture and go straight to `--live`.

## Reference

- Live plugins: `strategies/ask70/plugin.py`, `strategies/z_gap/plugin.py`, `strategies/feature_probe/plugin.py`.
- Guide contract (not a plugin): `Q_EDGE_INPUT_CONTRACT` in `facts/mappings.py`.
- Lab helper: `src/tyrex_pm/lab/evaluate_replay.py`.
- Inputs and adapters: [strategy_inputs.md](strategy_inputs.md).
- Protection YAML: [protection.md](protection.md).
- Parameter roles: [config_guide.md](config_guide.md).
