# R5.5 — Automatic Startup Reconciliation and Guided Recovery

**Status:** Implemented for standard binary settlement when `TYREX_POLYGON_RPC_URL` is a real Polygon mainnet endpoint. Chain 137 and a read-only CTF call were verified without printing that URL. The copied-state `--check-account` failed closed before any authenticated venue read because Polymarket API hosts presented a certificate for `*.anj.fr`. The three historical sessions remain unclassified. Neg-risk markets stay unresolved: official docs put them on Conditional Tokens, but pinned SDK 0.5.0 does not prove the held token's `payoutNumerators` slot or whether the condition is a named outcome, a placeholder, or Other. Not live-validated. Pinned `polymarket-client` 0.5.0 `Market.resolution` still has no payout vector.
**Reviewed:** 2026-09-23 against `80aeca24f11d9bd83d3ca99d6ed9a3afa7d4fb0d`.
**Depends on:** R1 execution/recovery, R5 evidence/reproducibility.
**Sequence:** R1–R5 → R5.5 offline gate → renewed bounded R7 validation → R6 when scheduled.
**Owner:** Shared host/execution infrastructure; applies to every registered strategy.

This plan extends R1's startup recovery policy. Its explicit evidence-backed operational clearance and approved historical-market binding supersede R1's permanent foreign/manual startup block only when implemented and accepted. Other R1 safety rules remain in force. Earlier task files describe their original baseline; do not mistake this planned behaviour for shipped functionality.

## 1. Objective and definition of done

Starting a strategy must automatically establish whether the configured account is ready for new trading. The operator selects a strategy and configuration; the application inspects prior sessions, current holdings, orders and market status. It reconciles cases supported by authoritative evidence, explains remaining cases in ordinary language, and offers specific supported recovery actions. The operator must not inspect or edit SQLite to perform normal recovery.

The first implementation is complete when it can:

1. Detect old bot positions closed outside the bot, cancelled/expired orders, unresolved submissions, active inventory, pending resolution and finalized settlement holdings.
2. Discover account holdings/orders absent from the local journal within the verified API visibility scope.
3. Clear an operational blocker automatically only after the required evidence is journaled and reduced.
4. Offer scoped cancel/close recovery for uniquely attributable old bot sessions, including a tradable market different from the newly requested window.
5. Recheck the account after recovery and start the requested strategy automatically if clear.
6. Produce useful reports when startup blocks, even if no market or execution session was opened.
7. Preserve the existing one-shot behaviour, R1 safeguards and strategy-plugin interface.

This is a finite operational feature, not a new portfolio engine. Automatic redemption, transfers, arbitrary manual-position liquidation, concurrent strategies, R6, F-L2, strategy retuning and unlimited retries are excluded. Resolved holdings must be classified now; submitting redemption transactions is deferred. Unknown evidence cannot be bypassed by an operator “continue anyway” choice.

## 2. Code findings that determine the design

Paths below are relative to the repository root. Read the named functions before implementing.

- `runtime/trading_runtime.py::create` replays every execution session, retains states where `safe_to_open_entry` is false, and creates a shared coordinator, lifecycle and `AccountStateAuthority`. It currently hardcodes live capability true. Inspection must not reuse that assumption.
- `_inspect_retained_session` immediately blocks foreign sessions with `has_journal_uncertainty`; other foreign sessions receive only scoped balances/open-order checks. It does not resolve their lifecycle or uncertain attempts.
- `_bind_pending_market` performs recovery after binding the current market, may enqueue `CRASH_RECOVERY_FLATTEN`, and installs a stream scoped to that market/session. Foreign recovery cannot safely reuse the current market's book or token context.
- `execution/reconciliation.py::SessionReconciler.reconcile` lists open orders, imports strictly owned trades through `attribute_owned_fill`, and calculates exposure as the maximum of account delta and remaining session trade obligation. This intentionally prevents a lagging zero balance from erasing a confirmed BUY. An external sale/redemption is currently not represented in that obligation.
- Listing open orders does not obtain the terminal status of an order absent from that list. Explicit lookup of each unresolved known order is required.
- `execution/reducer.py` preserves a prior MANUAL phase after ordinary evidence. `session_state.py::safe_to_open_entry` rejects MANUAL and unresolved mutations. Do not remove these protections globally.
- `SubmissionFailed.ambiguous` is not cleared by the current ordinary snapshot path. Recovery needs an explicit, independently justified attempt-resolution event.
- `persistence/execution_journal.py` is the authoritative append-only event store. `run_evidence_journal.py` explicitly forbids using reporting evidence as execution authority.
- `PolymarketAsyncGateway` already provides async balance, paginated order/trade reads, preparation, POST and cancellation. `AccountStateAuthority` serializes shared-client work; its comments document Windows cancellation/HTTP2 problems.
- `FinalExecutionGate` checks current binding, token, book age, tick and price after signing. Recovery must keep these checks and cannot pretend an old token belongs to a new window.
- `run_report.py` schema 4 assumes one principal execution session. Historical recovery and current-strategy results need separate projections.

