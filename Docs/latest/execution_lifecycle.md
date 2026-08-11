# Execution lifecycle

## State authority

An `ExecutionSessionState` aggregate is rebuilt exclusively from journaled evidence. Order HTTP results and user-stream events are observations, not independent sources of truth. Trade IDs are deduplicated and confirmed fills, balances, and open orders are reconciled against an immutable account baseline.

## Entry

1. Continuously prewarm collateral, token balances, allowances, and open orders for the active and prepared-next markets. Temporary read failures produce `UNAVAILABLE`, preserve the last known good snapshot until its freshness deadline, and recover automatically.
2. Start the authenticated user stream and establish capabilities.
3. A strategy emits `EnterIntent` only when the model and entry capabilities are ready.
4. The planner creates a `MarketBuyOrderSpec`; `amount` and `max_spend` are collateral, with total debit bounded by the configured cap.
5. The Polymarket gateway reads the authoritative token tick from the current `BookView`, rounds BUY protection down (or SELL protection up) without weakening the strategy limit, and asks the official SDK to sign the adapted order.
6. The coordinator obtains a newer book, verifies that the tick and executable price are still valid, persists authorization, and calls SDK `post_order` asynchronously.
7. Stream and REST evidence resolve the result to confirmed exposure, confirmed no-fill, or manual intervention.

A preparation or final-gate failure happens before mutation and is durably terminal as `COMPLETED_NO_DISPATCH`; it never waits for venue evidence or becomes manual intervention. Reports retain requested price, adapted price, tick size, failure stage, and error code.

The authenticated user channel supplies real-time order and trade lifecycle evidence. It does not replace balance, allowance, or open-order reads; the account-state authority and reconciler own those REST observations.

## Exit

1. Strategy policy or mandatory-flatten timing emits `ExitIntent`/`FlattenIntent`.
2. The planner creates a `MarketSellOrderSpec`; quantity is confirmed sellable shares, never collateral.
3. The lifecycle submits bounded FAK retries and waits on event-driven evidence.
4. Reconciliation confirms baseline-relative flatness. Only then is the session `COMPLETED_FLAT`.

Exit processing is not starved by entry/model readiness. Each retry captures the current book and applies its own synchronization, freshness, price, and sellability checks. If exposure remains at the configured manual deadline, the durable session becomes `MANUAL_INTERVENTION` and the run stops for operator action.

An exit submission is not a terminal result. Uncertain evidence or residual exposure becomes `MANUAL_INTERVENTION`; it is never reported as success.

## Recovery

On startup, SQLite sessions are replayed. A fully reconstructable session for the active market is reconciled and, if exposed, receives a protective flatten intent. Foreign or ambiguous unresolved sessions block new entry and require operator review.

## Time and run boundary

The trading-duration clock starts at the prepared target window, not process composition. PTB readiness uses the measured boundary receive lag and the configured clock-uncertainty threshold. Candidate freshness and execution latency use monotonic timestamps; raw and corrected wall timestamps are display evidence only. The outer duration boundary never cancels a submission or reconciliation operation in flight; SDK and lifecycle timeouts bound those operations, and the runtime stops at the next safe loop boundary.
