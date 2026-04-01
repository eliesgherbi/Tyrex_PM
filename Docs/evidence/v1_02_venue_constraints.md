# v1.02 — Observed CLOB sizing constraints (operator notes)

**Purpose:** Capture venue behaviour that a **share-count-only** dry-run might miss, for evidence and future operators.

## Minimum BUY notional (USDC)

On a supervised smoke (LIMIT BUY), the venue returned a validation error equivalent to:

```text
invalid amount for a marketable BUY order ($0.5), min size: $1
```

**Interpretation:** besides `min_order_size` (shares) from the book, the route treated the order as needing at least **~$1** notional on the BUY.

**Example that failed share-min but failed notional:**

- `price` = 0.10  
- `size` raised to `min_order_size` = 5  
- **Notional** = 0.10 × 5 = **$0.50** → rejected.

**Retry that satisfies both** (given the same price):

- `size` = 10 → **$1.00** notional (`TYREX_SMOKE_SIZE=10` or script auto-bump).

## Tooling alignment

`examples/order_lifecycle_smoke.py` dry-run now reports **BUY** `estimated_buy_notional_usd`, an assumed `min_buy_notional_usd_assumption` (default 1), and may set `size_adjusted_for_min_buy_notional`. See `Docs/Runbooks/order_lifecycle_v1_02.md` for overrides (`TYREX_SMOKE_MIN_BUY_NOTIONAL_USD`).

**Disclaimer:** exact floors can vary by market / venue rules; always treat **live errors** as authoritative and append new observations here or in runbook § *Venue sizing*.
