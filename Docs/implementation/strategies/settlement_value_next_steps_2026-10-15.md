# settlement_value: retries, exits, re-entry and continuous operation

Created: 15 October 2026. Updated: 16 October 2026, revision 3. Incorporates the user's second proposal, targeted execution-code checks, and a parallel-work policy for stage A on newly recorded data. Status: proposed implementation sequence. Only this planning document was changed; no strategy, runtime, notebook, experiment or saved result was changed. The user now reports additional recordings and is considering a new stage A grid; this update does not launch or verify completion of an experiment.

Companions: [research-to-live plan](research_to_live_plan.md), [operator runbook](settlement_value_operator_runbook.md), [execution lifecycle](../../latest/execution_lifecycle.md).

## Recommendation

Finish and review the v2 hold-to-resolution baseline, then implement **replay/live execution alignment → safe entry recovery → continuous hold-to-resolution operation → managed exits → serial re-entry**. Define episode identity, execution feedback and durable accounting together at the start, because all four features depend on them. Begin provider qualification and observation preparation alongside this sequence; they do not depend on finding the best parameters.

Continuous operation is infrastructure and can be developed against the simpler one-filled-episode-per-window hold policy. Exits and re-entry are separate strategy research extensions; they need not delay the continuous-runner work. Continuous implementation does not authorize live trading. The earliest optional live milestone is a separately qualified, frozen retry+hold strategy, first in bounded single-window trials and then bounded multi-window trials. If the user requires exits before any live money, keep that runner in observation/paper mode until exit qualification. No live run is requested by this roadmap update.

Stages A/B/C choose provisional parameters for the current behavior. Execution alignment, retry, exit and re-entry changes must each be compared against the previous baseline. Qualification belongs to a named policy/version: retry+hold can be assessed independently from a later managed-exit strategy. The staged A/B/C helper explicitly refuses holdout windows; completing those stages is not holdout validation.

## 1. What exists today

- `strategies/settlement_value/state.py`: `entry_emitted` permanently consumes the window's one attempt when an intent is emitted.
- `strategies/settlement_value/driver.py`: a confirmed position emits `HoldToResolutionIntent`; a previously emitted entry without a position waits with `PENDING_ORDER` or `EPISODE_CONSUMED`. There is no entry-outcome transition that re-arms it.
- `research/strategy_replay/run.py`: the strategy adapter translates entry and hold intents only. Its feedback handles BUY fills and clears pending orders, without clearing the strategy's consumed attempt. The lower-level `research/replay/engine.py` already implements SELL execution and realized accounting, but this adapter does not integrate them.
- Replay/live execution gap: `_order` uses `intended_gross_shares` from strategy evidence. Live `execution/planner.py` caps target notional and divides it by `1 + fee_reserve_rate` to form a collateral-denominated BUY. Replay does not traverse that planner or the live `FinalExecutionGate`. It has strategy/host readiness and engine-side tick, minimum, inventory and cash checks, but that is not full execution-policy parity.
- `execution/lifecycle.py`: entry preparation/submission is followed by evidence reconciliation; there is no entry retry loop. Exit submission already has bounded retries and remaining-share sizing. Terminal sessions can be released only when `safe_to_open_entry` holds.
- `runtime/trading_runtime.py`: terminal entry, exit and cancel outcomes currently request process stop; a committed settlement hold also requests stop. Several surrounding paths assume one selected episode.
- `protection/`: shared SL, TP and trailing mechanisms already exist. TP styles include reactive FAK and resting GTC/GTD. The current protection mark is the held best bid.
- `strategies/settlement_value/plugin.py`: only the `hold_to_resolution` lifecycle is allowed. Adding a protection block is not a complete early-exit implementation.
- `runtime/run_config.py` independently parses `protection` and permits hold mode without an explicit incompatibility check. The generic runtime can arm protection after a fill. Hold/protection intent competition needs an explicit configuration contract before relying on that combination.
- `runtime/settlement_hooks.py`: settlement episode identity is `run_name:window_id`. The settlement ledger models reservation, entry, hold and payout, without an early-sale lifecycle.
- `execution/settlement/ledger.py` also directly refuses a second episode with the same window, and `governor.py` blocks any window returned by `ledger.by_window`. Changing the key alone will not enable retries or re-entry. `max_unresolved_count` is an additional portfolio cap, not the same rule as one episode per window.
- `runtime/market_data_runtime.py`: active/prepared-next market discovery, feed warming and promotion already exist. `_bind_pending_market` in the trading runtime requires prior execution clearance. The trial profile still has a 300-second duration and one episode per process.

