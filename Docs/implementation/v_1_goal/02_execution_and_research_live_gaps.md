# Step 2 — Close the accepted settlement gaps

Status: deterministic fixes are in, and one bounded paper session was run for each setup. Not fully validated. Several target branches did not occur and stay unverified. Execution correctness, paper integration, real-provider qualification, and profitability stay separate. Real-money orders were not authorized and were not sent.

Follow-up deferred at the user's request on 20 October 2026: [Step 2 follow-up](02_step2_follow_up.md). It records the accounting discrepancy, reconstruction gaps, missing events and resume order. Step 3/4 offline workflow and interface work can proceed; required paper-integration acceptance stays open. The execution history below is preserved.

## Execution record — 20 Oct 2026

Build: git `047b26b5afbad8e62b28acf5a9d9bb0c143ae43b`, dirty. File hashes are in `var/research/trading_integrity/step2/paper/build_identity.json`. Package version on the paper attempt was 0.3.0. The S0-reach capture recorded `source_config_sha256` `a765bb78b9096a9550e172847d8e8e94071dd4f7aafe7db3c524c6d5dff55d06`.

### Corrections applied before the affected slices

- Exit cutoff is 30 seconds, with `lifecycle.mode: managed_exit`. A 15-second cutoff is below the host's 20-second mandatory exit and does not validate.
- S3 sizes each episode at 2.40 USDC and caps price at 0.45, under a 5 USDC window debit. Sale proceeds do not restore that debit.
- Retry evidence uses the configured ceiling of three attempts and three POSTs, including the first.
- Every verification config was validated with `--paper --validate-config` before any feed connection. State, ledger, and report paths contain `paper`.

### Tests

| Command | Result |
|---------|--------|
| `pytest tests/unit/strategies/settlement_value/test_episode_lifecycle.py tests/unit/execution/test_settlement_ledger_v2.py tests/unit/runtime/test_settlement_hold_profile.py tests/unit/execution/test_ledger_ownership.py` | 73 passed |
| `pytest tests/unit/strategies/settlement_value tests/unit/runtime tests/unit/execution` | 487 passed, 1 skipped. This run was before `test_sale_proceeds_do_not_restore_the_window_debit` |
| `pytest tests/unit/strategies/settlement_value/test_episode_lifecycle.py::test_sale_proceeds_do_not_restore_the_window_debit` | 1 passed |
| `test_restart_continues_the_service_failure_backoff_ladder`, `test_retry_clock_and_policy_survive_reload`, `test_paper_report_keeps_arming_separate_from_real_orders`, `test_startup_exposure_follows_the_recorded_policy` | 4 passed on the later re-check |

No failures. The skipped test was already skipped in that runtime/execution set; this pass did not investigate it. The service-failure count is restored only when the ledger recorded it. A missing count on an older journal stays at zero rather than being invented. `live_requested` in the report remains the execution-arming flag, which is true for a paper run so the simulator can submit. The same report now also carries `venue` and `real_orders_authorized`. For these paper runs that second flag is false.

### Configurations

Under `config/runs/coock_book_conf/`. The guide is `README.md` there. The reference run YAML was not edited.

| File | Purpose | Validation |
|------|---------|------------|
| `s0_reach_hold_paper.yaml` | Reachable hold, then a planned same-ledger restart | config ok, `hold_to_resolution` |
| `s1_retry_paper.yaml` | Reach profile plus the provisional 3-attempt retry | config ok |
| `s2_managed_exit_paper.yaml` | Point-value exit, trend off, cutoff 30 | config ok, `managed_exit` |
| `s2_protection_paper.yaml` | That exit plus a 1% `stop_market_fak` stop | config ok |
| `s3_reentry_paper.yaml` | Managed exit plus a second episode under the 5 USDC window cap | config ok |

### Paper