### Dependency verification before implementation

`pyproject.toml` pins `polymarket-client==0.5.0`. Read-only inspection during this planning pass found version **0.2.0** in `C:/Users/windo/miniconda3/python.exe`. That environment exposed `get_order`, `list_positions`, `list_closed_positions`, `list_activity` and paginated order/trade methods, but those inspected signatures are **not** acceptance evidence for the pinned version.

Use an isolated environment matching the project pin. Confirm response shapes, wallet identity, pagination, market lookup and finality semantics there. Do not silently change the dependency pin or copy current online SDK examples into the code without contract tests. Record exact Python/SDK versions with validation. The untracked `scripts/check_release_candidate.py` seen during review is outside this documentation change.

## 3. Locked architectural rules

1. Keep one `TradingRuntime`, coordinator, gateway and serialized mutation path. No strategy-specific startup branches or parallel recovery OMS.
2. Strategies keep the existing `evaluate()` interface and `DecisionContext.entry_allowed`. They never read account APIs, ask questions or modify journal state.
3. All automatic startup operations are read-only at the venue. Local durable evidence writes are permitted and required. `--live` alone does not authorize newly introduced foreign-session liquidation; that requires the scoped recovery approval described below.
4. Existing in-run protective exits and shutdown draining remain authorized by the original live run. Do not prompt during an emergency exit or delay its POST with interactive input.
5. Preserve original trades, manual incidents and baseline quantities. Append new evidence; do not delete sessions, manufacture trades or reset ambiguous flags directly.
6. Establish readiness from typed observations with wallet/condition/token scope, freshness and completeness. Never from CLI text, report JSON or a stored boolean alone.
7. Startup scans and recovery execute before strategy dispatch. Once trading starts, maintain readiness in the background and at final BUY authorization; no account-wide network scan on each strategy tick.
8. Missing data, truncated history, an expired deadline, authentication scope gaps and contradictory sources mean unknown, never zero.
9. Repeated startup and crash/restart during recovery must be idempotent. An ambiguous recovery POST is investigated before another SELL can be attempted.

## 4. Module ownership and interfaces

Keep proposed new modules small; do not create a general workflow framework.

### Execution domain

Add `execution/startup_reconciliation.py` for immutable normalized types and classification/reconciliation orchestration:

- `AccountIdentity`: chain/environment, effective position-owning wallet, authenticated visibility scope; signer is recorded separately and must not substitute for the wallet.
- `ReadCoverage`: source, requested identity/filter/time range, start/end UTC, receive monotonic time, pagination completion, source watermark where provided, errors and visibility limitations.
- `MarketLifecycleObservation`: canonical condition ID, both token IDs, display title/slug, tradability, final resolution evidence, per-token payout when final, source references. States: `TRADABLE`, `CLOSED_PENDING_RESOLUTION`, `RESOLVED_FINAL`, `UNKNOWN`.
- `AccountInventoryObservation`: normalized holdings, orders, scoped balances and available collateral, with coverage attached. Preserve all nonzero quantities; default API dust filters must not silently hide them.
- `StartupIssue`: stable issue ID, affected sessions/account/market/token, classification, evidence references, reason, permitted actions and any display estimate explicitly marked estimated.
- `StartupAssessment`: generation ID, account identity, session event-count fence, checked time, validity deadline, issues, resolved cases and disposition `READY`, `ACTION_REQUIRED` or `BLOCKED`.
- `RecoveryProposal`: assessment fingerprint, session/condition/token, action, exact owned order IDs, maximum sell quantity, minimum acceptable price, expiry and retry budget.
- `RecoveryDecision`: accepted/declined proposal identity; no general-purpose override.

Proposed service operations: `inspect() -> StartupAssessment`, `build_proposal(issue_id)`, and `recheck(assessment)`. Venue data collection uses adapters; classification is deterministic and independently testable. Execution events are applied through `coordinator.apply()`.

Add `runtime/startup_controller.py` to coordinate inspection, user decisions and recovery using the existing execution components. The controller owns a temporary startup/recovery phase, not another trading engine.

### Adapters and account authority

Extend `execution/polymarket/gateway.py` with normalized async read methods for:

- Effective account identity and read visibility.
- A known order by exact venue ID.
- Fully paginated account open orders, positions and relevant activity.
- Exact historical market lookup by canonical condition/token identity.
- Final resolution/payout verification for the supported standard binary markets.

