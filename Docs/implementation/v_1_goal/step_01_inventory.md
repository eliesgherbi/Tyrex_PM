# Step 1 inventory — settlement lifecycle

Status: accepted. The restart rules below were accepted and then implemented in Step 2. The sections under "Retry duration" and "Recovery of existing exposure" describe the code as it was at inventory time. The Step 2 result is in `02_execution_and_research_live_gaps.md`.

## Step 2 result for the two accepted defects

Implemented on git `047b26b` plus the uncommitted working tree fingerprinted in `var/research/trading_integrity/step2/paper/build_identity.json`.

| Expected | Observed |
|----------|----------|
| A restored no-fill keeps the original start, and downtime counts toward `max_retry_duration_s` | `test_restart_keeps_remaining_backoff_and_counts_downtime` closes the episode when the stored start is already 31 seconds old against a 30-second budget |
| A remaining backoff deadline survives restart | The same test waits with `RETRY_BACKOFF` until the stored deadline, then emits attempt `e1a2` |
| Missing start or backoff blocks further entry attempts and is distinct from "no backoff" and "already elapsed" | `test_restart_without_retry_timing_blocks_further_attempts` closes with `RETRY_CLOCK_UNRECOVERABLE`. `backoff_state="none"` is an explicit recorded "no wait". A past deadline is not treated as missing |
| Attempt, POST, and prepare-failure counts survive | Restore copies `latest_posts` and `latest_prepare_failures`. The host gate still enforces the ledger caps. Default retry allows three attempts, including the first (`test_default_retry_ceiling_is_three_attempts_including_the_first`) |
| A committed hold is not sold on shutdown | Existing `test_shutdown_preserves_a_committed_hold` still passes |
| An uncommitted hold is not flattened because the process is stopping | `startup_exposure_decision` returns `commit_hold` when the recorded policy matches. `test_shutdown_does_not_flatten_an_uncommitted_hold` does not call `drain_open_session`. A missing or conflicting policy returns `block` and does not commit or sell |
| A filled restore keeps managing the position; an unknown dispatch does not buy again | `test_restart_of_a_fill_keeps_the_position_and_an_unknown_dispatch_blocks` |
| Sale proceeds do not restore the window debit | `test_sale_proceeds_do_not_restore_the_window_debit` |

Public-feed paper later exercised a committed-hold restart and a rollover. The first restart stopped on `HOLD_COMMITTED` and kept the shares; the replacement run tracked the hold and rolled. See the Step 2 execution record. Deterministic coverage is not paper qualification.

The inventory body below is unchanged as the record of what the review accepted.

Source reviewed: git `047b26b`, plus uncommitted edits in `strategies/settlement_value/driver.py`, `runtime/trading_runtime.py`, and `runtime/settlement_hooks.py`. Tests and the 20 Oct 2026 qualification report were read, not rerun. Where a saved result predates those uncommitted edits, it is marked outdated for the working tree.

Labels used below:

| Label | Means |
|-------|--------|
| Implemented | Present in this working tree |
| Tested | An automated test exercises it. The test file was not executed in this pass |
| Replay | A closed-loop or scripted replay test exercises it |
| Paper | A saved public-feed paper result shows it |
| Qualified | Paper or real-provider evidence on stated conditions. Flat paper does not qualify a fill, a sale, or a restart with exposure |

The two restart recommendations were proposals when this inventory was written. They were accepted afterward and implemented; the result table above is the current status. The behavior sections below remain the inventory-time description.

## Retry duration across restart

### Current behavior

`restore_window_usage` handles a ledger state of `NO_FILL` or `NO_DISPATCH` with retry enabled by setting phase `REARMABLE` and `episode_started_at = None`. The comment in that function says the pre-restart start time is not durable and the elapsed budget restarts, while attempt and POST ceilings still bound the episode.

`_retry_exhausted` applies `max_retry_duration_s` only when `episode_started_at` parses. A missing timestamp skips the check.

On a later retry the driver does not call `_start_episode` (`if not retrying`). `_start_episode` is the only writer of `episode_started_at`. The restored episode therefore never receives a new start timestamp, and the duration limit stays skipped for the rest of that episode.

