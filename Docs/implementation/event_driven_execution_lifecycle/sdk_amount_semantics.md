# Polymarket SDK amount semantics (Phase 3 / EVD-02)

**Source:** `polymarket-client==0.2.0` (`SecureClient.place_market_order`,
`AcceptedOrder`, `RawOrderResponse`) and Polymarket CLOB order docs.

## Market order request parameters

| Side | SDK parameter | Meaning |
|---|---|---|
| BUY | `amount` | USDC notional to spend |
| BUY | `max_price` | Worst acceptable share price (marketable limit) |
| SELL | `shares` | Share quantity to sell |
| SELL | `min_price` | Worst acceptable proceeds price |
| Either | `order_type` | `FAK` (default) or `FOK` |

Tyrex `SubmitOrderRequest.order_type` maps to the SDK when set to `FAK`/`FOK`
via `SdkMutationTransport.submit_order` → `place_market_order(..., order_type=)`.
GTC/GTD use `place_limit_order`.

## Response `makingAmount` / `takingAmount`

Wire fields (`makingAmount`/`takingAmount`) and SDK fields
(`making_amount`/`taking_amount`) are the filled maker/taker amounts for the
posted order:

| Side | `making_amount` | `taking_amount` | Share qty for MATCHED HWM |
|---|---|---|---|
| BUY | USDC spent | Shares received | `taking_amount` |
| SELL | Shares sold | USDC received | `making_amount` |

Resting accept (`status=live`) and delayed accept (`status=delayed`) typically
return both amounts as `0` with empty `tradeIDs`.

## Status values on accepted posts

SDK `OrderPostStatus`: `live` | `matched` | `delayed`.

- `matched` with `trade_ids` → immediate match evidence (MATCHED, not CONFIRMED)
- `live` → resting; no match HWM increase
- `delayed` → accepted but settlement/ack uncertain; preserve venue id

## FAK decision (D-04)

`SecureClient.place_market_order` types `order_type: Literal['FAK', 'FOK'] = 'FAK'`.
Rejected FAK empty-book messages map to SDK code `fak_not_filled`.
**No GTC substitution** is required for the one-shot admission experiment.

## Inventory axes (do not conflate)

```text
matched_qty     → OrderStore HWM + MatchedExposureProjection (supervision)
confirmed_qty   → Portfolio / settlement CONFIRMED fills
sellable_qty    → min(confirmed, conditional balance)
```