Trading parameters were not changed after a miss. S1, S2, S2-protect, and S3 gained `recording.raw_inputs: true` so the finished captures could be reconstructed. That is a capture setting, not an entry, size, or exit change. Each session used the existing bounded launcher: 20 minutes of wall clock, and `maximum_duration_s: 900` measured from the first prepared window. That duration bound closed the session on the fourth bound window. No session was extended to force a missing event.

Reports from the sessions that reached the book say `live_requested: true`, `venue: paper`, and `real_orders_authorized: false`. The first flag is arming. The other two say the process was the simulator and was not allowed to send a real order.

#### S0-reach — hold

The 12:31 UTC attempt stopped in discovery (`RUNTIME_DEGRADED`, 0 decisions, 0 orders). Local DNS had aliased `gamma-api.polymarket.com` to a certificate that did not match. TLS verification stayed on. A later connection resolved the real host, and the same config was launched again.

| Process | Report | What happened |
|---------|--------|----------------|
| Fill and commit | `s0_reach/reports/step2_s0_reach_hold_paper_20261020T124243Z` | Window `btc-updown-5m-1792500300`. Ledger seq 1–4: reserved, fill 10.20 shares at cost 4.8960, hold requested, hold committed. The launcher then stopped on `HOLD_COMMITTED` (exit `3221225786`). This process has no `run_summary.json` because it was stopped that way. |
| Restart | `...20261020T124549Z` | Outcome `OPEN_EXPOSURE`. Startup saw `HOLD_COMMITTED` and recovered it with action `stop`, then preserved the hold. The shares were not sold. Rollover did not happen. |
| Replacement | `...20261020T124727Z` | Same config, same account and ledger, no trading-parameter change, no in-session restart. Protocol written first at `s0_reach/replacement_protocol.json`. Outcome `COMPLETED_CONTINUOUS`. It tracked the committed hold, filled window `1792500600` (7.09 shares, cost 4.8921) and committed that hold too, then stopped on `max_duration_s=900`. |

Ending paper account: both tokens still present, `9.828720` and `6.936147` shares, collateral `90.2119`, order serial 2. The account balances are smaller than the ledger fill quantities; this record does not assign a fee formula to that difference. Window `1792500900` bound with no episode. Window `1792501200` reserved and then `DISPATCH_NOT_SENT`, closed `RETRY_DISABLED`.

The `stop` action on an already committed hold was the defect in the first restart. `startup_exposure_decision` now returns `track_hold` for a committed hold. The replacement run is the session that stayed up and rolled.

Exact reconstruction of S0 was refused. That capture has decisions, but it has no raw trading-context stream. Report: `s0_reach/reconstruction/reconstruction.json`.

#### S1 — retry

Report `s1_retry/reports/step2_s1_retry_paper_20261020T130640Z`. Outcome `COMPLETED_CONTINUOUS`. `mutation_attempts` 1.

| Window | Ledger |
|--------|--------|
| `1792501500` | No episode |
| `1792501800` | One post, fill 6.04 shares, cost 4.8924, hold committed. No retry was required. |
| `1792502100` | One attempt, not sent: `FINAL_GATE_REJECTED:entry_price_moved`. The ledger stored `consecutive_service_failures: 1`, side `DOWN`, and a retry deadline about 1.2 seconds later. Decisions then show one `RETRY_BACKOFF` and 221 `RETRY_SIDE_NOT_ELIGIBLE`. No second post. |
| `1792502400` | No episode. Stopped on the duration bound. |

A second POST, a three-attempt exhaustion, and a restart in the middle of a backoff were not observed. The failure count was recorded on the not-sent event. Preserving that count across a process restart is covered by `test_restart_continues_the_service_failure_backoff_ladder`, not by this paper session.

Reconstruction: 1,078 captured position contexts matched the execution prefix. Decisions matched on 1,077 evaluations. The one mismatch is on `1792502400`: the live decision was `EDGE_BELOW_COST` with a probability, and the replay was `MODEL_NOT_READY` with null probability and sigma, at the same captured timestamp. Report: `s1_retry/reconstruction/reconstruction.json`.

#### S2 — managed exit

