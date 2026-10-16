# settlement_value: implementation plan for retry, continuous operation, exits and re-entry

Date: 16 October 2026. Revision 4: merged into `rest_project` after the v2 reference commit (tag `settlement-v2-reference`); the temporary worktree was removed. Research continues in notebook `05_staged_settlement_research_v3.ipynb`; notebook 04 is the completed v2 reference. **Nothing is enabled for real money.**

Source: [next-steps proposal](settlement_value_next_steps_2026-10-15.md). This is the implementation checklist for that proposal; use the reader's map below for scope and the P0–P8 sections for detailed tasks. Companions: [research-to-live plan](research_to_live_plan.md), [operator runbook](settlement_value_operator_runbook.md).

## Implementation status (16 October 2026)

Status words: **implemented** = code and automated tests exist; **researched** = evaluated on development data with a written conclusion; **qualified** = frozen and checked on reserved data, providers, observation and paper; **enabled** = switched on in a profile used for real money. A passing test is never "qualified" and an existing switch is never "enabled".

| Phase / objective | Implemented | Researched | Qualified | Enabled |
|---|---|---|---|---|
| P0 isolation, baseline identity, reference set, reserved evaluation sets | yes | n/a | n/a | n/a |
| P1 v2 baseline review | no (user's research track; stage A/B/C still running) | no | no | n/a |
| P2 replay/live order contract, shared `dispatch_check`, attempt trace, protection+hold rejection | yes | mechanics only (reference set) | no | v3 is the replay default for new experiments |
| P3 episode/attempt model, outcome contract, reason table, ledger schema 2, governor, restart seeding | yes | regression anchor passed | no | feature-off by default |
| P4 safe entry retry (objective 1) | yes | mechanics only; numbers provisional | no | no (`retry.enabled: false` in the trial profile; on in the paper profile) |
| P5 continuous runner, run limits, resolution worker, paper venue, ordered-window capital simulation (objective 4) | yes | capital simulation implemented, not yet run on a grid | no (paper soak not run) | no (live template refuses to validate) |
| P6 managed exits: strategy thesis/value exits, protection in replay and live, early-sale accounting, exit arbitration (objective 2) | yes | mechanics only | no | no |
| P7 serial re-entry after confirmed sale (objective 3) | yes | mechanics only | no | no |
| P8 freeze, fresh-data evaluation, provider checks, observation, paper soak, supervised live | no | no | no | no |

### What was built, by objective

**Objective 1 — do not lose a window to a recoverable entry problem (P2–P4).**
- Strategy episode state machine (`strategies/settlement_value/state.py`, `driver.py`): `IDLE → PENDING → {REARMABLE | UNCERTAIN | POSITION | CLOSED}`. Pending starts when the intent is created. The legacy single-attempt behaviour is reproduced exactly when retry is off.
- Outcome contract `strategies/outcomes.py` (`NOT_SENT`, `ZERO_FILL`, `PARTIAL`, `FILLED`, `UNCERTAIN`) and one reason → retry-class table `execution/outcome_reasons.py` (unknown reasons pause entries; they are never fast-retried).
- `retry` config section. User default: 3 attempts per episode including the first, provisional. It also has ceilings for preparation failures, POSTs per episode and per window, window-wide attempts and elapsed time, a minimum interval and exponential service backoff. Every attempt re-runs the full evaluation (signal, size, price, all gates). The side stays fixed within an episode. A partial fill becomes a position, with no top-up.
- Live host (`runtime/trading_runtime.py`):
  - one execution session per attempt (`stream:e<ep>a<att>`); user-stream evidence is routed to the session that owns the order (`execution/lifecycle.py`);
  - the outcome is delivered to the driver;
  - a terminal safe attempt is released, while uncertain or manual outcomes stop the runner with `MANUAL_INTERVENTION_REQUIRED`;
  - released attempts are watched for late REST evidence; a late fill blocks entries, and a ledger no-fill becomes `MANUAL_INTERVENTION`.
- Durable budgets: ledger schema 2 (`ATTEMPT_REARMED`, attempt-scoped dedupe keys, per-episode POST and preparation counters) and `WindowBudget` in the governor, so a restart cannot reset counts. The driver is re-seeded from the ledger when a window is re-bound after a restart.

**Objective 2 — sell before resolution (P6).**
- `exit.policy` is selected before entry:
  - `hold_to_resolution` is the default and a valid final choice;
  - `managed_exit` is matched by `lifecycle.mode: managed_exit`.
- Strategy exits (`economics.exit_walk`):
  - an economic value exit, taken when the fee-net bid exceeds the model hold value plus a margin;
  - a declared thesis stop;
  - an exit cutoff with a residual hold-to-resolution policy.
- Protection (SL/TP/trailing) runs in replay through the same pure trigger functions as live.
- One exit owner (`execution/exit_arbitration.py`), with precedence mandatory > SL/trail > thesis > TP/value. A committed hold is never sold by anything.
- `protection:` combined with `hold_to_resolution` is rejected.
- At stop, a managed position follows its residual policy (commit to hold) before any drain.
- The ledger handles early sales:
  - `EXIT_FILL_CONFIRMED` reduces cost basis proportionally;
  - `EPISODE_SOLD` records realized P&L = proceeds − total cost;
  - loss already locked in by a partial sale stays in open risk.

**Objective 3 — another trade after a complete exit (P7).**
- The `reentry` section:
  - `max_trades_per_window` counts episodes with confirmed exposure;
  - re-arming requires `signal_reset` or a `cooldown`;
  - SL/thesis lockouts;
  - same/opposite-side rules (opposite side is research-only);
  - an extra-edge requirement;
  - a last re-entry tau;
  - a window debit cap.
- A new episode requires a confirmed sale (`SOLD`), and in live a fresh account snapshot taken after release.
- The ledger allows a second episode only when no episode of the window is open, within the window cap.

**Objective 4 — run continuously across BTC 5-minute windows (P5).**
- `market.run_mode: continuous`: no deadline unless `maximum_duration_s` is set explicitly (a bounded soak).
- No stop after a hold or a no-fill.
- Committed holds are detached into the ledger, so the next window binds. Startup and rollover treat ledger-committed holds as tracked, not blocking (continuous only).
- Next-market preparation is retried when unavailable.
- `settlement.run_limits` are required, with no defaults:
  - a daily UTC loss limit that includes all open worst-case loss, so midnight never erases risk;
  - open worst-case loss and reserved-debit caps;
  - unredeemed count and cost caps;
  - collateral ≥ reservations + proposal;
  - consecutive-loss pause with operator acknowledgement (`tyrex-pm settlement ack-loss-pause`).
- Automatic resolution/payout tracking (`runtime/settlement_worker.py`); redemption stays an operator step.
- Paper venue (`execution/paper.py`):
  - one coherent simulated account (collateral, balances, trades, redemptions), restart-safe JSON journal;
  - fills against live public books;
  - no credentials read and no signing.
  - Paper state paths must contain `paper`.
- `windows.jsonl` per-window summaries, with Paris-time presentation of UTC accounting.
- Ordered-window capital simulation for research: `research/strategy_replay/sequence.py`.

### Replay result changes (development reference set, 23 windows × {friction, favorable})

| Comparison | Result |
|---|---|
| v2 (experiment checkout source) vs v3 with the v2 contract (`legacy_v2`) | **46 / 46 identical**: every decision, fill and economics field. Also pinned by `tests/integration/test_replay_v3_mechanics.py` against a saved v2 fixture. |
| v2 vs v3 aligned (live order contract), decisions | 23 / 23 identical decision sequences in each scenario: the strategy's decisions do not change. |
| v2 vs v3 aligned, economics | Fills happen in the same windows (friction 10/10, favorable 10/10). Each fill is smaller, because the planner spends ≤ capped notional / 1.02, rounded to 0.01 USDC, instead of the strategy's gross shares at walk prices. Total debit: friction 47.16 → 45.58, favorable 49.98 → 48.91 USDC. Total proxy-labelled P&L: friction 27.27 → 25.27, favorable 32.28 → 31.66 USDC. No final-gate refusals occurred with the shipped delays. |
| v3 retry on (provisional numbers) | Same fills, debit and P&L as v3 aligned. The only zero fill (friction, `btc-updown-5m-1792077600`) was not re-sent: the episode's side lost eligibility at the next evaluation, so pursuit stopped (`RETRY_SIDE_NOT_ELIGIBLE`) as specified. 22/23 friction decision sequences are unchanged. On this small set retry recovers nothing; P1's no-fill inventory must size the real opportunity. |

These are development-window mechanics checks, not strategy evaluations. The stage A/B/C conclusions must be re-derived under v3 as new experiments (the experiment id now includes the execution-contract version).

### Tests

- Full suite in the worktree: 768 passed, 2 skipped (ipywidgets not installed), including the recorded-window integration tests. Ruff is clean on all changed and new files.
- New test modules:
  - `tests/unit/strategies/settlement_value/test_episode_lifecycle.py` (retry, uncertain blocking, ceilings, backoff, side lock, partial, managed exits, re-entry, lockouts, restart seeding);
  - `tests/unit/runtime/test_live_retry_and_continuity.py` (fake-backed live path: zero fill → fill, preparation failure → recovery, POST timeout with no duplicate BUY, durable budget across restart, cutoff, late fill, stream routing, continuous detach, managed sale + re-entry, committed hold never sold);
  - `tests/unit/execution/test_settlement_ledger_v2.py`;
  - `tests/unit/execution/test_outcome_reasons.py`;
  - `tests/unit/execution/test_paper_venue.py`;
  - `tests/unit/research/replay/test_engine_amount_buy.py`;
  - `tests/unit/research/strategy_replay/test_sequence_capital.py`;
  - `tests/unit/runtime/test_run_config_lifecycles.py`;
  - `tests/integration/test_replay_v3_mechanics.py`.

### Remaining live-readiness blockers

1. **P1 research:** finish and review v2 A/B/C, then rerun the shortlist under v3. Size the retry opportunity from the no-fill inventory, and derive exit hypotheses. The numeric retry, exit and re-entry settings stay provisional until then.
2. **Retry/exit/re-entry research:** compare retry-off vs on and hold vs managed exits on development windows. Run the ordered-window capital simulation on the grid (`sequence.run_sequence`).
3. **Provider qualification (P8):**
   - actual resolution rule, K and ties;
   - live reference feed vs recorded (RTDS → PolyBolt);
   - real fee schedule;
   - buy-fee-in-shares assumption;
   - the venue's amount rounding for market BUYs (replay assumes 0.01 USDC spend and 0.01 share precision);
   - sell-fee charging (early-sale proceeds are estimated with the strategy fee curve);
   - order/confirmation latency.
4. **Observation and paper soak:** at least 24 h continuous paper run on live feeds with a restart, every boundary accounted for, and no accounting divergence. Not run.
5. **Explicit live monetary limits:** the live continuous template fails validation until the operator sets every `run_limits` value. Also set `settlement.redemption_wallet` and `TYREX_POLYGON_RPC_URL` (resolution worker).
6. **Known limitations to resolve or accept before any live use:**
   - opposite-side re-entry is refused live (fill attribution is scoped to one token per window);
   - resting TP orders are not supported by the paper venue;
   - an uncertain entry outcome stops the continuous runner for operator review instead of auto-resolving;
   - after a restart inside a window the retry elapsed-time budget restarts (attempt/POST ceilings still hold).
7. **Fresh evaluation data:** M1 (retry+hold) uses the frozen holdout (39 windows including the split extension). M2 (exits/re-entry) needs a new collection recorded after 2026-10-16 (see `var/research/reference/evaluation_reservations.json`). The collector was not restarted by this work.
8. **v3 verification of the two frozen candidates** (notebook 05 section 2, about 50 minutes at 4 workers): run before trusting v3 numbers for them.

## Reader's map: the four objectives remain the same

The goal is to take a researched entry signal and give it a complete, reliable trading lifecycle. The probability model and entry parameters are still selected through research. The implementation adds the ability to recover, exit, re-enter and continue across windows, with the same declared policy exercised in replay and live.

### Mapping to the user's original objectives

1. **Do not lose a window to a recoverable entry problem → P4, supported by P2/P3.** Retry after a known no-dispatch or confirmed zero-fill outcome, provided the signal and all limits still pass. Pending/uncertain orders block another BUY. A partial fill becomes a position; no automatic top-up initially.
2. **Allow selling before resolution → P6.** Add managed-exit execution and accounting, then evaluate strategy-value exits and SL/TP. Keep hold-to-resolution available as an explicit alternative. Exit capability remains part of the target even if a tested policy leaves it disabled.
3. **Allow another trade after exiting, in the same window → P7.** Add a new episode only after confirmed closure, with fresh signal, time, capital, cooldown/reset and window budgets. Building the capability and deciding to enable it are separate decisions.
4. **Run continuously through successive BTC 5-minute windows → P5.** Prepare the next market, track any old holdings, reconcile resolution, respect shared-account limits and stop cleanly. Begin with one filled episode per window; later combine the runner with P6/P7. Continuing while cash or risk limits are exhausted is not a target behavior.

P0–P3 are prerequisites that make these four behaviors trustworthy. P8 checks the chosen combination before live promotion. They are not additional trading objectives.

### What belongs to replay, live or both?

**Shared policy and contracts — affects both.** Entry/exit eligibility, order sizing and limits (P2); episode/attempt identity and outcome feedback (P3); retry rules (P4); exit rules (P6); and re-entry rules (P7). Implement these rules once where practical, with separate adapters delivering historical/simulated evidence or real evidence. Shared rules do not imply identical simulated and real fills.

**Research replay — simulated execution, no real orders.** Feed recorded windows through the shared rules; model latency, book arrival, amount-based fills and fees; translate SELLs; compare policy variants; and report per-attempt, episode and window results. P1 is research-only review. P2 changes replay execution assumptions and can change historical results even before retries are enabled. P4/P6/P7 also change replay outcomes when enabled. Ordered multi-window simulation tests shared capital and delayed payouts for P5; isolated one-window results cannot establish continuity.

**Live operations — real account and venue.** Account reads, signing/submission, authenticated execution evidence, reconciliation, durable state and restart recovery support all features. P5 adds continuous supervision, next-market preparation, outstanding holdings, account-wide limits and shutdown ownership. P6 adds actual sales; P7 permits new entries after closure. Live execution uses real evidence instead of replay's simulated evidence.

**Paper mode — a test of the live runner without real orders.** Use live public feeds and a coherent simulated account/venue. It exercises the operational runner with fake money. It differs from historical replay and cannot prove actual fill quality or profitability. Signal-only observation is simpler: live feeds and recommendations, without pretending to execute trades.

**Data and qualification — supports both.** P0 freezes experiment inputs and reserves evaluation windows. P8 freezes the complete selected policy, checks untouched data and providers, then validates observation/paper behavior and bounded supervised real execution.

### Order and parallel work

- Research track: new-data preflight → frozen A/B/C → P1 review. Results select parameters and supply retry/exit hypotheses.
- Engineering track in an isolated checkout: P0 → P2 execution alignment → P3 lifecycle foundation.
- After P3: P4 retry mechanics and P5 continuous-runner work may proceed in parallel with coordinated shared-interface changes. P6 replay mechanics/research can also begin; integrated live qualification waits for its dependencies.
- P7 follows reliable complete-exit accounting. Numeric retry/exit/re-entry settings await evidence, even if the mechanics are already built.
- First operational milestone: continuous retry+hold, one filled episode per window. Later milestone: the same runner with managed exits and optional re-entry. G1 decides whether real-money trials begin at the first milestone or wait for the later one.

Keep the baseline experiment checkout unchanged during its intended A/B/C sequence. A separate checkout must capture needed uncommitted/untracked files and use separate imports, kernels and mutable outputs/state. No infrastructure implementation needs to wait for stage C when that isolation is established.

### How to follow progress without losing the objective

At each phase completion, record: original objective served; shared/replay/live areas changed; feature flag and default; meaningful tests/evidence; whether results changed; and what still prevents live promotion. Distinguish **implemented**, **researched**, **qualified** and **enabled**. A passing test or an implemented switch does not mean the feature is enabled or ready for real money.

## 0. Review of the proposal

**Accepted as is.**
- The order: alignment → retry → continuous hold → exits → re-entry.
- The opportunity / episode / attempt / re-entry vocabulary.
- The rule that "pending" starts when an intent is queued, not when a fill is confirmed. This corrects the earlier suggestion to move the latch to fill confirmation.
- Uncertain outcomes block until reconciled.
- No top-up after a partial fill.
- Committed holds keep their meaning.
- Shared-account risk across windows.
- One qualification per named policy version.

**Adjustments made in this plan.**

1. **Work isolation is a hard constraint.** Current source fingerprints protect resumes of an existing stage, but do not universally enforce identical code across newly started stages. Their listed trees also omit `execution/`, where P2 changes the planner/gate. Keep a frozen baseline checkout; implement separately and extend new-version provenance to every result-affecting dependency. Do not use `allow_code_change` as an isolation substitute.
2. **Fresh evaluation data needs lead time.** Reserve untouched windows for each qualification milestone, using existing untouched recordings or further collection. Restart the collector only if its running process predates the recorder fix. Current collection discovery is automatic; saved splits still control inclusion.
3. **Reuse existing test venues; build paper operation explicitly.** Tests already include fake gateways/venues for submission, cancellation, recovery and connected workflows. P2 extends these into a reusable deterministic parity harness. P5 adds the coherent simulated account/venue composition for live-feed paper operation; a complete paper exchange is not required before all foundation work.
4. **Regression gates use the right baseline.** P2 deliberately changes sizing/gates: explain differences from v2 and save aligned-hold reference results. P3 with new features disabled must reproduce those P2 decisions, planned orders and economics. Exclude timestamps, timings, generated IDs and other nondeterministic metadata from semantic comparisons.
5. **Design the ledger once; deliver incrementally.** P3 defines identities/accounting that accommodate attempts, partial fills and early sales, and implements backward reading, migration and retry-related events. P6 activates and completes tested early-sale behavior. Do not require every future exit detail before P4.
6. **Alignment scope is bounded.** P2 shares the planner and the pure pre-send checks, and records an attempt trace. Account, transport and settlement failures are covered by failure-injection fixtures in P4. P2 does not build a calibrated model of them.
7. **Decision gates are named** (G0–G4) with the question each answers. They replace "decide at the relevant milestone".

## 1. Cross-cutting rules (apply to every phase)

- **Default off.** Each feature lands behind config and defaults to current behaviour. Enabling it is a visible config change, recorded in provenance.
- **Versioning.** Any change to execution semantics bumps the replay version (currently `strategy_closed_loop_v2`) and the cache compatibility key. Strategy, lifecycle, protection and run-policy identity go into every result's provenance.
- **No mixing.** New behaviour never resumes or reuses parts of a v2 experiment. Each phase that changes results runs a new experiment.
- **Attribution chain.** The comparison sequence is: v2 hold → aligned hold → retry+hold → retry+exit → retry+exit+re-entry. Each step is compared with the previous one on identical windows and parameters, with windows as the unit of comparison.
- **Holdout discipline.** Retry, exit and re-entry rules are developed only on development windows. A holdout set used to inform a later change counts as development data from then on.
- **Bounded recovery.** Count created candidates, preparation attempts and POSTs separately. Hard time and preparation-attempt ceilings apply even if no POST occurs; limits survive restart. Ineligible scheduler ticks are not order attempts.
- **Evidence identity.** Hash the actual result-affecting source/configuration, including shared execution policies and simulator/protection dependencies when introduced. Preserve old result identities and derive new-version caches from the new contracts; a runner version string alone is insufficient.
- **Live safety.** No phase authorises live trading. Live steps happen only at decision gates, and only if the user says so explicitly.

## 2. Phase overview

| Phase | Goal | Depends on | Changes results? | Relative size |
|---|---|---|---|---|
| P0 | Isolate work; start fresh data collection | none | no | S |
| P1 | Review the v2 baseline; shortlist, no-fill inventory, exit ideas | v2 A/B/C | no | M (research) |
| P2 | Same order economics/checks; reusable parity harness; config validation | P0 | yes (new version) | L |
| P3 | Episode/attempt identity, outcome feedback, ledger v2, governor counting; behaviour unchanged | P2 | no (gate) | L |
| P4 | Opt-in safe entry retry | P3, P1 no-fill inventory | yes | M |
| P5 | Continuous hold runner and coherent paper account; multi-window capital simulation | P3 (P4 recommended) | yes at run/account level | L |
| G1 | Decide on small-money retry+hold trials | P4, P5 | — | — |
| P6 | Managed exits (one filled episode per window) | P2/P3 for mechanics; P1 for selection; P5 for integrated runner | yes | L |
| P7 | Serial re-entry | P6 + research support | yes | M |
| P8 | Freeze, fresh-data evaluation, observation, supervised live | P7 or chosen stop point | — | M |

P5 can be built in parallel with P4 once P3 has landed. P6 research (replay only) can start once P2 and P3 are in place, in parallel with P5 engineering.

---

## P0. Isolation and data (now, while A/B/C run)

**Affects:** research/data preparation and implementation isolation; no trading-policy change.

**Goal.** Allow engineering to proceed without invalidating the running experiment, and start building fresh evaluation data.

Tasks
1. Create an isolated worktree/checkout with a branch such as `codex/settlement-live-readiness`. Capture the actual current implementation, including necessary uncommitted/untracked files; HEAD alone may omit research code. Use separate source imports, notebook kernels and mutable outputs/state. Keep the experiment checkout frozen through its intended A/B/C sequence. Record implementation-source identity separately.
2. Save the baseline source snapshot/fingerprint, environment/dependency identity, selected windows and split identity so v2 comparisons remain reproducible. In the new implementation version, extend provenance to shared execution and simulator dependencies, not just the old fingerprint list.
3. Check the collector's running build; restart only if it predates the `clob_time` recorder fix. Further recording should use a separate collection from frozen experiment inputs. Current `batch.all_collections()` discovers `var/warehouse*/collections/` automatically. Confirm selected dates/counts in notebook 04; unsplit windows remain excluded. Preserve prior holdout assignments rather than blindly resplitting a larger inventory.
4. Declare separate reserved evaluation windows for the retry+hold and later enhanced-policy milestones, or collect a fresh set before the later milestone. Quality/integrity checks are allowed, but do not inspect strategy outcomes or tune using reserved data. Freeze/version the assignments before evaluation and leave an active experiment's split unchanged.
5. Pick a representative **development reference set** for regression gates (20–30 windows is an initial target, not a hard requirement). Cover fills, zero fills, skips and gaps; use deterministic fixtures for rare cases such as partial fills if historical examples are absent. Save v2 economic/decision evidence now and the aligned P2 reference later.

Exit criteria: implementation and experiment sources/imports/outputs are isolated; additional recordings use the intended collector build; the new grid's data selection and reserved evaluation assignments are explicit; reproducible baseline references are saved. This plan update itself performs none of those operations.

## P1. v2 baseline review (research, no runtime changes)

**Affects:** research analysis only; produces choices and hypotheses, not new execution behavior.

**Goal.** Decide what the hold strategy is worth. Measure how many opportunities retry could recover, and form exit ideas.

Inputs: v2 stages A/B/C, with timelines kept.

Tasks
1. Check data quality on every development window: replay version, clock provenance, completeness, host-skip reasons, gaps.
2. **No-fill inventory.** For every intent that was created but did not fill, record:
   - window and time to close;
   - actual reason (engine rejection or cancellation, such as FAK zero-fill cancellation, `below_minimum` or `off_tick`; do not invent FOK failures for a FAK-only run);
   - zero fill vs partial-fill remainder;
   - how long the signal stayed eligible afterwards;
   - whether later observable signal/book conditions remain eligible. Label counterfactual checks; v2 recordings cannot establish unrecorded live account/transport readiness or every aligned gate.
   Output: counts by reason and windows affected; potential recovery opportunities, not assumed recovered fills or profits. P4 measures the counterfactual under explicit simulation assumptions.
3. Results: fill rate, fees, slippage over the latency, P&L per window, sensitivity to scenarios, stability across both collections and neighbouring parameters, and concentration in a few windows.
4. **Exit-hypothesis material** for selected trades:
   - executable bid depth over time;
   - model probability vs best bid;
   - maximum favourable and adverse movement, against time remaining.
   Write two to four explicit exit ideas, for example "sell when the net executable bid is above the model hold value plus a margin", "thesis-break stop", "time-based rule".
5. Write the review note `settlement_value_v2_review.md`.

Deliverables: a provisional parameter shortlist (or the conclusion that nothing is worth promoting), the no-fill inventory, and the exit hypotheses.

**Gate G0.** Is the hold strategy worth continuing? What does the evidence imply for retry tuning and research priority? Rare historical no-fills can lower the priority of tuning retries, but do not remove the user's reliability objective. Keep bounded, safe recovery in scope; P5 may be developed first while P4 settings remain provisional.

## P2. Replay/live execution alignment

**Affects:** both. Shared sizing/gates and live refactoring, plus changed replay execution assumptions. Real fills remain venue-controlled.

**Goal.** Make replay produce the same order economics and the same pre-send decisions as live for the same inputs, so that later comparisons mean something.

Scope: `execution/planner.py`, `execution/final_gate.py`, `research/strategy_replay/run.py` (`_order`, feedback), `runtime/decision_records.py`, `runtime/run_config.py`, existing test venues and a reusable parity harness. New paper composition follows in P5.

Tasks
1. **Shared order planner.** Replay builds its orders through the same planner contract as live: debit cap, fee reserve (`1 + fee_reserve_rate`), BUY by collateral amount, limit adaptation, rounding, venue-legal size. The replay engine walks the book by spend amount for BUYs and stops at the limit, matching the exchange's amount-based BUY. It no longer sends `intended_gross_shares`.
2. **Pure pre-send checks.** Extract the decision logic of `FinalExecutionGate` and the host checks (entry cutoff, candidate age, binding/token identity, book freshness and sync, tick changes, price moved, depth minimum) into pure functions that take an injected clock. Live calls them with its real clock; replay calls them with virtual time. Live I/O (signing, POST, account reads) stays outside them.
3. **Phase timing in replay.** Decision → prepare → final check → order arrival → feedback, each checked against the book visible at that time, with no future data. Latencies come from the scenario config.
4. **Attempt trace record**, written by both replay and live: requested notional, allowed debit, spend, gross/net shares, adapted limit, fees, phase times, each gate's outcome, evidence provenance. Reports and the explorer read it.
5. **Reusable deterministic venue harness.** Inventory/reuse the existing gateway and venue doubles, including submission/cancel/recovery/connected-workflow tests. Exercise the actual planner, gates and lifecycle interfaces with recorded-book/fixture evidence, partial fills, rejections, delay and uncertainty. Begin with the order types required by this strategy; avoid a general exchange simulator as a P2 prerequisite. P5 extends these contracts into live-feed paper composition. Simulated matching remains an explicit assumption, not proof of actual exchange fills.
6. **Config validation.** `run_config.py` rejects `protection:` together with `lifecycle.mode: hold_to_resolution` by default. Any future pre-commit-only exception must be explicit and tested.
7. Assign a new replay semantics version (proposed `strategy_closed_loop_v3`, subject to the actual implementation state), extend source/config hashes and cache compatibility to the new dependencies, and rerun the P1 shortlist as a new experiment ("aligned hold"). Alignment coding/fixture tests can precede P1; shortlist comparison waits for it. Do not overwrite v2 references.

Tests
- Fixtures run through replay and through the live path on the simulated venue must produce identical planned economics and identical gate outcomes. Cases: amount-based fills across several price levels, fee reserve and rounding, a book that moves between decision and arrival, candidate expiry, cutoff passing.
- The config rejection test.
- Determinism: identical inputs give identical normalized decisions, planned economics, execution events and semantic digest; exclude generated IDs, wall timestamps, profiling durations and storage paths. Live-vs-replay fixtures compare the shared contract, not incidental event envelope bytes.

Exit criteria: parity tests pass. The aligned shortlist rerun is complete, and every difference from v2 on the reference windows is explained (expected cause: sizing and gate differences).

## P3. Episode/attempt foundation (behaviour unchanged)

**Affects:** both. Shared lifecycle/outcome contracts; live durable recovery/accounting and replay feedback/reporting. Feature-off economics match the P2 baseline.

**Goal.** Give the system the data model and feedback paths that retry, re-entry and exits need, without changing any decision yet.

Scope: `strategies/settlement_value/state.py` and `driver.py`, the strategy context, `runtime/trading_runtime.py` (intent outcome delivery, `seen_intents` and `semantic_key`), `execution/lifecycle.py` and `session_state.py`, `runtime/settlement_hooks.py`, `execution/settlement/ledger.py` and `governor.py`, replay feedback, reports.

Tasks
1. **Identities.** `run_id`, `window_id`, `episode_seq` (per window), `attempt_seq` (per episode). Every order gets a unique client order id derived from these. Episode id: `run_name:window_id:e{seq}`. Audit `semantic_key`, session ids and evidence routing, so that a legitimate new attempt is not deduplicated and an event from an old attempt cannot touch a new one.
2. **Strategy state machine** replacing `entry_emitted`:
   `ELIGIBLE → PENDING (intent queued) → {UNCERTAIN | POSITION | REARMABLE | CLOSED}`; `POSITION → HOLD_COMMITTED | (later) EXITING → FLAT`.
   With features off, `REARMABLE` maps straight to `CLOSED`, which is today's behaviour.
3. **Outcome feedback contract.** The host sends an explicit `EntryOutcome` to the driver:
   - `NOT_SENT(reason, retryable)`;
   - `ZERO_FILL_CONFIRMED`;
   - `PARTIAL(confirmed_shares)`;
   - `FILLED`;
   - `UNCERTAIN(order_id)`;
   - `RESOLVED_FROM_UNCERTAIN(result)`.
   This includes runtime blocks that happen before `lifecycle.open`; today those return without creating a session. Replay delivers the same contract.
4. **Reason classification table.** Each refusal reason gets one class: retryable-when-cleared, retryable-after-zero-fill, blocking-until-reconciled, or structural (pause entries). Sources: `settlement_value/reasons.py`, the host refusals in `trading_runtime.py`, the final gate reasons, the governor reasons. Kept as data and covered by tests.
5. **Ledger schema v2** (one designed migration, staged implementation):
   - episode sequence per window, and attempts within an episode;
   - reservation, release on proven zero, and re-reservation;
   - fill events that update the position incrementally;
   - an extensible contract for early-sale and partial-exit events (activated and fully tested in P6);
   - realised P&L per episode.
   Read v1 journals backward-compatibly and preserve original evidence; use backed-up, separate v2 outputs with equivalence checks before activation. Replace window uniqueness with an explicit open/uncertain-episode rule plus a cap. In this foundation phase the feature-off cap remains one episode, preserving current behavior; P4 permits several attempts within it and P7 later permits several filled episodes.
6. **Governor counting.** Count attempts, started episodes, filled episodes and pending capacity as separate quantities. `EPISODE_ALREADY_EXISTS_FOR_WINDOW` becomes "open or uncertain episode exists", or "window episode cap reached".
7. **Persistence.** Counters and reservations survive a restart. Startup reconciliation understands ledger v2.
8. **Reports:** per-attempt and per-episode rows, plus window aggregates. The existing filled/entered booleans become derived fields.

Tests: state-machine transitions; outcome delivery for every reason class; ledger v1 read and v2 round-trip; restart with an open reservation; duplicated or out-of-order evidence; a late event for an old attempt.

**Regression gate:** with every new feature off, reference windows reproduce the normalized P2 aligned-hold decisions, planned orders and economics. New reporting identities are allowed and mapped explicitly. Existing relevant unit/contract suites pass. P3 is not required to reproduce the pre-alignment v2 economics.

## P4. Safe entry retry (opt-in)

**Affects:** both. Replay tests retry policies; live safely recovers actual entry attempts. Serves original objective 1.

**Goal.** Don't lose a window to a recoverable refusal or a confirmed zero fill, without ever risking duplicate exposure.

Config (new, under the strategy or run policy):
```yaml
entry_retry:
  enabled: false
  max_attempts_per_episode: <from P1>
  max_attempts_per_window: <from P1>
  max_retry_duration_s: <from P1>
  max_prepare_attempts_per_episode: <measured>
  max_prepare_attempts_per_window: <measured>
  max_posts_per_episode: <measured>
  max_posts_per_window: <measured>
  backoff: {min_interval_ms: <measured>, on_service_error: exponential, max_ms: <measured>}
```

This is a proposed settings sketch, not executable configuration. Define an attempt as one newly authorized entry candidate, and document exactly when each counter increments. Report created candidates, preparation starts and POSTs separately; enforce every applicable ceiling plus the hard elapsed-time budget. A prepare failure consumes preparation/attempt capacity even with zero POSTs. Counters cannot be reset by a new quote or a process restart.

Rules (from the proposal, §4)
- **Re-arm** only after `NOT_SENT(retryable)` once its blocker clears, or after `ZERO_FILL_CONFIRMED`.
- **Block another BUY** on `UNCERTAIN` or matched/mined/`RETRYING` trade states until reconciled. A confirmed `PARTIAL` is a position to manage, not permission to top up. Never send another BUY because a response timed out; normal holding/exit management is not blocked by the no-top-up rule.
- **Pause entries** on structural reasons (auth, config, metadata, capital, expired window), with a visible reason.
- Every attempt recomputes signal, depth, fees, edge, size and limit from current facts, and passes all gates including the cutoff.
- The side stays fixed within an episode. If that side loses eligibility, pursuit stops.
- Wake-ups are driven by reason, not by quote ticks. Ineligible ticks don't count as attempts. Budgets carry across attempts.
- Live: a no-fill no longer sets `stop_requested` when retry is enabled (`trading_runtime.py:1572`). The session is released and the driver re-arms.

Tasks: driver re-arm logic; host wake-up scheduling; releasing the lifecycle session after proven zero; failure injection on the reusable venue harness; replay support; report fields (candidate/prepare/POST counts, recovery latency, cutoff expiries, outcome by reason).

Tests, using the simulated venue:
- temporary no-POST failure, then recovery;
- zero fill, then fill;
- price moves outside the edge before the retry;
- partial fill;
- POST timeout followed by a delayed fill (must not send a duplicate BUY);
- duplicate or out-of-order evidence;
- stream gap;
- restart during reservation or dispatch;
- attempts, preparation capacity or POST capacity exhausted, including repeated failures before any POST;
- cutoff passes while waiting;
- permanent rejection;
- concurrent wake-ups.
Invariant checked in every test: no duplicate BUY, no overspend, no premature release of a reservation.

Evaluation: retry-off vs retry-on on identical development windows and parameters, as a new experiment. Report fills, debit, P&L, attempts, recovery latency, and reason counts. More fills alone is not success.

Exit criteria: all tests pass; the comparison report is written; numeric settings are chosen and justified from P1 data and measured latency.

## P5. Continuous hold runner (one filled episode per window)

**Affects:** primarily live operations and paper testing, plus ordered-window capital simulation. Serves original objective 4; initially without early exits or re-entry.

**Goal.** One process that trades window after window until stopped, with hold to resolution, safe across restarts, inside account-wide limits.

Scope: `runtime/trading_runtime.py`, `market_data_runtime.py`, `market_runtime.py`, `market_family.py`, `run_config.py`, `startup_controller.py`, `execution/startup_reconciliation.py`, `execution/settlement/resolution.py`, `application/cli.py`, a new run-policy module, the simulated venue (paper mode).

Tasks
1. **Run policy** replaces the single-trial completion. After a terminal episode the runner can stay eligible in the same window, wait for the next window, or stop the run. User stop, hard risk limit and manual-intervention-needed are separate durable states. Remove the hold-commit and no-fill `stop_requested` paths behind the policy.
2. **`duration: continuous` mode.** Don't fake it with a large `maximum_duration_s`. Use `process_lock.py` for single ownership.
3. **Rollover.** Reuse the existing market-family preparation and promotion loop. Before promotion, prepare books, metadata, account state and evidence routing. At promotion, verify market/window/token identity, clear/detach only qualified old execution scope, require fresh facts, and capture the new opening threshold. Carry nothing per-window across: K, side, cooldowns, protection, queued intents. Measure the readiness gap; prewarming cannot remove the wait for valid K, signal and execution facts.
4. **Detaching held positions.** A committed hold moves into a tracked settlement portfolio. Its old-market evidence and reconciliation keep running while the new market is bound. Only a known, reconciled holding can be detached. Unknown dispatch, unmatched attribution or unreconciled exposure still blocks promotion.
5. **Account-wide limits (durable, restart-proof):**
   - loss per period, with a defined timezone and reset;
   - total open worst-case loss;
   - reserved and unknown debit;
   - unresolved count and cost;
   - available collateral, excluding receivables;
   - consecutive-loss pause, with a defined rule for delayed resolutions;
   - a kill switch.
6. **Automatic resolution and payout reconciliation** run as a worker inside the runner. Redemption stays operator-driven at first. The runner pauses entries when usable cash, or the unresolved/unredeemed caps, run out.
7. **Graceful stop.** Close the BUY gate, drain submissions and cancellations, keep committed holds and their resolution worklist, and record who owns the remaining settlement work after the stop.
8. **Health and observability.** Heartbeat, per-window summary records, the run metrics from the proposal §8, and alert hooks. External notification channels need separate approval.
9. **Paper composition.** Live public feeds plus simulated submission, account balances/allowances, reservations, orders/trades, authenticated-event equivalents, fees and payout/redemption evidence. All components use one coherent simulated account state and retain restart-safe paper journals in separate locations. Reuse the shared driver/lifecycle/supervisor, but inject the complete execution/account/evidence composition rather than swapping POST alone. Paper operation must not construct a real signing/submission path or require real trading credentials. Public read-only provider facts remain allowed.
10. **Ordered-window capital validation.** Run sequential-window simulations sharing capital, reservations, delayed resolution and unredeemed payouts. Cover cases where independent window replays would have enough starting cash but the continuous account does not. Include pause/recovery behavior and measure run-level outcomes; P5 can change executable trade availability even with unchanged per-window signals.

Tests:
- consecutive boundaries with no signal;
- filled episodes across boundaries;
- a held position at a boundary;
- the next market is unavailable;
- a feed gap at the boundary;
- a stale queued entry;
- late evidence from the previous window;
- clock loss;
- restart during promotion;
- user stop during prepare, POST, or exit;
- the loss limit is hit;
- cash runs out while payouts are pending.

Soak target: paper mode for at least 24 h on live BTC 5m markets (about 288 scheduled windows). Report actual covered/missed boundaries, rather than assuming all windows traded. Combine the soak with controlled disconnect/restart/provider-failure scenarios. Pass conditions: no unexplained accounting divergence or duplicate exposure; limits enforced across restart; every boundary/outcome accounted for; rollover/recovery delays measured; unresolved evidence pauses safely and recovery is documented. Duration alone is not proof of correctness. Paper fills do not establish real fill quality or profitability.

Exit criteria: the tests pass, the paper soak passes, and the runbook is updated with continuous-mode procedures.

**Gate G1.** Run small-money, supervised retry+hold trials now, or keep the runner in observation/paper mode until exits are ready? If now: qualify the frozen version through the applicable P8 data, provider, observation and paper checks, using its separately reserved evaluation set. Then one window at a time, followed by bounded multi-window trials. Reserve fresh evidence for later exit/re-entry qualification if these trial/evaluation results inform their development.

## P6. Managed exits (one filled episode per window)

**Affects:** both. Shared exit policy, replay SELL/economics research, and live SELL/accounting/recovery. Serves original objective 2.

**Goal.** Let a position be sold before resolution, either because the strategy's own valuation says so or as a protective stop, with correct accounting in replay and live.

Tasks
1. **Lifecycle choice per policy, selected before entry:** `hold_to_resolution` (the current baseline, kept as a named config) or `managed_exit`. In managed mode a position is open and exitable, never auto-committed. Existing committed holds keep their meaning.
2. **Exit mechanics:**
   - shared remaining-share SELL sizing (`execution/exit_sizing.py`);
   - confirmation and cancellation;
   - bounded retry (reuse `lifecycle.submit_exit` / `exit_retry_limit`);
   - sell fees and dust classification.
3. **Replay adapter:** translate Exit, Flatten and Cancel intents. Handle SELL feedback, cost-basis reduction, net proceeds, cash availability and dust. The low-level engine already supports SELL.
4. **Ledger:** use the early-sale and partial-exit events from schema v2.
5. **Exit arbitration:** precedence between protective exit, strategy exit, mandatory close and resting-TP cancellation. A single exit owner ensures one SELL at a time, and every retry is sized from confirmed quantities.
6. **Exit cutoff and residual policy:** the latest point to submit a sale, the time budget to finish or escalate it, and what happens to inventory carried past that point.
7. **Exit research** (replay only, development windows), using the hypotheses from P1:
   - strategy exit: net executable sale value across full bid depth, after fees and latency, compared with the model hold value, with the convention stated explicitly;
   - thesis-break and time-based rules;
   - the shared `protection/` SL/TP for comparison.
   Entry cost is sunk for the hold-vs-sell choice. A risk stop is declared as a risk rule, not as an expected-value claim.
8. Add resting TP (GTC/GTD) only after cancel-vs-fill race tests exist.

Tests:
- partial and zero-fill exits;
- sellability declining;
- stale or missing bid;
- dust;
- SL and TP at the same time;
- a late entry fill;
- a TP that fills during cancellation;
- fee rounding;
- expiry near the close;
- restart with an exit pending;
- never selling more than is held.

Evaluation: hold vs retry+hold vs retry+exit on the same windows. Net P&L, tail loss, turnover, fees, exit reliability. Re-check the neighbouring entry parameters, since the best entry may change under exits.

**Gate G2.** Does a managed-exit policy meet the predeclared net-performance and risk objectives relative to retry+hold after fees? A risk-oriented exit may trade some expected return for reduced tail loss; state that tradeoff rather than requiring higher P&L alone. Keep exit capability in scope, and choose whether/how to enable it in the frozen policy, or retain hold behavior.

## P7. Serial re-entry (only if P6 research supports it)

**Affects:** both. Shared new-episode eligibility and budgets, replay incremental-benefit analysis, and live entries after confirmed closure. Serves original objective 3; policy activation depends on evidence.

**Goal.** After a complete exit, allow a new episode in the same window when there is a fresh valid signal, with strict limits against churn.

Config:
```yaml
reentry:
  max_trades_per_window: 1       # counts episodes that acquired confirmed exposure
  rearm: signal_reset            # signal_reset | cooldown
  cooldown_s: <research>
  lock_window_after: [protective_stop, manual_risk_stop]
  same_side_allowed: true
  opposite_side_allowed: false
  min_edge_add_for_reentry: <research>
  last_reentry_cutoff_s: <research>
  max_window_debit: <limit>
```

Preconditions for a new episode (all must hold):
- The prior position is closed with authority: no live order, no unresolved match, any dust classified.
- Fresh account state with reservations released.
- A current valid signal and time remaining.
- Every per-window and per-run budget still has room.
- The cooldown or re-arm rule and the lockout rule are satisfied.

Only one position is open at a time: no pyramiding, no top-up, no simultaneous opposing positions. Pending or unknown orders reserve capacity immediately.

Tasks: driver multi-episode logic on top of P3 identities; hysteresis and re-arm; lockouts; late-fill handling (attribute to the original order, update exposure, pause entries, reconcile the residual); replay multi-episode position view; reports at episode level with window aggregates.

Tests:
- a second same-side episode;
- a permitted side switch;
- churn on an unchanged signal;
- a late duplicate fill from episode 1;
- dust attribution;
- SL or manual lockout;
- counters restored after a restart;
- window budget exhausted.

Evaluation: the gain over the selected P6 policy after fees, compared window by window (trades inside one window are not independent).

**Gate G3.** Enable re-entry in the frozen policy, or not.

## P8. Freeze and qualify for live

**Affects:** research evaluation and live/paper qualification; validates the selected combination rather than introducing a fifth strategy feature.

**Goal.** Evidence that the frozen policy works on untouched data and in the real environment before money scales up.

1. Freeze the policy: complete effective config, replay version, actual source snapshot/hash and dependencies. A commit alone is insufficient if necessary changes remain uncommitted.
2. Evaluate once on the untouched set assigned to this milestone in P0. Report the result as is. If it leads to tuning or informs a later strategy change, it is no longer untouched evaluation for that change; use a distinct reserved set or collect fresh data. Quality/integrity checks may precede the freeze without strategy-outcome inspection.
3. Provider qualification:
   - resolution rule, opening K and tie semantics against the real contract;
   - live vs recorded reference feed; check the current provider contract, including the RTDS → PolyBolt migration notes;
   - real fees, ticks, minimum sizes, allowance and balance on the target markets;
   - SDK behaviour and order/confirmation latency.
4. Observation: the continuous runner with public feeds and no orders, its decisions compared with replay of the same recordings.
5. Paper soak with the frozen policy.
6. **Gate G4.** Supervised small-money trials: one window, then a bounded number of windows, then a longer supervised run. Each step needs reconciled accounting and reviewed reports before the next.

## 3. Decisions to make (owner: user)

| Id | Decision | When | User decision (16 Oct 2026) / status |
|---|---|---|---|
| D1 | Retry numeric settings | end of P1 / P4 | Provisional default 3 attempts per episode including the first; preparation, POST, elapsed and window ceilings bounded. Re-derive after P1. |
| D2 | Top-up after a partial fill | P4 | No automatic top-up; manage the quantity received. Retries after confirmed zero fills and new episodes after complete exits remain allowed. |
| D3 | Trials with retry+hold before exits (G1) | after P5 | Allowed after research, provider, accounting and paper checks pass; exits need not be enabled first. Not authorized yet (implementation only). |
| D4 | Automatic redemption | before long unattended runs | Automatic resolution/payout tracking; manual redemption with capital caps; automatic redemption is a later extension. |
| D5 | Loss-limit period and timezone | P5 | Daily UTC accounting, Paris-time reporting; limits persist across restarts and midnight never erases open risk. |
| D6 | Exit policy to freeze (G2) | after P6 | Hold-to-resolution and managed exits are both configurable final choices; selection awaits research. |
| D7 | Re-entry on/off and opposite side (G3) | after P7 | Mechanics built; off by default; opposite side research-only. |
| D8 | Capital allocated to continuous runs | before G1/G4 | Open: monetary limits must be explicit before live activation (the live template refuses to validate). |

## 4. Main risks

| Risk | Mitigation |
|---|---|
| Edits invalidate the running A/B/C | Separate worktree (P0); merge only between experiments |
| Duplicate BUY on an uncertain outcome | PENDING starts at queue time; UNCERTAIN blocks; timeout tests in P4 |
| Ledger migration corrupts evidence | Single v2 migration, read-only v1 compatibility, journals backed up before the first v2 write |
| Replay and live drift again | Shared planner and pure checks; parity tests in CI |
| Capital locked in unredeemed payouts stops the runner | Caps on unresolved/unredeemed positions, a pause with a reason, D4 |
| Over-fitting exits and re-entry to development paths | Attribution chain, reserved fresh data, freeze before evaluation |
| Rollover gap loses the start of the next window | Prepare the next market early; measure the gap; never bypass K readiness |

## Revision 2 change record

- Added a reader's map connecting all four user objectives to phases and separating shared policy, replay, live operations, observation/paper and qualification.
- Corrected fingerprint/collection-discovery claims and required actual-source isolation/provenance, including uncommitted files and execution dependencies.
- Replaced the claim that no test venues exist with reuse of existing doubles in P2 and coherent paper composition in P5.
- Separated expected P2 result changes from feature-off P3 regression, using semantic evidence rather than byte-identical output files.
- Added independent preparation/POST ceilings and clarified partial-fill handling.
- Made ledger design forward-compatible while staging implementation and preserving the feature-off episode cap.
- Added ordered-window capital validation and failure-driven soak acceptance; paper results remain operational evidence.
- Assigned separate untouched evaluation evidence to policy milestones and clarified G1/G2 decisions.
- No implementation, experiment, collection restart or trading was performed by this revision.

## Revision 3 change record (16 October 2026)

- Implemented P0 and P2–P7 in the isolated worktree (`E:\polymarket\Tyrex_PM-live`, branch `feature/settlement-live-readiness`, branched from 49e1db1 whose fingerprinted sources equal the running experiment's `f60ef585…`). Recordings and the S2 split were copied read-only into the worktree; the experiment checkout, its kernel, outputs and runtime state were not touched (verified: clean git status, unchanged experiment folders).
- Integrated the user's decisions (retry default 3 incl. first; no top-up; explicit hold vs managed-exit policy; bounded supervised trials allowed later without exits; automatic resolution tracking with manual redemption and capital caps; daily UTC limits with Paris-time reporting, persistent and explicit).
- Replay semantics version `strategy_closed_loop_v3` with `replay_execution_contract_v1`; `staged.CODE_TREES` now also fingerprints `execution/` and `protection/`; v2 results are labelled as superseded by the explorer.
- Status table, replay-result changes and remaining blockers added at the top. P1 and P8 are not done; nothing is qualified or enabled.

## Revision 4 change record (16 October 2026)

- v2 frozen: commit `4a10212` (tag `settlement-v2-reference`) holds notebook 04, the baseline selection, the two hold candidates and their paper/live run configs.
- Live-readiness work merged (`a39a504`, fast-forward). The worktree and its copied recordings were removed; reference evidence now lives in `var/research/reference/`.
- Notebook 05 and `research/strategy_lab/v3.py`: frozen candidates (identities equal to v2), run-config agreement check, verification experiment on the 94 frozen windows, v2-vs-v3 tables, optional feature runs (retry, managed exits + SL, re-entry) and shared-account replay, each behind its own switch. Batch runs accept a replay execution policy (protection) and summaries carry v3 counters.
