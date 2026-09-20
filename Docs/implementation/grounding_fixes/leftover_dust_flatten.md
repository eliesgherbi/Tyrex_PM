# G1 — Leftover crumbs (dust flatten)

**Phase:** Grounding fixes  
**Status:** implemented  
**Depends on:** none  
**Blocks:** G3 (honest SL live), any claim of `COMPLETED_FLAT` after a filled tiny-live

## Goal

After a successful sell of almost all tickets, the session must **stop retrying the old size**, sell only what is still sellable if the venue will accept it, and otherwise finish as **known leftover** — not “human emergency.”

## Venue facts (Polymarket, 2026-09-07)

- A sell is limited by **token balance minus size already reserved by open orders**:  
  `maxOrderSize = balance − Σ(openOrderSize − filledAmount)`  
  ([order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)).
- Book/market metadata includes **`min_order_size`** (examples in docs: `"5"` or `"1"`) and **`tick_size`**. An order below min size is `INVALID_ORDER_MIN_SIZE`.
- CLOB amount precision (community + official client issues, still observed on our posts): **SELL maker (shares) is typically 2 decimal places**. A leftover of `0.001448` cannot be a legal sell size.
- FAK fills what is available and cancels the rest. Partial fill is normal. Retrying the **original** size after a partial/almost-full fill is exactly what the venue rejects (`not enough balance`, `invalid maker amount`).
- Trade confirmation is `MATCHED` → `MINED` → `CONFIRMED`. Balance can lag confirmation. Do not treat a lagging balance as “still own 7.10.”

Polymarket has **no dust-cleanup API**. Tiny inventory after a round-down sell is a **local policy** problem.

## Current code (do not invent a second OMS)

| Piece | Behavior today |
|-------|----------------|
| `ExecutionLifecycle.submit_exit` | `shares = min(confirmed_position_shares, sellable_shares, confirmed_trade_net_shares)` then POST FAK. On budget exhaust + `confirmed_position_shares > 0` → `ManualInterventionRequired(exit_retry_budget_exhausted)`. |
| `confirmed_position_shares` | Increases on confirmed BUY; decreases only when a SELL trade is **CONFIRMED**. After a matched-but-not-confirmed sell, this stays at the **full buy** (e.g. `7.101448`). |
| `sellable_shares` | Balance ∩ allowance. After first exit: `0.001448`. |
| Retry 2/3 live | Still posted **7.10** (`6710000` / `7100000` atomic). Venue: already-matched + `invalid maker amount`. |
| `min_order_size` | Stored on binding/book (`discovery`, `BookView`) but **not used** on the exit size path. |
| Terminal phases | `COMPLETED_FLAT` or `MANUAL_INTERVENTION`. No `COMPLETED_FLAT_WITH_DUST`. |
| Recovery | `PRIOR_SCOPE_NOT_CLEAR` / `ACCOUNT_STATE_0` after MANUAL can block the next `--live`. |

## Live evidence

| Run | Buy | First sell | Leftover | Terminal |
|-----|-----|------------|----------|----------|
| `…090845Z` trail | 6.712325 | ~6.71 | 0.002325 | MANUAL |
| `…100203Z` TP | 7.101448 @ 0.69 | 7.10 | 0.001448 | MANUAL |

Same bug twice. Protection worked; flatten did not.

## Required implementation

### 1. Authoritative remaining size (every exit attempt)

Before each FAK sell, compute:

```text
remaining = min(
  confirmed_position_shares,          # reducer, if already applied
  sellable_shares,                    # last READY balance ∩ allowance
  max(0, confirmed_trade_net_shares), # buy − confirmed sells
  max(0, last_balance − reserved_open_sells)  # venue formula
)
```

Use **that** as `MarketSellOrderSpec.shares`. Never reuse the first-exit size.

If a previous EXIT is `matched` or `ambiguous` and not confirmed, **wait / reconcile** instead of posting a second full clip (the lifecycle already has a `has_confirmed_sell` wait; extend it to “matched HWM > 0” and “open venue sell ids”).

### 2. Venue-legal size

Round **down** remaining shares to the SELL maker increment (2 decimals unless the official SDK/book says otherwise for that tick). If `rounded < min_order_size` (from active `BookView` / binding, fail closed if missing): **do not POST**.

### 3. Dust terminal (honest, not fake flat)

Add a durable policy, e.g. in `LifecyclePolicy` + run YAML (explicit, no hidden default):

- `dust_abs_shares` or “below `min_order_size`”
- `dust_max_notional` (e.g. leftover × last bid ≤ $0.05)

If remaining is in policy and no open orders:

- persist a **known-dust residual** on the session (token, qty, market, run id)
- terminal **`COMPLETED_FLAT_WITH_DUST`** (or keep MANUAL only when leftover is **above** min size / unknown)
- `ok` may be `true` only if the residual is classified and scoped so the next window does not treat it as unknown exposure

Do **not** call this `COMPLETED_FLAT`. Docs/latest must say leftover exists.

### 4. Next-run gate

Known dust on a **resolved / non-selected** token must not set `PRIOR_SCOPE_NOT_CLEAR`. Unknown or tradable leftover still blocks entry.

### 5. Tests (no live required)

Replay the two live number sets:

- buy `7.101448`, sell match `7.10`, balance `0.001448` → no second 7.10 POST; dust terminal
- buy `6.712325`, sell `6.71`, retry attempted 6.71 → rejected path becomes “resize or dust”
- remaining `6.00` and `min_order_size=5` → POST 6.00 (or rounded legal size)
- remaining `0.01` if min is `5` → no POST, dust/manual per policy
- confirmed sell applied → `confirmed_position` drops; no double-count

## Files likely to change

- `src/tyrex_pm/execution/lifecycle.py` — size + dust terminal
- `src/tyrex_pm/execution/session_state.py` / `reducer.py` — apply matched sell HWM to remaining; optional `FLAT_WITH_DUST` phase
- `src/tyrex_pm/execution/planner.py` — optional size clamp helper
- `src/tyrex_pm/runtime/run_config.py` — explicit dust fields
- `src/tyrex_pm/reporting/run_report.py` — new outcome, leftover in `run_summary.json`
- `Docs/latest/running.md`, `execution_lifecycle.md`
- New unit tests under `tests/unit/execution/`

## Out of scope

- Resting GTC, cancel, maker POST
- Changing $5 cap or ask70 entry rule
- Auto-redeem of winning dust
- Deleting SQLite to “clear” a leftover

## Acceptance

1. Full deterministic suite green.  
2. Fixture replay of `…100203Z` / `…090845Z` numbers: **zero** retry of the original clip; terminal is dust or flat, not `exit_retry_budget_exhausted` for sub-min leftover.  
3. Report shows leftover qty + classification.  
4. Operator tiny-live (after this ships): a TP/trail fill can end without MANUAL **when leftover is sub-min**.
