# Execution lifecycle

This page is the **OMS** path: plan → sign → POST → evidence. The `eval` lab calls `driver.evaluate()` only and never enters this spine. Prove evaluate() first ([extending.md](extending.md#lab-fixture-before-live)); then `--live` is what exercises the steps below.

## State authority

An `ExecutionSessionState` aggregate is rebuilt exclusively from journaled evidence. Order HTTP results and user-stream events are observations, not independent sources of truth. Trade IDs are deduplicated and confirmed fills, balances, and open orders are reconciled against an immutable account baseline.

## Entry

1. Continuously prewarm collateral, token balances, allowances, and open orders for the active and prepared-next markets. Temporary read failures produce `UNAVAILABLE`, preserve the last known good snapshot until its freshness deadline, and recover automatically.
2. Warm official SDK order metadata (tick / neg-risk / fees) for active and prepared-next tokens via discarded `create_market_order` (sign only, never POST). Entry stays blocked with `ORDER_METADATA_NOT_READY` until that warm succeeds.
3. Start the authenticated user stream and establish capabilities.
4. A strategy emits `EnterIntent` only when the model and entry capabilities are ready. Default `liquidity_role` is `TAKER`; `MAKER` is accepted on the intent. The runtime stamps candidate monotonic time and wakes a serial intent dispatcher immediately (it does not wait for the 1 Hz market-data tick).
5. The planner maps `TAKER` to `MarketBuyOrderSpec` (FAK). `amount` / `max_spend` are collateral, with total debit bounded by the configured cap. `MAKER` maps to a post-only `LimitOrderSpec` GTC. The host POSTs it and returns while the bid is `live` (`MAKER_LIMIT_POSTED`); it does not wait for a fill. Mandatory flatten or window end cancels that id, then sells any fill with G1 remaining size. Unfilled cancel/expire is `COMPLETED_NO_FILL`.
6. For TAKER, the Polymarket gateway adapts the limit to the authoritative BookView tick and asks the official SDK to sign. With a warm metadata cache this prepare path is local math + EIP-712, not cold CLOB REST.
7. During submit, account-state refresh for that market is held so a concurrent REST cycle cannot flip `entry_executable` mid prepare/final-gate. Sign waits up to `lifecycle.refresh_wait_budget_ms` for an **in-flight** refresh (the hold only pauses *new* cycles). If the client is still busy, prepare fails closed with `ACCOUNT_REFRESH_IN_FLIGHT` and does not start `create_market_order`. Sign itself is serialized on the same SDK single-flight as account reads.
8. `create_market_order` is bounded by `lifecycle.prepare_budget_ms` (must be less than `market.maximum_candidate_age_ms`). If the budget is exceeded, the attempt is abandoned without cancelling the official HTTP/2 call (`PREPARE_BUDGET_EXCEEDED`, no POST). The 5 s candidate fuse is unchanged.
9. The coordinator obtains a newer book, verifies that the tick and executable price are still valid, persists authorization, and calls SDK `post_order` asynchronously.
10. Stream and REST evidence resolve the result to confirmed exposure, confirmed no-fill, or manual intervention.

A preparation or final-gate failure happens before mutation and is durably terminal as `COMPLETED_NO_DISPATCH`; it never waits for venue evidence or becomes manual intervention. Reports retain requested price, adapted price, tick size, failure stage, error code, and prepare splits (`prepare_queue_wait_ms`, `tick_resolve_ms`, `create_market_order_ms`, `refresh_wait_ms`, `refresh_elapsed_ms`). A warm token whose live create still looks like CLOB HTTP records `ORDER_METADATA_CACHE_MISS`. Warm events (`ORDER_METADATA_WARM_*`) and execution timeline spans (`candidate_to_request_ms`, `request_to_prepared_ms`) are retained for latency diagnosis.

Public book DESYNC/REST recovery uses a separate public client, not `AsyncSecureClient`, so it does not join the authenticated sign/account pool.

The authenticated user channel supplies real-time order and trade lifecycle evidence. It does not replace balance, allowance, or open-order reads; the account-state authority and reconciler own those REST observations.

## Exit

1. Strategy policy, protection overlay (`PROTECTION_SL` / `PROTECTION_TP` / `PROTECTION_TRAIL`), or mandatory-flatten timing emits `ExitIntent`/`FlattenIntent` or `CancelIntent`. Protection evaluates on Polymarket book updates via `on_public_fact`, even when the strategy scheduler does not wake. Polymarket has no native OCO: flatten/SL first cancel session-owned live ids (`cancel_order` / `cancel_orders`), wait out the ~250 ms taker delay with one retry, then sell. `cancel_all` is not used on the $5 path. Already-matched cancel reasons are inventory, not empty success.
2. After sitting orders are gone, the planner creates a `MarketSellOrderSpec`; quantity is the **current remaining** venue-legal sell size (confirmed position ∩ sellable ∩ trade net ∩ matched-exit HWM), never the first-exit clip and never collateral.
3. Remaining shares are rounded **down** to `lifecycle.sell_size_increment` (typically `0.01`). If that size is below book/binding `min_order_size`, the host does not POST.
4. The lifecycle submits bounded FAK retries and waits on event-driven evidence. A previous EXIT that is still open or ambiguous is reconciled before another POST.
5. Reconciliation confirms baseline-relative flatness. Only then is the session `COMPLETED_FLAT`.
6. If leftover is inside the explicit dust policy (`dust_abs_shares` / below `min_order_size` / `dust_max_notional`) and no orders remain open, the session is `COMPLETED_FLAT_WITH_DUST` with a persisted residual. Known dust on a resolved or non-selected token does not set `PRIOR_SCOPE_NOT_CLEAR`. Tradable or unknown leftover still becomes `MANUAL_INTERVENTION`.

Exit processing is not starved by entry/model readiness. Exit and flatten intents use the same wake/dispatcher path as entry. Each retry captures the current book and applies its own synchronization, freshness, price, and sellability checks. If tradable exposure remains at the configured manual deadline, the durable session becomes `MANUAL_INTERVENTION` and the run stops for operator action.

An exit submission is not a terminal result. Uncertain evidence or leftover that is still tradable or unclassified becomes `MANUAL_INTERVENTION`; it is never reported as `COMPLETED_FLAT`. A classified crumb is `COMPLETED_FLAT_WITH_DUST`.

Protection arms after confirmed exposure using the run-config `protection:` block and evaluates the held best bid. `take_profit.style` may be `reactive_fak`, `resting_gtc`, or `resting_gtd`. Resting styles POST a post-only SELL at the TP when bid is below the trigger; they do not also FAK-TP on book ticks. Bid already at/above TP, or GTD with fewer than 180 s to `event_end`, falls back to one reactive FAK. SL / trail / mandatory flatten cancel the resting TP first. See [protection.md](protection.md).

`HoldToResolutionIntent` is consumed: the runtime records `HOLD_TO_RESOLUTION` and does not mutate the venue. Automatic on-chain redeem / merge is not implemented. z_gap config with `resolution_capability_default: true` is rejected at load.

## Recovery

On startup, SQLite sessions are replayed. `terminal` (including `MANUAL_INTERVENTION`) is not the same as `safe_to_open_entry`. Restore keeps every session that is not safe to open: MANUAL, confirmed exposure, live owned ids, match-awaiting-confirmation, or an unresolved mutation.

Same-market unsafe sessions are cancelled (session-owned ids only) and, when confirmed exposure remains, flattened through the existing lifecycle/coordinator/reducer path. That recovery EXIT is allowed only while the session is MANUAL and `confirmed_position_shares > 0`. It never posts a BUY. The original `ManualInterventionRequired` row stays in the append-only journal; recovery events do not rewrite MANUAL to `COMPLETED_FLAT`. New trading stays blocked until a **scoped** observation of that session’s own market and token proves no material position, open order, or unresolved mutation.

A clear snapshot of the newly discovered window does not clear a retained old market. Foreign leftover is inspect-and-block (`block_foreign`); this process does not POST or cancel on the foreign token and cannot bind an old 5m slug.

## Shutdown drain

`run_market_data_runtime` `finally` calls `TradingRuntime._shutdown_execution` **once** (`_cleanup_lock`) **before** clock, public feeds, `feed_supervisor`, account authority, coordinator, or gateway close. Drain waits for an in-flight `submit()` (`_mutation_lock`), cancels session-owned live ids, reconciles, and flattens confirmed inventory. Stop during prepare closes the final BUY gate (`STOP_REQUESTED`) so POST does not start. Stop during POST waits for that mutation’s outcome or ambiguity and does not start another BUY. Incomplete recovery becomes `MANUAL_INTERVENTION` before resources close.

## Time and run boundary

The trading-duration clock starts at the prepared target window, not process composition. PTB readiness uses the measured boundary receive lag and the configured clock-uncertainty threshold. Candidate freshness and execution latency use monotonic timestamps; raw and corrected wall timestamps are display evidence only. The outer duration boundary never cancels a submission or reconciliation operation in flight; SDK and lifecycle timeouts bound those operations, and the runtime stops at the next safe loop boundary.
