# 08 — Validation report

## Checkpoints

| Phase | Commit | Message |
|-------|--------|---------|
| R1 | `630bac2acf67961a30b4be014d1df0434af967f1` | reset project with isolated legacy tree and minimal skeleton |
| R2 | `ccccc969bb4877ae97e6e56c656b839739034425` | add deterministic event-driven core contracts |
| R3 | `8b8f34f8a275d0986fa1988e6f617094d5fc6cf9` | add read-only market data and momentum strategy slice |

`.env` SHA256 (unchanged): `27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772`  
`var/` gitignored · no `old/` imports · no NautilusTrader

---

## R3 (summary)

Option B books; Binance `@trade`; observe decisions; 64 tests at R3 gate. See prior R3 sections in git history of this file if needed.

---

## R4 completion report

**R3 checkpoint:** `8b8f34f8a275d0986fa1988e6f617094d5fc6cf9`  
**Pytest:** **95 passed** (no network in default suite)

### Packages / files created

```text
src/tyrex_pm/core/modes.py
src/tyrex_pm/core/intents.py
src/tyrex_pm/strategies/{protocol,context}.py
src/tyrex_pm/strategies/framework_validation/reference_momentum.py  (transitions)
src/tyrex_pm/risk/{reasons,context,decision,dedup,policies,engine}.py
src/tyrex_pm/planning/{plan,planner}.py
src/tyrex_pm/runtime/{config,observe_host,live_observe}.py  (extended)
config/observe_shadow_r4.json
tests/test_r4_*.py
```

### Strategy contract

`on_start` / `on_signal` (via `apply_transition`) / `on_stop`.  
`on_timer` and `on_execution_event` deferred.  
Strategy does not import adapters, risk, planner, or OMS.

### Intent types

**Implemented:** `EnterIntent` (BUY, `target_notional`, outcome, semantic_key).  
**Deferred to R5:** `ExitIntent`, `CancelIntent`, `FlattenIntent`.

### Transition + dedup

| Rule | Behavior |
|------|----------|
| None/FLAT/UNAVAILABLE → UP/DOWN | One `EnterIntent` |
| Same direction repeat | Suppress `REPEATED_DIRECTION` |
| UP ↔ DOWN | Observe decision only; `REVERSAL_NO_PORTFOLIO` |
| → FLAT/UNAVAILABLE | No entry; exit deferred |
| Framework dedup | `strategy\|market\|instrument\|ENTER\|epoch\|outcome` in risk registry |

### Risk policies (order)

1. schema_validity  
2. runtime_mode (`LIVE_TINY` → `LIVE_NOT_SUPPORTED`)  
3. kill_switch  
4. duplicate_intent  
5. instrument_allowlist  
6. market_timing  
7. data_readiness  
8. price_spread_liquidity  
9. notional_cap  

Fail-closed on policy exceptions. Exposure limits deferred (`exposure_available=False`).

### Planner

Limit BUY at tick-rounded (UP) best ask; quantity floor so `Q*P ≤ N`; min-size / depth / max-price checks; `PLANNED` vs `UNPLANNABLE`.

### Causality

`ReferencePriceUpdated` → signal → observe decision → `EnterIntent` → `RiskDecision` → `ExecutionPlan` (shared `correlation_id`).

### Public live dry-plan evidence (not shadow trading)

| Item | Value |
|------|-------|
| Market | `btc-updown-5m-1784217300` — Bitcoin Up or Down 11:55AM–12:00PM ET |
| Duration | ~40s |
| Signals | 781 (UP 105, FLAT 643, UNAVAILABLE 33) |
| Strategy intents | **1** (105 repeated UP suppressed) |
| Risk | 1 approved / 0 denied |
| Plans | 1 PLANNED / 0 unplannable |
| Artifact | `var/reporting/r4/live_dry_plan_facts.jsonl` (gitignored) |
| Private/trading calls | None |

### Confirmations

No OMS · no orders · no fills · no portfolio · no private endpoints · no `old/` · no NautilusTrader · distribution excludes `old/`.

### Proposed R5

Shadow OMS + portfolio + exit/cancel/flatten intents + execution events + restart persistence + exposure risk. See `07_implementation_plan.md`.

**Stop before R5.**