Report `s2_exit/reports/step2_s2_exit_paper_20261020T133744Z`. Outcome `COMPLETED_CONTINUOUS`. Two posts, both `ZERO_FILL` / `NO_FILL_CONFIRMED`, on `1792503600` and `1792503900`. Retry is off, so each episode closed after that one post. Exit posts: 0. No position was opened, so the point-value sale and the residual cutoff hold were not observed.

Reconstruction: 1,016 of 1,016 decisions matched, and 1,016 position contexts matched. Report: `s2_exit/reconstruction/reconstruction.json`.

#### S2-protect — stop

Report `s2_protect/reports/step2_s2_protect_paper_20261020T135522Z`. Outcome `COMPLETED_CONTINUOUS`. `mutation_attempts` 2. Exit posts: 1.

Window `1792504800`: fill 10.78 shares, cost 4.8988. Protection armed at entry price `0.4544341372912801484230055659` with a 1% trigger of `0.4498897959183673469387755102`. The mark was `0.44`, reason `PROTECTION_SL`. The sale filled 10.36 shares for proceeds `4.48251300`. The ledger then recorded `EPISODE_SOLD` with `dust_shares: 0.42`. The paper account's remaining balance of that token is `0.008316`. This sale was the host stop, not a strategy value exit. A value-exit decision was not emitted.

Window `1792505100`: not sent, `FINAL_GATE_REJECTED:entry_price_moved`, retry off, so the episode closed. No second attempt.

Reconstruction matched the three windows that did not contain the sale (274, 297, and 307 evaluations). The sale window, `1792504800`, was refused: `feedback has no preceding captured intent` for exit intent `a85224b8-6bd3-4c17-b5f5-52415fb409fe`. That intent was created by the host stop, and the raw capture has no strategy intent ahead of its feedback. Position contexts for the run were verified (1,176). Decision equality for the sale window is not established. Report: `s2_protect/reconstruction/reconstruction.json`.

#### S3 — re-entry

Report `s3_reentry/reports/step2_s3_reentry_paper_20261020T141553Z`. Outcome `COMPLETED_CONTINUOUS`. `mutation_attempts` 0.

Three windows had no episode. The fourth, `1792506600`, emitted one entry. The final gate rejected it with `entry_price_moved`. The episode closed `RETRY_DISABLED`. There was no fill, no sale, and no second episode. Re-entry and the window-debit cap were not exercised on the feed. The debit rule remains the unit test only.

Reconstruction: 1,121 position contexts matched. Decisions matched on 1,120 evaluations. The mismatch is the last evaluation of `1792505700`, at the window boundary. Live was still `BLOCKED` / `ENTRY_NOT_ALLOWED` with tau about 298.8 seconds. Replay was `HOST_SKIPPED` / `exact_candidate_absent,incomplete_fallback_candidates` with tau about -0.68 seconds, and the book and probability fields were null. The entry window itself matched. Report: `s3_reentry/reconstruction/reconstruction.json`.

### What this does and does not show

| Branch | Evidence |
|--------|----------|
| Paper fill, hold commit, shares kept on restart, rollover into a new committed hold | Observed on S0. The first restart stopped instead of tracking the hold. The replacement run tracked it and rolled. |
| Service-failure count written, and one backoff wait | Observed on S1. The next post did not happen because the locked side lost eligibility. |
| Service-failure count restored after a process restart | Unit test only |
| Point-value sale, residual hold at the exit cutoff | Not observed. S2 posted twice and neither post filled. |
| 1% stop sale | Observed on S2-protect. Dust remains on the paper account. |
| Uncertain exit | Not observed |
| Re-entry after a confirmed flat sale | Not observed. S3's only entry was rejected before POST. |
| Decision reconstruction | S2 matched entirely. S1 and S3 each mismatched one boundary evaluation. S2-protect's sale window could not be replayed. S0 has no raw context stream. |
| Real order, provider fee schedule, operator redemption | Not started |

