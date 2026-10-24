# Step 1 — Verification plan for the settlement lifecycle

Status: draft for review. This file defines the plan. Carrying it out writes `step_01_inventory.md`. It does not create configuration files, change code, or launch tests or trading runs.

The stage map is the settlement lifecycle canvas. Stages stay in that order. The work of Step 1 is the four objectives below.

## Goal

State the correct behavior of each supported settlement lifecycle branch, attach the proof that already exists, and leave Step 2 a short list of missing checks and open defects.

## Objective

1. Define correct behavior at each branch, separately from what the code happens to do today.
2. Define the smallest set of setups that can exercise those branches.
3. Map the evidence already available, including what it does not show.
4. Produce an ordered Step 2 backlog.

Setups exist to reach a behavior. They are not a search for a more profitable strategy.

## Reference identity

The reference remains comparison + legacy hold: `twap_proxy_diffusion`, legacy volatility, 5-cent conservative edge, entry from 240 seconds remaining until 30 seconds remaining, maximum entry price 0.97, one-tick limit buffer, intended debit cap 5 USDC, one filled trade, hold to resolution, strategy retry off, no re-entry. The current paper file with that shape is `config/runs/settlement_value_leader_5c_240s_paper.yaml`. The 2-cent / 150-second baseline is a different identity and stays out of these setups.

Every variant keeps the execution constraints: debit cap, fee reserve, no top-up, durable episode budgets, and partial fills counted as positions. Account transport retries (`account.retry_attempts`) stay a different mechanism from strategy entry retries.

## 1. Correct behavior

For every branch, the inventory states:

- the starting condition or event
- the expected action and the resulting state
- what must remain true
- an observable pass/fail condition

Intended behavior is written first. Where today's code differs, the inventory records a question or a potential defect. Current behavior is not adopted as the rule by being observed.

### Restart, rollover, and shutdown

| Event | Persists | Resets or is rebuilt |
|-------|----------|----------------------|
| Restart in the same window | Confirmed exposure, cost, committed holds, unresolved order uncertainty, debit already spent, episode and attempt counts, POST ceilings | In-memory phase, rebuilt from the ledger and the host position. No new BUY for an order already pending, filled, or uncertain |
| Rollover to the next window | Ledger obligations for earlier windows, including committed holds and anything still unresolved | Window-local attempt state for the new window. Entry eligibility follows the new window's band |
| Shutdown | The same durable obligations. A committed hold is not sold to make the process exit | New entries. Resting orders follow the lifecycle rule already in force for that position |

Open questions the inventory must classify, not silently accept. `step_01_inventory.md` proposes answers and leaves them open for review. Step 2 does not implement them until that review accepts them.

- The driver restores attempt and POST ceilings from the ledger and starts the retry-duration clock again (`restore_window_usage`). Decide whether that clock reset is accepted or a defect.
- A same-market restored execution session with exposure can be recorded as `resume_flatten`. The intended behavior for uncommitted exposure is to recover management without a duplicate BUY and without dropping shares. If that path sells or stops instead, record the difference as a question.
- An ambiguous prior submission stopping the run is the current host behavior. The inventory states whether "stop" or "reconcile and continue" is the intended outcome, then labels any difference.

Pass/fail for restart: exposure and budgets after the restart match the pre-restart ledger, and the process does not submit a second BUY for the same attempt.

### Stage expectations

**1. Start or restart.** Load and validate configuration, take execution and ledger ownership, inspect prior orders and positions.

| Condition | Expected result | Must remain true | Pass/fail |
|-----------|-----------------|------------------|-----------|
| State clear | Prepare the market | No order during startup | Preparation starts with no exposure and the configured budgets |
| Committed hold on the ledger | Track it | Shares, cost, and hold treatment | No sell and no new BUY for that exposure |
| Uncommitted exposure | Recover management | Shares and the existing reservation | Quantity matches the ledger; no duplicate BUY |
| Uncertain or ambiguous order | No further BUY until reconciliation | No new reservation | BUY refused; ambiguous case follows the classified rule above |
| Invalid config or ownership | Block startup | Ledger unchanged after the refusal | Process does not trade |

