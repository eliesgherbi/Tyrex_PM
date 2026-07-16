# 01 — Functional requirements

**Phase:** R5.1 complete; R6A/B adapter + read-only only. Real mutations begin R7.

| Capability | Phase | Status |
|------------|-------|--------|
| Market data + observe signal/decision | R3 | Done |
| EnterIntent + transition policy | R4 | Done |
| Risk authorization (dry) | R4 | Done |
| Dry execution plan | R4 | Done |
| Shadow OMS + fills + portfolio | R5 | Done |
| Exit/Cancel/Flatten intents | R5 | Done |
| Unified TradingHost + retry/escalation | R5.1 | Done |
| Live OMS adapter (mutations disabled) | R6A | Done |
| Reconciliation + readiness | R6A | Done |
| Authenticated read-only validation | R6B | Done (CLOB L2 Cloudflare-blocked in this env) |
| Real submit/cancel (tiny-live) | R7 | Not started |

## Deferred to Z-Gap (after R8)

PTB, Chainlink attestation, σ / FV / fee edge, Z-Gap policies.
