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

## PTB / sealed K (BTC 5m)

| Piece | Role |
|-------|------|
| Chainlink RTDS ticks | Boundary candidates; preferred rule `EXACT_AT_START` |
| `PtbCaptureEngine` | Select candidate → optional attest → **seal** immutable `ptb_k` / `sealed_k` |
| SSR `openPrice` adapter | Optional comparison evidence (`SsrDisplayedPtbAttestationProvider`) |
| Binance | Trading reference / basis / \(\hat{C}_t\) — **not** K |

N7 / live compose readiness:

- `require_ssr_price_match: false` → sealed Chainlink K is enough; **no SSR fetch**
- `require_ssr_price_match: true` → require SSR MATCH before aligned eval
- Z-Gap `model_anchor` / K must equal the immutable sealed Chainlink value

## Invariants

- Timestamp basis configurable (`EVENT_TIME` in current configs)
- Stale books must fail closed for execution decisions
- Binance never becomes an execution venue
- SSR display price is never substituted as independently derived K

## Tests / limits

- Fixture observe/shadow suites; adapter normalize tests; N3/N4 PTB suites; `tests/test_n7_ssr_optional.py`  
- Limits: recovery edge cases; no full historical replay platform; SSR HTML scrape is brittle when enabled