Nothing here is a profitability result. A flat window does not qualify a fill. A single paper stop does not qualify the value-exit path. Real-provider qualification was not started.

Accepted intended behavior:

- The original retry deadline survives restart. Downtime counts. If that deadline cannot be recovered, block further entry attempts for that episode. Reconciliation and position management continue.
- Remaining retry backoff survives restart. Elapsed downtime counts toward the wait.
- Attempt, POST, preparation-failure, and spending limits keep their scope across restart.
- A committed hold stays held.
- Uncommitted exposure is reconciled and then recovered under the lifecycle policy that was proven for that position. Restart and startup shutdown do not silently switch that policy. If recovery evidence is insufficient, new entries stay blocked and the unresolved action is visible.
- A documented safety exit and an explicitly approved recovery close keep their existing authority. They are not the silent policy switch this step removes.

The sections below are the plan that was accepted and then executed. They describe the defects as they stood before the fixes. The execution record above is the result. Inventory items 1 and 2 no longer wait on a behavior review.

## What Step 2 is allowed to do

After this plan is accepted, Step 2 may add the verification configs described here, run the listed tests and paper commands, fix the two defect classes, and keep the artifacts. It may adjust a supported entry, time-band, exit, or cooldown parameter only as a named deviation whose purpose is to reach a branch.

It does not search parameters for profit. It does not change the reference identity: comparison + legacy hold, 5-cent edge, 240 seconds to 30 seconds, debit cap 5 USDC, hold to resolution, strategy retry off, one filled trade. That file remains `config/runs/settlement_value_leader_5c_240s_paper.yaml`. The 2-cent / 150-second baseline is not a setup.

Debit cap, fee reserve, no top-up, ledger budgets, and the settlement reservation gate stay as on the reference. Account transport retries stay distinct from strategy retries.

## Order

Each slice is reviewable on its own. A later slice does not start by widening an earlier failure into a new profile.

| Slice | Work | Class | Milestone |
|-------|------|-------|-----------|
| 1 | Persist retry deadline and backoff. Recover uncommitted exposure under its proven policy. Deterministic tests for those fixes and for the rare hold-path cases | Known defects, plus missing tests | Required |
| 2 | Hold entry, confirmed fill, hold commitment, restart with shares, continuous rollover. Refresh flat reconstruction on this tree | Missing paper demonstration. Outdated flat evidence | Required |
| 3 | Retry ceilings and a public-feed retry if one occurs inside the stop | Missing paper demonstration. Deterministic ceilings already exist and are re-checked where the restore fix touches them | Deferred |
| 4 | Managed-exit sale outcomes and one protection overlay | Missing paper demonstration. Replay and unit tests exist | Deferred |
| 5 | Re-entry after a confirmed close | Missing paper demonstration. Unit and replay tests exist | Deferred |

Real-provider latency, the live fee schedule, and redemption payment are a separate proposal at the end. They are not part of either milestone.

Rare conditions use fixtures, not a public market: uncertain submission, partial fill, delayed or duplicate feedback, competing sells, and rollover with a still-reserved episode. Competing sells are deferred with slice 4, except the negative check that a committed hold is not sold by protection, which is in slice 1.

## Slice 1 — restore and recovery

### Defects

**Retry clock.** `restore_window_usage` sets `episode_started_at` to `None`. `_retry_exhausted` skips duration when that timestamp is missing, and a restored retry never calls `_start_episode`, so the limit is skipped for the rest of the episode. `next_attempt_not_before` is also dropped. The ledger stores attempt, post, and prepare-failure counts and the host gate already enforces those. It does not store the episode start or the backoff deadline.

Fix: persist the episode start and the backoff deadline with the episode. On restore, downtime counts toward both. If the start cannot be recovered, block further entry attempts for that episode and keep managing any confirmed position. A retry that is still inside the deadline still needs a definitive retryable outcome and a fresh eligibility check.

