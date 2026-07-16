# 01 — Functional requirements

**Phase:** R1 documents the target; implementation lands R2–R8.

## Required for framework validation (`ReferenceMomentumStrategy`)

| Capability | First phase |
|------------|-------------|
| Polymarket market discovery | R3 |
| CLOB book ingestion | R3 |
| Binance reference ingestion | R3 |
| Normalized timestamps + freshness | R2–R3 |
| Market-state ownership | R3 |
| Reusable indicators (momentum, mid, spread) | R3 |
| Typed directional signal | R3 |
| Strategy callbacks → intents | R4 |
| Risk (mode, allowlist, freshness, size, kill switch, duplicate guard) | R4 |
| Execution plan + shadow OMS | R5 |
| Order/fill/position truth + restart (shadow) | R5 |
| Live Polymarket OMS boundary | R6 |
| Tiny-live bounded validation | R7 (auth) |
| One CLI, one host, facts chain | R1–R8 |

## Deferred to Z-Gap (after R8)

PTB, Chainlink/RTDS attestation, TimeAuthority hardening, σ / binary FV / `fd` edge, Z-Gap entry/exit policies, calibration compare.

## Non-requirements

NautilusTrader runtime · multi-venue execution · distributed message bus · profitability of the validation strategy.
