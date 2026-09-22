# R1 — Execution completion and recovery

**Status:** Implemented (offline, 2026-09-20). **First-release required.** Recovery-only MANUAL flatten/cancel, `safe_to_open_entry`, once-only drain, F7 reverse-then-apply, F17 / F-INT shutdown tests, and F6 / F8–F9 / F14–F15 / F20–F22 / F21 are landed. Named F10–F13 / F16 / F18–F19 remain covered by existing maker/cancel/protection tests rather than standalone rows. Journal MANUAL is never rewritten to a completed phase.
**Depends on:** nothing.
**Reuse:** `ExecutionLifecycle`, `AccountExecutionCoordinator` (`apply` queue), `SessionReconciler`, reducer, SQLite journal, G4 `cancel_orders` / `CancelResult`, G1 `exit_sizing`.
**Bounded scope:** truthful session completion, owned maker/taker fill identity, once-only drain on every shutdown path, restore that blocks unsafe resume. Not a second OMS, not `--continue`, not redeem, not old-window rebind.

## Current behavior (verified)

A session can POST, then fail to reach a truthful terminal: maker fills are attributed as taker prints; zero-position working orders are not reconciled; shutdown closes feeds and the gateway **before** cancel/flatten; cancel + “not in open orders” is treated as done; `MANUAL_INTERVENTION` is treated as a finished session.

### Attribution (stream + REST)

`src/tyrex_pm/execution/polymarket/user_events.py` TRADE/FILL:

- `venue_order = taker_order_id or order_id or maker_order_id`
- size / price / side / token = **trade-level** (taker) fields

Gateway `_normalize_stream_event` (`gateway.py`) emits `TradeStatusObserved` from that. Reducer: unknown `venue_order_id` → `unowned_trade`. `confirmed_buy_shares` stays 0.

REST `_trade_order_id` (`reconciliation.py`) **does** find an owned `maker_orders[].order_id`, then still stamps trade-level side/size/price. Ownership can be right while the clip is wrong.

Same defect for **maker SELL** (resting GTD TP in `z_gap_tiny_live.yaml` / `submit_resting_limit`). Trade-level side is typically BUY (the taker); our clip is SELL.

No unit test feeds `taker_order_id != our_id` with a `maker_orders[]` clip. `test_maker_entry.py` only checks live POST returns.

Journal `TradeStatusObserved` persists the **already-wrong** fields. Original CLOB `maker_orders[]` is **not** stored. Replay cannot reconstruct the clip from old rows alone.

Reducer `_apply_confirmed_trade` adds `_confirmed_trade_delta` once (`position_applied=True`). There is no reverse-then-replace path today.

### Zero-position work ignored

`has_exposure` = `confirmed_position_shares > 0`. `working_maker_entry` is live accepted BUY with no confirmed buy.

`on_async_tick` reconciles only if `has_exposure`. Mandatory flatten / manual deadline also require `has_exposure`. Resting TP ids in `resting_limit_venue_ids` are skipped the same way if position projection is 0.

Recovery (`_bind_pending_market`): flatten only if `reconciled.has_exposure`; else ambiguous POST → manual; else `stop_requested` only when already terminal. Live unfilled owned id at position 0 is not cancelled.

### Terminal vs safe to resume trading (the current distinction)

`TERMINAL_PHASES` (`session_state.py`) includes `COMPLETED_NO_DISPATCH`, `COMPLETED_NO_FILL`, `COMPLETED_FLAT`, `COMPLETED_FLAT_WITH_DUST`, **and** `MANUAL_INTERVENTION`.

`ExecutionSessionState.terminal` is `phase in TERMINAL_PHASES`.

`TradingRuntime.can_promote()` is `state is None or state.terminal` — so MANUAL **can promote**.

Restore (`trading_runtime.py` ~299–303) keeps only `if not restored.terminal`. MANUAL sessions are **dropped from `restored_sessions`**, so `UNRESOLVED_PRIOR_MARKET_SESSION` does not fire for them.

