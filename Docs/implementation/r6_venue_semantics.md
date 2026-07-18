# R6 — Official Polymarket CLOB venue semantics

**Verified against official docs (2026).** CLOB **V2** is required.  
Historical `old/` inspected for lessons only — **not imported**.

## Sources

- https://docs.polymarket.com/v2-migration
- https://docs.polymarket.com/api-reference/authentication
- https://docs.polymarket.com/trading/orders/create
- https://docs.polymarket.com/trading/orders/cancel
- https://docs.polymarket.com/market-data/websocket/user-channel
- https://docs.polymarket.com/api-reference/rate-limits
- https://docs.polymarket.com/resources/error-codes
- https://github.com/Polymarket/py-clob-client-v2

## Verified behavior (summary)

| Topic | Finding |
|-------|---------|
| Auth | L1 EIP-712 → API key triple; L2 HMAC headers for trading/queries |
| Sign | V2 EIP-712 Exchange domain `version: "2"`; order still wallet-signed |
| Submit | `POST /order` → `orderID` (hash); statuses `live\|matched\|delayed` |
| Cancel | `DELETE /order` with `{orderID}` |
| User WS | `wss://ws-subscriptions-clob.polymarket.com/ws/user` + L2 auth JSON |
| Client order id | **Not documented** — correlate via returned `orderID`; optional local `salt` |
| Tick / min size | Per-token via market info; reject `INVALID_ORDER_MIN_*` |
| Fees | Not in signed order; applied at match; REST may expose `fee_rate_bps` |
| Heartbeat | `POST /heartbeats` (L2). Official docs: if heartbeats are used and then not maintained (~10s + 5s buffer), **all open orders cancel**. First call uses empty `heartbeat_id`; responses chain the next id. **R6C: do not call** — starting then stopping can itself change cancellation behavior. Readiness unresolved until separately approved. |
| Rate limits | Documented Cloudflare windows (see rate-limits doc) |
| Public vs auth | Public: Gamma, Data API, CLOB reads (`/time`, `/book`, prices, spreads). Authenticated L2: open orders, balances/allowances, post/cancel, heartbeat. Positions via public Data API `GET /positions?user=…` (not CLOB L2). |

## Endpoint taxonomy (R6C)

See `Docs/implementation/r6c_completion_report.md` and `endpoint_taxonomy.py`.

## Historical lessons (port selectively)

- Single-writer OMS; provisional → venue-confirmed lifecycle
- User-WS primary + REST repair; never assume flat from missing evidence
- Parse `orderID` / `order_id` / `id` defensively
- **Reject:** dual execution paths, logging credentials, treating absent client-order-id as venue field

## Unresolved assumptions

1. OpenAPI vs docs gaps on `signatureType: 3` (deposit wallets)
2. WS vs REST fee/tx field parity
3. Exact adoption heuristics when submit times out without `orderID`
4. Heartbeat id nesting across SDK versions

## R6 constraint

R6 implements adapter + reconciliation + **read-only** validation.  
**No real submit/cancel** until explicitly authorized R7.