These are code capabilities and restrictions, not evidence of successful live operation. Existing plan/runbook statements about what has or has not been run may be stale; obtain current operator evidence during qualification.

The four enhancements share the current episode/lifecycle assumptions. Those assumptions are distinct from the clock defect that caused missing usable inputs; changing retry or continuity policy does not fix missing facts, and missing-fact gates remain in force.

## 2. First: review the v2 baseline

Preserve the experiment checkout's source and identity through A/B/C. Avoid changing strategy/replay/runtime sources in that checkout while the batch runs or between its stages: these trees are fingerprinted. Roadmap implementation may proceed in a separate frozen-source checkout as described in section 11. Keep the current timelines setting fixed when resuming a stage.

Review:

1. Replay version, clock provenance, completeness, host-skipped reasons and data gaps across all development windows, rather than extrapolating from the four clock-fix examples.
2. Every emitted entry without a fill, using its simulated order-arrival events and rejection/cancellation reason. Distinguish a zero-fill cancellation from the unfilled remainder of a partial fill.
3. Fill frequency, quantities, price movement during latency, limit buffer, fees, per-window P&L and sensitivity to scenario assumptions.
4. Stability across both collections and neighboring parameter choices; concentration in a few windows, and uncertainty with windows as the unit of comparison.
5. Selected-trade paths: executable bid depth, model probability/edge, peak favorable/adverse movement and time remaining. These support exit hypotheses; selecting exits from the same paths remains development work.

Deliverable: a small provisional shortlist, an annotated no-fill inventory and explicit exit hypotheses. Separate recorded replay no-fills from live-only host/transport failure classes that these recordings cannot measure. Count affected windows/opportunities and how long a signal remains eligible; this sizes the opportunity for recovery but does not establish that a later retry would fill or profit. The review can conclude that no candidate warrants promotion. Do not force a winner.

Do not use holdout to invent retry, exit or re-entry rules. Freeze each policy before its holdout evaluation. If a hold baseline consumes an evaluation set and those results inform subsequent exit development, that set is no longer untouched evidence for the enhanced strategy; reserve a distinct set or collect fresh data. If holdout results cause further tuning, treat that set as development thereafter and obtain fresh evaluation data.

## 3. Replay/live execution alignment — prerequisite to retry measurement

The common strategy driver is valuable, but common decisions do not imply identical executable orders. Align the economic and eligibility rules before using replay to estimate the benefit of recovery:

- Use a common planner/order contract for debit cap, fee reserve, collateral spend, limits, rounding and venue-legal quantity. Preserve the exchange's amount-based BUY semantics; simply dividing dollars by the limit can differ from a spend-based depth walk with better prices.
- Separate pure pre-send policy from live I/O. Reuse eligibility checks for cutoff, candidate age, binding/token identity, book synchronization/freshness, tick changes, price movement and relevant capabilities. Inject virtual/monotonic time for replay; calling a gate that reads real process time would break determinism.
- Model decision, preparation, final-gate, order-arrival and feedback times distinctly. Check the book available at each phase without using future data. Apply live SELL sizing and legal-size rules when exits are added.
- Model account, allowance, signing, transport and settlement uncertainty through explicit recorded evidence where available or labelled scenarios where absent. Historical market recordings cannot reconstruct the real account's readiness or make simulated fills a prediction of actual fills.
- Record a comparable attempt trace: requested notional, allowed debit, spend amount, gross/net quantity, adapted limit, fees, phase times, gate outcomes and evidence provenance.

