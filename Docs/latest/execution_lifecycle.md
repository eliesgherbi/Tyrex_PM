# Execution lifecycle

## State authority

An `ExecutionSessionState` aggregate is rebuilt exclusively from journaled evidence. Order HTTP results and user-stream events are observations, not independent sources of truth. Trade IDs are deduplicated and confirmed fills, balances, and open orders are reconciled against an immutable account baseline.

## Entry

1. Continuously prewarm collateral, token balances, allowances, and open orders for the active and prepared-next markets. Temporary read failures produce `UNAVAILABLE`, preserve the last known good snapshot until its freshness deadline, and recover automatically.
2. Warm official SDK order metadata (tick / neg-risk / fees) for active and prepared-next tokens via discarded `create_market_order` (sign only, never POST). Entry stays blocked with `ORDER_METADATA_NOT_READY` until that warm succeeds.
3. Start the authenticated user stream and establish capabilities.
4. A strategy emits `EnterIntent` only when the model and entry capabilities are ready. Default `liquidity_role` is `TAKER`; `MAKER` is accepted on the intent. The runtime stamps candidate monotonic time and wakes a serial intent dispatcher immediately (it does not wait for the 1 Hz market-data tick).
5. The planner maps `TAKER` to `MarketBuyOrderSpec` (FAK). `amount` / `max_spend` are collateral, with total debit bounded by the configured cap. `MAKER` maps to `LimitOrderSpec` GTC; the runtime records `MAKER_LIMIT_PLANNED` and **does not POST** in this release.
6. For TAKER, the Polymarket gateway adapts the limit to the authoritative BookView tick and asks the official SDK to sign. With a warm metadata cache this prepare path is local math + EIP-712, not cold CLOB REST.
7. During submit, account-state refresh for that market is held so a concurrent REST cycle cannot flip `entry_executable` mid prepare/final-gate.
8. The coordinator obtains a newer book, verifies that the tick and executable price are still valid, persists authorization, and calls SDK `post_order` asynchronously.
9. Stream and REST evidence resolve the result to confirmed exposure, confirmed no-fill, or manual intervention.

A preparation or final-gate failure happens before mutation and is durably terminal as `COMPLETED_NO_DISPATCH`; it never waits for venue evidence or becomes manual intervention. Reports retain requested price, adapted price, tick size, failure stage, and error code. Warm events (`ORDER_METADATA_WARM_*`) and execution timeline spans (`candidate_to_request_ms`, `request_to_prepared_ms`) are retained for latency diagnosis.

The authenticated user channel supplies real-time order and trade lifecycle evidence. It does not replace balance, allowance, or open-order reads; the account-state authority and reconciler own those REST observations.

## Exit

1. Strategy policy, protection overlay (`PROTECTION_SL` / `PROTECTION_TP` / `PROTECTION_TRAIL`), or mandatory-flatten timing emits `ExitIntent`/`FlattenIntent`. Protection evaluates on Polymarket book updates via `on_public_fact`, even when the strategy scheduler does not wake.
2. The planner creates a `MarketSellOrderSpec`; quantity is confirmed sellable shares, never collateral.
3. The lifecycle submits bounded FAK retries and waits on event-driven evidence.
4. Reconciliation confirms baseline-relative flatness. Only then is the session `COMPLETED_FLAT`.

Exit processing is not starved by entry/model readiness. Exit and flatten intents use the same wake/dispatcher path as entry. Each retry captures the current book and applies its own synchronization, freshness, price, and sellability checks. If exposure remains at the configured manual deadline, the durable session becomes `MANUAL_INTERVENTION` and the run stops for operator action.

An exit submission is not a terminal result. Uncertain evidence or residual exposure becomes `MANUAL_INTERVENTION`; it is never reported as success.

Reactive protection arms after confirmed exposure using the run-config `protection:` block and evaluates the held best bid. It does not place resting GTC take-profit orders in the current release. See [protection.md](protection.md).

`HoldToResolutionIntent` is consumed: the runtime records `HOLD_TO_RESOLUTION` and does not mutate the venue. Automatic on-chain redeem / merge is not implemented. z_gap config with `resolution_capability_default: true` is rejected at load.

## Recovery

On startup, SQLite sessions are replayed. A fully reconstructable session for the active market is reconciled and, if exposed, receives a protective flatten intent. Foreign or ambiguous unresolved sessions block new entry and require operator review.

## Time and run boundary

The trading-duration clock starts at the prepared target window, not process composition. PTB readiness uses the measured boundary receive lag and the configured clock-uncertainty threshold. Candidate freshness and execution latency use monotonic timestamps; raw and corrected wall timestamps are display evidence only. The outer duration boundary never cancels a submission or reconciliation operation in flight; SDK and lifecycle timeouts bound those operations, and the runtime stops at the next safe loop boundary.
