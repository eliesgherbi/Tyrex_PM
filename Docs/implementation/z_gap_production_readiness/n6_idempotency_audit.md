# N6 idempotency capability audit

**Result:** `VENUE_IDEMPOTENCY_NOT_AVAILABLE_OR_INSUFFICIENT`

## Sources

1. Polymarket API Reference — `POST /order` (`SendOrder`): signed order body +
   owner/orderType; **no** caller idempotency key field.
2. `py-clob-client` `OrderArgs`: `token_id`, `price`, `size`, `side`, optional
   `nonce`, `expiration`, `taker` — `nonce` is for on-chain cancellation semantics,
   not HTTP idempotent retry.
3. Order identity = EIP-712 hash (includes `salt`). Re-posting an identical signed
   order may yield `INVALID_ORDER_DUPLICATED`.
4. Third-party integration notes (NautilusTrader): venue does not expose an
   idempotency key for batch retry paths.

## N6 policy

Framework lineage is authoritative:

```text
intent_id → plan_id → request_fingerprint → submission_attempt_id → venue_order_id?
```

Ack timeout → `AMBIGUOUS` → user-stream/REST reconciliation.  
Never submit a fresh entry solely because the original response was lost.