What restore does copy from the ledger: episode count, latest attempt count, window attempts, window posts, filled-episode count. What it does not copy, even though the ledger stores some of them:

| Restriction | After restore in the driver | Enforced later by the host ledger gate |
|-------------|-----------------------------|----------------------------------------|
| Attempts per episode and per window | Copied | Yes, `EPISODE_ATTEMPT_CAP` / `WINDOW_ATTEMPT_CAP` |
| Posts per window | Copied | Yes, `WINDOW_POST_CAP` |
| Posts per episode | `episode_posts` stays 0 | Yes, `EPISODE_POST_CAP` from `episode.posts` |
| Prepare-failure count | Stays 0. Backoff exponent and `next_attempt_not_before` also reset | Yes, `EPISODE_PREPARE_FAILURE_CAP` |
| Retry duration | Skipped | No. The ledger has no episode start time |
| In-progress backoff wait | Cleared | No |

The host can still refuse an attempt the driver proposes when the ledger's attempt, post, or prepare-failure cap is already spent. That refusal does not restore the duration budget. A restart can also emit the next retry immediately, because the backoff deadline was only in memory.

Spending limits and window debit for a new attempt are checked by `check_entry` against the ledger, not by the restored driver clock.

### Recommendation

Agree with the proposed expectation.

The original episode deadline should persist, and downtime should count. Restart must not grant a longer retry window. If the original start cannot be recovered, block further entry attempts for that episode and keep reconciling any confirmed position. A permitted retry still needs a definitive retryable outcome and fresh eligibility checks.

Rationale: the duration budget is the bound on how long a no-fill episode may keep trading. Skipping it because the timestamp was not stored extends permission. Attempt and POST counts are not a substitute. Blocking when the timestamp is missing is the safe behavior until a durable start time exists. Persisting the timestamp is the useful fix, so a restart inside an unexpired budget can still retry. A missing timestamp then blocks, rather than starting a new clock.

The cleared backoff wait is a smaller extension: one earlier attempt, still inside the other ceilings. Do not treat it as the same defect. Step 2 can cover it with the same fixture if the timestamp is being persisted anyway. Prepare-failure and per-episode POST caps already fail closed at the host gate; the driver being unaware is a consistency gap, not an open permission, as long as every live retry passes that gate.

### Evidence

`tests/unit/strategies/settlement_value/test_episode_lifecycle.py` `test_restart_restores_window_counters_from_the_ledger` shows a restored no-fill episode continuing to the attempt ceiling. It uses `min_retry_interval_ms: 0` and does not assert duration. `test_retry_ceilings_close_the_episode` covers duration only inside one uninterrupted process. No test shows a restart either preserving a deadline or blocking when the deadline is unknown. No paper capture shows a retry after a restart.

### Mismatch and smallest Step 2 check

Mismatch: the proposal says preserve the deadline or block. The code skips the duration check for the whole restored episode.

Do not fix it in Step 1. Smallest Step 2 work: one deterministic fixture on S1 that restores `NO_FILL` with the attempt budget still open and asserts today's driver emits another entry while `episode_started_at` is missing. The fix, after review, is to persist the episode start on the ledger and, when it is absent, refuse further entry attempts without refusing position management. Re-run the same fixture as the pass condition: an already-expired deadline does not enter; a missing timestamp does not enter; a deadline still in the future may enter only after a fresh eligibility check.

## Recovery of existing exposure

### Current behavior

`resume_flatten` is a log label inside `_inspect_retained_session`, not a sell. For a restored session on the market just bound, exposure selects that label, a live maker order selects `resume_cancel_then_prove`, and anything else on that market selects `block_manual`. The caller discards this return value for the matching market. It does not, by itself, submit an order.

What happens next on that path:

1. The runtime resumes the session and reconciles.
2. Reconciliation incomplete sets `PRIOR_SESSION_RECONCILIATION_INCOMPLETE`.
3. Confirmed exposure sets `STARTUP_ACTION_REQUIRED`, marks the token sellable, and sets `stop_requested`.
4. An unresolved mutation marks the session manual, sets `AMBIGUOUS_PRIOR_SUBMISSION`, and sets `stop_requested`.
5. A stop reaches `_shutdown_execution`. A committed hold cancels resting orders, records `SHUTDOWN_HOLD_PRESERVED`, and returns without a sale. Otherwise `drain_open_session` cancels orders and, while exposure remains, submits a `FlattenIntent`.

