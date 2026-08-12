# 07 — Implementation plan

> **Historical R-series plan.** Completed. Current runtime: [`../latest/`](../latest/README.md).

**Branch:** `rest_project`  
**R7B CLI checkpoint:** `d506ea661a6478f7359fc623e69a0054b5dcdc65`  
**Status:** R7C settlement hardening in progress — **no further live test until review**

## Roadmap

| Phase | Scope | Status |
|-------|--------|--------|
| R1–R6 | Reset → live adapter / read-only | **Done** |
| R7A / R7A.1 / R7A.2 | Tiny-live prep, windows/fees, ack+session | **Done** |
| R7B | Operator CLI `--execute-live` one-shot | **Done** (`d506ea6`) — first live hit settlement bug |
| R7C | Incident analysis + settlement/recon hardening | **Done** (`da31508`) |
| R7C.1 | Acceptance: CONFIRMED-only, dust, ack fail-closed | **In progress** |
| R8 / Z1–Z4 | Acceptance → Z-Gap | Planned after R7C review |

## Proposed R7 scope (exact)

1. Enable `mutations_enabled=True` behind explicit operator approval.  
2. Wallet EIP-712 order signing at the execution boundary.  
3. Live submit/cancel with UNKNOWN_SUBMISSION reconciliation-first policy.  
4. REST heartbeat supervisor (missing heartbeat cancels all opens).  
5. Tiny-live notional caps + kill switch + human approval gate.  
6. Still no Z-Gap strategy.
