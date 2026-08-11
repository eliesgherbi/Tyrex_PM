# Account-state and reporting correction

## Outcome

Implemented the narrow architectural correction prompted by the live run in which a transient collateral read disabled entry for the full window and the report mislabeled the result as a successful no-entry run.

## Root cause removed

The runtime previously captured collateral, two token balances, and open orders sequentially during market binding. A failed read was projected as zero collateral, and the one-shot result remained authoritative for the window. This combined a transport observation, a financial fact, and a permanent capability decision in one operation.

## Authoritative design

- `execution/account_state.py` is the sole account-read authority.
- Active and prepared-next markets are refreshed asynchronously before promotion.
- Independent reads run concurrently with bounded timeout and retry policy.
- Snapshots distinguish `READY`, `INSUFFICIENT`, `UNAVAILABLE`, and `STALE`.
- A failed read never becomes zero; the last known good snapshot is usable only within its configured age.
- The main trading runtime is the sole consumer that applies immutable snapshots to capabilities.
- Entry rechecks the latest authoritative snapshot before creating the execution-session baseline.
- Prior positions and open orders remain fail-closed.

The implementation uses the SDK boundary already owned by `PolymarketAsyncGateway`; it adds no direct HTTP client and no alternative account path.

## Operation semantics

- Read transport failures are retryable.
- POST transport failures and cancellation after the durable attempt record are `UNCERTAIN_DISPATCH` and require reconciliation.
- The outer duration deadline does not cancel an in-flight execution operation.

## Accompanying correctness fixes

- PTB reports and strategy policy now receive the measured boundary lag rather than a reconstructed zero.
- The configured clock-uncertainty limit is passed to the time authority.
- Trading duration is anchored to the prepared target-window start.
- No-entry reporting distinguishes `COMPLETED_NO_ENTRY_SIGNAL` from `NO_ENTRY_RUNTIME_BLOCKED` and includes a structured diagnosis.
- An emitted entry intent that cannot open a session is reported as `ENTRY_ABORTED_BEFORE_SESSION`, never as a successful no-signal run.
- Runtime and documentation use account-state terminology; the deleted one-shot account-scope implementation is not retained as a fallback.

## Polymarket contract alignment

The implementation follows the official separation of concerns: L2-authenticated APIs provide balances, allowances, open orders, and signed-order submission; the authenticated user WebSocket provides order and trade updates. Account refresh remains well below the documented balance/allowance and ledger rate limits.

## Validation

- Unit suite: 127 passed.
- Full project suite under `.venv` / Python 3.11: 162 passed.
- Ruff: clean after the final validation pass.
- Bytecode compilation: clean.
- No network run, wallet mutation, order submission, or allowance update was performed.
