# G4 — Cancel path (needed before “one cancels the other”)

**Status:** Implemented.

**Phase:** Grounding fixes  
**Depends on:** G1 recommended (so flatten after cancel is sized correctly)  
**Blocks:** G5 resting TP, G6 maker POST safety

## Goal

The host must be able to **take down a sitting order by id** (and, if needed, all of our orders on one token), wait until the book/account agrees it is gone or filled, and only then sell the rest. Polymarket will not do “OCO” for us.

## Venue facts

Official cancel surface ([cancel single](https://docs.polymarket.com/api-reference/trade/cancel-single-order), [cancel many](https://docs.polymarket.com/api-reference/trade/cancel-multiple-orders), developer cancel-orders):

| Call | Meaning |
|------|---------|
| `DELETE /order` `{orderID}` | One order. Works in cancel-only mode. |
| `DELETE /orders` `[ids…]` | Up to 1000 ids. |
| `DELETE /cancel-all` | All of **this user**. Too wide for a $5 one-shot — **do not** use as the default flatten. |
| `DELETE /cancel-market-orders` | Optional `market` + `asset_id`. Prefer this for “kill our resting TP on this token.” |

Response: `canceled: []` and `not_canceled: {id: reason}` (`Order not found`, `Order already matched`, …). **Treat `already matched` as inventory, not as success-empty.**

Other venue rules:

- You **cannot cancel** while a marketable order is in the **taker delay** window (~250 ms on `itode` up/down). Retry after the window; do not assume cancel failed forever.
- Only the **unfilled remainder** of a partial fill can be cancelled.
- Size reserved by live orders reduces `maxOrderSize`. A FAK flatten while a GTC sell is still live will get `not enough balance` — the exact class of error we already saw on dust retries.

**There is no native OCO / bracket / stop order.** Soft OCO = cancel the other order in our software.

User WS: `order` messages `PLACEMENT` / `UPDATE` / `CANCELLATION`. Use those as evidence; REST `list_open_orders` remains authority for “still live?”

## Current code

| Piece | Today |
|-------|--------|
| `CancelIntent` | Exists in `core/intents.py` (`order_id`, `semantic_key`). |
| `IntentLike` | **Does not include** `CancelIntent` (`strategies/decisions.py`). |
| `TradingRuntime._consume_intent` | `Enter` / `Exit` / `Flatten` / `HoldToResolution` only; else `TypeError`. |
| `PolymarketAsyncGateway` | `create_limit_order`, `post_order`, balances, open orders, user stream. **No `cancel_*`.** |
| Protection | Emits Exit/Flatten only. No cancel-before-flatten. |
| `open_venue_order_ids` | Reconciler already tracks them. |

Inspect `polymarket-client==0.5.0` `AsyncSecureClient` for the official names (`cancel`, `cancel_orders`, `cancel_market_orders`, `cancel_all`). Wrap those only.

## Required implementation

### 1. Gateway

Add async methods that call the official client and normalize:

```text
canceled: tuple[str, ...]
not_canceled: mapping[id → reason]
```

Never interpret HTTP 200 + `not_canceled` as full success. Classify `already matched` vs `not found` vs `in delay`.

### 2. Consume `CancelIntent`

- Add `CancelIntent` to `IntentLike`.
- Coordinator/lifecycle: durable `CancelRequested` → gateway → `CancelObserved` (canceled / not_canceled / matched-instead).
- Reconcile open orders + balance before any following FAK sell.

### 3. Soft OCO helper (framework, not strategy)

When flatten/SL/mandatory-exit fires and `open_venue_order_ids` is non-empty on the selected token:

1. Cancel those ids (or `cancel_market_orders` for that `asset_id`).
2. Wait until open ids for that token are empty **or** a fill is confirmed.
3. Then G1-sized FAK for remaining sellable.

Do not `cancel_all` in tiny-live (would kill unrelated user orders).

### 4. Delay-window policy

If cancel returns pending/delay: wait ≥ 250 ms + jitter, reconcile, retry once. If still live after budget → MANUAL (do not POST a second full sell on top).

### 5. Tests

- Cancel success → open ids empty; flatten sizes remaining only.
- `not_canceled: already matched` → treat as fill evidence, no second full sell.
- `CancelIntent` from a fake strategy is consumed (no `TypeError`).
- `cancel_all` is not called from the tiny-live path.

## Files likely to change

- `src/tyrex_pm/execution/polymarket/gateway.py`
- `src/tyrex_pm/execution/evidence.py` — cancel events
- `src/tyrex_pm/execution/reducer.py` / `lifecycle.py` / `coordinator.py`
- `src/tyrex_pm/runtime/trading_runtime.py`
- `src/tyrex_pm/strategies/decisions.py`
- `Docs/latest/execution_lifecycle.md`
- Contract tests next to `tests/contract/polymarket/test_async_gateway.py`

## Out of scope

- Placing the resting TP itself (G5)
- Maker entry POST (G6)
- Native venue OCO (does not exist)

## Acceptance

1. Fixture: live GTC id in state → CancelIntent → gateway cancel → open ids empty.  
2. Fixture: cancel says matched → remaining size uses G1 rules.  
3. Docs state: Polymarket has no OCO; Tyrex cancels then sells.  
4. No `cancel_all` on the $5 path.
