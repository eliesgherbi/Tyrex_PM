# 07 — Implementation plan

**Branch:** `rest_project`  
**R1:** `630bac2acf67961a30b4be014d1df0434af967f1`  
**R2:** `ccccc969bb4877ae97e6e56c656b839739034425`  
**R3:** `8b8f34f8a275d0986fa1988e6f617094d5fc6cf9`  
**R4:** `9813001465db1fd188a4e00a3c82a24fa2cb4292`  
**R5:** `5cc1a306ee168f18df40e2107e78785d5a097364`  
**Status:** R5.1 stabilization; R6 adapter next (no real order mutation)

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
| R3 | Read-only MD + observe strategy | **Done** |
| R4 | Intents + risk + dry planning | **Done** (`9813001`) |
| R5 | Shadow OMS + portfolio + exits | **Done** (`5cc1a30`) |
| R5.1 | Host unify + entry/exit retry | **Done** |
| R6A/B | Live adapter + read-only reconcile | Next (no mutations) |
| R7–R8 | Tiny-live → acceptance | Planned |
| Z1–Z4 | Z-Gap after R8 | Planned |

## R5 decisions (summary)

- Lifecycle host owns `FLAT → ENTRY_PENDING → ACTIVE → EXIT_PENDING → FLAT/TERMINAL`.
- Position truth from fills (`FillLedger` + `OrderStore` + `Portfolio`).
- `ShadowOMS` implements `OMS` protocol; results are execution events only.
- Shadow fill model: visible-depth marketable limits; no queue/latency/impact.
- Exit precedence: `KILL_SWITCH → MARKET_CLOSE_BOUNDARY → MAX_HOLD → SIGNAL_REVERSAL → SIGNAL_FLAT`.
- Entry vs exit risk asymmetry: stale reference blocks entry; flatten may proceed with emergency policy.
- R4 dry path remains available when `shadow.enable_oms` is false/absent.
- Max-loss exit deferred (no mark-to-market P&L consumer yet).

## Proposed R6 scope (exact)

1. Live Polymarket OMS adapter implementing the same `OMS` protocol.
2. Private authenticated order submit/cancel (credentials from env; never logged).
3. Venue order/fill reconciliation into `OrderStore` / `FillLedger`.
4. Map venue rejects/cancels to existing execution events.
5. Keep ShadowOMS for offline/fixture validation.
6. Still no Z-Gap strategy logic.
