# 03 — Module contracts

**Phase:** R6A/B

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

## R7C settlement contract

Module: `tyrex_pm.execution.polymarket.settlement`

- Insert `status=matched` → `ENTRY_MATCHED` only (never `entry_filled`).
- Acquired qty from trades in `{MINED, CONFIRMED}` only.
- Sellable qty = `min(confirmed_acquired, conditional_balance)` (never planned shares).
- Bounded wait after MATCHED; no SELL while balance is zero; no SELL storm.
- Manual UI flatten → `FLAT_EXTERNAL_ACTION`.
- CLI read-only verify: `tyrex-pm r7c-recon` (never mutates).

## Forbidden

Blind live retest before R7C review · logging credentials · `old/` imports · NautilusTrader.