Acceptance: deterministic fixtures run through replay and a fake-backed live execution path produce equivalent planned economics and eligibility outcomes for the same inputs. Cover amount-based fills at multiple price levels, fee reserves/rounding, a moving book, candidate expiry and cutoff. Validate injected uncertainty separately from economic fill simulation.

Keep existing v2 A/B/C results intact as the previous model baseline. Alignment changes need a new version/experiment and a rerun of the shortlist before retry-on/off comparisons. Close the hold-plus-protection configuration gap in the same foundational work: reject conflicting combinations by default; any pre-commit-only protection exception must be explicit and tested, not inferred from timing.

## 4. Foundation and safe entry recovery

### Separate the concepts

- **Opportunity:** a currently eligible signal, price and size. It can disappear while recovery runs.
- **Episode:** one intended position lifecycle, from entry pursuit through exit or settlement.
- **Attempt:** one candidate/submission within that episode. Several attempts can produce no position; a partial fill creates exposure.
- **Re-entry:** a new episode after the previous position is closed. It is distinct from retrying an unfilled entry.

Use durable identities for run, window, episode and attempt. Keep a stable parent episode with unique attempt/order identities. For new filled episodes, increment the episode identity rather than reopening a historical journal aggregate. Persist counters and reservations across restart. Audit intent semantic keys, session identifiers and stream routing so legitimate new attempts are not deduplicated and old events cannot affect a new episode.

Replace the single latch with explicit eligible/pending/uncertain/position/closed transitions. Pending begins when an intent is queued, not only when a fill is confirmed. Moving the latch to fill confirmation alone would permit duplicate requests while signing, dispatch or confirmation is still in flight. Authoritative no-dispatch or zero-fill outcomes can re-arm; uncertain outcomes remain blocked until resolved. Once resolved, the ordinary gates decide whether recovery is still permitted; uncertainty need not permanently forfeit the entire window.

The durable ledger must support several attempts within one episode, including reservation release/re-reservation after known zero outcomes. Its current NO_DISPATCH/NO_FILL terminal transitions and window uniqueness cannot be bypassed by merely clearing `entry_emitted`. Distinguish attempt counts, started episodes, filled episodes and pending capacity in the governor. Version the schema and preserve old journals as evidence with an explicit backward-read/migration policy.

### Recovery rules

- **Not sent:** a temporary prepare/final-gate failure or host rejection may re-arm after its blocker clears. Include runtime blocks before `lifecycle.open`, which currently can return without any execution session. Deliver an explicit outcome back to the strategy.
- **Authoritatively zero fill:** a terminated FAK with no fills or outstanding settlement can re-arm. A zero-fill FAK can be normal market movement or insufficient liquidity at the allowed price, not a bug.
- **Acceptance or fills uncertain:** keep the reservation and reconcile the existing order/trades/account evidence. Do not send another BUY just because an HTTP response timed out or the order is missing from open orders.
- **Partially filled:** manage confirmed and pending exposure. Initial scope has no automatic top-up; this is a position, not a zero-fill retry.
- **Matched/mined/settlement-retrying:** still potentially live exposure. Wait for authoritative terminal evidence and reconcile; never interpret settlement's `RETRYING` state as permission to submit another entry.
- **Persistent or structural failure:** invalid configuration, authentication, unsupported metadata, unavailable capital or an expired window does not enter a rapid retry loop. Recover automatically only where refreshed evidence can establish that the blocker is gone; otherwise pause entries with a visible reason.

On every new attempt, recompute signal, side, depth, fees, edge, quantity and allowed limit from current facts, then apply the normal final gate. Price changes stay inside strategy constraints. An episode's selected side remains fixed during retries in the first implementation; if that side loses eligibility, stop its pursuit. Opposite-side pursuit requires a separately specified later policy.

