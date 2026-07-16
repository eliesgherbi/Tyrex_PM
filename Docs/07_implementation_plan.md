# 07 — Implementation plan

**Branch:** `rest_project`  
**R1:** `630bac2acf67961a30b4be014d1df0434af967f1`  
**R2:** `ccccc969bb4877ae97e6e56c656b839739034425`  
**R3:** `8b8f34f8a275d0986fa1988e6f617094d5fc6cf9`  
**Status:** R4 complete (intents + risk + dry plans); stop before R5

## Engine decision

Minimal Tyrex event-driven engine. NautilusTrader is not a dependency.

## Safe rollback

Explicit reverse of named `git mv` / targeted `git restore` / `git revert`.  
Do **not** use `git checkout HEAD -- .` or `git clean -fd`.

## Roadmap

| Phase | Scope | Status |
|-------|--------|--------|
| R1 | Archive + skeleton | **Done** |
| R2 | Core contracts + dispatcher | **Done** |
| R3 | Read-only MD + observe strategy | **Done** (`8b8f34f`) |
| R4 | Intents + risk + dry planning | **Done** |
| R5 | Shadow OMS + portfolio + exits | Next |
| R6–R8 | Live OMS → tiny-live → acceptance | Planned |
| Z1–Z4 | Z-Gap after R8 | Planned |

## R4 decisions (summary)

- Modes: `OBSERVE` / `SHADOW` / `LIVE_TINY` (LIVE_TINY fail-closed in R4).
- Intent: `EnterIntent` only (`target_notional`); Exit/Cancel/Flatten deferred to R5.
- Transition: entry only from None/FLAT/UNAVAILABLE → UP/DOWN; no reversal intents without portfolio.
- Duplicate guard: single owner in risk (`IntentDedupRegistry` + semantic key).
- Planner: dry limit-buy at tick-rounded best ask; `PLANNED` vs `UNPLANNABLE`.
- No OMS, orders, fills, portfolio, private endpoints.

## Proposed R5 scope (exact)

1. Shadow OMS: accept dry `ExecutionPlan` → simulated order lifecycle events.
2. Portfolio / position truth from fills (shadow).
3. `ExitIntent` / `FlattenIntent` / `CancelIntent` when position/order consumers exist.
4. Strategy `on_execution_event`; reversal/exit transition policy.
5. Persist strategy/dedup/portfolio state across restart.
6. Exposure limits in risk (replace deferred `exposure_available=False`).
7. Still no live private trading (R6) and no Z-Gap.