Keep SDK models and any necessary explicit public/on-chain read adapter under `adapters/polymarket/`. Reuse discovery normalization where valid, but do not assume Gamma's market `id` parameter accepts a CLOB condition ID. Resolve and validate that mapping. Never derive finality just from a slug, end timestamp, a price near 0/1, `closed=true`, or an indexer's `redeemable` flag.

If the pinned SDK cannot provide finality proof, add the smallest read-only adapter for canonical condition payout evidence, with verified contracts/chain and fake transport tests. No redemption/signing capability is needed for this reader. Unsupported market mechanisms remain explicitly unknown; do not generalize to all Polymarket market types.

Extend `AccountStateAuthority` to coordinate startup reads under the shared-client guard. Reuse its freshness/retry policy and normalization. Avoid recursive acquisition of the single-flight lock. Partial pagination or history coverage must return a typed incomplete observation rather than an empty collection.

### Existing state and reporting

Extend `evidence.py`, `reducer.py`, `session_state.py`, `exit_sizing.py` and `reconciliation.py` for the evidence/state rules in section 7. Update all event registries and serializers.

Extend `capabilities.py`, `final_gate.py` and `trading_runtime.py` to consume assessment validity and scope. Extend `reporting/run_report.py` and add `reporting/startup_report.py` for pure projections.

Add `application/startup_interaction.py` for terminal presentation and injected async decision input. CLI owns wording and input; execution code must never call `input()`.

## 5. Account identity, coverage and concurrency

Before inspecting a session, establish the actual position-owning wallet from authenticated SDK configuration. Persist a nonsecret journal identity binding (chain/environment/wallet) in journal metadata. Check it on every startup; a mismatch blocks before recovery or strategy entry.

Legacy journals have no wallet binding. Establish it from durable identity where available and verified ownership of known venue orders/trades. If that cannot uniquely establish ownership, present a one-time explicit legacy-journal association choice showing the configured wallet and affected sessions. Association records provenance only: it cannot classify unknown order outcomes, authorize liquidation, or clear exposure. Sessions without provable ownership remain blocked.

Acquire a portable OS-backed exclusive process lock for this state directory before journal writers/startup recovery. Store diagnostic PID/wallet metadata, but use the OS lock, not a stale PID file, as the exclusion mechanism. Support Windows and release only after draining and closing. This protects cooperating processes sharing the directory; it does not claim to exclude other machines or a second directory. Keep concurrent independent traders on the same account unsupported and detect external changes through reconciliation.

Inventory is the union of retained journal tokens/sessions, current wallet positions and all visible open orders. Retrieve all pages and request zero holding threshold where supported. Explicitly check each known token even when absent from indexed positions. List closed positions/activity as supporting history, not as proof that every order settled.

Scope matters: authenticated order access can be credential/session-key restricted. If the runtime cannot establish adequate visibility for the account being traded, return `ACCOUNT_VISIBILITY_INCOMPLETE`; never claim account-wide clearance from a restricted empty list.

Correlate by `(chain, wallet, condition_id, token_id)` and exact order/trade/transaction identifiers. Names, approximate sizes and timestamps alone do not establish ownership. Multiple unresolved sessions sharing the same token require an unambiguous allocation of evidence; one external disposal cannot clear both.

## 6. Inspection algorithm and outcome policy

Run these stages with all BUY paths blocked:

1. Validate config/output directory, authenticate without trading, verify wallet binding and acquire the process lock.
2. Restore journal states and start the coordinator/evidence writer; create account authority with mutation permission explicitly false for inspection-only mode.
3. Scan account inventory and fetch exact metadata for the union of relevant markets.
4. Investigate every unresolved attempt and known order, including orders missing from open-order lists. Import strictly owned fills using R1 attribution.
5. Investigate remaining balance/trade discrepancies using activity and finality evidence. Classify each issue according to the cases below.
6. Persist validated execution evidence, recompute states, and construct one complete assessment covering all issues. A crash halfway through must never leave a persisted global ready flag.
7. If ready, refresh collateral and final account coverage, persist the assessment projection, then compose current/next strategy-market feeds and dispatch.
8. If action is needed, present one grouped summary and scoped choices. If blocked, explain the missing fact and offer refresh/stop; do not consume an entire five-minute window waiting to discover that startup cannot proceed.

### Classification rules

