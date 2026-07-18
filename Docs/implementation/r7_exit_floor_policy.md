# R7 / R8 exit-floor policy (exact formulas)

**Source of truth:** `src/tyrex_pm/runtime/r7_lifecycle_policy.py`  
**Planner:** `src/tyrex_pm/execution/polymarket/lifecycle_exit_plan.py`

## Configured values

| Parameter | Value |
|-----------|-------|
| `BOOK_FRESHNESS_MAX_AGE_MS` | `2000` |
| `NORMAL_EXIT_PRICE_FLOOR` | `0.01` |
| `EMERGENCY_EXIT_PRICE_FLOOR` | `0.01` |
| `MAX_EXIT_SLIPPAGE_FROM_TOUCH` | `0.05` (NORMAL only) |
| `MAX_EXIT_BOOK_SPREAD` | `0.20` |
| `REQUIRE_FULL_BID_DEPTH` | `true` |

Normal and emergency are **not** operationally identical: emergency skips the touch-slippage cap only. Absolute floors are intentionally identical (venue min tick).

## Normal SELL limit

```text
worst = deepest bid level consumed walking size qty (bids descending)
limit = tick_floor(worst)
require:
  book_age_ms ≤ 2000
  (best_ask − best_bid) ≤ 0.20   # if ask present
  full bid depth for qty
  limit ≥ 0.01
  (best_bid − worst) ≤ 0.05
  not (limit ≥ entry_buy_limit AND best_bid < entry_buy_limit)
```

## Emergency SELL limit

Same as normal **except** `(best_bid − worst) ≤ 0.05` is **not** enforced.  
Activation: runtime escalates urgency within bounded exit attempts / deadline pressure.

- Worst possible accepted price: **0.01**
- Worst possible dollar loss for a $5 fee-inclusive entry: up to ~**$5** of committed collateral (exit at floor after adverse entry)

## Legacy REJECT reconciliation

Rejected legacy behavior: emergency unwind using touch/`0.01` **without** fresh-book gate, depth walk, fingerprint, or confirmed-qty ownership.

Active path with floors `0.01`/`0.01` is **not** equivalent — see tests in `tests/test_r8_flat_with_dust_terminal.py` (`test_exit_floor_formulas_normal_vs_emergency`).
