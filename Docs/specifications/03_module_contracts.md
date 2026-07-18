# 03 — Module contracts

**Phase:** R8 (R7 live closed; see `Docs/implementation/r8_framework_acceptance.md`)

## Strategy / risk / planner

Must not import `tyrex_pm.execution.polymarket.*`.  
Strategy decides economic need; `RetryController` schedules attempts; dedup is a backstop.

## OMS protocol

```python
class OMS(Protocol):
    def submit(self, command: SubmitOrderCommand) -> OrderId: ...
    def cancel(self, command: CancelOrderCommand) -> None: ...
    def stop(self) -> None: ...
```

`ShadowOMS` and `LiveOMS` both implement this.  
`submit` returns local `OrderId` only — never implies venue acceptance.

## Live transport

`PolymarketTransport`: submit/cancel/query/subscribe/stop.  
R6A tests use `FakeTransport`. R6B uses `ReadOnlyClobClient` (submit/cancel raise).

## Submission uncertainty

`COMMAND_CREATED → SUBMITTING → VENUE_ACCEPTED | REJECTED | UNKNOWN_SUBMISSION`  
Unknown: reconcile first; do not resubmit; block entries.

## Reconciliation classes

`MATCHED` · `LOCAL_MISSING` · `VENUE_MISSING` · `FILL_MISSING_LOCAL` ·  
`ORDER_STATUS_MISMATCH` · `POSITION_MISMATCH` · `UNKNOWN_EXTERNAL_ORDER` · `UNRESOLVED`

Unknown external orders are never auto-canceled. Missing evidence never means flat.

## R7C / R7C.1 settlement contract

Module: `tyrex_pm.execution.polymarket.settlement`

- Insert `status=matched` → `ENTRY_MATCHED` only (never `entry_filled`).
- Acquired qty from trades with status **CONFIRMED** only (`MINED`/`MATCHED`/`RETRYING` pending).
- Sellable qty = `min(confirmed_acquired, funder_conditional_balance)` (never planned shares).
- Flatness: `FLAT` | `FLAT_WITH_DUST` | `RESIDUAL_EXPOSURE` | `UNKNOWN` — dust never authorizes on-chain cleanup.
- Ack validation fail-closed on incomplete/duplicate/disagreeing inventory rows.
- Address roles: `address_roles.py` — refuse signer conditional balance when `signature_type=1`.
- Live path: clean worktree required; CLI read-only verify: `tyrex-pm r7c-recon`.

## R7E / R7F lifecycle exit planning

Modules: `lifecycle_exit_plan.py`, `r7_lifecycle_policy.py`

- Strategy emits intent; planner owns venue-side price/qty/depth/order-type.
- BUY limit ≠ SELL limit. Never reuse entry `sized.limit_price` for SELL.
- Fresh bid-side book required; stale/empty bids → wait or refuse (no knowingly unmatchable FAK).
- Exact policy: freshness 2000 ms; floors 0.01; max slip from touch 0.05; max spread 0.20;
  exit attempts 3; cooldown 0.5 s; settlement wait 45 s; flatten−30 s / entry−45 s / min 90 s.
- SELL limit = `tick_floor(worst_bid_walk)`; NORMAL also requires `(best_bid−worst)≤0.05`.
  EMERGENCY skips touch-slippage only — not a legacy blind `0.01` unwind
  (see `Docs/implementation/r7_exit_floor_policy.md`).
- SELL qty ≤ `min(confirmed_acquired, sellable_balance, remaining_after_confirmed_exits)`.
- Partial FAK + no-match retries: new book fingerprint each try; attempt cap + flatten deadline.
- Residual registry records incomplete exits; cleanup policy remains `NONE`.
- Inventory terminal: exact zero → `FLAT`; dust `<0.01` → `FLAT_WITH_DUST` (never call dust `FLAT`).
- FAK is floor-protected, not fill-guaranteed under book move.
- R7 third live complete (`55fd9a76`); no further R7 live under closed runbook.

## Forbidden

`--execute-live` on dirty worktree · blind live retest · logging credentials · `old/` imports · NautilusTrader.
