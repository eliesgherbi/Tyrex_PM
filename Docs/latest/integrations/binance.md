# Binance integration (reference only)

**Purpose:** document Binance as a **reference-data** source — never an execution venue.

## Role

| Statement | Status |
|-----------|--------|
| Reference trades/prices for BTC | implemented |
| Used by momentum / decision snapshots | implemented |
| Strategy must not call Binance APIs directly | enforced by architecture |
| Order execution on Binance | **unsupported** |

## Package

- `tyrex_pm.adapters.binance` — WS/fixture sources + normalize
- Config keys such as `binance_symbol` (e.g. `BTCUSDT` in observe/shadow JSON)

## Freshness and timestamps

- Reference freshness thresholds live in observe/shadow JSON (`freshness.reference_threshold_ms`)
- Timestamp basis typically `EVENT_TIME`
- Stale reference fails closed for decisions that require it

## Reconnection / limits

- Adapter reconnect behavior is best-effort within the live runner loop
- No Binance trading, balances, or OMS
- Not a second runtime — same host consumes normalized reference events

## Isolation

Strategies depend on framework context/signals, not on Binance clients.  
Execution planning and OMS are Polymarket-only in the accepted framework.
