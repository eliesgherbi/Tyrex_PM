# polymarket-client 0.2.0 → 0.5.0 Compatibility Report

**Date:** 2026-08-08  
**Spike environment:** isolated temp venv (`%TEMP%\tyrex_sdk_spike_050`)  
**Project pin decision:** **PASS → upgrade to `polymarket-client==0.5.0`**

## Method

1. Created isolated venv; installed `polymarket-client==0.5.0` only (did not mutate project until gate PASS).
2. Inspected public API surface used by Tyrex.
3. Inspected 0.5.0 wheel for `AsyncOrderMetadataCache` / order prep caching.
4. Confirmed `create_market_order` / `post_order` / allowance-recovery separation.
5. Updated project `pyproject.toml` pin and installed into project `.venv`.
6. Ran focused + full deterministic suites against 0.5.0.

## Results

| Check | Result |
|-------|--------|
| Version on PyPI / spike | 0.5.0 |
| `AsyncSecureClient` / `AsyncPublicClient` / `SecureClient` / `PublicClient` | OK |
| `MarketSpec` / `UserSpec` / `CryptoPricesSpec` | OK (signatures unchanged) |
| `from polymarket.streams import MarketSpec` | OK |
| `from polymarket.streams._specs import MarketSpec` (legacy Tyrex import) | Still works |
| `create_market_order` → `SignedOrder` | OK |
| `post_order` | OK; **no** allowance recovery in source |
| `place_market_order` | OK; uses `post_order_with_allowance_recovery` |
| `AcceptedOrder` / `RejectedOrder` | OK; `order_id` typed as `OrderId` NewType (str-compatible) |
| `wait_for_order_fill_settlement` | OK (`timeout_s=30`) |
| `get_balance_allowance` | OK |
| `RequestRejectedError.retry_after` | Present on `__init__` |
| Official order metadata cache | **Present in 0.5.0** (`AsyncOrderMetadataCache`, TTL 10 min) |
| Cache in 0.2.0 | **Absent** |
| Custom metadata cache | **Not implemented** (prefer official) |

## Warm / cold metadata expectation

- **0.2.0:** every `create_market_order` / `place_market_order` re-fetches tick / neg-risk / fee metadata.
- **0.5.0:** repeated preparation hits in-process cache; stale tick refresh on protected-price rejection.

Mutation-free warm policy: discarded `create_market_order` only — never `post_order`, never `place_market_order`, never obligation / mutation counter.

## Gate decision

**PASS.** Project dependency pinned to:

```toml
"polymarket-client==0.5.0"
```

No production Tyrex code imports `polymarket._internal.*`.

Deterministic regression on 0.5.0 (post-implementation): **1076 passed** (`full_suite_result.txt`).

## Venue / chain mutations during spike

**Zero.**
