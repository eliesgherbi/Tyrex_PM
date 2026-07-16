# 05 — Validation strategy

**Phase:** R4 — observe decisions + entry intents + dry risk/plan.

## Name

`ReferenceMomentumStrategy` (`framework_validation`)

## R3 path (unchanged)

Momentum, directional signal, observe decisions (`WOULD_ENTER_*` / `HOLD` / `SKIP`).

## R4 additions

1. Transition policy → at most one `EnterIntent` per eligible direction change.
2. Risk evaluation (fail-closed, explicit config).
3. Dry execution plan (limit buy at ask) — not an order.

No exit/flatten until R5 has positions. No OMS submit.

## Out of scope

PTB · Chainlink · Z-Gap edge · live private trading · portfolio PnL.