Use reason-aware wakeups, bounded attempt count, an elapsed-time budget, entry cutoff and service backoff. Prefer immediate reevaluation when a recoverable blocker clears, without promising a fixed recovery latency before measurement. Do not reset the budget on every new quote. Release reservations only with evidence, and carry window/run limits across attempts.

Suggested future settings, **not existing config fields**: `entry_retry.enabled`, `max_attempts_per_episode`, `max_attempts_per_window`, `max_retry_duration_s`, `backoff_policy`. Use a window-wide ceiling as well as an episode ceiling so multiple episodes cannot multiply requests without bound. Specify whether a budget counts created candidates, signing starts or POSTs, and report those separately. Do not count every ineligible scheduler tick as an order attempt. Choose numerical settings after the no-fill review and latency measurements; three attempts or 500 ms are test hypotheses, not accepted defaults.

### Code scope and acceptance

Scope: strategy state/driver and context; runtime intent-outcome feedback; lifecycle/session creation and clearance; settlement reservation identity and governor counting; replay feedback and failure injection; version/provenance; per-attempt reporting.

Meaningful checks: temporary no-POST failure then recovery; confirmed zero fill then fill; price moves outside edge before retry; partial fill; POST timeout followed by a delayed fill; duplicated/out-of-order evidence; stream gap; restart during reservation/dispatch; exhausted attempts; cutoff while waiting; permanent rejection; concurrent wakeups. No duplicate BUY, overspend or premature reservation release is acceptable.

Compare retry-off with retry-on on identical development windows and parameters. Report recovery latency, attempts, fills, debit, P&L, reason counts and cutoff expirations. More fills alone is not evidence of improvement. Simulated transport and settlement failure handling is correctness evidence, not a calibrated forecast of live failure frequency.

## 5. Continuous cross-window execution — initially one filled episode per window

Build and validate this against retry+hold before the exit/re-entry extensions. Observation/paper operation can exercise continuity without live money. Any actual hold-strategy promotion still needs its own research and provider qualification plus bounded supervised single-window evidence before multi-window trials. Later, reuse the supervisor for managed exits and re-entry rather than building a separate runner.

Replace single-trial completion with an explicit run policy: terminal episode can lead to same-window eligibility, next-window waiting, or a real run stop. Manual intervention, hard risk limits and user stop retain distinct durable meanings. Removing one `stop_requested` assignment is insufficient.

Reuse active/prepared-next discovery and feed warming. Ahead of promotion prepare books, order metadata, account state and authenticated evidence routing. At promotion verify market/window/token identity, old-scope clearance, fresh facts and the new opening threshold. Do not carry K, position selection, cooldowns, protection or queued old-window intents into the new window. Retain long-lived feeds/indicators only where their existing chronology semantics allow it.

Keep old-market evidence routing until pending work is resolved. Prepare the next market while the current one trades. Bound and measure the rollover readiness gap, but allow the next entry only when K, signal and execution facts are ready. The clock-fix example's expected opening delay is not a reason to bypass readiness.

Explicitly support a continuous duration mode; do not approximate it with a large number in the current 300-second trial setting. Add process ownership/locking, recovery from feed/provider disconnects, durable run loss/capital limits, health records and graceful manual stop. Stop closes the BUY gate first, drains submissions/cancellations and handles exposure according to the chosen lifecycle.

Define an account-wide loss period and its timezone/reset rules; restarting must not reset limits. Include total open worst-case loss, reserved/unknown debit, unresolved count and cost, available collateral and a kill switch. Add configurable consecutive-loss pauses only with a defined update rule for delayed resolutions. An unresolved outcome is not yet a known win or loss. Provide durable reasons and alert hooks for manual action; external notification channels require separate authorization.

Two completion paths need separate treatment:

