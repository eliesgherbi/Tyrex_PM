# F5 — Resolution-aware Z-Gap SHADOW lifecycle

**Status:** implemented  
**Starting commit:** `2477ce369ce9fe4a6375e22d37d29f067624ebec`  
**Branch:** `rest_project`

## Objective

Complete the fixture SHADOW economic lifecycle with optional, simulated
hold-to-resolution behavior — without live trading, network providers,
on-chain redemption, or venue integration.

## Responsibility / module map

| Concern | Owner |
|---------|-------|
| Economic preference + reason | Thin `ZGapStrategy` + F2 policies |
| Explicit resolution request | `HoldToResolutionIntent` (`core/intents.py`) |
| Capability contract | `domain/polymarket/resolution_capability.py` (composition-supplied) |
| Evidence validate / payout math | `domain/polymarket/resolution_evidence.py` |
| Capability gate | `ResolutionCapabilityPolicy` (RiskEngine) |
| Commitment / pending / confirmed / flat | `TradeLifecycle` |
| Simulated payout application | `SimulatedResolutionSettled` → `Portfolio` |
| Host orchestration | `ShadowHost` (generic intent kind; no Z-Gap branches) |
| Facts | JsonlFactSink via host `_emit` |

## Intent and capability contracts

### `HoldToResolutionIntent`

Normalized intent (not a `StrategyAction`):

- `kind = HOLD_TO_RESOLUTION`
- `window_id` (required)
- `instrument_id`, `market_id`, strategy/correlation lineage
- `reason_code` (strategy preference, typically `RESOLUTION_PREFERENCE`)
- evidence labels: `economics_label=estimated`, `operation=hold_to_resolution`

Generic `StrategyDecision.action` remains `HOLD`. The intent carries the
requested operation.

### `ResolutionCapability`

```text
status: AVAILABLE | DISABLED | UNAVAILABLE
ponr_before_event_end_s: float   # τ threshold for point of no return
```

Supplied through `DecisionContext.resolution_capability` and
`RiskContext.resolution_capability_available` from config composition
(`z_gap.resolution_capability`). Never inferred by Z-Gap math.

**Failure behavior:** if capability is off/unavailable, time policy emits
`TIME_SELL` at `flatten_before_event_end_s` (existing safe pre-resolution
exit). Risk denies any `HoldToResolutionIntent` without capability
(`RESOLUTION_CAPABILITY_UNAVAILABLE`). The framework never silently accepts
a resolution intent it cannot complete.

## Resolution commitment / PONR semantics

**Point of no return (PONR):** wall-clock τ ≤ `ponr_before_event_end_s`
while a resolution commitment is active. Configurable in fixture config
(`z_gap.ponr_before_event_end_s`, default 5s). Units: seconds of τ to
`event_end`.

| Phase | Behavior |
|-------|----------|
| Before commitment | Every eval re-compares `V_sell` vs `V_resolve,adj` (non-sticky). Market-rich / thesis / risk flatten remain eligible. |
| Commitment | Only an **approved** `HoldToResolutionIntent` moves lifecycle → `RESOLUTION_PENDING`. Strategy does not mutate lifecycle. |
| After commitment, before PONR | Superior market-rich sell may still exit (clears commitment). |
| After commitment + PONR | No fabricated executable sell. Remain pending/blocked; operator-attention facts if kill/settlement cannot complete. |
| Kill before commitment | Normal framework `FLATTEN`. |
| Kill after commitment | `BLOCKED` / hold path — no flatten sell. |

## Lifecycle transitions

```text
ACTIVE
  → (accepted HoldToResolutionIntent) RESOLUTION_PENDING
  → (validated evidence) RESOLUTION_CONFIRMED
  → (simulated payout applied) FLAT

RESOLUTION_PENDING
  → (pre-PONR Exit/Flatten) EXIT_* → FLAT   # clears commitment
```

Single source of truth: `TradeLifecycle` fields
`resolution_committed`, `resolution_window_id`, `resolution_evidence_id`,
`settlement_applied_id` (persisted).