So a confirmed but uncommitted position on the bound market can be sold on the way out. The sale is the shutdown drain, not the `resume_flatten` label. This pass found no test that follows that label through to a flatten or to a recovered hold.

Separate startup inspection, before the run loop, is `execution/startup_reconciliation.py`:

| Situation | Inspection result | Sale |
|-----------|-------------------|------|
| Committed hold, non-continuous | `COMMITTED_HOLD_AWAITING_RESOLUTION`, entries blocked | Not offered |
| Committed hold, continuous, no live order and no uncertainty | `COMMITTED_HOLD_TRACKED` | Not offered. Other windows may enter |
| Owned tradable shares that are not a committed hold | Proposal `cancel_and_close`, code `OWNED_POSITION` | Only after an explicit live approval in `_resolve_startup_action`. Decline is the default |
| Live order and no shares | Proposal `cancel`, needs approval | Cancel only |
| Foreign market session that is not a tracked committed hold | `UNRESOLVED_PRIOR_MARKET_SESSION` | Blocks. No automatic sale on that label |
| Ambiguous prior submission on the bind path | Manual mark and stop | Shutdown then follows the drain rules above |

A different loaded config does not rewrite a committed hold into a managed exit. `commit_hold` and exit arbitration refuse sells of a committed hold. An uncommitted position has no stored "this position's policy" on the shutdown drain: the drain flattens whatever is still an open execution session.

Driver restore maps ledger state to phase: `NO_FILL` / `NO_DISPATCH` to rearm or close; `SOLD` to idle; `RESERVED` / `DISPATCH_UNKNOWN` / `MANUAL_INTERVENTION` to uncertain; other holding states to `POSITION`. The host position, not the new YAML alone, is what `_sync_position` uses to mark a fill.

### Recommendation

Agree with the proposed expectation, with the limitations below.

- A committed hold stays held across restart, rollover, and shutdown. That part is already the implemented rule.
- Uncommitted exposure is reconciled first: quantity, orders, and uncertainty.
- When the episode's original policy is the hold profile and commit prerequisites are proven, finish the hold commitment. Do not flatten it because the process is restarting.
- When the original policy is managed exit and the position, orders, and inputs are recoverable, resume management. If they are not, block new entries and surface the recovery action.
- A newly loaded config must not retarget an existing position. A hold committed under the old policy stays a hold.
- An approved `cancel_and_close` at startup, and a shutdown drain of a session that was never a settlement hold, can be valid. The gap is the bind path that stops and then drains a filled settlement episode which has not committed yet. That flatten is not an operator decision and does not consult the position's policy.

Supported limitation: do not build a second recovery framework. The existing assessment already distinguishes committed holds, owned positions, and live orders, and it already requires approval for a close. The missing piece is to keep the bind/shutdown path on that same rule for a settlement episode.

### Evidence

Unit tests, not rerun here: `test_startup_never_offers_to_close_a_committed_hold`, `test_shutdown_preserves_a_committed_hold`, `test_committed_hold_is_exempt_from_manual_deadline_and_mandatory_flatten`, `test_continuous_startup_tracks_committed_holds_without_blocking`, `test_a_committed_hold_is_never_sold`, `test_committed_hold_survives_rollover_and_shutdown_without_flattening`. These support the committed-hold rule. They do not show an uncommitted fill being recovered into a hold, and they do not show `resume_flatten` submitting anything.

The 20 Oct 2026 paper runs finished flat. They do not qualify restart with exposure.

### Smallest Step 2 check

One deterministic host fixture, no public feed:

- Committed hold: restart and shutdown submit no flatten. This extends the existing unit tests only if they do not already cover the bind path's `stop_requested` together with `drain_open_session`.
- Filled, not yet committed, hold profile: reconcile quantity, then commit when prerequisites hold, and do not submit `FlattenIntent` merely because the session was restored.
- Uncertain submission: no new BUY; no flatten while the outcome is unknown.
- Approved `OWNED_POSITION` still requires the explicit approval already implemented.