- **Flat between windows:** confirmed sale proceeds may fund subsequent entries after authoritative reconciliation.
- **Hold through close:** outstanding positions, resolution uncertainty and payout receivables require cross-window accounting and resolution servicing. A receivable is not available cash. Continuing while old holdings await settlement needs portfolio caps and a qualified detachment/routing path; simply bypassing startup clearance is unsafe. Operator-assisted redemption may remain the initial implementation if sufficient capital and unresolved limits are explicit. Automatic redemption is a separate enhancement.

For the hold runner, detach only a known committed holding into a tracked settlement portfolio, continue old-market evidence/reconciliation, and bind the new market under account-wide limits. Different token IDs alone do not establish safety: the markets still share collateral and the same account. Unknown dispatch, unmatched attribution or unreconciled exposure stays a blocker. Raising `max_unresolved_count` alone is insufficient.

Add automatic resolution/payout reconciliation as part of the hold runner. Redemption remains a separate transaction capability: initially retain operator redemption with bounded unreclaimed-capital limits and pause when usable cash or unresolved caps run out. Enough capital for N positions is only a buffer, not indefinite continuity. Automatic redemption can extend unattended duration after receipt attribution and idempotency are qualified. On manual stop, preserve committed holds and their durable resolution worklist; the remaining settlement worker's ownership/restart behavior must be explicit.

Continuous mode should continue through ordinary cleared no-fill/recoverable conditions. It must pause or stop on unresolved execution or hard limits, even when the user has not manually stopped it.

Checks: consecutive boundaries with no signals; filled/closed episodes; a held position at boundary; unavailable next market; boundary feed gap; stale queued entry; late prior-window evidence; clock loss; restart during promotion; user stop during prepare/POST/exit; run-loss limit. Begin with a bounded multi-window soak, then extend duration with supervision.

## 6. Managed exits — strategy extension after the hold runner

Introduce an explicit lifecycle choice such as hold-to-resolution versus managed exit. A managed position may remain open while the policy evaluates; it must not immediately become an irrevocably committed hold. Preserve the current hold baseline as a named configuration.

Preserve the meaning of an existing committed hold. Select managed-exit behavior before entry; do not reinterpret historical committed holdings as freely tradable positions. Reject ambiguous protection/lifecycle combinations during config validation.

Implement mechanics first with one entry episode per window:

- Shared remaining-share SELL sizing, confirmation, cancellation and bounded recovery.
- Replay translation for Exit/Flatten/Cancel intents, SELL feedback, basis reduction, sell fees, net proceeds, dust and cash availability.
- Early-sale ledger events and loss accounting; the current settlement ledger cannot represent this just by recording a payout.
- Explicit arbitration between protective exits, strategy exits, mandatory close and resting TP cancellation. One exit owner prevents duplicate SELLs; confirmed quantities determine every retry.
- An explicit close-time and shutdown policy, including failed exits, unavailable liquidity and residual inventory. A managed strategy must not silently inherit a hold commitment or silently change to a different exit behavior.

Use a simple reactive trigger to verify exit mechanics, but do not assume fixed SL/TP is the preferred trading policy. Research strategy-value exits alongside shared reactive SL/TP; protective risk exits and economic exits can coexist with explicit precedence. Add resting TP only after cancel-versus-fill races are covered. Thresholds are hypotheses to test, not guarantees of execution price. A trigger on best bid does not prove that the full position can sell at that bid.

Evaluate strategy-driven candidates, for example net executable sale value versus model hold value, edge deterioration or time-based rules. Use current information only, full relevant bid depth, fees and latency. Make the value convention (point/conservative estimate) explicit. For the economic choice between holding and selling the existing position, compare future net proceeds and future hold value: entry cost is sunk, though it still matters for P&L and risk limits. A fall below entry price can trigger a declared risk stop; it does not by itself prove selling has higher expected value. Protection must continue to work when entry/model readiness is unavailable; sells still require valid execution facts.

