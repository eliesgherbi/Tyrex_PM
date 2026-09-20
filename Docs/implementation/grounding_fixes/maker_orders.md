# G6 — Maker entry (sit on the bid)

**Status:** Implemented.

**Phase:** Grounding fixes  
**Depends on:** G4 (must cancel a live bid if we flatten or the window ends)  
**Depends on (soft):** G2 (signing a limit should stay fast)

## Goal

If a strategy says “I am a **maker**,” the host should **POST a GTC/GTD buy** at the strategy’s price and wait, instead of only FAK-taking the ask. Today that intent is planned and then **thrown away**.

## Venue facts

- Maker = order **rests** and is later matched. Taker = matches immediately ([order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)).
- Limit GTC/GTD: price on tick, size ≥ `min_order_size`. BUY size is **shares**, not the FAK `amount` USDC used on market buys.
- **Post-only:** rejected if it would cross the ask. Use this when the strategy wants “never take.” If the strategy accepts a chance of immediate match, omit post-only.
- Price improvement helps the **taker**. A maker buy at 0.70 that later trades at 0.70 gets 0.70, not a better ask.
- Unfilled maker bid **reserves collateral** (same `maxOrderSize` formula). A later FAK buy/sell will fail if we ignore that — G4 cancel first.
- 5-minute markets: prefer **GTD** before `event_end` so the bid does not sit on a dead book.

## Current code

| Piece | Today |
|-------|--------|
| `EnterIntent.liquidity_role` | `TAKER` (default) or `MAKER`. |
| Planner | MAKER → `LimitOrderSpec` GTC (`execution/planner.py`). |
| `TradingRuntime` | Records `MAKER_LIMIT_PLANNED` and **returns without POST**. |
| Gateway | `create_limit_order` already implemented. |
| ask70 / z_gap | Emit TAKER only. No live plugin exercises MAKER. |

`Docs/latest/extending.md` already says MAKER is planned, not posted. This task makes that sentence false.

## Required implementation

### 1. POST the planned limit

When `liquidity_role is MAKER`:

- Size shares from `target_notional / limit_price` (fee reserve as today), round to venue size precision, reject if `< min_order_size`.
- Limit price = strategy `max_price` (or a future explicit `limit_price` if you add it — do not silently use the ask).
- Tick-adapt **down** for BUY (never weaken the user’s worst price upward).
- `create_limit_order` → final gate (book still same market; optional: reject if post-only would cross) → authorize → `post_order` with **GTC or GTD**.
- Accept `status=live` as success-so-far (not a fill). Session stays `POSITION_PENDING` / working until fill, cancel, or expire.

Do **not** use `place_market_order` for this path.

### 2. Lifetime

Config or intent: `gtd_event_end` default for `btc_updown_5m`. Mandatory flatten / window end / kill: **G4 cancel** the bid, then G1 flatten if any fill arrived.

### 3. Fill / no-fill

- User stream + REST: `CONFIRMED` buy → same protection arm as taker fills.
- Expire / cancel unfilled → `COMPLETED_NO_FILL` (already a legal terminal).
- Partial fill → protection on confirmed qty; cancel remainder or leave per spec (explicit YAML, default **cancel remainder** on flatten).

### 4. Who emits MAKER

Do **not** change ask70 (it exists to enter fast). Add either:

- a tiny `ask70` parameter `liquidity_role: maker` for a **separate** YAML, or
- wait for G8 / q-edge to emit MAKER for real.

The host work is independent of which plugin emits the intent.

### 5. Tests

- MAKER intent → one limit POST, no FAK buy.
- Crossed book + post-only → no POST, `COMPLETED_NO_DISPATCH`.
- Open maker bid + flatten → cancel then no double spend.
- TAKER path unchanged.

## Files likely to change

- `src/tyrex_pm/runtime/trading_runtime.py` — remove “plan only” return
- `src/tyrex_pm/execution/lifecycle.py` — wait-for-maker-fill vs one-shot FAK
- `src/tyrex_pm/execution/planner.py` — GTD expiration, min size
- `src/tyrex_pm/execution/final_gate.py` — optional post-only / would-cross
- `Docs/latest/execution_lifecycle.md`, `extending.md`
- Optional `config/runs/ask70_maker_tiny_live.yaml`

## Out of scope

- Market-making two-sided quotes
- Builder rebate / feeRateBps customization beyond what the SDK already signs
- Changing $5 cap

## Acceptance

1. Docs and code agree: MAKER POSTs.  
2. Fixture live status + later fill arms protection.  
3. Unfilled GTD expire → `COMPLETED_NO_FILL`, 0 leftover flatten.  
4. Operator optional tiny-live with a far bid that we then cancel (proves POST+G4 without needing a fill).