**2. Prepare inputs.** Discover the five-minute market, bind Up and Down, connect required feeds, synchronize time, build books, establish the opening threshold, warm indicators.

| Condition | Expected result | Must remain true | Pass/fail |
|-----------|-----------------|------------------|-----------|
| Required inputs ready | Evaluation may enter | Facts belong to the bound window | An entry is possible only after those inputs are ready |
| Input missing, stale, or inconsistent | Wait with a reason | No entry intent | The reason names the failed input; intents are empty |
| Readiness returns | Evaluation may enter again | No order is created from the bad interval | The next entry uses the recovered inputs |

A recorded observation during a wait is not an entry.

**3. Evaluate.** On the declared triggers, use inputs, indicators, position, and permissions.

| Condition | Expected result | Must remain true | Pass/fail |
|-----------|-----------------|------------------|-----------|
| No qualifying opportunity | Wait | Budgets unchanged, no intent | Reason is a wait, not an order |
| Entry conditions pass and the account is flat | Entry request, episode pending | Side, size, price, and debit match the setup | Intent fields match the setup; phase is pending |
| Confirmed inventory | Manage that position | No additional BUY, no top-up | No entry intent while quantity is positive |
| Unknown inventory | Block | No assumed position | Blocked, no intents |

**4. Entry outcomes.** Permissions, settlement reservation, preparation, the final gate, durable dispatch authorization, then submit only if those pass. Until an outcome arrives the episode stays pending.

| Condition | Expected result | Must remain true | Pass/fail |
|-----------|-----------------|------------------|-----------|
| Not sent or zero fill, retry on, reason retryable, ceilings remain | Backoff, re-check current conditions, new attempt in the same episode | Attempt accounting increases once | A later attempt has a new intent id and respects backoff and ceilings |
| Not sent or zero fill, retry off or reason not retryable | Episode closed | No later entry in that episode | No further entry intent; reason recorded |
| Still pending | Wait | No second live order | No other BUY for that attempt |
| Partial or full fill | Position, filled once | Remaining shares and cost match the fill; original BUY is not repeated | Quantity equals the confirmed fill |
| Uncertain | Block until reconciliation | No other BUY | BUY refused until the outcome is known |
| Delayed or duplicate feedback | One transition | A late result for another intent id does not move the episode; exposure comes from the host position | State matches one outcome, not a replay of the same fill |

Retryable classes are `RETRY_WHEN_CLEARED` and `RETRY_AFTER_ZERO_FILL`. Ceilings are attempts, posts, prepare failures, and retry duration.

**5. Position and sales.** The exit policy is fixed before entry.

Hold to resolution: one hold commitment, then keep holding. Commit only with confirmed inventory, no unresolved mutation, and no live resting order. A committed hold survives restart and rollover and is not sold by mandatory flatten, the manual deadline, protection, or shutdown.

Managed exit: keep evaluating. A strategy exit, protection, or a host deadline may request a sale. At the exit cutoff, residual shares are committed rather than sold. Exhausted exit attempts leave the position in place without another sale request.

| Sale outcome | Expected result | Must remain true | Pass/fail |
|--------------|-----------------|------------------|-----------|
| Not sent, no fill, or partial | Manage what remains | Unsold shares stay owned; one sale at a time | Quantity falls only by the confirmed sale |
| Uncertain | Stay exiting | No second sell and no re-entry | BUY and a competing sell are refused |
| Flat, then quantity zero | Episode idle | A request alone does not close the position | Idle begins only after confirmed flat |
| Exit cutoff or attempts exhausted | Residual hold, or hold with no new sale | Policy does not flip to a discretionary sell | No further exit intent for that reason |

**6. Re-entry.** Only after a confirmed close. Retry is another attempt in the same episode after an entry that did not open a position. Re-entry is a new episode after idle with a filled episode already counted.