**Uncommitted exposure.** `resume_flatten` is only a log label. Confirmed exposure on the bound market then sets `stop_requested`. Shutdown preserves a committed hold. Otherwise `drain_open_session` can submit a `FlattenIntent`. That drain is a silent flatten of a settlement position that has not been committed and has not been approved for close.

Fix: for a settlement episode, reconcile quantity, orders, and uncertainty, then continue the proven policy. Hold: commit when the existing commit prerequisites hold; otherwise block new entries and record the refusal. Managed exit: resume management only when the position state required by that policy is recoverable; otherwise block and record the gap. Do not apply the YAML that happens to be loaded now as a new policy for that position. Leave these paths in place: operator-approved `cancel_and_close`, a documented safety flatten of a session that is not a settlement hold, and the existing committed-hold exemption from mandatory flatten, the manual deadline, protection, and shutdown sale.

### Tests

Extend `tests/unit/strategies/settlement_value/test_episode_lifecycle.py` and add one host-level fixture. No public feed.

| Check | Pass |
|-------|------|
| Restored no-fill, start missing | No new entry. Position management still runs if a fill is injected |
| Restored no-fill, deadline already past, attempts remaining | No new entry |
| Restored no-fill, deadline still ahead, backoff still ahead | Wait with the backoff reason until that deadline, then one new attempt only if eligibility passes |
| Restored no-fill, backoff deadline already past | The wait is over. Other ceilings still apply |
| Attempt, post, prepare-failure, and debit caps | A restored episode cannot exceed the cap the ledger already stored |
| Committed hold, restart and shutdown | No `FlattenIntent` |
| Filled hold, not yet committed | Commit when prerequisites hold, or a visible block. No drain flatten |
| `DISPATCH_UNKNOWN` through stop | No new BUY and no flatten |
| Partial fill and a late outcome for an older intent | Shares equal the confirmed fill. One manual mark if a fill arrives after the ledger said no-fill |
| Previous window still `RESERVED` | `SETTLEMENT_EPISODE_NEEDS_RECONCILIATION` and no new BUY |
| Protection armed on a committed hold | No protection sale |
| Approved `OWNED_POSITION` | Still requires the explicit approval already implemented |

### Slice 1 done when

Those tests pass on the reviewed tree. The two defects are fixed or an item is returned as still open with the failing assertion. No paper run is required for this slice.

Review point inside this slice: if the backoff deadline itself cannot be recovered, block further entry attempts for that episode, matching the duration rule. Inventing a fresh full wait would also change the permission. This fail-closed choice is proposed here and can be rejected before the fix.

## Slice 2 — hold lifecycle

Required for the first milestone. Starts after slice 1, so a paper restart is not scored against the drain defect.

### S0 identity

Use the reference paper file unchanged for any check that must show the 5-cent rule, including replay comparisons already cited as E-replay-v3 and E-book-188. Do not rerun those grids. They remain replay evidence.

### S0-reach overlay

Copy the reference into an isolated paper config. Change only:

| Field | Reference | Overlay | Why |
|-------|-----------|---------|-----|
| `entry.min_edge_per_share` | `0.05` | `0.00` | The 20 Oct paper runs stayed flat because the 5-cent edge did not pass. Zero edge asks the model for any positive conservative edge so a bounded session can fill |
| `entry.tau_max_s` | `240` | `270` | Entry may start 30 seconds earlier in the five-minute window. `tau_min_s` stays 30, so the cutoff behavior is unchanged |

Needs review before the run: `0.00` will take trades the reference would refuse. It is a reach overlay, not a new baseline. If it is rejected, name a replacement edge before the session. Do not lower it further after a flat run. Debit cap stays 5 USDC. Retry stays off. Exit policy stays hold. Price cap stays 0.97.

### Run