Account snapshot can still set `prior_scope_clear=False` via `PRIOR_POSITION` / `PRIOR_OPEN_ORDER`. That is a different gate. Journal MANUAL alone does **not** currently block a new strategy session.

**Proposed:** keep `terminal` as “this session will not emit more strategy entries / reporting may treat the phase as finished.” Add `safe_to_open_entry` (name locked) that is false for MANUAL, exposure, live owned ids, ambiguous POST, match-awaiting-confirmation, or incomplete recon. Restore keeps sessions where `not safe_to_open_entry`. `can_promote` and `_bind_pending_market` use `safe_to_open_entry`, not `terminal`.

### Shutdown order (drain is too late and too narrow)

`run_market_data_runtime` `finally` stops public feeds (clock, Chainlink, Binance, L2, `feed_supervisor`) when the loop exits — including duration `break`, `should_stop` `break`, and **unhandled exceptions / `CancelledError`** (Python `finally` still runs).

`TradingRuntime.run` then: stop intent dispatcher (cancels the task) → `account_state_authority.close()` → `coordinator.close()` (user stream + `AsyncSecureClient`) → write report.

There is no cancel/flatten on that path. A callback **immediately before the normal `break`** would miss: `CancelledError` on the runtime task, exceptions in `run_market_data_runtime` outside the tick `try`, and any exit that skips the break site.

Final gate for SELL still needs a `BookView`. Drain **after** `feed_supervisor.stop()` cannot POST a legal flatten.

`_stop_intent_dispatcher` **cancels** the dispatcher. `submit()` already journals `SubmissionFailed(ambiguous=True)` on `CancelledError`, but drain must not race a second cancel/flatten while that POST is still in flight.

`apply()` is serialized on `AccountExecutionCoordinator._queue`. `submit()` and `cancel()` are **not** covered by that queue for the HTTP calls themselves — two concurrent `submit()`/`cancel()` can overlap.

### Cancel is not completion

`_cancel_open_orders`: cancel, optional 0.30 s delay retry, one REST reconcile, then if `resting_limit_venue_ids` empty → return. `submit_exit` then returns if `confirmed_position_shares <= 0`.

`CancelResult.fully_cleared` treats not-found and already-matched as cleared **of the live book**, not as no-fill. Absence from `list_open_orders` **alone** does not prove no fill.

### Discovery cannot rebind an old 5m window

`btc_updown_5m` (`market_family.py`) binds `current_btc_updown_slug()` and `prepare_next_btc_5m()`. CLI / `TradingRuntime` have no “select this historical slug / event URL” switch. `GammaMarketDiscovery` can resolve an `event_url` in isolation; the live one-shot path **does not** use it. Operator procedure must not say “run against that market’s window.”

## Owning modules

| Concern | Owner |
|---------|--------|
| Stream/REST clip | new `execution/fill_attribution.py`; call from `gateway.py` and `reconciliation.py` |
| Normalizer | `user_events.py` — keep payload-faithful (include `maker_orders`); do not pick taker id as “ours” |
| Phase / trades / correction | `reducer.py`, `session_state.py`, `evidence.py` |
| Cancel then prove | `lifecycle.py` (extend, do not fork) |
| Drain once, all exits | `market_data_runtime.py` `finally` **before** feed stop; `TradingRuntime` drain implementation |
| Submit vs drain race | `coordinator.py` mutation lock around `submit` / `cancel` / drain wait |
| Restore / promote | `trading_runtime.py` `safe_to_open_entry` |
| Journal | append-only `TradeAttributionCorrected` + existing `ManualInterventionRequired` + run-evidence `PRIOR_SESSION_INSPECTED` |
| Docs (when landing) | `Docs/latest/running.md`, `execution_lifecycle.md` |

## Smallest change