| Condition | Expected result | Must remain true | Pass/fail |
|-----------|-----------------|------------------|-----------|
| Count, cooldown, signal reset, time, side, edge, or window budget fails | Stay idle | The closed trade's debit remains spent | No entry; the failing reason is visible |
| Checks pass | New episode, return to evaluation | Prior episode stays closed | The new entry uses the next episode id and the remaining budget |

**7. Window end.** Entry eligibility closes at the cutoff. Outstanding orders, uncertainty, and positions stay owned.

| Condition | Expected result | Must remain true | Pass/fail |
|-----------|-----------------|------------------|-----------|
| Single-window run | Finish under lifecycle and shutdown rules | Open obligations are explicit | No new entry after cutoff; exposure is still recorded |
| Continuous run | Promote the next market when its checks pass | Prior unresolved work is not dropped | The new window starts clean; old obligations remain on the ledger |
| Hold committed | Ledger tracks it through settlement | Later entries respect account and exposure limits. A non-continuous run does not open a new entry while committed holds exist | Hold state is unchanged after promotion or shutdown |

**Ongoing.** Reconciliation resolves pending and uncertain orders without inventing a fill. Recording stores the decisions and outcomes the inventory cites, and a missing capture is reported as missing. Settlement tracking is the durable owner of committed holds, reservations, and redemption evidence. Account refresh, feeds, and protection run beside the stages; protection may request a sale only on a position whose policy allows it.

## 2. Verification setups

Describe these in the inventory. Do not write YAML in Step 1.

Rules:

- Reuse a setup when one run can show several compatible behaviors.
- Use an overlay for a small, named difference.
- Split setups when the lifecycle forbids the combination, or when combining the changes would make a failure ambiguous.
- Use a deterministic fixture or fault injection when a public market cannot reliably produce the event.
- Record every deviation from the reference and why it exists.
- Keep debit, fee, and position accounting as on the reference.

| Setup | Difference from the reference | Branches | Also covers | Why it is separate |
|-------|-------------------------------|----------|-------------|-------------------|
| S0 Hold | None | Clear startup, input readiness, no-opportunity wait, entry request, retry-off close, full fill to hold commit, committed-hold restart, single-window shutdown, continuous rollover | Flat recording and reconstruction; input-wait reasons if they occur | It is the reference identity |
| S0-reach overlay | Wider entry edge and/or time band, chosen before the run, accounting unchanged | A public-feed fill, hold commit, and restart while shares are open | Input readiness on a live book | Only so a bounded paper session can reach a fill. Economics of the 5-cent rule stay on S0 through existing replay and unit evidence |
| S1 Retry | Strategy retry enabled, with explicit backoff and ceilings | Retryable not-sent and zero fill, backoff, exhausted ceilings, restart of counters | Pending entry refusing a second BUY, when that state appears | Retry-off and retry-on permit different next states |
| S2 Managed exit | `exit.policy: managed_exit`, re-entry still one trade, protection off. Exit margin or threshold may be loosened, recorded in advance | Sale request, not-sent / no-fill / partial, flat, residual cutoff, exit-attempt cap | Position evaluation while held | Hold-to-resolution cannot sell. One trade keeps a sale from looking like a second episode |
| S2-protect overlay | S2 plus one protection block | Protection sale joins the same sale outcomes | Competing-sell refusal if both fire | Protection is host-owned. Its reason must be visible apart from the strategy exit |
| S3 Re-entry | S2 plus a second trade allowed, with cooldown, side, extra edge, and window debit set explicitly. Cooldown may be shortened, recorded in advance | Re-entry allowed and re-entry refused | Window debit after the first close | A second episode changes window accounting, so it is not part of the first sale proof |
| F Fixtures | No new market profile. Inject the event under the smallest setup that owns the branch | Uncertain submit, partial fill, delayed and duplicate feedback, concurrent sells, restart with exposure, rollover with an unresolved reservation, committed hold with protection configured | Ceiling exhaustion and retry-clock restart | These events are rare or unsafe to wait for on a public feed |