Define an exit submission cutoff and the time/evidence budget needed to finish or escalate; do not confuse that with guaranteed execution or guaranteed flatness before resolution. Any inventory carried beyond it needs the predeclared residual/settlement policy and capital accounting.

Checks: partial/zero-fill exits; declining sellability; stale or absent bid; dust; simultaneous SL and TP; late entry fills; TP fill during cancellation; fee rounding; near-close expiry; restart with an exit pending; no oversell. Extend replay to exercise the same protection policy as live, rather than reproducing an approximate notebook-only version.

Compare hold, retry+hold and retry+exit on the same windows. Review net P&L, tail loss, turnover, fees and exit reliability before enabling multiple filled episodes.

## 7. Serial re-entry — after exit research

Prefer `max_trades_per_window` with a documented definition over a lone `allow_several_trades` boolean. Count an episode once it first acquires confirmed exposure; zero-fill attempts use their separate attempt budget. Pending/unknown orders immediately reserve capacity even before a trade is confirmed. Do not count each partial fill or exit clip as a separate trade.

First scope: one owned position at a time; no pyramiding, automatic top-ups or simultaneous opposing positions. A new episode requires:

1. Authoritative closure of the prior position, with no live order, unresolved match or submission uncertainty; explicit durable classification for any dust.
2. Fresh account state establishing available cash, released reservations and correct baseline attribution.
3. A current valid entry signal, remaining entry time and all ordinary quality/risk gates.
4. Remaining per-window trade, cumulative turnover/debit, loss and attempt budgets, plus run-level budgets.
5. The configured cooldown/signal-reset and stop-loss lockout rules.

Define whether a persistent signal can retrigger after cooldown, or must first leave and re-enter an eligible band. Define hysteresis to prevent immediate buy/sell loops. Initial recommendation: require re-arming after an exit, and lock the window after a protective SL/manual risk stop; study relaxing that separately. Same-side and opposite-side re-entry need explicit policy. Confirmed prior closure is mandatory for either. Study a stricter edge requirement and an earlier last-entry deadline for subsequent episodes; neither is an automatic improvement. Known classified dust may be permitted only under an explicit residual policy, while unknown or tradable leftovers block re-entry.

Checks: same-side second episode, permitted side switch, unchanged-signal churn, late duplicate fills from episode one, dust attribution, manual/SL lockout, restart counter restoration and exhaustion of window budgets. Reports need episode-level rows and window aggregates; existing filled/entered booleans are insufficient. Parameter comparisons use window-level outcomes, since multiple trades inside one window are dependent.

Deduplicate repeated evidence, but never discard a genuinely new late fill because its episode was marked closed. Attribute it to the original order, update account exposure/reservations, pause new entries and reconcile/manage the residual before continuing.

## 8. Live qualification, started early and completed before each promotion

The existing minimum trial path is useful, but the new behavior needs new evidence:

- Verify actual contract resolution rule, opening K, tie handling and reference semantics. Current research results still carry proxy labels.
- Establish live-reference versus recorded-reference compatibility and freshness. Official documentation now includes an RTDS-to-PolyBolt migration guide; check the deployed provider contract rather than relying on the older runbook's feed names. This planning review does not establish a migration requirement or change the selected feed.
- Read real fees, ticks, minimum sizes, allowance/balance and payout evidence for the actual target markets. Verify current SDK/provider behavior and measure order/confirmation latency.
- Run the same driver continuously with public feeds and no submissions, comparing its decisions with replay on matching recordings. Observation proves signal behavior, not live fills or exit success.
- Exercise the enhanced execution path with controlled failure fixtures and paper simulation, then conduct bounded supervised small-money trials. Grow from one episode to multiple episodes/windows only after confirming accounting, recovery, stop behavior and reports.

Useful run metrics: host skips by reason; entry/exit attempts and fills; recovery time; unknown-order age; partial fills; actual fees/slippage; rollover readiness delay; open and reserved capital; unresolved holdings; realized loss; manual stops. Monitor decision and execution health separately.

