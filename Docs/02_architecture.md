# 02 — Architecture and ownership

**Phase:** R3 implemented (read-only observe path)  
**Engine:** Minimal Tyrex in-process event-driven engine (no NautilusTrader)  
**R2 checkpoint:** `ccccc969bb4877ae97e6e56c656b839739034425`

## Implemented flow (R3)

```text
MarketDiscovery (fixture | Gamma)
    → BinaryMarket → InstrumentRegistry
Polymarket adapter / fixture
    → BookSnapshotReceived | BookDeltaReceived | TickSizeChanged
    → MarketStateStore (authoritative book)
    → BookUpdated (complete reconstructed view)
Binance trade adapter / fixture
    → ReferencePriceUpdated
    → ReferenceDataStore
ObserveHost (on reference update)
    → DecisionSnapshot (immutable)
    → FreshnessAssessment (derived, not latched)
    → ShortHorizonMomentum + mid/spread views
    → DirectionalSignal
    → ReferenceMomentumStrategy (observe decision)
    → FactEnvelope → JSONL sink
```

## Packages present

| Package | Role |
|---------|------|
| `tyrex_pm.application` | CLI (`version`, `observe`, `discover-btc-window`) |
| `tyrex_pm.core` | Ids, clock, events, snapshots, envelopes, book ingress events |
| `tyrex_pm.engine` | `EventDispatcher` |
| `tyrex_pm.domain.polymarket` | `BinaryMarket`, `MarketRequest` |
| `tyrex_pm.adapters.polymarket` | Normalize + fixture + Gamma discovery + market WS |
| `tyrex_pm.adapters.binance` | Normalize + fixture + public trade WS |
| `tyrex_pm.market_data` | Registry, book/reference stores, freshness, executable views, snapshot |
| `tyrex_pm.indicators` | Short-horizon momentum (stateful) |
| `tyrex_pm.signals` | Directional signal builder |
| `tyrex_pm.strategies.framework_validation` | Observe-only `ReferenceMomentumStrategy` |
| `tyrex_pm.reporting` | JSONL fact sink |
| `tyrex_pm.runtime` | Config + observe composition root + live runner |
| `tyrex_pm.operations` | BTC Up/Down window slug helpers (not a second runtime) |

**Merged:** BTC window selection lives under `operations` re-exporting discovery helpers rather than a separate package tree. No empty R4–R8 placeholders.

## State ownership

| State | Authoritative owner |
|-------|---------------------|
| Resolved market / YES-NO tokens | `InstrumentRegistry` |
| Reconstructed books | `MarketStateStore` (Option B) |
| Latest Binance reference | `ReferenceDataStore` |
| Momentum rolling buffer | `ShortHorizonMomentum` |
| Freshness | Derived at decision time via `assess_freshness` |
| Facts | `JsonlFactSink` |

The dispatcher transports events; it does **not** own market state.

## Book protocol choice (Option B)

Official Polymarket CLOB market channel publishes:

- `book` — full snapshot
- `price_change` — level deltas (`size` `"0"` removes)
- `tick_size_change` — tick change (store invalidates until new snapshot)

Therefore ingress events are snapshot/delta/tick; the **store** reconstructs. Adapters do not present a silently mutated book as a stateless snapshot.

Legacy `old/.../market_ws.py` is a stub and is not imported.

## Dependency direction

`application → runtime → strategies/signals/indicators/market_data/adapters → core`  
`engine` may import `core`. Nothing imports `old/`. No NautilusTrader.

## Numeric policy

Trading prices/quantities use `Decimal`. Venue tick rounding deferred past R3.
