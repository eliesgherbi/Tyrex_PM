# WS fixture spike results (Phase 2 M1) — captured 2026-06-29

## Connection verified

Live connect to `wss://ws-subscriptions-clob.polymarket.com/ws/market` succeeded from this environment. Initial ack `[]` and broadcast `new_market` events received. Book/price_change for inactive paired-binary shadow tokens did not arrive within capture window; fixtures use **official Polymarket wire schema** with paired-binary YES token id substituted.

## Answers

| # | Item | Result |
|---|------|--------|
| 1 | WS URL | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| 2 | Subscribe payload | `{"assets_ids": ["<token_id>", ...], "type": "market", "custom_feature_enabled": true}` |
| 3 | Book snapshot format | `event_type: "book"`, `asset_id`, `market`, `bids[]`, `asks[]`, `timestamp`, `hash` |
| 4 | Price change format | `event_type: "price_change"`, `price_changes[]` with per-row `asset_id`, `price`, `size`, `side`, `hash`, `best_bid`, `best_ask`; top-level `timestamp` |
| 5 | Asset id field | `asset_id` on book; `price_changes[].asset_id` on deltas |
| 6 | Bid/ask fields | `{ "price": "<decimal string>", "size": "<decimal string>" }`; size `"0"` removes level |
| 7 | Exchange timestamp | `timestamp` (milliseconds string/int) on book and price_change |
| 8 | Sequence/hash | `hash` on book and per price_change row; no numeric sequence field observed |
| 9 | Reconnect behavior | Server accepts reconnect; client must re-send subscribe; send `PING` every ~10s |
| 10 | Auth required? | **No** for market channel (public) |

## Parser compatibility note

Existing parser assumed legacy `changes[]` + top-level `asset_id`. **Polymarket live wire uses `price_changes[]`.** Fixed in `ingestion/market_stream.py` (supports both).

## Fixtures

- `tests/fixtures/ws/market_book.json`
- `tests/fixtures/ws/market_price_change.json`

## Enable shadow ingest

```text
TYREX_MARKET_WS_SHADOW=1
# or runtime.market_data.websocket.shadow_enabled: true
```

REST remains authoritative for all paired-binary decisions in M1.