- **Clean account / previously completed session:** no new action; require fresh account coverage. Proceed.
- **Cancelled/expired/rejected order, no fill:** exact order proof plus trade settlement and balance reconciliation; absence from open orders alone is insufficient. Resolve the attempt, then clear automatically.
- **Manually sold, transferred, merged or redeemed position:** correlate confirmed external disposal with the exact holding, account, chronology and quantity; reconcile current balance and all bot orders. Record external adjustment, then clear if no tradable obligation remains. Label the actual evidenced cause, not always “manual sale.”
- **Zero balance but unresolved BUY/SELL:** investigate exact order identity and trade settlement; never clear from repeated zero snapshots alone. A missing order ID may be recovered only using a canonical venue order hash/identity already persisted or reconstructible from validated signing data. The current `_signed_digest` must not be assumed to be that hash. A 404 is not rejection evidence. If exact evidence remains unavailable, stay blocked with a useful reason.
- **Tradable, uniquely owned old bot position:** action required; offer cancel-and-close with a quantity ceiling and explicit price floor. Do not extend the new strategy's $5 entry budget to authorize selling an arbitrary historical holding.
- **Owned live order without inventory:** offer cancel-and-reconcile. A cancellation race that creates inventory produces a new proposal unless the approved cancel-and-close proposal already covers the resulting quantity.
- **Closed but unresolved/disputed market:** classify as awaiting resolution; keep new entry blocked in R5.5. Explain that liquidation may be unavailable; offer refresh/stop.
- **Finalized winning/partial-payout tokens:** classify as settlement holdings, not active tradable exposure. Permit new entry only after finality, absence of unsettled orders/trades, and sufficient actual collateral are proven. Report redemption pending; do not credit the expected payout to cash.
- **Finalized zero-payout tokens:** retain token quantities and final payout evidence in the report; exclude the settled quantity from tradable exposure only after all execution uncertainty is resolved.
- **Classified dust:** reuse R1 quantity/notional rules and scoped exemptions; do not relabel an ordinary old position as dust merely because trading ended.
- **External inventory or orders without unique bot ownership:** report them explicitly and block new entries. No automatic adoption, cancel-all, or account-wide liquidation. Display market links and precise context when external resolution is needed; the operator never needs SQLite.
- **Unknown/stale/conflicting evidence or missing identity:** block with source-specific reason, bounded retry and refresh/stop choices.

The same policy applies to same-market and foreign-market startup findings. Replace the old automatic same-market startup flatten branch with this assessed/approved route; keep emergency recovery during an already authorized run unchanged.

Final settlement classification also applies to externally discovered holdings when account identity and finality are proven; they need not be adopted into a bot session. The external-inventory blocking rule concerns active, pending-resolution or unknown holdings/orders. Any unresolved bot attempt associated with that market still blocks independently.

Cross-source reads are not an atomic venue snapshot. After importing activity, perform a closing balance/order read and reject material changes during the assessment. Correlate settlement/transaction finality and pending trades explicitly. Two agreeing reads improve consistency but cannot replace missing order proof or overcome limited visibility.

## 7. Durable evidence and reducer changes

### Separate history from present readiness

Keep the original `MANUAL_INTERVENTION` incident and original fill ledger. Add a separately projected operational disposition on each session: `UNRESOLVED`, `CLEARED_FLAT`, `CLEARED_DUST`, or `SETTLEMENT_ONLY`, with proof references and an event-count fence. This is how a previously manual session can become operationally cleared without rewriting its historical outcome.

Add narrowly typed evidence (names may be adjusted consistently):

1. `OrderAttemptResolved`: local order/attempt ID, exact venue identity if accepted, terminal/current order result, settled trade references, coverage and proof reason. It may clear an ambiguity flag only for the named attempt. Preserve the original failure event. Multiple attempts require separate resolution.
2. `ExternalPositionAdjustmentObserved`: wallet/condition/token, disposal type, quantity, external transaction/activity identity and finality, unique session allocation, evidence cutoff. It does not create a `TradeStatusObserved` under a bot order. Conflicting or double-allocated adjustments block.
3. `MarketSettlementObserved`: validated final market resolution and token payout mapping, source/block references, settled residual quantity. Do not encode a price assumption as finality.
4. `SessionOperationalClearanceRecorded`: disposition, references to the applied proof events, accounted quantity, unresolved-attempt/open-order checks and expected session event count. Reducer validates those prerequisites; an unbacked “clear” event is rejected.
5. `SessionOperationalClearanceRevoked`: reason and contradicting evidence references. Relevant new exposure, unknown orders, reopened uncertainty, changed allocation or invalidated finality must invalidate operational clearance.
6. `RecoveryActionAuthorized` / `RecoveryActionFinished`: one-use scoped proposal, approved limits, account/session identity, expiry and resulting evidence references. Authorization is audit evidence, never a substitute for the final execution gate.

Use the existing execution journal for session authority. Account-wide scan diagnostics and discovered unowned inventory may be projected through `RunEvidenceRecorder`, but such records never authorize a BUY. The live assessment is computed from normalized observations plus reduced execution states; per-session clearance proof is in execution events.

### Quantity calculation

