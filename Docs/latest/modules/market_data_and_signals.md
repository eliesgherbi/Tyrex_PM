# Market data and signals

**Purpose:** ingestion, authoritative books/reference, freshness, indicators, signals.

## Packages

- `tyrex_pm.adapters.polymarket` / `tyrex_pm.adapters.binance`
- `tyrex_pm.domain.polymarket`
- `tyrex_pm.market_data`
- `tyrex_pm.indicators`
- `tyrex_pm.signals`

## Responsibilities

| Piece | Role | Authoritative? |
|-------|------|----------------|
| Polymarket adapter | Discover windows, WS books, normalize | Emits only |
| Binance adapter | BTC reference trades/prices | Emits only |
| Instrument registry / binary mapping | YES/NO tokens per market | Yes for instruments |
| `BookStore` | Order book state | Yes for books |
| Reference store | External price series | Yes for reference |
| Freshness | Age / basis checks | Assessment only |
| Indicators | Transforms | Derived |
| Signals | Directional packages | Derived |

## Inputs / outputs

- **In:** venue WS/REST/fixtures  
- **Out:** book/reference events → `DecisionSnapshot` → indicator values → signal structs

## Allowed / prohibited

- Allowed: normalize, store, assess freshness, compute features  
- Prohibited: submit orders, authorize risk, own portfolio

## Invariants

- Timestamp basis configurable (`EVENT_TIME` in current configs)
- Stale books must fail closed for execution decisions
- Binance never becomes an execution venue

## Tests / limits

- Fixture observe/shadow suites; adapter normalize tests  
- Limits: recovery edge cases; no full historical replay platform