## Evidence validation

Fixture evidence (`resolution_evidence_events`) requires:

- market_id, window_id, boundary_k
- settlement_price and/or resolved_side
- observed_at, source, provenance, status

Rejects (no portfolio/lifecycle mutation): mismatched market/window/K,
`UNKNOWN` / `STALE` / `REJECTED`, incomplete (no price and no side).

Missing evidence → remain `RESOLUTION_PENDING` / blocked. Never guess
win, loss, payout, or flat.

## Simulated payout / accounting

1. Validate evidence (+ optional binary rule for price→side)
2. `simulated_payout_per_share` (1 if held side matches resolved side else 0)
3. Publish `SimulatedResolutionSettled` (`economics_label=simulated_shadow`)
4. Portfolio applies idempotently (`sim_settle:{evidence_id}`)
5. Lifecycle → FLAT via `note_resolution_settled`

Labels: `simulated_shadow` / `simulated_shadow_pnl` — never confirmed
venue economics. No redemption or network code.

## Persistence and idempotency

Persisted: orders, fills, portfolio (`applied_execution_ids` includes
settlement keys), lifecycle resolution fields, retry, dedup, strategy slice.

Prevents: duplicate commitment, duplicate payout, position resurrection,
same-window re-entry (strategy lineage), loss of pending state.

Restart coverage: before commitment; after commitment before evidence;
after evidence/payout (idempotent replay).

## Facts / calibration

Versioned facts include:

- resolution preference / `sell_vs_resolve` comparison (strategy evidence)
- `intent_created` for `HOLD_TO_RESOLUTION`
- `resolution_capability_rejected` / risk deny reasons
- `resolution_committed`, lifecycle transitions
- `resolution_evidence_accepted` / `resolution_evidence_rejected` / pending
- `simulated_resolution_settled` (payout + simulated PnL)

Estimated / counterfactual / simulated values are labelled honestly.

## Scenario results

| # | Scenario | Result |
|---|----------|--------|
| 1 | Resolution eligible, market-rich superior | `MARKET_RICH_EXIT` → flat; no simulated settlement |
| 2 | Resolve win (held UP, YES) | Hold intent → pending → payout 1 → flat |
| 3 | Resolve lose (held UP, NO) | Hold intent → payout 0 → flat |
| 4 | Capability disabled | `TIME_SELL` pre-expiry; no hold intent |
| 5 | Missing evidence | Stay `RESOLUTION_PENDING` |
| 6 | Mismatched market/K | Validator reject; no mutation |
| 7 | Restart while pending | Recover pending; one payout; no duplicate commit |
| 8 | Replay confirmed evidence | Idempotent |
| 9 | Kill before commitment | Framework flatten |
| 10 | Kill after commitment | No fabricated sell; remain pending |

## Configuration / CLI

- Config: `config/observe_shadow_z_gap_f5.json`
- Fixtures: `tests/fixtures/z_gap/shadow_f5_resolve_*.json`
- Tests: `tests/test_f5_z_gap_resolution.py`

```bash
python -m pytest tests/test_f5_z_gap_resolution.py -q
```

## Exclusions and remaining work

**Out of F5:**

- Real PTB / Chainlink / RTDS providers
- Network TimeAuthority
- Live OMS / venue mutation
- On-chain redemption / claim
- Threshold calibration as production defaults
- Imports from `old/`, `runtime/r7*`, `config/r7/`
- `.env` or real-money commands

**Remaining (future live):** authorization, settlement truth from venue,
redeem tooling, operator runbooks beyond fixture SHADOW.

## Architectural invariants (verified)

1. `ZGapStrategy` stays thin; no lifecycle mutation.
2. No Z-Gap branching in RiskEngine, planners, OMS, Portfolio, lifecycle modules.
3. No `HOLD_TO_RESOLUTION` on `StrategyAction`.
4. OBSERVE/SHADOW share sealed `binding.evaluate` decision path.
5. Framework validates capability and owns commitment/settlement.