- Venue paper, `--paper`, `recording.raw_inputs: true`.
- Own state directory, ledger, and report directory under `var/research/trading_integrity/step2/s0-reach/<utc-stamp>/`. Do not reuse the example paper ledger.
- Stop at 3 windows or 20 minutes, whichever comes first. Write that stop in the run note before launch.
- After the first confirmed fill and hold commit, stop the process and start it again on the same ledger. That is the restart-with-shares check. If the window has already ended, the restart still counts for a committed hold. The fill-before-commit race stays the slice 1 fixture.
- If no fill happens, keep the capture and leave fill, commit, and restart-with-shares unverified.

Pass: one entry fills, the ledger reaches a committed hold, the restarted process shows the same quantity and does not sell it, and a following window either promotes with that hold still tracked or the run hits the stop first. Reconstruction of the capture matches the recorded decisions and position context.

Also refresh E-qual-2026-10-20: reconstruct the existing flat captures with this tree. Equality, or a listed mismatch, is the artifact. A mismatch does not get relabeled as a pass. This refresh does not need S0-reach to fill.

### Slice 2 done when

The refresh report exists, and the S0-reach capture exists. Filled branches are qualified only if the fill, commit, and restart happened. Branches the market did not offer stay open. A flat capture does not close them.

## Slice 3 — retry

Deferred past the first milestone. Deterministic ceilings that the restore fix touches are already in slice 1. This slice is the public-feed demonstration.

S1 overlay: S0-reach plus the code's provisional retry block, turned on without retuning:

- `retry.enabled: true`
- `max_attempts_per_episode` and `max_attempts_per_window`: 3
- `max_posts_per_episode` and `max_posts_per_window`: 3
- `max_prepare_failures_per_episode`: 3
- `max_retry_duration_s`: 30
- `min_retry_interval_ms`: 500
- `service_backoff_ms`: 1000, `service_backoff_max_ms`: 8000

Those are the existing defaults in `SettlementRetryConfig`, not a new search. Same debit cap. Same isolated directories. Same 3-window or 20-minute stop. Recording on.

Pass if a not-sent or zero-fill is followed by a backoff and further attempts only while the configured ceilings remain (three attempts and three POSTs, including the first), and a restart during that episode obeys the slice 1 clocks. If the session never produces a retryable miss, keep the capture and leave the paper retry unverified. The slice 1 fixtures still count as deterministic evidence.

## Slice 4 — managed exit and protection

Deferred. Separate from S0 because hold-to-resolution cannot sell.

S2: reference entry, with the S0-reach edge and time band so a position is reachable, then:

- `exit.policy: managed_exit`
- `value_exit_enabled: true`
- `value_exit_basis: point`
- `value_exit_margin_per_share: '0.02'`
- thesis stop off, trend off
- `reentry.max_trades_per_window: 1`
- `exit_cutoff_tau_s: 30`, and `lifecycle.mode: managed_exit`. The host mandatory exit is 20 seconds, so the code default of 15 does not validate. 30 seconds is the cutoff already used by `config/runs/settlement_value_managed_paper.yaml`.

The 2-cent point margin is the margin already used by the experimental value-exit profiles. It is reused so this slice does not invent a second exit rule. It does not promote those profiles. One trade keeps a sale from looking like re-entry.

S2-protect: the same file plus one protection block, `stop_loss.style: stop_market_fak` and `stop_loss.pct_from_entry: '0.01'`. One percent under the entry is enough to arm a real stop without a strategy-exit change. Take-profit and trailing stay off so a sale has one reason to explain. This value needs review with the rest of the deferred slice; it is not needed for the first milestone.

Same isolation, recording, and 3-window or 20-minute stop. Pass: a sale reduces shares by the confirmed amount, a partial or no-fill leaves the rest, an uncertain exit blocks another sell, and the cutoff commits the residual. A competing strategy exit and protection exit produce one sale. If no position or no sale appears, the capture stays and the branch stays open. Unit and replay evidence is not upgraded to paper.

## Slice 5 — re-entry

Deferred. S3 is S2 plus:

- `reentry.max_trades_per_window: 2`
- `reentry.rearm: cooldown`
- `reentry.cooldown_s: 5`
- `reentry.last_entry_tau_s: 30`
- `reentry.max_window_debit_usdc: '5'`

