# 05 — Validation strategy

> **Historical (R6).** Current tests live under `tests/`. Live strategies are `z_gap` and `ask70`, not `ReferenceMomentumStrategy`.

**Phase:** R6A/B

## Framework strategy

`ReferenceMomentumStrategy` — observe + lifecycle exits + R5.1 retry gates.

## Shadow model (unchanged honesty)

Visible-depth only; no queue/latency/impact; not profitability evidence.

## Live validation in R6

- Scripted `FakeTransport` transcripts for submit/cancel/unknown/reconcile races.
- Authenticated **read-only** probe (`scripts/r6b_readonly_probe.py`).
- No real order submit/cancel until R7 authorization.

## Out of scope until R7

Wallet signing for live posts · tiny-live capital · on-chain approvals · Z-Gap.