Extend the conservative trade obligation with **uniquely proved external disposals**. For a fully attributable holding, the obligation is the existing remaining-trade obligation less nonduplicated confirmed external reductions, floored at zero. Reconcile that with the actual scoped balance delta using the existing conservative maximum. Do not subtract the same sale once as an owned trade and again as external activity.

Never subtract a whole wallet sale from each session sharing a token. Ambiguous allocation, nonzero unexplained baselines, or unexplained balance increases remain blocked. Transfers can prove quantity movement without proving a sale price; cashflows/PnL remain unknown as appropriate.

For `SETTLEMENT_ONLY`, preserve actual token quantity in a distinct settlement holding projection. Do not set the wallet balance to zero or manufacture a SELL. The active trading obligation is discharged only by final settlement evidence plus completed order/trade reconciliation. Validate all quantity helpers (`remaining_exit_shares`, `trade_remaining_after_exits`, `has_exposure`, `needs_venue_followup`) so a later reconciliation does not resurrect externally disposed or finalized inventory.

### Readiness and replay

`safe_to_open_entry` may accept a structurally valid operational clearance on a historical MANUAL session only when its specific quantity/order/attempt prerequisites still hold. Ordinary manual states remain unsafe. Do not use a permissive `manual and balance == 0` shortcut.

Replay reconstructs historical clearance, but every new process starts with `STARTUP_CHECK_PENDING`. Fresh assessment is required before BUY. A snapshot's monotonic clock cannot be reused across processes. Validate clearance against relevant event-count/generation fences and invalidate it on newly contradictory evidence before dispatch. Benign repeated observations need not revoke it, but cannot extend its freshness without a completed check.

Update `release_terminal()`, restored-session filtering, `_bind_pending_market`, `can_promote`, and `prior_session_risk` computation together. Never remove a retained session merely because `.terminal` is true. Distinguish historical prior risk from current unresolved risk in reports.

Extend serialization with backward-compatible missing-field defaults; test real schema-1 journal replay with new events. Keep old rows immutable. If journal metadata/schema must change, provide an explicit transactional migration and reject unsupported newer schemas; do not silently rewrite history.

## 8. Guided recovery through the existing execution path

Recovery is serial and completes before the requested strategy dispatcher starts.

1. Build a proposal only for a uniquely owned supported session. Show market/outcome, shares, owned orders, current bid, configured minimum sell price, quantity ceiling and that actual proceeds are uncertain.
2. Collect an explicit decision for that proposal. `--live` must also be present. Default decline on EOF, noninteractive input or expiry. There is no automatic liquidation setting in R5.5.
3. Re-fetch the relevant balance, order state, market status and book. If identity, quantity above the approved ceiling, finality, or action scope changed, invalidate the proposal and request a new decision. Re-price below the approved floor is prohibited.
4. Temporarily bind the **exact old market** as the host's execution target using existing discovery binding, book store, feed supervisor and gateway metadata mechanisms. Add a recovery-only binding role/loader where needed; do not expose arbitrary old-market strategy trading. It has no strategy evaluation, no BUY, no automatic promotion and no new protection arm.
5. Route that session's user-stream evidence through the same lifecycle/coordinator. Reuse `resume`, owned cancellation and `submit_exit(... recovery ...)`, with R1 mutation lock, final gate, fresh books, tick/minimum size and bounded retries. Expose a public lifecycle recovery operation if needed rather than duplicating its private loops.
6. Recovery proposal limits must reach the planner/spec and be rechecked inside the serialized coordinator authorization immediately before POST. A UI-only limit is insufficient. Recovery dispatch remains SELL/cancel only; available inventory, approved quantity and price floor must all hold.
7. Cancel owned resting orders before selling; reconcile possible fills during cancellation. Unknown SELL outcome prevents a duplicate SELL. Cancellation failure prevents assuming the quantity is free.
8. Journal results, recompute operational clearance, and end the temporary binding after any in-flight operation completes. Do not force-release an unsafe lifecycle.
9. Re-run account assessment. Only after all issues are clear, bind the requested current/next market with a new session identity, refreshed account/metadata/stream readiness, and start the usual strategy dispatcher.

If a historical market is no longer tradable, remove the close option and classify pending/final settlement. Foreign metadata must never fall back to the current market's tick, token or book.

Keep inspection/recovery cleanup separate from “drain the newly started trading session.” Stopping at a prompt must not invoke the old unconditional flatten path on an unapproved session. Once a recovery action is authorized and dispatched, Ctrl+C waits for its in-flight POST and reconciles/drains only within that approval's scope. A crash does not turn an old approval into standing permission for a fresh POST; restart inspects first and asks again if a new mutation is required.

## 9. CLI, bounded timing and ongoing checks

