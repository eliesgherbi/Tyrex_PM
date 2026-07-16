# 03 — Module contracts

**Phase:** R3 — read-only observe path implemented.

## `tyrex_pm.application`

CLI: `version`, `help`, `observe` (fixture|live), `discover-btc-window`.  
CLI selects mode/paths/slug/duration; trading decisions stay in the host/strategy.

## `tyrex_pm.core`

R2 contracts plus R3 ingress:

| Module | Contract |
|--------|----------|
| `book_events` | `BookSnapshotReceived`, `BookDeltaReceived`, `TickSizeChanged`, `BookLevelDelta`, `BookSide` |
| `events` | `BookUpdated` (store-emitted complete view), `ReferencePriceUpdated`, `TimerElapsed` |
| `facts` / `signals` / `indicators` | Envelopes consumed by R3 builders |

## `tyrex_pm.domain.polymarket`

| Type | Contract |
|------|----------|
| `MarketRequest` | slug / url / condition_id / fixture_path |
| `BinaryMarket` | condition, YES/NO instruments, timing, tick/min size, status |
| `make_binary_instruments` | Token → Instrument mapping |

No BTC/Z-Gap strategy logic in this domain module.

## `tyrex_pm.adapters`

Protocols: `MarketDiscovery`, `MarketDataAdapter` (minimal).  
**May:** connect, parse, validate venue fields, normalize, publish, reconnect, health.  
**Must not:** momentum, signals, entry/exit, orders, portfolio, write facts directly.

| Adapter | Notes |
|---------|-------|
| Polymarket normalize | Option B book/delta/tick |
| Polymarket fixture source | Deterministic publish |
| Gamma discovery | Public HTTP, User-Agent required |
| Polymarket market WS | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| Binance normalize | `@trade` prints |
| Binance trade WS | `wss://stream.binance.com:9443/ws/<symbol>@trade` |

## `tyrex_pm.market_data`

| Owner | Owns |
|-------|------|
| `InstrumentRegistry` | Resolved `BinaryMarket` |
| `MarketStateStore` | Books, init/recovery, tick size |
| `ReferenceDataStore` | Latest reference observation |
| `freshness` | Decision-time `FreshnessAssessment` |
| `executable` | Mid, spread, touch size, VWAP |
| `DecisionSnapshot` | Immutable evaluation context |

## `tyrex_pm.indicators` / `signals` / `strategies`

- Momentum: \(m_t = P_t / P_{t-L} - 1\) (no interpolation; out-of-order ignored).
- Directional: `UP|DOWN|FLAT|UNAVAILABLE`.
- Strategy observe decisions: `WOULD_ENTER_UP|WOULD_ENTER_DOWN|HOLD|SKIP` (no intents).

## `tyrex_pm.reporting`

`JsonlFactSink` — append-only UTF-8 JSONL, schema_version=1, Decimal/datetime/enum/ID serialization, flush on append, failures propagate.

## `tyrex_pm.runtime`

`ObserveConfig` (validated thresholds, fingerprint), `ObserveHost` (fixture composition), `run_live_observe` (same host + live adapters).

## Forbidden in R3

Risk authorization, execution planning, OMS, orders, fills, portfolio, Z-Gap, PTB/Chainlink, imports from `old/`, NautilusTrader.