1. One `attribute_owned_fill(payload, owned_venue_ids, selected_token_id)` used by stream and REST.
2. Session predicate `needs_venue_followup` (exposure **or** live owned ids **or** ambiguous POST **or** match-awaiting-confirmation **or** unmatched cancel). Tick reconciles on that.
3. `safe_to_open_entry` vs `terminal` as specified above.
4. `drain_open_session(reason)` on `ExecutionLifecycle`: wait for in-flight mutation → cancel owned live ids → bounded evidence recon → flatten if inventory → `COMPLETED_NO_FILL` / flat / dust / `MANUAL_INTERVENTION`.
5. Invoke drain **once** from `run_market_data_runtime` `finally`, **before** stopping feeds, for duration, `should_stop`, exception, and cancellation.
6. Historical attribution: reverse applied contribution, then apply correction; duplicate corrections idempotent.
7. Foreign / earlier-market sessions: inspect + block using **supported** capabilities (below). Never delete SQLite.

Do not add a second OMS. Do not `cancel_all`. Do not rewrite journal rows.

## Locked decisions

### Fill identity (going forward)

- Taker: `taker_order_id` ∈ owned → trade-level size/side/price/token; token must equal session token else fail closed (recon incomplete, no guessed fill).
- Maker: exactly one `maker_orders[]` row with `order_id` ∈ owned → **that row’s** matched quantity, price, asset/token, and **our order’s side** (from `OrderExecutionState.spec.side`, not trade-level side).
- Both / neither / multiple owned clips → no confirmed trade; recon incomplete.
- Persist on `TradeStatusObserved` (going forward) optional clip fields used, so later replay is self-contained. Dedup remains `venue_trade_id` + status.

### Historical `TradeAttributionCorrected` (reverse then apply; idempotent)

Leave original `TradeStatusObserved` rows untouched. New evidence type, append-only, `dedupe_key` includes `venue_trade_id` + correction digest (new venue_order_id, side, shares, price, token).

**Reducer (serial, same `apply` queue):**

1. If `event.event_id` already in `applied_event_ids` → no-op (journal replay and duplicate appends).
2. Locate the working `TradeRecord` by `venue_trade_id`. If none → ignore correction (do not invent a fill).
3. If a correction with the **same digest** was already applied to that trade → no-op.
4. If the trade’s current working fields already equal the correction clip → no-op (idempotent).
5. If `position_applied`: subtract `_confirmed_trade_delta(state, trade)` using the **current working** fields, set `position_applied=False`.
6. Replace working venue_order_id / side / shares / price / token with the correction clip.
7. Apply `_apply_confirmed_trade` (adds the new delta, sets `position_applied=True` when CONFIRMED).
8. If the new position is negative or the clip conflicts with another already-corrected identity → do **not** guess; `MANUAL_INTERVENTION` `conflicting_trade_identity` / `unrepairable_trade_attribution`.

**Evidence that authorizes correction of the known normalization defect**

REST `list_account_trades` (read-only) yields `venue_trade_id` already stored, **and** the stored `venue_order_id` is **not** in the session’s owned venue ids, **and** exactly one `maker_orders[]` row has `order_id` in owned ids, **and** that clip’s token matches the session token. Source on the correction: `readonly_rest`. This is the known stream/REST taker-id defect.

**Conflicting evidence that must remain unresolved** (no correction apply; MANUAL if still material)

- REST clip and stored owned clip disagree on shares/price/side/token **and** both look owned.
- Two or more owned `maker_orders[]` rows on one trade.
- REST cannot find the `venue_trade_id`.
- Stream later reports a **different** owned clip for the same `venue_trade_id` after a REST correction.
- Correction would require inventing a token or side that is not on the owned clip or the session order spec.

Tests: replay wrong stream trade → REST correction → confirmed shares = clip; replay the journal a second time → same shares (no double reverse); duplicate correction event → no-op.

### Authoritative no-fill after cancel (all required)

1. REST: owned id not in open orders **and** cancel ack is `canceled` or venue status cancelled/expired/unmatched — **not** `matched_instead` / delay-pending.
2. No owned confirmed trade (stream+REST, attributed), including **SELL**.
3. `cumulative_matched_hwm == 0` and no `trade_ids_from_response`.
4. Token balance at session baseline (existing baseline-relative math).

