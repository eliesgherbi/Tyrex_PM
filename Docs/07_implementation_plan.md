# 07 — Implementation plan

**Branch:** `rest_project`  
**R4:** `9813001465db1fd188a4e00a3c82a24fa2cb4292`  
**R5:** `5cc1a306ee168f18df40e2107e78785d5a097364`  
**R5.1:** `6b03cc32e3d65dfdf787ce346a73dcd7b545b6d1`  
**Status:** R6A/B implemented (uncommitted); **stop before R7 mutations**

## Roadmap

| Phase | Scope | Status |
|-------|--------|--------|
| R1–R4 | Reset → dry plans | **Done** |
| R5 | Shadow OMS + lifecycle | **Done** (`5cc1a30`) |
| R5.1 | Host unify + retry/escalation | **Done** (`6b03cc3`) |
| R6A | LiveOMS + transport + reconcile (fixtures) | **Done** (uncommitted) |
| R6B | Authenticated read-only | **Done** (CLOB L2 blocked here) |
| R7 | Explicit tiny-live mutations | Next (requires authorization) |
| R8 / Z1–Z4 | Acceptance → Z-Gap | Planned |

## Proposed R7 scope (exact)

1. Enable `mutations_enabled=True` behind explicit operator approval.  
2. Wallet EIP-712 order signing at the execution boundary.  
3. Live submit/cancel with UNKNOWN_SUBMISSION reconciliation-first policy.  
4. REST heartbeat supervisor (missing heartbeat cancels all opens).  
5. Tiny-live notional caps + kill switch + human approval gate.  
6. Still no Z-Gap strategy.