## 9. Work packaging and experiment discipline

Recommended reviewable units:

1. V2 baseline review and provisional shortlist; no runtime changes.
2. Replay/live planned economics and eligibility alignment, explicit scenario limits, and hold/protection config validation; new-version shortlist rerun.
3. Shared episode/attempt identity, outcome contract, reason classification and durable budgets; default behavior unchanged.
4. Opt-in safe entry retry in replay and live, with failure evidence and baseline comparison.
5. Continuous hold supervisor, tracked outstanding holdings, account-wide limits, automatic resolution reconciliation, manual-redemption capital limits and restart/stop behavior; observation/paper soak.
6. Optional frozen retry+hold qualification and supervised trial milestone: single-window first, then bounded multi-window. This milestone may be deferred until exits if the user wants that; no live execution is authorized by this plan.
7. Managed-exit mode, early-sale accounting and replay/live parity; one filled episode per window. Research economic exits and optional SL/TP, then qualify the selected version.
8. Opt-in serial re-entry only if research supports it: budgets, reset/lockout rules and episode reports; compare incremental benefit after fees.
9. Frozen enhanced policy, fresh untouched-data evaluation, observation and supervised live evidence for the continuous runner with the selected behavior.

Version all behavior changes in configuration and results. Bump replay identity/cache compatibility when execution semantics change; include strategy, lifecycle, protection and run-policy identity in provenance. Do not mix new behavior into the running v2 experiment or reuse its parts as enhanced-policy results.

For attribution compare the sequence: existing v2 hold baseline → execution-aligned hold → retry+hold → retry+exit → retry+exit+re-entry. Test continuity separately across ordered windows with shared capital and delayed settlement: independent one-window replays cannot prove the runner can fund and execute every planned trade. Then check a small number of interactions and nearby entry parameters: the best entry settings under hold may change under exits. Do not begin a large joint search before understanding each addition.

## 10. Feedback disposition and decisions to settle at the relevant milestone

Accepted additions: execution-policy parity before retry measurement; explicit ledger/governor uniqueness changes; hold/protection config validation; continuous hold operation before exit development; account-wide limits and automatic resolution servicing; re-entry-specific cutoff/edge hypotheses; a separate qualification milestone for each strategy version.

Corrections to the feedback: SELL already exists in the low-level engine, while the strategy adapter does not translate exits; current market preparation is family-based, not a new dependency on a hard-coded BTC helper. A single current-position view is compatible with serial episodes after complete closure, provided historical episodes/accounting are retained. A pending lock cannot wait until fill confirmation. An uncertain result blocks while unresolved and may recover after proof. A valid opening threshold cannot be fabricated by prebinding. Committed holds retain their declared meaning. Different tokens do not remove shared-account risk.

Planning defaults, not user-approved trading settings: no partial-fill top-up; one position at a time; explicit classified-dust policy; no forced numeric retry/backoff setting yet; operator redemption with strict capital limits initially. Determine attempt/time limits from the v2 review and measured execution latency. At the qualification milestone, decide whether the user wants hold-strategy small-money trials before exits. Automatic redemption is optional for a bounded runner but requires separate implementation/qualification for longer unattended operation. These choices do not block the current baseline review or foundational design.

## 11. What can proceed alongside a new stage A grid

The stage A results are not a prerequisite for writing lifecycle infrastructure. They are a prerequisite for choosing and judging its research policies. Separate the experiment baseline from the implementation workspace.

### Before starting a grid on the additional recordings