The normal command remains `run --config ... --live`. It performs startup inspection automatically. Add `run --check-account --config ...` for the same online inspection/classification without venue mutations or strategy feeds. This command may persist read-derived reconciliation evidence locally. It must not sign, POST, cancel, redeem or change allowances. Reject combining `--check-account` with `--live`; existing `--validate-config` and `--show-config` remain offline and unchanged.

Add typed shared `startup` configuration with documented defaults:

- `inspection_budget_s: 60` per assessment, excluding time spent awaiting a decision.
- `prompt_timeout_s: 120` for a presented proposal; on expiry return `ACTION_REQUIRED` and exit.
- `monitor_interval_s: 15` for background inventory checks while running.

Reuse account retry/read settings and `snapshot_max_age_s` for dynamic evidence freshness. Use transport-level timeouts and safe task supervision; do not introduce destructive outer cancellation on the shared HTTP2 client. If the overall inspection budget expires, prevent entry immediately and finish/close outstanding reads safely without overlapping another scan. A stuck read must not keep the process alive indefinitely; transport deadline behaviour requires a contract test.

Print concise progress and a grouped account summary before any prompt. Inject an async decision provider so terminal input never blocks feed/account tasks or shutdown. Non-TTY execution makes no automatic choices: emit the assessment/report and exit 2 when action or evidence is needed. Clean noninteractive startup may proceed. Inspection exits 0 only for `READY`, 2 for action/blocked, 130 for operator interruption.

After a long startup or prompt, discover the current/next window afresh. The original selected five-minute target may have expired. Keep one-shot duration anchored to the selected trading window; inspection/recovery time does not count as strategy execution time.

Maintain a current assessment generation and validity deadline. The final BUY gate reads local validity; it does not do network I/O. On expiry, stream gap, contradictory account evidence or wallet scope change, close the BUY gate and schedule reconciliation. Reopen only after successful revalidation. New issues during an exposed run must not block authorized protective exits. No interactive recovery prompt while current exposure is being managed; finish bounded drain and report the issue. Background scans serialize and yield priority to active execution; never replay the entire history on each tick.

The background classifier must distinguish expected orders/positions of the current authorized lifecycle from unexpected external activity and retained-session risk. It must not classify every normal current BUY as an external startup conflict. Opening another entry remains subject to the existing lifecycle limits independently of account assessment readiness.

R6 will consume this same assessment at promotion; do not implement continuous windows here.

## 10. Operator messages and reports

