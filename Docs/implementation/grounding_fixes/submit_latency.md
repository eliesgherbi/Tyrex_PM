# G2 — Slow stamp (submit latency)

**Phase:** Grounding fixes  
**Status:** implemented  
**Depends on:** none (can overlap G1; do not weaken the 5 s candidate fuse)  
**Blocks:** reliable ask70/z_gap entry when books or Wi-Fi are noisy

## Goal

From “buy now” to POST should stay in **hundreds of milliseconds** when metadata is already warm. If signing cannot finish in time, **fail fast** — do not wake up 38 seconds later and then notice the price moved.

## Venue facts

- Every order is a **signed EIP-712 limit**. “Market” FAK is a marketable limit. Sign (`create_market_order`) is not the POST ([order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)).
- Official SDK resolves **tick / neg-risk / fees** on create. Cold create hits CLOB HTTP; warm cache should make create local math + sign.
- Selected crypto up/down markets can apply a **250 ms taker delay** (`itode: true` on `GET /clob-markets/{condition_id}`). That delay is **after POST**, during match — it does **not** explain 38 s of **pre-POST** sign.
- Tick can change (`tick_size_change` on the market WS). A late create that re-fetches metadata on a sick book can hang.
- Docs: do not cancel during the pending delay window. Irrelevant to sign-before-post, relevant later for G4.

## Current code

Hot path (already documented in `Docs/latest/execution_lifecycle.md`):

```text
plan → create/sign → recapture BookView → final gate → durable authorize → post_order
```

| Piece | Today |
|-------|--------|
| `warm_order_metadata` | Discarded `create_market_order` on bind. Entry blocked until `ORDER_METADATA_READY`. Live warms were **~0.5 s**. |
| `prepare_order` | `create_market_order` on the **same** `AsyncSecureClient` as account reads. **Does not** take `_PrioritySingleFlight`. |
| `hold_ready_snapshot` | Pauses **new** account refreshes during submit. An **in-flight** refresh (13–14 s live) still owns the HTTP/2 client. |
| Account reads | Sequential COLLATERAL + each TOKEN + OPEN_ORDERS. Timeouts are **SDK-owned**; Tyrex must not `asyncio.timeout` them (known HTTP/2 semaphore bug). |
| Book recovery | REST snapshot on DESYNC; can fail (`rest_snapshot_rejected`) and retry off-loop, still competing for the PC’s network. |
| Final gate | Price (`ask > worst_price`) **then** candidate age (`maximum_candidate_age_ms`, default **5000**). |
| One-shot | `COMPLETED_NO_DISPATCH` stops the run. No second entry. |

## Live evidence

| Run | `request_to_prepared_ms` | Gate | POST |
|-----|--------------------------|------|------|
| Fill + trail | 1500 | — | yes |
| Tight pad | 609 | `entry_price_moved` | no |
| Slow + pad 0.08 | **37641** | `candidate_stale_after_signing` | no |
| Fill + TP | **515** | — | yes |
| Slow again | **37828** | `entry_price_moved` (age also ~39 s; price check wins) | no |

Same machine. Slow runs overlapped a **13–14 s** `ACCOUNT_STATE_UPDATED` and heavy DESYNC/REST failures on the selected token. Warm 2 minutes earlier was 0.5 s — cache did not save the live create.

## Required implementation

Do **not** raise `maximum_candidate_age_ms` to 40 s. The fuse is correct.

### 1. Measure the wait (first, so the next live run is diagnosable)

Split `request_to_prepared_ms` in the execution timeline:

- `prepare_queue_wait_ms` — time until the SDK client is free
- `tick_resolve_ms`
- `create_market_order_ms`

Record whether an account refresh was in flight at submit start (`refresh_elapsed_ms`, operations).

### 2. Do not share the stamp line with a slow wallet check

Pick one, in this order of preference:

1. **Wait for the in-flight refresh to finish, with a short budget** (e.g. 500 ms). If it is not done, **do not start sign**; pre-dispatch fail `ACCOUNT_REFRESH_IN_FLIGHT` (no POST). Safer than 38 s sign.
2. Or **serialize prepare through the same `_PrioritySingleFlight` as ACTIVE account ops**, so create never multiplexes with COLLATERAL/TOKEN/OPEN_ORDERS on one HTTP/2 client — and still apply (1) so you do not wait 14 s.

Forbidden: cancelling an in-flight SDK HTTP/2 call with an outer timeout.

### 3. Fail fast on create

If `create_market_order` exceeds an explicit `prepare_budget_ms` (new lifecycle/gateway field, e.g. 1500–2000, **less than** `maximum_candidate_age_ms`): abandon the signed attempt (discard), `PREPARATION` pre-dispatch fail `PREPARE_BUDGET_EXCEEDED`. Do not let the SDK sit 38 s if the official client exposes a per-call timeout; if it does not, the in-flight-refresh gate in (2) is the main lever.

### 4. Keep warm honest

If live create still does CLOB HTTP after a successful warm of **that token**, record `ORDER_METADATA_CACHE_MISS` with the SDK error/elapsed. Do not build a Tyrex-side metadata cache (already forbidden). If official cache is proven stale/missing, document it; only then consider a bounded retry of warm on the **prepared-next** bind.

### 5. Book noise

DESYNC/REST recovery must not start extra public REST on the **same** process at the moment of submit if that path can stall the authenticated client. Coalesce/postpone resync until after POST or after `prepare_budget` (public REST is already off-loop; confirm it cannot join the SDK pool).

### 6. Tests

- Submit while a fake refresh holds the client 200 ms → prepare waits ≤ budget, timeline shows queue wait.
- Submit while refresh already 5 s in → no 38 s create; pre-dispatch fail or wait-then-fail.
- Warm token + create returns in fixture < 100 ms.
- Candidate age still 5 s; a 6 s delayed create still stale if it somehow returns.

## Files likely to change

- `src/tyrex_pm/execution/polymarket/gateway.py` — timings, optional serialize, prepare budget
- `src/tyrex_pm/execution/account_state.py` — expose “refresh in flight” / hold interaction
- `src/tyrex_pm/execution/coordinator.py` — pass timings into `OrderPrepared` / pre-dispatch
- `src/tyrex_pm/runtime/trading_runtime.py` — timeline fields
- `Docs/latest/execution_lifecycle.md`
- `tests/contract/polymarket/test_async_gateway.py`, `tests/unit/execution/`

## Out of scope

- Raising candidate TTL
- Raising `max_price_pad` as a latency fix
- Custom CLOB metadata cache
- Changing taker 250 ms delay (venue)

## Acceptance

1. On a quiet fixture, `request_to_prepared_ms` stays well under 1 s after warm.  
2. An overlapping long account refresh **cannot** produce a 30 s+ successful prepare; it fails closed before POST.  
3. Next noisy live: if sign is slow, report shows **which** wait (queue vs create vs refresh), not only `entry_price_moved`.  
4. 5 s candidate fuse unchanged.
