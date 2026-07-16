# 01 — Functional requirements

**Phase:** R3 complete for the read-only observe path. Intents begin R4.

## Required for framework validation (`ReferenceMomentumStrategy`)

| Capability | Phase | Status |
|------------|-------|--------|
| Polymarket market discovery | R3 | Done (fixture + Gamma) |
| CLOB book ingestion | R3 | Done (Option B snapshot/delta) |
| Binance reference ingestion | R3 | Done (`@trade` stream) |
| Normalized timestamps + freshness | R2–R3 | Done (decision-time policy) |
| Market-state ownership | R3 | Done |
| Reusable indicators (momentum, mid, spread) | R3 | Done |
| Typed directional signal | R3 | Done |
| Observe decisions + facts JSONL | R3 | Done |
| Strategy callbacks → intents | R4 | Not started |
| Risk (mode, allowlist, freshness, size, kill switch, duplicate guard) | R4 | Not started |
| Execution plan + shadow OMS | R5 | Not started |
| Order/fill/position truth + restart (shadow) | R5 | Not started |
| Live Polymarket OMS boundary | R6 | Not started |
| Tiny-live bounded validation | R7 (auth) | Not started |
| One CLI, one host, facts chain | R1–R8 | Host + observe CLI in R3 |

## Deferred to Z-Gap (after R8)

PTB, Chainlink/RTDS attestation, TimeAuthority hardening, σ / binary FV / `fd` edge, Z-Gap entry/exit policies, calibration compare.

## Non-requirements

NautilusTrader runtime · multi-venue execution · distributed message bus · profitability of the validation strategy.