Pass condition: the uncommitted hold fixture records a hold commitment or a visible blocked recovery, and the order log contains no flatten unless the fixture's operator decision approved one.

## Lifecycle and what is shown

Ongoing reconciliation, recording, and settlement tracking are included. "Tested" means the test exists in the tree. This pass did not re-execute it.

### Stage 1 — start or restart

| Branch | Expected pass | What is shown | Limit |
|--------|---------------|---------------|-------|
| Clear state prepares | No order at startup; budgets intact | Implemented. Startup tests cover a clear assessment | Not qualified on a new paper start in this inventory |
| Committed hold tracked, not sold | No sell, no new BUY for that exposure | Implemented and unit-tested, including shutdown and rollover | No paper restart with shares. Recommendation above is still a proposal |
| Uncommitted exposure recovered under its policy | Quantity matches; no duplicate BUY; hold completes only when proven | Driver restore to `POSITION` is implemented. Bind path can stop and shutdown can flatten | Apparent defect versus the recommendation. No end-to-end test |
| Uncertain or ambiguous order blocks BUY | No new reservation | Driver `UNCERTAIN` is unit-tested. Host sets `AMBIGUOUS_PRIOR_SUBMISSION` and stops | Shutdown may then drain if exposure exists. Not paper-qualified |
| Invalid config or ownership blocks | No trade, ledger unchanged | Ledger ownership tests exist (`test_ledger_ownership.py`). Config validation exists | Not re-run here |

Restart persistence that is implemented and host-enforced: attempt counts, window posts, prepare-failure caps, debit caps. Duration and in-memory backoff are not persisted. See the recommendation.

### Stage 2 — inputs

| Branch | Expected pass | What is shown | Limit |
|--------|---------------|---------------|-------|
| Ready inputs allow evaluation | Entry only after books, clock, and reference are usable | Driver waits are unit-tested (`K_MISSING`, model, books). Replay covers reference availability | A wait is not an entry |
| Missing or stale input waits with a reason | Empty intents, reason names the input | Same tests. Qualification paper skipped a window with no exact boundary tick | That paper result is flat and does not prove a later entry |
| Readiness returns | Next entry uses recovered inputs | Implemented in the evaluate loop | No saved case that fails then recovers then enters |

### Stage 3 — evaluation

| Branch | Expected pass | What is shown | Limit |
|--------|---------------|---------------|-------|
| No opportunity waits | No intent, budgets unchanged | Replay and the flat paper runs (731 and 576 evaluations, no entry) | Paper shows the wait. It is outdated relative to uncommitted edits until reconstructed again |
| Entry when flat | Intent matches side, size, price, debit; phase pending | Driver unit tests and replay fills under friction/favorable scenarios | Replay fills are scenario fills, not venue fills |
| Inventory routes to management | No second BUY, no top-up | `test_partial_fill_is_a_position_and_never_a_top_up` | Unit only |
| Unknown inventory blocks | No assumed position | Driver returns `UNKNOWN_INVENTORY` | Unit path; not a paper case |

### Stage 4 — entry outcomes

| Branch | Expected pass | What is shown | Limit |
|--------|---------------|---------------|-------|
| Retryable not-sent or zero fill | New attempt after backoff and a fresh check, same episode | Unit backoff, side lock, and ceilings. Replay `test_retry_after_zero_fills_is_bounded` and `test_gate_refusal_is_not_sent_and_retried` | Replay, not paper. Duration across restart is the open defect |
| Retry off or not retryable | Episode closed | `test_retry_disabled_zero_fill_closes_the_window_with_legacy_reasons`, `test_structural_refusal_is_not_retried` | Unit |
| Pending blocks a second BUY | One live attempt | `test_pending_attempt_blocks_a_second_buy_before_any_outcome`. Host `PRIOR_ATTEMPT_UNRESOLVED` | Unit / host code. Not paper |
| Partial or full fill | Position once; original BUY not repeated | Partial-fill unit test. Replay economics for full fills | No paper fill |
| Uncertain blocks | No other BUY | `test_uncertain_outcome_blocks_any_further_buy`. Ledger keeps the reservation (`test_no_fill_releases_and_unknown_keeps_reservation`) | Unit |
| Late or duplicate feedback | One transition | `test_late_outcome_for_an_old_attempt_is_ignored`. Host `LATE_FILL_DETECTED` can mark manual if the ledger already said no-fill | The manual late-fill path is implemented; this pass found no fixture that asserts the full host sequence |