If (1) holds but (2)–(4) do not within `evidence_timeout_s` at `reconciliation_interval_s` → keep `RECONCILING`, then `MANUAL_INTERVENTION` (`cancel_fill_unproven` / `entry_evidence_timeout`). Never `COMPLETED_NO_FILL`.

If `matched_instead` or owned clip appears → inventory; G1-sized FAK; not a second BUY.

### Shutdown: once, while execution resources remain, no race with submit

**Where (locked):** `run_market_data_runtime` `finally`, **before** `clock_loop.stop` / feed / `feed_supervisor.stop`. That `finally` already runs for duration expiry, `should_stop` (Ctrl+C sets `TradingRuntime.stop_requested`), unhandled exceptions in the loop, and task cancellation. Do **not** rely on a callback immediately before `break`.

**What:** `on_shutdown` / drain callback implemented by `TradingRuntime`:

1. Acquire process-level `_cleanup_lock`. If `_cleanup_done`: return. Set `_cleanup_done`.
2. Set `stop_requested` so evaluate/consume refuse new BUY.
3. **Do not cancel** the intent dispatcher yet. Set dispatcher stop flag and **await the current `submit()`** (dispatcher loop exits after the in-flight consume). Coordinator adds `asyncio.Lock` `_mutation_lock` held for the whole `submit()` and `cancel()` body so drain cannot start HTTP cancel while POST is outstanding.
4. `await lifecycle.drain_open_session(reason)` while user stream, gateway, account authority, and public books are still up.
5. Then stop the intent dispatcher (idempotent).
6. Return; market-data `finally` then stops feeds. `TradingRuntime.run` then closes account authority, coordinator, writes report.

If drain is incomplete when budgets expire: `mark_manual` **before** returning from drain so close never leaves silent `OPEN_EXPOSURE`.

**Outstanding submission:** if `SubmissionAttempted` has no response yet, wait until `submit()` journals `SubmissionResponseObserved` or `SubmissionFailed(ambiguous=True)`, then recon. Do not POST a second BUY. Do not cancel the HTTP/2 create mid-flight (existing G2). After the attempt is durable, drain may cancel **session-owned live ids** if a venue id exists.

`TradingRuntime.run`’s existing post-loop `_stop_intent_dispatcher` must be idempotent with step 5 (already true if the task is None/done).

### Uncertainty vs new entry after restart

`entry_allowed` / `lifecycle.open` remain false while any restored session is not `safe_to_open_entry`, including `MANUAL_INTERVENTION`, or the **scoped** leftover check below is incomplete or dirty.

A later process opening a **new** session while MANUAL exists is currently possible (MANUAL dropped at restore). After this task it stays blocked until the retained session’s **own** market and token are freshly observed clear **and** journal uncertainty is absent.

### Prior / other-market retained sessions (executable procedure)

Host already sets `UNRESOLVED_PRIOR_MARKET_SESSION` / `MULTIPLE_RESTORED_SESSIONS` for restored sessions it still holds. After restore includes unsafe terminals, MANUAL is in that set.

On every startup, for each restored session that is not `safe_to_open_entry`, persist run evidence `PRIOR_SESSION_INSPECTED` with: session_id, market_id, window_id, token_id, phase, open venue ids, confirmed_position_shares, mutation ambiguous?, `host_action`.

**Supported capabilities (do not invent others):**

- Read-only journal replay and report.
- REST recon / G4 cancel **only** for the **currently bound** market’s session-owned ids (do not POST/cancel on a foreign token).
- `AccountStateAuthority.prepare` / `current` / `_capture_once`: token balances for **specified** token ids and open orders for **one specified** market (`account_state.py`). This is **not** wallet-wide. A clear snapshot of the newly bound window proves nothing about a retained session’s market.
- Discovery: current + next 5m window only.

