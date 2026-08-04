# Strategy, risk, and planning

**Purpose:** from signals to authorized, sized execution plans.

## Packages

- `tyrex_pm.strategies` (+ `framework_validation.reference_momentum`)
- `tyrex_pm.strategies.z_gap` — pure model/valuation/policy (F2) + thin `ZGapStrategy` (F3/F4/N7)
- Fixture OBSERVE: `config/observe_z_gap_fixture_f3.json` (`strategy_kind=z_gap`; no OMS)
- Fixture SHADOW: `config/observe_shadow_z_gap_f4.json` (same decision path + ShadowOMS)
- N7 live one-shot: `config/n7_tiny_live.json` + `runtime/n7_*` (Scope A; must not import `runtime/r7*`)
- Host evaluation uses `StrategyBinding.evaluate` (no host `isinstance` / formula branching)
- `tyrex_pm.core.intents`
- `tyrex_pm.risk`
- `tyrex_pm.planning`
- N7 fee-inclusive sizing: `runtime/n7_sizing.py`
- R7 exit pricing: `execution.polymarket.lifecycle_exit_plan` + `runtime.r7_lifecycle_policy` (**phase-specific; not Z-Gap**)

## Strategy interface (active)

Protocol callbacks only: `on_start`, `on_signal`, `on_stop`.  
Protocol `on_signal` → `tuple[StrategyDecision, list[IntentLike]]` (`IntentLike` = enter | exit | flatten).

F1 actions: `WAIT` · `SKIP` · `ENTER` · `HOLD` · `EXIT` · `FLATTEN` · `BLOCKED`. No `on_timer` / `on_execution_event`.

**ReferenceMomentumStrategy:** structural validation only — not a recommended trading strategy. Validation labels live in `evidence["validation_kind"]`.

## Risk

- `RiskEngine.evaluate(intent, RiskContext) → RiskDecision`
- Dedup registry prevents intent storms
- Generic `LIVE_TINY` remains denied outside guarded one-shot paths (R7 or N7)
- Kill switch and spread/liquidity/price bounds from config
- N7: at most one entry lineage; no same-window re-entry/reversal; historical externals untouched

## Planning

| Planner | Owns |
|---------|------|
| Entry sizing | BUY limit, qty, fee-inclusive collateral bound (`≤ $5` on N7) |
| Exit planner (R7 path) | Bid-side FAK limit, depth, freshness, floors |
| Exit ladder (N7 Scope A) | Bounded inventory-reducing exits; exit fees never block flatten |

Entry and exit planning are separate. Never reuse entry BUY limit as SELL limit.

## Z-Gap entry parameters (current N7 wiring)

Signal gates (`theta_take`, `z_min`/`z_max`, `tau_*`, `require_repricing_edge`, …) live on
`ZGapEntryConfig` / live-compose `ZGapConfig` **code defaults**.  
`n7_tiny_live.json` currently wires caps, timing freeze, SSR flag, and fee curve — **not** those entry thresholds. Relaxing entry frequency requires a code/default change or future config plumbing (see [configuration](../how_to/configuration.md)).

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
- Z-Gap / N7: `tests/test_n7_*.py`, `tests/test_n7_ssr_optional.py`, F1–F5 / N3–N5 suites
- Fee amount certainty remains experimental; fee-inclusive cap is a reservation, not realized fee proof