Cooldown 5 seconds and a 30-second last-entry floor are the verification settings. The window debit cap stays 5 USDC. Each episode is sized at 2.40 USDC with `max_entry_price: 0.45`, so two venue-minimum orders of 5 shares (2.25 USDC each) fit under that cap. A sale does not put the spent debit back. `same_side_allowed` stays true and `opposite_side_allowed` stays false. Lock-after-stop stays on. The host's soft entry floor is about 31 seconds, so `last_entry_tau_s: 30` does not open a later entry than the host allows.

Pass: a new episode id only after quantity is zero and the checks pass. A refusal leaves the first debit in place. A no-fill retry does not consume the second trade. If the sale never happens, re-entry stays unverified.

## Evidence to retain

Every slice records:

- Git revision and a note of any uncommitted files that the run or test used
- Effective configuration path and hash, including which overlay fields differ from the reference
- Fixture name, or the paper capture directory
- Expected outcome and observed outcome
- Artifact path

Paper captures are reconstructed for decisions and position context. Replay evidence is reused where the code under test did not change. E-qual-2026-10-20 is refreshed in slice 2 because the driver and runtime have uncommitted edits. E-exit-cases and E-book-188 stay replay evidence unless a slice edits that replay path.

| Kind | Can show | Cannot show |
|------|----------|-------------|
| Deterministic test | The injected branch, including rare failures and the two fixes | A live book, venue latency, or a fee schedule read from the provider |
| Public-feed paper | Operational integration on a live book with a simulated account | Real order placement, real fees, or redemption payment |
| Real provider | Only the question in the proposal below | Anything this plan has not separately accepted |

A flat paper run qualifies flat startup, waits, and reconstruction of that flat run. It does not qualify a fill, a sale, or a restart with shares.

## Real-provider proposal

Not authorized by this document. Write the protocol before any order, and run it only after a separate decision.

| Question | Method | Limit | Acceptance |
|----------|--------|-------|------------|
| Submit latency | Timed paper-comparable POSTs on the real venue, at least 30 posted orders, same build with recording on and off | One account, the reference debit cap, one market family | A reported p50 and p99 with the sample count. The Oct 20 probes had zero posts and do not count |
| Fee schedule | Read the provider schedule for one traded window and compare it with the strategy curve used at the gate | No invented rate | Match, or a recorded mismatch that blocks the entry |
| Redemption | One operator redemption of a committed hold, confirmed by the existing settlement command and a transaction hash | No automatic redeem | Ledger moves to the proven-cash state for that episode only |

## Completion

**First milestone** is slice 1 and slice 2.

- Slice 1: the restore and recovery tests pass, including the service-failure count and `track_hold` for an already committed hold.
- Slice 2: S0 showed a fill, a committed hold, a restart that kept the shares, and a later window that filled and committed again. Exact reconstruction of that capture was refused because the raw context stream was not recorded.
- Slices 3, 4, and 5 were each run once under the same stop rule. A retry backoff and a 1% stop sale occurred. The point-value sale, the residual cutoff hold, an uncertain exit, and a second episode did not occur. Those branches stay unverified.
- A required branch that was attempted and did not occur stays open. It is not marked qualified.
- The real-provider table stays a proposal.

**Step 2 overall** is not fully validated. The sessions above are the evidence for analysis. A missing branch is not a pass.

## Decisions still open

- The 0.00 edge, 270-second band, missing-backoff block, 1% stop, and 5-second cooldown were accepted for verification and are in the configs above. They are not a new reference strategy.
- Any real-provider session, which this plan does not approve.
- The host protection sale is not tied to a captured strategy intent, so the S2-protect sale window cannot be decision-reconstructed.
- S0 was recorded without raw trading context. A later S0 capture would need `raw_inputs` to be reconstructed. That was not rerun.
- The point-value exit and re-entry still have no paper event. Do not retune the configs to chase them without a new protocol.