| Situation | `host_action` | Operator procedure |
|-----------|---------------|--------------------|
| Same market + token as the **currently discovered** window, confirmed exposure | `resume_flatten` | Next `--live` same `state_directory`; confirm flatten in report. If MANUAL after drain, inspect venue balance + open orders; do not delete SQLite. |
| Same market, live maker BUY/SELL, position 0 | `resume_cancel_then_prove` | Same; expect cancel + no-fill or flatten late fill. |
| Same market, ambiguous POST | `block_manual` | Process does not open a new strategy session. Operator: CLOB open orders + trades for token vs journal `attempt_ids`. Resolve leftover **on the venue UI**. New entry forbidden until `safe_to_open_entry` or account leftover is gone **and** journal session is no longer blocking (see durable clear). |
| Different market/window than active bind | `block_foreign` | **Do not** flatten or cancel the foreign token from this process (wrong binding/books). **Do not** select that old window — CLI/runtime cannot bind it. Inspect journal + venue UI for **that** condition_id. Cancel leftover live orders on the venue. Wait for 5m expiry/resolution. Unredeemed tokens may remain (`PRIOR_POSITION`); first release has no redeem. Never `DELETE` sqlite. |
| Multiple unsafe restored | `block_multiple` | Inspect all session_ids in the inspect events. Do not open a new strategy session. |

No `reset-state` CLI. No inspect CLI required in R1 (R5 documents sqlite + report). No “run `--event-url` against the old slug.”

### How recovery / settlement resolution becomes durable

Journal `MANUAL_INTERVENTION` is **never rewritten** to a completed phase.

`AccountStateAuthority.current(bound_market_id)` only contains balances for that target’s `token_ids` and open orders for that `market_id`. **Do not** clear a foreign session from a clean snapshot of the new window.

Durable facts are **new** append-only evidence plus a **scoped** observation of the retained session:

1. Each process start appends `PRIOR_SESSION_INSPECTED` (run evidence, not a reducer phase change).
2. For every restored session that is not `safe_to_open_entry`, `prepare`/`current` (or one equivalent capture) the session’s `identity.market_id` and `identity.token_id`. Reuse existing account-read operations. Do not bind that window for trading.
3. **Keep `UNRESOLVED_PRIOR_MARKET_SESSION` (block new entry)** if any of the following hold:
   - identity missing (`market_id` / `token_id` unknown)
   - snapshot missing, `UNAVAILABLE`, `STALE`, or `snapshot.market_id` ≠ session market
   - session token not in `snapshot.token_ids`
   - that snapshot reports `PRIOR_POSITION` on the session token (non-dust) or `PRIOR_OPEN_ORDER` on the session market
   - journal uncertainty: `has_unresolved_mutation_attempt`, `entry_match_awaiting_confirmation`, or live owned venue ids still unproven cancelled
4. **`foreign_inventory_absent` (stop blocking new entry on the current window)** only when **all** of the following hold:
   - session market ≠ currently bound market
   - snapshot is fresh and `read_complete` for **that** market and token
   - no leftover position/open order on **that** market and token (dust exemptions unchanged)
   - no journal submission/fill uncertainty (step 3)
   The MANUAL row stays in sqlite for audit.
5. **Same-market MANUAL** (discovered current window still matches the session market_id): never auto-clear; drain or stay blocked.
6. Missing, stale, or wrong-market evidence is **not** clearance. Tokens that remain after expiry keep blocking via the **scoped** snapshot until they disappear (out-of-band redeem / dust).

## Fixture scenarios (deterministic, fake gateway)

Reuse `_MakerVenue` / `_Gateway` style from `test_maker_entry.py`, `test_cancel_path.py`, `test_execution_authority.py`. Do not import `AsyncSecureClient`. Do not set `entry_executable=True` to skip recovery.

Each row: **inputs → events → state → POSTs → outcome**.