- Confirm the new warehouse/collection is included. `batch.all_collections()` currently enumerates two known collections, rather than discovering arbitrary new recordings. If recording extended an existing collection, verify its refreshed inventory too.
- Inspect notebook 04's collection/role summary and actual selected windows. `staged.window_table()` labels windows absent from the saved split as `not in frozen split (excluded)`. New files alone do not increase the grid.
- Freeze an explicitly versioned development/holdout assignment for the new study. Preserve existing holdout identities as excluded from development; do not blindly regenerate the chronological split on the larger inventory, which can move previously held-out windows into development. Do not change the split file during a running experiment; it is read again when stages launch.
- Finish writes for the selected windows and required late metadata/resolution evidence, then freeze their data identity. Additional recording can continue in a separate collection; changes to overlapping parts/metadata can invalidate cache identities even if they seem to concern later windows.
- Create a new experiment for the expanded selected set, confirm its preview/window count, and save timelines from the start if they are needed for the review. Use the same source revision for A, B and C.

These are preflight tasks, not evidence that the new recordings have already been included or qualified. Make any required collection-registration or split-tool changes before freezing/launching the experiment, then restart its notebook kernel to load the intended revision.

### Parallel work that does not need the grid's results

In the existing experiment checkout: documentation, policy specifications, read-only review of completed results and separate analysis tooling that only reads saved output. Give analysis new output locations; do not rewrite stage files or diagnostic caches while workers use them.

In a separate implementation checkout: shared order-planning/eligibility alignment; hold/protection config validation; durable window/episode/attempt identities; outcome feedback and failure classification; reservation/counter recovery; opt-in retry mechanics with synthetic failure fixtures; and continuous-runner/settlement-service design and implementation after their dependencies are in place. Keep existing behavior as the default until compared and qualified. Single-episode exit translation/accounting can also be scaffolded without choosing the economic exit rule; re-entry mechanics follow complete-close accounting.

Isolation means separate filesystem paths, Python import sources, notebook kernels and mutable run/state/output locations. Switching branches in the experiment's directory is not isolation. Editable installs and shared environments must not redirect workers toward implementation code. A new checkout must capture the actual current source, including needed uncommitted/untracked strategy files; starting from HEAD alone may omit this project's research implementation. Record the source identity used by each track. Do not copy live credentials or enable order submission for implementation tests.

### Decisions and comparisons that should wait

- Retry count, elapsed-time/backoff limits and cutoff settings: use no-fill classifications and execution-latency evidence. Mechanics can be implemented earlier with explicit test settings.
- Final entry parameters and exit rules/thresholds: use completed baseline review. Exit ideas may be specified now, but not selected as improvements without comparison.
- Re-entry cooldown, signal reset, side-switch permission, later-entry cutoff and budgets: follow reliable exit mechanics and research on incremental net benefit.
- Enabling changed behavior in an active experiment, merging it into the experiment checkout, or reusing old parts under new semantics: wait until that baseline's intended A/B/C sequence is closed and archived as evidence. Alternative early stopping is valid only if the baseline is explicitly ended and the enhanced study gets a new identity.
- Live promotion and increased exposure/unresolved limits: wait for qualification of the specific selected policy and execution path. Stage A completion alone is insufficient.

After alignment is ready, rerun the provisional shortlist under the aligned version before retry comparisons; there is no automatic need to repeat the entire grid immediately. Preserve the full existing grid as an old-execution baseline and label that limitation. Material ranking changes may justify a broader aligned grid. A/B/C runs in the baseline checkout can continue while this happens elsewhere, with separate identities and outputs.

Heavy replay batches and cache construction compete for RAM and disk. Keep the baseline grid as the main compute job; schedule substantial implementation benchmarks separately or reduce concurrent workloads. Isolation protects evidence, not machine capacity.

## External references checked on 15 October 2026

- [Polymarket order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle): FAK fills available quantity and cancels the remainder; matching and settlement are distinct.
- [Real-time order updates](https://docs.polymarket.com/trading/realtime-order-updates): matched/mined/retrying trade states are nonterminal; reconnect recovery requires authoritative account reads.
- [Manage orders](https://docs.polymarket.com/trading/manage-orders): order, open-order and trade queries for reconciliation.
- [RTDS-to-PolyBolt migration](https://docs.polymarket.com/migrate/rtds-to-polybolt): current reference-feed integration documentation to consult during qualification.