### Stage 5 — position and sales

| Branch | Expected pass | What is shown | Limit |
|--------|---------------|---------------|-------|
| Hold commits once, then holds | One hold intent; later evaluations do not sell | Driver hold path. `test_outcome_mapping_and_hold_commitment`. Exit arbitration refuses a committed hold | Paper probes had no position to commit |
| Committed hold exempt from flatten, deadline, protection, shutdown | No sale | Runtime unit tests listed under recovery | Unit |
| Managed exit requests a sale | One exit at a time; request is not closure | Thesis, value, and trend unit tests. Replay thesis stop and protection take-profit | Research scenarios. 20 Oct exit cases did not show a public-feed sale. E1/E2 are not promoted |
| Sale not sent, no fill, or partial | Remaining shares stay | `on_exit_outcome` returns to `POSITION` | Unit logic. No paper sale |
| Sale uncertain | Stay exiting; no re-entry | Driver sets `EXITING` | Unit |
| Flat then idle | Idle only after quantity is zero | `_sync_position` | Unit |
| Cutoff or exhausted exit attempts | Residual hold, or no new sale | `test_managed_exit_residual_is_held_at_the_exit_cutoff` | Unit. Host mandatory flatten must not override a proven residual; the plan states that, and a contradictory shutdown drain of an uncommitted position is the recovery gap above |

### Stage 6 — re-entry

| Branch | Expected pass | What is shown | Limit |
|--------|---------------|---------------|-------|
| Failed re-entry checks stay idle | Closed debit remains; reason visible | Cooldown, cap, protective lock, signal-reset unit tests. Hold config rejects `max_trades_per_window: 2` | Unit |
| Passed checks start a new episode | Next episode id, remaining budget | `test_reentry_after_confirmed_closure_with_cooldown_and_cap`. Replay `test_thesis_stop_then_reentry_after_confirmed_sale` | Replay, not paper |
| Retry is not re-entry | A no-fill does not increment a new episode | Separate phases in the driver and the restart unit test | Unit |

### Stage 7 — window end

| Branch | Expected pass | What is shown | Limit |
|--------|---------------|---------------|-------|
| Cutoff stops new entries | No entry after `tau_min`; obligations remain | Driver `ENTRY_WINDOW_CLOSED` | Unit |
| Single-window finish | Process stops after a committed hold | `commit_hold` sets `stop_requested` when not continuous | Unit behavior of the flag. Paper continuous/single distinction not re-shown here |
| Continuous promotion | New window is clean; old ledger rows remain | `test_continuous_startup_tracks_committed_holds_without_blocking`. Qualification restart was flat and observation-only on the startup window | Flat paper |
| Committed hold through promotion | Hold unchanged; non-continuous blocks new entries | `committed_holds_block_entries = not continuous` | Unit |

### Ongoing

| Responsibility | Expected pass | What is shown | Limit |
|----------------|---------------|---------------|-------|
| Reconciliation | Pending and uncertain orders resolve without an invented fill | Lifecycle reconcile and startup assessment tests | No paper uncertain order |
| Recording | Decisions and outcomes used above can be reconstructed, or the capture is reported missing | Qualification: 2,038/2,038 flat evaluations after clock and ordering fixes. Component paper venue BUY/SELL restart test is described in the status report | Flat public feeds. Report is outdated for uncommitted edits until repeated. Reconstruction does not re-authorize orders |
| Settlement ledger | Reservations, holds, and redemption evidence have one writer | Ownership tests. `before_entry` reserves or rearms. Operator redemption is manual | No live redemption in this inventory |
| Account refresh and feeds | A failed read is not a zero balance; stale books do not enter | Account-state and book tests exist. Qualification paper connected after the TLS failure was cleared | Those paper runs did not enter |
| Protection | May sell only when the position policy allows | Replay protection test. Committed-hold exemption tests | No paper protection sale |

## Verification setups

