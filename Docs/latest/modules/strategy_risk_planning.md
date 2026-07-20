# Strategy, risk, and planning

**Purpose:** from signals to authorized, sized execution plans.

## Packages

- `tyrex_pm.strategies` (+ `framework_validation.reference_momentum`)
- `tyrex_pm.strategies.z_gap` — pure model/valuation/policy (F2) + thin `ZGapStrategy` (F3)
- Fixture OBSERVE: `config/observe_z_gap_fixture_f3.json` (`strategy_kind=z_gap`; no OMS; not a public live path)
- `tyrex_pm.core.intents`
- `tyrex_pm.risk`
- `tyrex_pm.planning`
- R7 exit pricing: `execution.polymarket.lifecycle_exit_plan` + `runtime.r7_lifecycle_policy` (**phase-specific**)

## Strategy interface (active)

Protocol callbacks only: `on_start`, `on_signal`, `on_stop`.  
Protocol `on_signal` → `tuple[StrategyDecision, list[IntentLike]]` (`IntentLike` = enter | exit | flatten).

F1 actions: `WAIT` · `SKIP` · `ENTER` · `HOLD` · `EXIT` · `FLATTEN` · `BLOCKED`. No `on_timer` / `on_execution_event`.

**ReferenceMomentumStrategy:** structural validation only — not a recommended trading strategy. Validation labels live in `evidence["validation_kind"]`.

## Risk

- `RiskEngine.evaluate(intent, RiskContext) → RiskDecision`
- Dedup registry prevents intent storms
- Generic `LIVE_TINY` remains denied outside the guarded R7 one-shot path
- Kill switch and spread/liquidity/price bounds from config

## Planning

| Planner | Owns |
|---------|------|
| Entry sizing | BUY limit, qty, fee-inclusive collateral bound |
| Exit planner (R7 path) | Bid-side FAK limit, depth, freshness, floors |

Entry and exit planning are separate. Never reuse entry BUY limit as SELL limit.

## Fee and P&L vocabulary

| Term | Meaning |
|------|---------|
| BUY notional | Price × acquired size paid on entry (ex-fee unless stated) |
| SELL proceeds | Price × sold size received on exit |
| Gross price P&L | SELL proceeds − BUY notional |
| Maximum fee bound | Risk reservation used to keep fee-inclusive collateral ≤ cap |
| Estimated fee | Pre-submit estimate used for sizing / reporting |
| Confirmed actual fee | Venue-reported fee amount when present |
| Realized net P&L | Proceeds − notional − **confirmed** fees (only when fees known) |
| Unknown net P&L | When fee amount is missing/uncertain |

A fee bound is a **risk reservation**, not a confirmed expense.  
`fee_rate_bps=0` or a missing fee field does **not** prove the estimated maximum fee was charged.  
Do not label fee-bound-adjusted estimates as confirmed realized P&L.

## Invariants

- Strategy must not import `tyrex_pm.execution.polymarket.*`
- SELL qty ≤ `min(confirmed_acquired, sellable_balance, remaining_after_confirmed_exits)` on the live one-shot path

## Tests / limits

- Exit/risk suites under `tests/test_r7*.py`, `test_r8_*.py`
- Z-Gap contracts not present