| ID | Inputs / events | Expected |
|----|-----------------|----------|
| F1 | Stream TRADE: `taker_order_id=foreign`, `maker_orders=[{order_id:our, matched:3, price:0.40, asset:yes}]`, our ENTRY BUY live | One `TradeStatusObserved` venue_order=our, side=BUY, shares=3, price=0.40, token=yes. `confirmed_buy_shares=3`. Not `unowned_trade`. POST count BUY=1. |
| F2 | Same as F1 but our order is EXIT SELL (resting TP) | side=SELL, `confirmed_sell_shares=3`. BUY POST=0 additional. |
| F3 | We are taker: `taker_order_id=our`, trade size 5 | Taker fields; BUY shares=5. |
| F4 | REST trade size 10, maker clip 4 | confirmed=4. |
| F5 | Two owned maker clips on one trade | no confirm; recon incomplete; no flatten guess. |
| F6 | Stream F1 then REST same `venue_trade_id` same clip | one trade ledger row; no `conflicting_trade_identity`. |
| F7 | Stream F1 (wrong taker id) **already in journal**, then REST owned clip | original event retained; `TradeAttributionCorrected`; reverse then apply; working shares=clip. Second replay of the same journal: same shares. Duplicate correction: no-op. |
| F8 | TRADE (our venue id) **before** `SubmissionResponseObserved` | pending until response **or** recon attaches; then confirm; not dropped; BUY POST=1. |
| F9 | Working maker BUY, position 0, tick | `reconcile` runs; phase pending; still live. |
| F10 | Working maker, mandatory-exit tau, REST: not open, no trades, baseline balance | after proof loop: `COMPLETED_NO_FILL`; BUY POST=1; SELL POST=0. |
| F11 | Cancel ack `canceled`, 200 ms later maker TRADE clip 2 | SELL FAK remaining legal size; not `COMPLETED_NO_FILL`; not second BUY. |
| F12 | Partial BUY HWM 2 of 5, cancel remainder, REST clip 2 | one SELL of legal(2); not 5. |
| F13 | Resting TP live SELL, flatten/SL | cancel that id then FAK leftover (G4+G1); no `cancel_all`. |
| F14 | `post_order` raises after attempt (existing uncertain) | `ambiguous`; recon; if REST empty+unfilled proof → no-fill; if timeout → `MANUAL_INTERVENTION`; BUY POST=1. |
| F15 | Restore live maker, position 0, same market | inspect event `resume_cancel_then_prove`; drain; sqlite row count not decreased. |
| F16 | Restore other `market_id` | inspect `block_foreign`; `entry_allowed` false; **no** cancel_all; **no** POST on the foreign token; **no** attempted old-window bind. |
| F17 | Duration/stop with live maker + books still available | drain then feed/gateway close; timestamps: cancel/recon **before** close. |
| F18 | Open orders empty after cancel, but REST trade appears inside `evidence_timeout_s` | treat as fill (F11), not no-fill. |
| F19 | Open orders empty, no trades, balance **above** baseline after cancel | not `COMPLETED_NO_FILL`; `MANUAL_INTERVENTION` or recon until timeout then manual. |
| F20 | Restore `MANUAL_INTERVENTION` on **other** `market_id`/`token_id`; **scoped** snapshot of that market+token is fresh, complete, zero position, no open orders; journal has no unresolved attempt/match | inspect `foreign_inventory_absent`; new entry on current window allowed; journal still MANUAL. A clear snapshot of the **current** window alone is **not** sufficient. |
| F20b | Restore `MANUAL_INTERVENTION` on old market with confirmed exposure or open orders; **current** market snapshot is clear (no PRIOR_*) | `UNRESOLVED_PRIOR_MARKET_SESSION`; `entry_allowed` false; **no** new BUY. Regression: new-window clear ≠ old-window clear. |
| F20c | Restore foreign MANUAL; current market clear; old-market snapshot `STALE` / `UNAVAILABLE` / wrong `market_id` | stay blocked (missing/stale/wrong-market evidence). |
| F20d | Restore foreign MANUAL; scoped old-market snapshot clear; journal `has_unresolved_mutation_attempt` or match-awaiting | stay blocked (submission/fill uncertainty). |
| F21 | Restore `MANUAL_INTERVENTION` same market with account `PRIOR_OPEN_ORDER` on **that** market | `safe_to_open_entry` false; no new BUY. |
| F22 | Conflicting REST vs owned stream clip | no correction apply; MANUAL `unrepairable_trade_attribution` or `conflicting_trade_identity`. |

