# Strategy inputs

Strategies declare what they read. The runtime supplies only that closure and wakes `evaluate()` only on declared triggers. Ingest never implies evaluation.

## Fact catalog

Stable identifiers live in `src/tyrex_pm/facts/ids.py`. Unknown ids fail contract construction.

**Source facts** (adapters):

| Fact id | Live adapter today |
|---------|--------------------|
| `clock.sync` | Yes |
| `account.snapshot` | Yes |
| `polymarket.books` | Yes |
| `polymarket.market_meta` | Yes |
| `chainlink.twap` | Yes |
| `binance.spot.trades` | Yes |
| `binance.spot.l2` | Yes (`adapters/binance/depth_adapter.py`) |
| `binance.perp.trades` | Catalogued; **no adapter** |
| `binance.perp.l2` | Catalogued; **no adapter** |
| `binance.perp.funding` | Catalogued; **no adapter** |

**Derived facts** (producers in `facts/producers.py`):

| Fact id | Upstream sources |
|---------|------------------|
| `ptb.sealed` | Chainlink TWAP, clock, market meta, Binance spot trades |
| `reference.aligned` | Binance spot trades, Chainlink TWAP |
| `tau` | Market meta, clock |
| `binance.spot.mid` | `binance.spot.l2` (live when that adapter starts) |
| `binance.perp.mid` | `binance.perp.l2` (unimplemented adapter) |

`runtime/composition.py` starts the transitive **source** closure. A contract that names an unimplemented source fails closed at composition (`missing_live_adapters`).

## InputContract

`src/tyrex_pm/facts/contract.py`:

- `required` / `optional` — fact ids the strategy may read.
- `indicators` — parameterized `IndicatorSpec`s (sources must be in the contract or its derived closure).
- `evaluate_on` — `OnFact(fact, coalesce_s, after_ready=...)` and/or `OnTimer(interval_s)`.

`source_facts()` expands derived facts to the adapters the host must start. A connector that is not in that set is not a liveness dependency.

`EligibilityFacts` is the host’s only strategy-readiness surface: `strategy_inputs_eligible`, `model_ready`, and `blockers`. The host must not inspect `strategy_kind` to decide model readiness.

Every plugin’s `evaluate()` takes the same arguments: `market_snapshot`, `context` (`DecisionContext`, including `references` when aligned-eval has them), `features`, `trigger` (`"feed"` or `"timer"`), and the two ids. Strategy-private snapshots are assembled inside `strategies/<name>/`.

## EvaluationScheduler

`src/tyrex_pm/runtime/scheduler.py` answers two questions after stores are updated:

1. `should_evaluate(fact)` — this fact is in `evaluate_on`, optional `after_ready` is true, coalesce window elapsed.
2. `should_evaluate_timer()` — an `OnTimer` interval elapsed.

Callers always ingest; they then ask the scheduler. A Binance trade does not wake ask70. A Polymarket book update does not wake z_gap unless the contract says so.

## Registered contracts

Reference mappings: `src/tyrex_pm/facts/mappings.py`.

### ask70 (live plugin)

Required: `polymarket.books`, `clock.sync`, `account.snapshot`.  
Optional: `polymarket.market_meta`.  
Wake: book updates (1 s coalesce) and a 1 s timer.  
**Does not** start Binance or Chainlink.

### z_gap (live plugin)

Required: books, Binance spot trades, Chainlink TWAP, clock, account, `ptb.sealed`, `reference.aligned`, market meta.  
Indicator: EWMA vol on spot trades.  
Wake: Binance spot trades **after** `ptb.sealed`, 1 s coalesce.

### feature_probe (live plugin)

Required: `polymarket.books`, `clock.sync`, `account.snapshot`, `binance.spot.l2`.  
Optional: `polymarket.market_meta`.  
Indicator: L1 `imbalance` on spot L2.  
Wake: book updates and spot L2 (1 s coalesce).  
**Does not** start Chainlink, spot trades, or perp. Shipped YAML is validate-only (`emit_entry: false`).

### q_edge (guide only)

Mapped in `facts/mappings.py` for a future multi-venue strategy. **Not** a registered plugin. It names L2 / perp / funding facts whose adapters are not implemented. Do not treat it as runnable.

## Indicators

`src/tyrex_pm/indicators/` owns reusable producers over a generic instrument-keyed L2/trade store (`DepthSnapshot` / `DepthStore`). That store is **not** the Polymarket `[0,1]` `BookSnapshot`.

z_gap EWMA vol is one `IndicatorSpec` instance. The registered plugin supplies YAML `volatility.*` as spec `params`; the host runs `IndicatorGraph` and passes a `FeatureBundle` into `evaluate()`. Trade-tape freshness for that graph uses `market.maximum_book_age_ms` (not a hardcoded 2000 ms). A reconnect increments the Binance trade `connection_generation` and invalidates the estimator; eligibility returns only after `min_samples_s` on the new generation. The final BUY gate re-reads the bound graph snapshot (`features_ready`) and fails closed if EWMA is stale, invalidated, or unready. It does not re-run `evaluate()`. SELL and protective flatten ignore EWMA unreadiness. ask70’s empty indicator graph is a no-op at the gate.

`feature_probe` does the same graph wiring for L1 `imbalance` on `binance.spot.l2`. Consume-time L2 age and L2 reconnect invalidation are **deferred (F-L2)** and are not a first-release pass criterion. Producers for OFI, imbalance, trade imbalance, and realized vol exist for later strategies; they do not start connectors by themselves.

The lab (`tyrex_pm.lab.evaluate_replay`) rebuilds that graph from the same `indicator_specs(config)` and applies fixture `trade` / `depth` steps before `evaluate()`. It does not start adapters. Adding a pack: [extending.md](extending.md#lab-fixture-before-live).

## Market families

`src/tyrex_pm/runtime/market_family.py` owns discovery and window duration independently of strategy. The only registered family today is `btc_updown_5m` (300 s). YAML `market.family` must match a registered family.

`market.binance_symbol` remains on the run config (default `BTCUSDT`) for families that use a spot reference. ask70 YAML may still set it; composition will not start Binance unless the contract requires `binance.spot.trades`.
