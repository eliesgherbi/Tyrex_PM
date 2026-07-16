# Polymarket + RTDS data coverage audit (M2B.3-A)

**Purpose:** Document what provider data exists, what we record, and known limitations before M2B.4 analysis.

**Last updated:** M2B.3-A data completeness extension

## Summary

| Layer | Status |
|-------|--------|
| PM market-channel (book + extended events) | Parser + recorder support; `custom_feature_enabled: true` in WS subscribe |
| RTDS Chainlink BTC/USD | Record-only via `reference_prices.enabled` |
| Price-to-beat | **Derived** from first Chainlink tick at/after `event_start_ts` (not direct Gamma field live) |
| Gamma `eventMetadata.priceToBeat` | **Deferred** — post-settlement backfill only (not implemented in M2B.3-A) |

## Coverage table

| Provider data item | Provider source | Available live? | Requires `custom_feature_enabled`? | Currently recorded? | M2B.3-A status | Notes |
|---|---|---|---|---|---|---|
| `book` | CLOB WS market channel | yes | no | yes (M2B.0-B) | complete | Maps to `book_snapshot` |
| `price_change` | CLOB WS market channel | yes | no | yes (M2B.0-B) | complete | Maps to `book_delta` |
| `last_trade_price` | CLOB WS market channel | yes | no | yes (M2B.3-A) | complete | Trade prints with `raw` preserved |
| `tick_size_change` | CLOB WS market channel | yes | no | yes (M2B.3-A) | complete | Near resolution tick changes |
| `best_bid_ask` | CLOB WS market channel | yes | **yes** | parser yes / live TBD | parser-supported | Recorder sets `custom_feature_enabled: true`; BTC 5m emission not guaranteed |
| `new_market` | CLOB WS market channel | yes | **yes** | parser yes / live TBD | parser-supported | Does not replace Gamma discovery |
| `market_resolved` | CLOB WS market channel | yes | **yes** | parser yes / live TBD | parser-supported | Winning outcome when emitted |
| RTDS Chainlink BTC/USD | `wss://ws-live-data.polymarket.com` topic `crypto_prices_chainlink` | yes | no | yes when enabled | complete | Filter `{"symbol":"btc/usd"}` |
| RTDS Binance BTC | topic `crypto_prices` filter `btcusdt` | yes | no | optional (not default) | deferred | Independent from M2B.2 Binance direct WS |
| Gamma `eventMetadata.priceToBeat` | Gamma REST post-settlement | post-close only | n/a | no | deferred | Explicit backfill milestone |
| Winner / `winning_outcome` | `market_resolved` WS event | when emitted | yes | yes when emitted | parser-supported | Also in `market_resolutions.parquet` |
| Price-to-beat at T0 | Derived from RTDS Chainlink | derived | n/a | yes when RTDS enabled | complete | `PRICE_TO_BEAT_OBSERVED` event |
| Final reference at T_end | Derived from RTDS Chainlink | derived | n/a | yes when RTDS enabled | complete | Labeled derived; not claimed as official settlement |

## RTDS subscription (Chainlink BTC/USD)

Provider contract (confirmed from [Polymarket RTDS docs](https://docs.polymarket.com/market-data/websocket/rtds)):

| Field | Value |
|-------|-------|
| Endpoint | `wss://ws-live-data.polymarket.com` |
| Topic | `crypto_prices_chainlink` |
| Subscription key | **`type`** (not `msg_type`) |
| Type value | `"*"` (all message types) or `"update"` (live only) |
| Filters | JSON **string**, not a Python dict |
| Symbol | `btc/usd` (slash-separated) |
| Payload fields | `symbol`, `timestamp` (ms), `value` |
| Keepalive | Send `PING` every 5 seconds |

Exact subscription payload used by the recorder:

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices_chainlink",
      "type": "*",
      "filters": "{\"symbol\":\"btc/usd\"}"
    }
  ]
}
```

Built via `build_reference_price_subscription("chainlink", "btc/usd")` in `normalize.py`.

**M2B.3-A bug (fixed):** An earlier implementation sent `msg_type` instead of `type`, so RTDS accepted the connection but delivered zero Chainlink ticks. Normalizer correctly produced `missing` price-to-beat when no reference ticks were recorded.

Historical snapshot (defensive support): when a filter is used, RTDS may send `type: "subscribe"` with `payload.data[]` of `{timestamp, value}` entries before live `type: "update"` ticks. Each snapshot row is normalized to its own `REFERENCE_PRICE_TICK` with deterministic event IDs.

## Recording layout

```text
var/recordings/<day>/
  btc_5m_*/
    last_trade_price, tick_size_change, best_bid_ask, market_resolved, price_to_beat_observed
  external/btc_binance/           # Binance direct (M2B.2)
  external/polymarket_rtds_chainlink/  # RTDS Chainlink (M2B.3-A)
```

## Import boundary

Record-only ingestion. No strategy, risk, execution, or live trading imports from `research/` or RTDS modules.
