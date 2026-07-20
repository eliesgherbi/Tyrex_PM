# F4 — Z-Gap SHADOW entry/exit lifecycle

**Status:** implemented  
**Starting commit:** `c73ca61b9edf79f7707dc3668c2e5cf9bca94776`  
**Branch:** `rest_project`

## Objective

Integrate the F3 Z-Gap decision path with RiskEngine, planning, ShadowOMS, Portfolio, and TradeLifecycle so fixture SHADOW runs complete deterministic entry→exit lifecycles — without live data, real PTB providers, or venue mutation.

## Binding architecture verdict

**Uniform host-facing interface:** `StrategyBinding.evaluate(...) → StrategyEvalResult`.

| Layer | Behavior |
|-------|----------|
| Composition | `build_strategy_binding(strategy_kind=…)` selects `ReferenceMomentumBinding` or `ZGapBinding` |
| Host `evaluate_once` | Always: snapshot → momentum indicator update → `_build_decision_context` → `binding.evaluate` → `_dispatch_eval_result` |
| Intent dispatch | Momentum: `_process_transition` (TransitionResult). Other bindings: `_process_intents` |
| OBSERVE | `_process_intents` → record-only (`observe_only`) |
| SHADOW | `_process_intents` → risk → plan → ShadowOMS |

**Evidence of correction:** ObserveHost no longer forks `evaluate_once` on `strategy_kind` / `isinstance(ZGapStrategy)`. Signal construction for momentum lives in the binding. A third strategy requires a new binding + registry entry only.

## Runtime flow

```text
Fixture timeline (+ timer)
→ atomic ZGapDecisionSnapshot (assemble)
→ ZGapStrategy.on_decision
→ StrategyDecision + Enter/Exit/Flatten intents
→ RiskEngine → planner / ExitPlanner
→ ShadowOMS → OrderStore / FillLedger
→ Portfolio + TradeLifecycle
→ next DecisionContext with position truth
→ active valuation → exit → flat
```

## State ownership

| Concern | Owner |
|---------|-------|
| Orders / fills | OrderStore / FillLedger |
| Confirmed qty / cost | Portfolio |
| Phase / exit outstanding | TradeLifecycle + RetryController |
| Entry lineage / thesis confirm | ZGapStrategy (minimal private slice) |
| EWMA / PTB lock | ZGapBinding |

**Entry lineage vs lifecycle retry:** strategy consumes one semantic entry lineage when `EnterIntent` is emitted (no same-window re-entry). Lifecycle/RetryController may still own retries of that single attempt via `attempt_id` — not a new strategy lineage.

## Scenario timelines

| Scenario | Result |
|----------|--------|
| Market-rich | WAIT → ENTER → fill → ACTIVE → EXIT(`MARKET_RICH_EXIT`) → flat |
| Thesis | ENTER → HOLD(confirming) → EXIT(`THESIS_INVALID`) → flat |
| Time | ENTER → EXIT(`TIME_SELL`) when τ ≤ flatten_before (resolution capability off) |
| Risk flatten | ACTIVE + kill_switch → FLATTEN → flat |
| UNKNOWN | `mark_unknown_inventory` → BLOCKED; no guessed SELL |

## OBSERVE / SHADOW parity

Identical entrypoint: `binding.evaluate` → `ZGapStrategy.on_decision` + F2 policies.  
Difference only after intents: OBSERVE records; SHADOW dispatches OMS.

## Facts / P&L terminology

Facts extend F3 with lifecycle/plan/command/fill evidence. Labels:

- fees: `estimated` / `shadow_model` — never “confirmed actual venue fees”
- P&L: `estimated_shadow_pnl` — never “confirmed live realized P&L”
- OBSERVE valuations remain `counterfactual`

Reporting does not import Z-Gap valuation modules.

## Persistence / recovery

`StateSnapshotStore` persists orders, fills, portfolio, lifecycle, retry, dedup, kill switch, and `strategy_state` from `binding.persistence_slice()` (lineage + thesis). Restart tests prove no duplicate entry when lineage consumed.

## Configuration / CLI

| Artifact | Role |
|----------|------|
| `config/observe_shadow_z_gap_f4.json` | Fixture SHADOW + `strategy_kind=z_gap` |
| `tests/fixtures/z_gap/shadow_f4_*.json` | Rich / thesis scenarios |

```bash
tyrex-pm shadow --config config/observe_shadow_z_gap_f4.json
```

Offline fixture only. Do not claim public live Z-Gap or real PTB support.

## Tests / results

`tests/test_f4_z_gap_shadow.py` (+ architecture updates).

```text
python -m pytest tests -q --tb=no
```

Result: **450 passed** (baseline 434).

## Explicit exclusions (F4 scope)

No real PTB/Chainlink/RTDS · no network TimeAuthority · no live OMS · no venue mutation · no threshold calibration · no `runtime/r7*` / `config/r7/` / `.env` edits.

## Unresolved real PTB/reference provider

Still open. F4 uses fixture K only.

## F5 follow-on

See `Docs/implementation/f5_z_gap_resolution_shadow.md` for resolution capability,
`HoldToResolutionIntent`, PONR, and simulated settlement (still no live venue).
