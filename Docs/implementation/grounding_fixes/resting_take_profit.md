# G5 — Sitting take-profit (resting GTC)

**Status:** Implemented.

**Phase:** Grounding fixes  
**Depends on:** G4 (cancel), G1 (size/dust)  
**Do not start** until G4 is PASS

## Goal

Right after a confirmed fill, **leave a sell on the book** at the take-profit price (e.g. 0.85) instead of only watching and then rushing a FAK. If stop-loss or flatten fires first, **cancel that sell** (G4) then FAK what’s left.

## Venue facts

- Resting orders are **GTC** or **GTD** limit sells ([place orders](https://docs.polymarket.com/trading/place-orders), [create](https://docs.polymarket.com/trading/orders/create.md)).
- Price must sit on `tick_size`. Size must be ≥ `min_order_size` and ≤ available tokens (minus other open sells).
- GTD auto-expires at a unix timestamp. For BTC 5m, **GTD before window end** is safer than a naked GTC that can outlive the market.
- Status `live` = resting; `matched` = took liquidity or was taken. User WS `UPDATE` for partials.
- **Post-only:** rejected if it would cross. A TP sell at 0.85 when bid is already 0.86 **crosses** — it would take, not rest. Then either reject (post-only) or it becomes a taker. For “leave a note at 0.85,” post-only is correct **only if** bid < 0.85. If bid is already ≥ TP, use today’s **reactive FAK** (already live-proven).
- No venue bracket. Soft OCO is G4.

## Current code

- `ProtectionSpec.take_profit.style` allowed value: **`reactive_fak` only**. Docs say resting GTC is not implemented.
- Overlay fires when `mark >= take_profit`, then FAK.
- `create_limit_order` exists on the gateway for GTC; **not used for protection**.
- ask70 YAML: `absolute: "0.85"`.

## Required implementation

### 1. Spec (explicit, no hidden default)

Add style `resting_gtc` (or `resting_gtd`) **in addition to** `reactive_fak`. Unknown styles still fail load.

Optional: `expiration: window_end` | unix. Default for `btc_updown_5m`: **GTD at event_end − small margin**, not immortal GTC.

### 2. Arm behavior

After confirmed fill and `PROTECTION_ARMED`:

- If `resting_*` and best bid **<** TP: plan `LimitOrderSpec` SELL at tick-adapted TP, size = G1-legal **full** sellable (or `size_mode: full` only, as today).
- POST through coordinator (same authorize-then-post). Record venue id on the armed protection object.
- If bid **already ≥** TP: do **not** rest; emit reactive FAK immediately (same as today).

### 3. While resting

- Do **not** also FAK-TP on every book tick (double sell).
- SL / trail / mandatory flatten: G4 cancel the TP id → wait → FAK remaining.
- Partial TP fill: reduce remaining; if leftover ≥ min size, leave remainder or re-arm; if leftover is dust, G1 dust terminal.
- User-stream fill of the TP: reconcile to flat / dust; do not FAK the same shares.

### 4. Watchdog

If the resting order disappears without a matching fill (venue cancel, expire): re-arm or fall back to `reactive_fak` once; persist the reason. Do not silently go naked.

### 5. ask70 validation

New or extra YAML, e.g. `take_profit.style: resting_gtd`, TP 0.85, SL 0.55, trailing off. Tiny-live **after** G1+G4. Success: `live` sell appears; either it fills (flat/dust) or SL cancels it then FAK.

### 6. Tests

- Bid 0.70, TP 0.85 → one GTC/GTD POST, no FAK.
- Bid 0.86, TP 0.85 → no rest, FAK once.
- SL while TP live → cancel then FAK remaining only.
- Partial TP 3 of 7 → remaining 4, no second 7-share FAK.

## Files likely to change

- `src/tyrex_pm/protection/spec.py`, `state.py`, `triggers.py`
- `src/tyrex_pm/runtime/trading_runtime.py` — arm POST, skip reactive TP while resting
- `src/tyrex_pm/execution/planner.py` — limit sell from protection
- `Docs/latest/protection.md`
- `config/runs/` — optional resting YAML
- `tests/unit/protection/`

## Out of scope

- True exchange OCO
- Resting **stop** (venue cannot hold a stop; SL stays reactive FAK)
- Maker **entry** (G6)

## Acceptance

1. Spec rejects unknown style; `reactive_fak` still works.  
2. Unit matrix above green.  
3. One operator tiny-live: resting order id in report; no double inventory sell; G1 terminal.