No YAML is added here. Deviations are for reaching a branch. Debit cap, fee reserve, no top-up, and ledger budgets stay as on the reference. The reference identity is comparison + legacy hold in `config/runs/settlement_value_leader_5c_240s_paper.yaml`. The 2-cent / 150-second baseline is not a setup.

| Setup | Deviation | Proves | Does not prove |
|-------|-----------|--------|----------------|
| S0 Hold | None | Retry-off close, hold commitment, committed-hold restart and shutdown, cutoff, recording of a flat or held run | A retry, a sale, a second trade |
| S0-reach | Wider edge and/or time band, chosen before the run | A public-feed fill, then hold commit and restart with shares | That the 5-cent rule would have entered. That economics stay on S0 replay and unit tests |
| S1 Retry | Retry enabled, explicit backoff and ceilings | Zero-fill and not-sent retry, ceilings, and the duration-across-restart fixture | A profitable entry |
| S2 Managed exit | `managed_exit`, one trade, protection off. Exit threshold may be loosened beforehand | Sale outcomes and residual hold | Re-entry, and the reference hold path |
| S2-protect | S2 plus one protection block | A protection sell uses the sale outcomes; a second sell does not race | A committed hold. The negative check is fixture F |
| S3 Re-entry | S2 plus a second trade, cooldown possibly shortened beforehand | Re-entry allowed and refused | The first sale's accounting. Run S2's sale assertion before reading S3's second episode |
| F Fixtures | Inject the event. No market profile | Uncertain submit, partial fill, duplicate feedback, concurrent sells, duration after restore, uncommitted exposure on restart, committed hold with protection armed, rollover with a reserved episode | Venue latency, the live fee schedule, or redemption payment |

Public-feed runs, when Step 2 executes them, stop on a duration or window count written first. If the fill or sale does not occur, keep the capture and leave the branch unverified.

## Evidence index

Shared citations. One row can support several branches above.

| Id | Kind | What it actually shows | Limit |
|----|------|------------------------|-------|
| E-unit-episode | Test file exists | `test_episode_lifecycle.py` driver branches listed above | Not executed in this pass. Not paper |
| E-replay-v3 | Replay tests exist | Bounded retry, gate retry, thesis stop then re-entry, protection take-profit | Scenario fills. Collection pinned by those tests, not re-hashed here |
| E-hold-runtime | Unit tests exist | Commit, shutdown preserve, startup does not offer to close a committed hold, continuous tracking | Not the uncommitted-exposure drain |
| E-ledger | Unit tests exist | Reservation, unknown keeps reservation, ownership lock | Not a filled paper ledger |
| E-qual-2026-10-20 | Saved paper report | 961 passed, 3 skipped; flat runs; 2,038/2,038 reconstructed evaluations | Flat. Predates uncommitted driver and runtime edits, so reconstruction equality is outdated until repeated. Loop p99 screen is not order-path qualification. Gate latency was not qualified (zero posts) |
| E-exit-cases | Saved replay | E0/E1/E2 on eight development windows. E1 reduced winners; no promotion | Research P&L, not execution qualification |
| E-book-188 | Saved replay | 94 windows, both scenarios, trade fields unchanged under `venue_top_v1` for the hold candidate | Book policy regression, not a restart or a sale |

Real-provider evidence for submit latency, the live fee schedule, and redemption payment was not found as a passing record in this pass. Settlement CLI tests cover the offline guard, not a confirmed redemption.

## Step 2 backlog

Priority is the order to do the work. Dependencies are earlier items or named setups. Artifacts are whatever the check writes: test name, or a capture directory plus reconstruction output.