Example output (illustrative, not an assertion about the operator's account):

```text
Checking previous trading activity for wallet 0x...1234...
2 sessions confirmed closed externally.
1 resolved holding remains redeemable; it is not counted as available cash.
No unresolved orders. Account ready. Starting ask70.
```

```text
Startup paused: an earlier ask70 session holds 7.10 UP shares.
Market: <title and link>. One owned sell order remains open.
Available action: cancel that order and close up to 7.10 shares,
subject to the displayed minimum sale price and execution limits.
Choices: review/approve this action, refresh, stop.
```

Write `startup_report.json` atomically on each completed assessment and after recovery, including blocked/interrupted startup. Use the normal unique output directory and collision protections, extending collision detection to existing startup reports. The report contains:

- Schema, run/assessment IDs, UTC times, software/config identities, masked display wallet plus unambiguous nonsecret account identifier.
- Coverage/completeness/freshness by source and credential scope; missing information.
- Per-session and external-inventory classification, exact condition/token/order identifiers, readable market title/link, historical phase and current operational disposition.
- Evidence/proof references, unknown accounting fields, settlement quantities separate from cash, permitted actions and proposed limits.
- Decision, execution and recheck results, including aborted/expired proposals.
- `ready_to_start`, blockers and a concrete next action.

Extend `run_summary.json` to schema 5 with a `startup_reconciliation` summary/reference and separate `recovery_sessions` and `strategy_session` attribution. Preserve existing fields where their meanings remain valid. Add startup outcomes `STARTUP_ACTION_REQUIRED`, `STARTUP_RECONCILIATION_BLOCKED`, `ACCOUNT_CHECK_READY`, `OPERATOR_STOP_BEFORE_TRADING` with explicit success/exit semantics; never classify an inspection as economic no-signal.

Account for all recovery mutations in process totals even if the new strategy never opened a session. Separate recovered historical BUYs from current-process BUYs. A recovery SELL alone cannot satisfy R7's requested-strategy round-trip requirement. Operational clearance does not imply complete historical fills, known PnL, a profitable strategy or successful strategy execution. Emergency reports retain startup status and identities.

## 11. Implementation sequence and review boundaries

### A. Validate contracts and establish account identity

Verify the pinned environment and APIs; implement normalized read adapters, complete pagination, visibility checks, exact market mapping, finality reader and journal identity/lock. Add contract tests with recorded/synthetic responses. No mutations or automatic clearing yet.

### B. Implement authoritative recovery evidence

Add event types, reducer projections, quantity adjustments, attempt resolution and clearance/revocation. Test historical replay and R1 invariants. Review this step before wiring readiness: these changes determine whether money can be traded safely.

### C. Implement automatic assessment and inspection command

Build the classifier, inventory scan and startup controller; add `--check-account`, typed policy and reports. Automatically clear only proven cases. No generic override. Ensure read-only mode cannot reach mutations through cleanup or client construction.

### D. Implement approved old-session recovery

Add proposal/decision flow, temporary exact-market execution binding and scoped lifecycle/coordinator limits. Prove cancel races, SELL ambiguity, post-signing checks and shutdown behaviour using the real dispatch/coordinator path before enabling live composition.

### E. Integrate ordinary startup and ongoing readiness

Replace duplicated retained-session decisions in `_bind_pending_market`; update restoration/release/promotion consumers, background invalidation and final BUY gate. Preserve the one-shot workflow and both ask70/z_gap input contracts. Update run reports and operator docs.

### F. Release evidence

Run all acceptance tests and the complete offline suite in the pinned environment, validate configs and fixture decisions, and inspect the final diff. Record baseline lint findings separately. Create a new candidate SHA through the normal release workflow; do not claim the old SHA validates changed code.

Each step must leave R1–R5 tests green or explicitly update only tests whose old startup behaviour is intentionally replaced by this plan. Do not weaken safety assertions to accommodate a new code path.

## 12. Acceptance tests

Use deterministic fake clock/decision provider/venue feeds, `MemoryExecutionJournal` for focused tests and SQLite temporary databases for restart tests. No network or credentials in the automated gate. Proposed test owners: `tests/unit/execution/test_startup_reconciliation.py`, `test_operational_clearance.py`, `tests/unit/runtime/test_startup_controller.py`, `tests/unit/reporting/test_startup_report.py`, and `tests/contract/polymarket/test_startup_reads.py`.

Every case below has explicit expected behaviour:

1. **S01 Clean startup:** complete empty account, no prior sessions; normal live composition can start. Inspection-only has zero prepare/POST/cancel/redeem calls.
2. **S02 External closure:** confirmed old BUY, later external sale and converged balance; unique adjustment clears operational risk, including historical MANUAL, without inventing a bot SELL.
3. **S03 Lagging balance:** zero account balance with confirmed BUY but no disposal proof; still blocked. Preserve original R1 guard.
4. **S04 Cancellation/expiry:** order absent from open list but found terminal with no fills; reconcile and clear. Missing lookup or terminal partial fill does not count as no-fill.
5. **S05 Ambiguous attempt:** resolve using exact order proof; accepted/matched orders import their settlement evidence. 404, fuzzy match or missing identity remains blocked.
6. **S06 Pending resolution:** end time passed/closed flag, unresolved payout; blocks even with no open orders.
7. **S07 Final settlement:** winning, zero-payout and split payout variants; preserve holdings, exclude only proven settled quantity from tradable exposure, do not credit unredeemed payout to collateral.
8. **S08 Source disagreement:** indexer says redeemable but finality read disagrees/unavailable; no automatic settlement clearance.
9. **S09 External inventory/orders:** account scan finds items absent from SQLite; display and block without adoption or mutation.
10. **S10 Pagination and dust:** relevant holding/order on later page or below default position threshold is detected; failed/truncated pagination blocks.
11. **S11 Scope:** wrong wallet, condition/token mismatch, credential visibility restriction and legacy journal ambiguity never clear a session. Association approval alone cannot clear it.
12. **S12 Shared token:** two old sessions and one external disposal; no double subtraction/clearance. Ambiguous allocation blocks and is explained.
13. **S13 Replay/idempotence:** repeat observations and restart after each appended recovery event; identical final state, no duplicate adjustments, no stale automatic permission to POST.
14. **S14 Revocation:** fresh contradictory exposure/order/trade evidence invalidates clearance and final BUY permission; historical outcome remains auditable.
15. **S15 Guided close:** real lifecycle/coordinator/final gate, exact old market binding, explicit approval; cancel then bounded SELL, prove flat, clear, fresh new-market binding and normal strategy start.
16. **S16 Approval changes:** increased quantity, changed market, proposal expiry or price below floor before POST; no unauthorized dispatch, re-prompt or block.
17. **S17 Cancel race:** late fill after cancellation; reconcile before selling and enforce proposal quantity ceiling. Ambiguous SELL prevents a second SELL.
18. **S18 Stop paths:** decline/EOF/non-TTY/timeout/Ctrl+C at prompt produces report, zero unapproved mutations; in-flight approved POST shutdown waits before closing resources.
19. **S19 Read failures:** authentication error, transport stall, rate limit and stale snapshots obey budget, do not block the event loop, do not transform unavailable data into zero.
20. **S20 Timing:** startup crosses one or more five-minute boundaries; requested strategy discovers a fresh target, no stale queued intent, original duration rule preserved.
21. **S21 Cross-strategy:** parameterize real composition for ask70 and z_gap. Ask70 never starts Binance/Chainlink due to account reconciliation. Strategy interfaces remain unchanged.
22. **S22 Reports:** blocked startup with no execution session, historical recovery-only mutations, cleared historical MANUAL with incomplete accounting, and emergency report all carry truthful separate attribution.
23. **S23 Process ownership:** second process sharing state directory is refused; lock releases on normal close/crash without deleting execution history; Windows path tested.
24. **S24 Freshness while running:** expired assessment/stream gap/external position closes BUY gate; protective SELL remains available; successful recheck reopens safely with bounded background work.
25. **S25 Backward compatibility:** replay old schema/event payloads, round-trip new events, existing dust/maker/attribution/prepare/POST/shutdown tests and one-shot CLI validation all pass.
26. **S26 Reduced partial position:** external partial sale reduces only proved quantity; recovery cannot sell the original stale BUY quantity or another owner's inventory.
27. **S27 Crash during recovery:** POST succeeded but response lost; restart resolves exact attempt before any retry, with approval scope enforced and no duplicate liquidation.
28. **S28 Finality cannot hide uncertainty:** resolved market plus unknown BUY/SELL or unsettled trade still blocks until execution evidence is resolved.

Acceptance is based on these behaviours, not test names or a target test count. Do not stub the final gate, lifecycle, dispatcher or recovery checks out of the integration tests that claim to prove them.

## 13. Documentation, validation and release boundary

When implemented, update `Docs/latest/running.md`, `execution_lifecycle.md`, `architecture.md`, `config_guide.md`, `config/runs/generic_parameter_catalog.yaml` and first-release status. Document inspection's local journal writes, proposal approval semantics, resolved holdings, unsupported cases, safe restart and report locations. Keep R6/F-L2 deferred.

Offline validation must include S01–S28, existing unit/contract tests, all four shipped config validations, four R4 lab packs, Ruff on changed Python files, and whitespace/link checks. Run the full suite in isolation for timing-sensitive checks; record and investigate any flaky failure rather than erasing it from the report with a green rerun.

After the offline gate, an operator-authorized **inspection-only** run against the three reported old sessions should show exact market/account classification and evidence. It may correctly remain blocked if evidence is insufficient; do not promise all historical ambiguity is recoverable. Live cancellation/SELL requires its explicit action approval. Only a fresh ready assessment permits the next supervised ask70 run, followed by z_gap. These operations are not authorized by writing or implementing this plan.

The 2026-09-26 copied-state inspection preserved the original journal hash `3f7c07ed822f620e601216c546c830e0fc47dcc6bcfbfef8b936483a67896538` and wrote nothing. It stopped on TLS hostname mismatch before positions, orders, or payout classification. That is a network blocker, not a cleared account.

The earlier ask70 attempt was operator-reported as zero mutations with prior-session risk. It demonstrates the need for this feature, not a BUY/fill/exit pass. R7 retains its bounded budget and required execution evidence; a new candidate does not silently reset the campaign allowance. Review remaining allowance and authorization before another live campaign.

## 14. External contracts to verify

The adapter contract tests and pinned SDK are implementation authority. Current public documentation supports the following distinctions:

- [Polymarket order management](https://docs.polymarket.com/trading/manage-orders): exact order reads, paginated account orders/trades, and authentication visibility limitations.
- [Market details](https://docs.polymarket.com/market-data/market-details): market metadata and state; map identifiers explicitly.
- [Resolution](https://docs.polymarket.com/concepts/resolution): final resolution and redemption are distinct from trading/end-of-window status.
- [How positions work](https://docs.polymarket.com/trading/positions/how-positions-work): standard and neg-risk positions both use Conditional Tokens. Neg-risk conversion uses a different exchange, not a different payout contract.
- [Negative risk](https://docs.polymarket.com/concepts/negative-risk): augmented events add placeholder and Other outcomes. Those are separate markets. The pinned SDK does not identify which kind a condition is.
- [Contracts](https://docs.polymarket.com/resources/contracts): Conditional Tokens `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` on chain 137. The deprecated CLOB v1 Neg Risk Adapter is not a payout source.
- [Position management](https://docs.polymarket.com/trading/positions/manage): holding lifecycle and settlement operations; redemption submission remains outside this plan.

Do not introduce an SDK migration merely because online examples changed. Any endpoint gap must be documented and covered by a minimal read adapter or an explicit unsupported classification, never guessed clearance.