A committed hold with protection enabled is a negative check in F: the hold must not be sold. That check does not need its own paper profile.

## 3. Evidence already available

Each branch row points at shared evidence. A new run is not required just because the branch has its own row.

For each cited item record:

- the implementation it exercises (module or test name)
- whether a test exists, and whether a saved result shows that test passed
- saved replay, paper, or real-provider output, or an explicit skipped / unavailable / outdated mark
- source revision, configuration identity, and data or fixture identity
- the exact behavior demonstrated and the limits

Labels stay separate:

| Label | Means |
|-------|--------|
| Implemented | The behavior is in the reviewed source |
| Tested | An automated test exercises it. Cite the test. A skipped test is not a pass |
| Qualified | A recorded operational result on that revision shows it under the stated conditions |

The 20 Oct 2026 qualification report is an input to classify, not a blanket pass: 961 unit tests passed, 3 skipped, and 2,038 of 2,038 reconstructed evaluations matched on public-feed paper runs that stayed flat. Those runs support flat startup, evaluation, and reconstruction. They do not qualify a filled entry, a sale, or a restart with exposure. Research P&L does not qualify an execution branch.

Start the search at `tests/unit/strategies/settlement_value/test_episode_lifecycle.py`, `tests/integration/test_replay_v3_mechanics.py`, `Docs/implementation/strategies/trading_integrity_and_managed_exit_status_2026-10-20.md`, and `var/research/trading_integrity/`. Follow what those files cite. Rerun a cited result only when the file is missing or the tree has moved since the report. Step 1 records that fact; it does not launch the rerun.

## 4. Step 2 backlog

Every remaining gap is one backlog item:

- branch and expected behavior
- kind: known defect, missing evidence, unsupported combination, or documentation mismatch
- evidence that can be reused
- the smallest additional check
- setup or injected event
- pass/fail condition and the artifacts to keep
- priority and dependencies

Methods:

- Deterministic tests for precise failures, races, delayed or duplicate feedback, partial outcomes, and limits.
- A recorded public-feed paper run for operational integration on a live book with a simulated account.
- A real-provider check only for a question simulation cannot answer, such as venue latency, the provider fee schedule, or settlement payment. Specify it before any run.

Branches that need extra care, and that default to fixtures unless a paper capture happens to contain them: uncertain submission, partial fill, delayed or duplicate feedback, concurrent sells, restart with exposure, and rollover with an unresolved obligation.

A public-feed run has a stopping rule written beforehand: a duration or a window count. If the target event does not occur, keep the capture and leave the branch unverified. Do not lengthen the run or change a threshold afterward to force the event. A wider threshold is a new named overlay, chosen before the run.

Default priority, which the inventory may reorder with a reason:

1. Open questions in the restart contract, then the careful branches above.
2. S0 and S0-reach: a real fill, hold commit, and restart while shares are open.
3. S1 retry.
4. S2 sale outcomes and the protection overlay.
5. S3 re-entry, which depends on a confirmed flat from S2 or from a fixture.

## How the inventory is produced

`Docs/implementation/v_1_goal/step_01_inventory.md` has four parts:

1. Lifecycle and observable expectations, in stage order, including the restart contract and any question marked against the code.
2. The setups in the table above, with each deviation written out.
3. Existing evidence and its limits, one citation shared by every branch it actually supports.
4. The ordered Step 2 backlog.

Work in that order. Fill a branch from the expectations in this document, attach evidence, and emit a backlog item only when something is missing or in conflict. Then stop for review.

## Completion

Step 1 is finished when this plan and `step_01_inventory.md` have been reviewed together. The inventory contains the four parts above. It orders the missing work. It does not perform that work.

Missing demonstrations and defect fixes belong to Step 2. Step 2 starts from the accepted backlog and does not reopen the reference identity.