| Pri | Branch | Expected | Class | Reuse | Smallest check | Setup | Pass | Artifacts | Depends |
|-----|--------|----------|-------|-------|----------------|-------|------|-----------|---------|
| 1 | Retry duration after restart | Deadline persists, or missing deadline blocks entry | Defect against the proposal | E-unit-episode attempt-ceiling test | Fixture: restore `NO_FILL` with attempts remaining; assert no entry when start is missing or already expired | F on S1 | No entry intent; position path still runs if a fill is injected | Test | Review accepts the duration proposal |
| 2 | Uncommitted exposure on restart | Reconcile, then commit a hold or block; no silent flatten | Defect against the proposal | E-hold-runtime committed-hold tests | Host fixture: filled, not committed; assert no `FlattenIntent` without approval | F | Hold commit or visible block; order log has no drain flatten | Test | Review accepts the recovery proposal |
| 3 | Uncertain submission through shutdown | No BUY and no flatten while unknown | Missing evidence | Driver uncertain test; ledger reservation test | Same host fixture with `DISPATCH_UNKNOWN` | F | Blocker set; no flatten | Test | 2 |
| 4 | Partial fill and duplicate feedback on the host path | One position; stale intent ignored; late fill after no-fill is manual | Missing evidence | Driver unit tests | Extend the driver tests only where the host ledger sequence is unasserted | F | Shares equal the confirmed fill; one manual mark for a late fill | Test | None |
| 5 | Concurrent sells | One exit; committed hold ignores protection | Missing evidence for the race; unit evidence for the exemption | Exit arbitration tests | Two exit sources in one evaluation under S2-protect; protection armed on a committed hold | F | One sale or none; hold unchanged | Test | None |
| 6 | Public fill, hold commit, restart with shares | S0-reach entry fills, commits, survives restart, reconstructs | Missing paper evidence | E-qual flat method | One recorded paper run with a pre-set stop | S0-reach | Fill, commit record, restart quantity matches, reconstruction matches | Capture and reconstruction | 2, so a restart is not flattened by the current drain |
| 7 | Retry on a live book | A zero fill retries or a not-sent retries inside ceilings | Replay exists; paper missing | E-replay-v3 | Paper only if a zero fill happens inside the stop rule; otherwise the F fixtures stand | S1 | Ceiling or backoff visible; if the event is absent, branch stays unverified | Capture or the F result | 1 |
| 8 | Managed sale outcomes | Remaining shares, uncertain exit, flat, residual cutoff | Unit and replay; no paper sale | E-exit-cases as research only | Paper with the loosened exit, else F for the outcomes | S2 | Sale quantity matches, or the run stops unverified | Capture | 6 is not required |
| 9 | Protection sale | Same sale accounting, distinct reason | Replay only | E-replay-v3 protection test | Overlay run or F | S2-protect | One exit reason `PROTECTION_*` or strategy, not both | Capture or test | 8 |
| 10 | Re-entry | New episode only after flat and passing checks | Unit and replay | E-unit-episode, E-replay-v3 | After a confirmed flat | S3 | Second episode id; refusals keep the first debit | Capture or test | 8 |
| 11 | Rollover with a reserved episode | New window does not drop the reservation; entries blocked until resolved | Partially implemented (`needs_reconciliation`) | Ledger unit tests | Fixture with `RESERVED` on the previous window | F | `SETTLEMENT_EPISODE_NEEDS_RECONCILIATION` and no new BUY | Test | None |
| 12 | Real-provider fee, latency, redemption | Only questions paper cannot answer | Unsupported as a passing record | Settlement CLI offline tests | A written protocol before any order: question, sample size, pass | Separate from S0–S3 | Protocol result matches the question | Protocol record | Explicit decision to use the venue |
| 13 | Repeat flat reconstruction on this tree | Flat path still matches after uncommitted edits | Evidence outdated | E-qual-2026-10-20 | Re-run the recorded-window reconstruction, not a new strategy | Existing captures | Equality or a listed mismatch | Reconstruction report | None |

Items 1 and 2 wait on review of the proposals. Items 3–5 and 11 can be written as tests before that review because their expected behavior is already the inventory's stage rules. Item 6 waits on item 2 so a paper restart is not judged against the drain path this inventory marks as a defect. Item 12 is not part of the paper setups.

## Review still open

- Accept, narrow, or reject the duration recommendation: persist the episode start; if it is missing, block further entries for that episode.
- Accept, narrow, or reject the exposure recommendation: do not flatten an uncommitted settlement position from the bind/shutdown path unless an existing approval or an explicit non-settlement rule applies.
- Whether a skipped in-memory backoff after restart is accepted. This inventory treats it as secondary to the duration bug.
- S0-reach's exact wider edge or time band, chosen before any paper run.
- Item 12, which needs a separate decision before any real order.
