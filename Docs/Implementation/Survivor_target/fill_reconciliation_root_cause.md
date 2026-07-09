# Fill reconciliation root cause — run `paired_binary_phase1_tiny_1782971569`

## Summary

The bot reported **+0.10** realized PnL while Polymarket UI showed approximately **-0.20** (~0.30 gap). The gap is not rounding: the bot treated **OMS submit-response `makingAmount` / `takingAmount`** as authoritative final cashflow, but those values align with **limit/submit-time amounts** (0.48, 0.53, 0.24, 0.79), not venue VWAP fills (0.497, 0.547, 0.227, 0.778).

User-WS **CONFIRMED** trade events exist in the wallet store but were **not wired** into paired-binary leg `entry_cash` / `exit_cash` or final PnL emission.

## Current source of each cashflow field

| Field | Current writer | Source type | Authoritative? |
|-------|----------------|-------------|----------------|
| `yes_entry_price` | `resolve_leg_entry_price` / state | OMS match avg or limit fallback | No (limit fallback is estimate) |
| `no_entry_price` | same | OMS match avg or limit fallback | No |
| `yes_entry_cash` | `PairedBinaryStrategy.notify_buy_submitted` | `extract_matched_cashflow` → `oms_match_evidence` | **Tentative** (OMS ack) |
| `no_entry_cash` | same | `oms_match_evidence` | **Tentative** |
| `yes_exit_cash` | `notify_exit_matched` | `oms_match_evidence` | **Tentative** |
| `no_exit_cash` | `notify_exit_matched` | `oms_match_evidence` | **Tentative** |
| `yes_exit_price` | derived `exit_cash / exit_qty` | OMS ack | **Tentative** |
| `no_exit_price` | derived | OMS ack | **Tentative** |
| `paired_binary_realized_pnl` | `emit_realized_pnl` → `realized_pnl_from_cashflows` | Sum of leg OMS ack cashflows | **Incorrectly treated as final** |

### Evidence from run `1782971569`

Four `oms_submit` facts with `match_evidence`:

- YES buy: making 2.4, taking 5 → 0.48
- NO buy: making 2.65, taking 5 → 0.53
- NO sell: taking 1.2, making 5 → 0.24
- YES sell: taking 3.95, making 5 → 0.79

All leg `*_cash_source` fields: `oms_match_evidence`.  
`paired_binary_realized_pnl` echoed the same → **+0.10**.

Polymarket UI (manual reference):

- UP buy 5 @ 0.497 → -2.49
- DOWN buy 5 @ 0.547 → -2.74
- DOWN sell 5 @ 0.227 → +1.14
- UP sell 5 @ 0.778 → +3.89  
→ PnL ≈ **-0.195**

## Why it diverges from Polymarket UI

1. **OMS submit response** reflects the order's matched amounts at submit time, often equal to limit price × size, not post-settlement VWAP.
2. **FAK exit retries** (3 `oms_reject` before successful fills) mean allocation may update from user-WS without refreshing strategy `exit_cash`.
3. **User-WS trades** (`TradeFillRecord` in `wallet.trade_fill_records`) carry per-fill `price` and `size` but were never aggregated into PnL.
4. **No REST trade/fill reconciliation** loop confirms average execution price after submit.

## Authoritative source hierarchy (implemented)

1. User-WS **CONFIRMED** trades (venue-reconciled VWAP from fill events)
2. User-WS MATCHED/MINED (tentative evidence only)
3. OMS submit `match_evidence` (tentative — not final realized PnL)
4. Planned/limit price (estimate only — never final)

## What is still unavailable from the venue adapter

- Dedicated REST **fills** / **trades-by-order** endpoint wired into paired-binary state refresh
- `trade_id` on `TradeFillRecord` (user-WS message may carry it; not persisted today)
- Automatic post-DONE REST reconciliation pass against Polymarket order history
- Settlement-fee adjustment (UI may include fees not in CLOB amounts)

## Patch behavior

- `paired_binary_realized_pnl` → **final only** when all four legs are venue-reconciled (CONFIRMED user-WS)
- `paired_binary_realized_pnl_tentative` → OMS-only or partial reconciliation
- `oms_fill_discrepancy_detected` → local OMS cash vs user-WS beyond tolerance
- `require_final_for_realized_pnl: true` (default) blocks treating OMS ack as final

Enforcement mode remains blocked until PnL is final or explicitly reconciled without unresolved discrepancies.
