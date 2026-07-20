# F1 — Generic strategy decision and intent contracts

**Status:** implemented  
**Starting commit:** `e6ecca06d031e741ba1cf932bbda7ffb55f8666d`  
**Branch:** `rest_project`

## Objective

Replace validation-strategy coupling in the generic `Strategy` protocol with neutral, reusable contracts so any strategy returns `StrategyDecision` and may emit `EnterIntent` | `ExitIntent` | `FlattenIntent` through shared risk/planning/OMS paths.

F1 is framework-generic. It contains no Z-Gap mathematics, PTB, TimeAuthority, indicators, policies, or runtime Z-Gap wiring.

## Starting state

| Item | Value |
|------|-------|
| HEAD | `e6ecca0` — Freeze full Z-Gap strategy and implementation baseline |
| Worktree | clean |
| Offline tests | 384 passed (P0 baseline) |

## Old coupling (removed)

| Before | After |
|--------|--------|
| `strategies/protocol.py` imported `ObserveDecision` from `framework_validation.reference_momentum` | Protocol imports `StrategyDecision` / `IntentLike` from `strategies/decisions.py` |
| Protocol typed `on_signal` → `tuple[ObserveDecision, list[EnterIntent]]` | `tuple[StrategyDecision, list[IntentLike]]` |
| `ObserveDecision` class carried validation-specific kinds as the protocol decision type | Validation labels retained only as `ObserveDecisionKind` in evidence (`validation_kind`); neutral `action` is F1 vocabulary |

## New neutral contracts

### `src/tyrex_pm/strategies/decisions.py`

- `StrategyAction`: `WAIT` · `SKIP` · `ENTER` · `HOLD` · `EXIT` · `FLATTEN` · `BLOCKED`
- `StrategyDecision`: `action`, `reason_code`, `decided_at`, `correlation_id`, `causation_id`, `decision_id`, `evidence`, `strategy_id`
- `IntentLike = EnterIntent | ExitIntent | FlattenIntent`

No `STOP`, no `HOLD_TO_RESOLUTION` as generic actions.  
No venue/OMS fields. No `framework_validation` imports. No Z-Gap types.

### Protocol

`Strategy.on_signal` → `tuple[StrategyDecision, list[IntentLike]]`  
Callbacks remain: `on_start`, `on_signal`, `on_stop` only.

## Action semantics

| Action | Meaning |
|--------|---------|
| WAIT | Inputs/lifecycle not ready; evaluate again later |
| SKIP | Evaluation completed; no opportunity justified |
| ENTER | Request new economic exposure |
| HOLD | Existing exposure remains desired |
| EXIT | Normal flattening for a strategy reason |
| FLATTEN | Urgent/risk-driven flattening |
| BLOCKED | Evaluation cannot safely proceed |

Exit-family distinctions (thesis, time, rich, …) use `reason_code`, not separate actions. Resolution-hold remains an F5 capability.

## Intent mapping

| Strategy output | Location |
|-----------------|----------|
| Enter / exit / flatten | `IntentLike` in `strategies/decisions.py` (imports core intents) |
| Cancel | Not in `IntentLike` (OMS/framework behavior) |
| Resolution | Not in F1 |

Dependency direction: `strategies` → `core.intents`; `core` does not import `strategies`.

## ReferenceMomentum compatibility

- Migrated to `StrategyDecision` + `IntentLike`
- Behavior preserved: enter/exit/flatten, reason codes, semantic keys, OBSERVE/SHADOW facts, dedup, lifecycle
- Explicit mapping: `WOULD_ENTER_UP` / `WOULD_ENTER_DOWN` → `action=ENTER` with `evidence["validation_kind"]` preserving the old label
- `ObserveDecision` class removed (no compatibility re-export — no active external package dependency required it)
- OBSERVE facts still emit `kind` from `validation_kind` when present for report continuity

## Host updates

- `ObserveHost` / `ObserveRunResult` consume `StrategyDecision` and `list[IntentLike]`
- Architecture unchanged: OBSERVE still no OMS submit; SHADOW still routes through risk/planning/OMS/lifecycle
- No LIVE_TINY / R7 / Z-Gap branches added

## Generic economics types

**Deferred.** Fee-bound vs estimated vs confirmed P&L distinctions already exist in risk/planning/portfolio paths sufficiently for F1. No `core/economics.py` added — unused abstractions would not keep the decision/intent contract honest. Revisit when Z-Gap valuation (F2+) needs shared value types.

## Architecture tests

`tests/test_f1_strategy_contracts.py` enforces:

- Exact F1 action set; `STOP` / `HOLD_TO_RESOLUTION` absent
- `decisions.py` / `protocol.py` free of `framework_validation`, `old/`, `runtime.r7*`, adapters, `execution.polymarket`
- `IntentLike` is enter | exit | flatten only
- Risk/planning/execution modules do not import concrete strategies
- Protocol callbacks remain `on_start` / `on_signal` / `on_stop`
- ReferenceMomentum validation_kind → neutral action mapping

## Test results

Command:

```text
python -m pytest tests -q --tb=no
```

Result: **394 passed** (baseline was 384; +F1 architecture/mapping tests).

## Files changed

| Path | Role |
|------|------|
| `src/tyrex_pm/strategies/decisions.py` | New neutral contracts |
| `src/tyrex_pm/strategies/protocol.py` | Neutral return types |
| `src/tyrex_pm/strategies/framework_validation/reference_momentum.py` | Migration |
| `src/tyrex_pm/strategies/framework_validation/__init__.py` | Export cleanup |
| `src/tyrex_pm/runtime/observe_host.py` | Consume `StrategyDecision` / `IntentLike` |
| `tests/test_f1_strategy_contracts.py` | New architecture/unit tests |
| `tests/test_r3_*.py`, `tests/test_r4_*.py` | Assert `action` + `validation_kind` |
| `Docs/implementation/f1_generic_strategy_contracts.md` | This report |
| `Docs/implementation/README.md` | Index link |
| `Docs/latest/concepts/*`, `modules/strategy_risk_planning.md`, `developer_guide/extending_the_framework.md` | Align with implemented protocol |

## Exclusions (confirmed not in F1)

- `strategies/z_gap/`, PTB/K, Chainlink/RTDS, TimeAuthority
- EWMA / binary FV / entry-position valuation / Z-Gap policy
- Atomic Z-Gap snapshots, timer evaluation, Z-Gap OBSERVE/SHADOW wiring
- Resolution intents / resolution-pending lifecycle / redeem
- Live commands, `.env`, `var/state/`, `config/r7/`, `runtime/r7*` mutations

## Remaining work for F2

F2 owns Z-Gap mathematics and PTB contracts: fair-value / edge helpers, K/PTB types, providers (fixture-first), TimeAuthority scaffolding as planned — without host Z-Gap formulas or live wiring. F1 stops at neutral contracts + ReferenceMomentum migration.
