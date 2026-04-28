# `risk/`

The **only** thing that can approve an intent. Fail-closed: anything not explicitly approved is denied. Every denial carries a stable reason code and structured evidence so `facts.jsonl` is enough to debug.

## Files

| File | Role |
|------|------|
| `engine.py` | `evaluate_intent(intent, ctx, app, run_id) -> RiskDecision` — orchestrates the gate sequence |
| `kill_switch.py` | Operator-flipped global block |
| `concurrency.py` | Cap on simultaneous unacked submits (`max_orders_in_flight`) |
| `health.py` | `check_aggressive_readiness` — wallet sync freshness, heartbeat, user-WS staleness |
| `pretrade.py` | `apply_notional_min_max` — clip-or-deny on USD notional band |
| `deployment.py` | Per-token + portfolio long-side cap including positions, resting BUYs, and in-flight reservations |
| `capital.py` | USDC balance + allowance gate for BUYs (with in-flight reservations subtracted) |
| `inventory.py` | SELL requires non-zero venue position (configurable) |
| `venue_min_size.py` | Final pre-submit guard against the venue's hard min-size floor (deny or bump) |
| `in_flight.py` | `derive_in_flight_buy_reservations` — synthesizes `OpenOrderView`s from provisional `OrderStore` rows so deployment + capital see venue-locked collateral immediately |
| `evidence_format.py` | `q_usd / s_usd / s_usd_map` — Decimal quantization for fact emission (6 decimals, ROUND_HALF_EVEN) with a high-precision `Context` and fallback for absurdly large allowances |

## Gate sequence (`engine.evaluate_intent`)

For BUY/SELL/REDUCE intents (cancels skip to the cancel-only branch):

1. **Kill switch** → `KILL_SWITCH`
2. **Concurrency** → `CONCURRENCY_LIMIT`
3. **Aggressive readiness** (wallet sync, heartbeat, user-WS, plus `BOOTSTRAP_NOT_COMPLETE` in live mode until the first venue truth rebuild succeeds) → `NOT_READY` / specifics
4. **Notional band** (`min_usd` ≤ N ≤ `max_usd`); `max_policy=cap` clips, `deny` rejects → `NOTIONAL_BELOW_MIN` / `NOTIONAL_ABOVE_MAX`
5. **In-flight reservation evidence** — totals are *always* attached to the decision (approve or deny) so audits show what was already locked
6. **Deployment caps** (per-token + portfolio, including in-flight) → `TOKEN_DEPLOYMENT_CAP` / `PORTFOLIO_DEPLOYMENT_CAP` / `DEPLOYMENT_MARK_UNKNOWN`
7. **Capital** (BUY only): wallet balance + allowance vs. notional + in-flight → `INSUFFICIENT_CAPITAL` / `INSUFFICIENT_ALLOWANCE` / `STALE_WALLET_SNAPSHOT`
8. **Inventory** (SELL only): non-zero venue position → `INSUFFICIENT_INVENTORY` / `NAKED_SELL`
9. **Venue min-size** (last gate): floor is `RiskContext.market_info[token].min_order_size` when populated by `MarketInfoCache` (live), else `cfg.default_min_size` (shadow / tests). If final `size < floor`, either deny (`BELOW_VENUE_MIN_SIZE`) or bump and **re-validate** deployment + capital (still denying with `BELOW_VENUE_MIN_SIZE` if the bump would breach a higher gate, evidence flagged `venue_min_size_bump_unsafe`). Evidence row carries `venue_min_size_source = "venue" \| "config_default"`.

## In-flight reservation lifecycle

A live BUY submit briefly locks venue collateral *before* the merged wallet view reflects it. The reservation accounting (synthetic `OpenOrderView`s) closes that gap:

- **Add**: at `register_submit` (provisional row created in `OrderStore`).
- **Visible to risk**: as `RiskContext.in_flight_buy_reservations`, derived per call by `derive_in_flight_buy_reservations`.
- **Release**: when the merged venue view actually carries the order (REST/WS), or when the order is terminal (cancel/fill/reject), or when `OrderStore` drops the row (provisional unknown-terminal timeout).
- **Evidence**: `risk_decision` payloads always carry `in_flight_reserved_usd_total`, `in_flight_reservation_count`, and `in_flight_reserved_usd_by_token`.

Detail: [LIVE_ARCHITECTURE §4](../../LIVE_ARCHITECTURE.md#4-in-flight-buy-reservations).

## Adding a policy

[developer_guide.md §4.1](../../developer_guide.md#41-add-a-new-risk-policy) walks through the recipe. Place new gates in the sequence carefully — anything that depends on size/price runs **after** notional clipping and (almost always) **before** `venue_min_size`.