### Interruption while resources are still up (required)

Drive the **real** market-data loop + lifecycle + fake gateway. Do not simulate “pre-break only.” Cancel/stop the runtime task while it is still inside `run_market_data_runtime`.

| ID | When | Expected |
|----|------|----------|
| F-INT-WAIT | Stop during the 1 s loop sleep; no open session | drain no-ops; feeds stop after on_shutdown; no POST; `_cleanup_done` so a second finally is a no-op |
| F-INT-PREPARE | Stop while `prepare_order` is awaited | **Final BUY gate refuses immediately** (`STOP_REQUESTED` / shutdown); `post_order` not called even though prepare later returns. Setting `stop_requested` and cancelling the dispatcher is **not** enough — the gate must see the shutdown flag. Drain may then run; no second BUY. |
| F-INT-POST | Stop while `post_order` is awaited (no HTTP result yet) | `SubmissionFailed` ambiguous or response if it won the race; then recon; **not** a second BUY; drain waits on `_mutation_lock`; terminal no-fill **only** if four-part proof, else MANUAL |

Existing `test_cancelled_post_is_durably_ambiguous_before_cancellation_propagates` stays; extend rather than replace.

## Failure / bounded recovery

| Failure | Bound | Terminal |
|---------|-------|----------|
| Entry evidence missing | `evidence_timeout_s` (15), starting **after** POST returns | `MANUAL_INTERVENTION` `entry_evidence_timeout` |
| Cancel unproven | same timeout, `reconciliation_interval_s` polling | `MANUAL_INTERVENTION` `cancel_fill_unproven` |
| Exit retries | **one** `exit_retry_budget_s` wall clock **and** `exit_retry_limit` (not multiplied) | existing `exit_retry_budget_exhausted` |
| Cancel still live | G4 retry + delay | `cancel_still_live` manual |
| Unrepairable old attribution | one REST pass | `unrepairable_trade_attribution` manual |
| Drain incomplete at close | mark_manual before returning | `MANUAL_INTERVENTION` |

No second BUY in any row.

## Acceptance

1. Maker BUY and maker SELL fills (stream+REST) use owned clip quantity/token/side/price.
2. Duplicate stream/REST same trade does not double count or conflict when clips match.
3. Fill-before-response attaches to the owned order.
4. `needs_venue_followup` sessions reconcile at position 0.
5. Cancel completion uses the four-part no-fill proof, not wait-or-missing-open-order.
6. Drain runs once in market-data `finally` before feed/account/gateway close; F17 + F-INT-* prove exceptional paths, not only a pre-break callback.
7. In-flight submit is waited/serialized; F-INT-POST does not race a second mutation.
8. Wrong historical journal trades: reverse then apply; originals kept; replay and duplicate corrections idempotent.
9. `terminal` ≠ `safe_to_open_entry`; MANUAL blocks new trading except documented **scoped** `foreign_inventory_absent` (F20). F20b–F20d stay blocked.
10. Foreign sessions: inspect + block; **no** old-window rebind; sqlite not deleted; clearance requires a fresh complete observation of the **retained** market and token, not the new window.
11. Uncertainty → no new entry; budget expiry → `MANUAL_INTERVENTION`.
12. Resting TP (maker SELL) cancel-then-flatten covered (F13).

**Evidence to retain:** fixture test names F1–F22 and F-INT-* in `tests/unit/execution/` (and runtime interruption tests as needed); when landing, `Docs/latest/execution_lifecycle.md` drain order + `running.md` prior-session table.

## Exclusions

Raise $5; `cancel_all`; redeem; new host; Z-Gap economics; R6 `--continue`; deleting state; `feature_probe` live; CLI bind-to-old-slug.
