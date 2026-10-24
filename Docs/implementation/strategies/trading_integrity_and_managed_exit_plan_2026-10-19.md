# Trading integrity and managed exit: four-task implementation plan

Date: 2026-10-19. Updated: 2026-10-20. Status: **implementation authorised by the user;
code and focused offline checks delivered; live qualification remains incomplete**.

Implementation evidence, limitations and usage are tracked in
[the status report](trading_integrity_and_managed_exit_status_2026-10-20.md).
The proposal below is retained as the acceptance criteria; its verification requirements
must not be read as already satisfied.

## 1. Objective and boundaries

Make books reliable, prevent competing settlement writers, reconstruct the complete context of a recorded trading run, and research a precisely defined early-exit rule. Keep the selected entry baseline intact while measuring each change separately.

Frozen entry baseline: **comparison + legacy volatility**: `twap_proxy_diffusion`, `variance_time_policy: per_sample_v0`, 5-cent conservative entry edge, 30–240 seconds remaining, maximum entry price 0.97, one-tick limit buffer, intended debit cap $5, one attempt and one filled trade per window. Correct 60-second TWAP reference and `reference_availability_v1`; corrected execution contract `replay_execution_contract_v2`. Hold to resolution remains the reference policy. No retry, veto, corrected volatility or re-entry is introduced by this plan.

This is software and research preparation, not approval to submit real orders or enable a new live policy. No automatic redemption signing. Existing live templates, capital limits, ledger evidence, frozen experiment records and notebook outputs must not be reset or overwritten.

Use four names, avoiding the already reused P6/P7 labels:

- **T1: settlement ownership and locking** — live operations and local accounting.
- **T2: book correctness, then performance** — shared market data, live and replay.
- **T3: captured trading-run reconstruction** — recording, execution evidence and offline reconstruction.
- **T4: value exit with optional downward-trend confirmation** — shared strategy, execution and research.

For each task record separate statuses: planned, implemented, verified, researched where applicable, qualified, enabled. Passing unit tests does not establish strategy profitability.

## 2. Evidence reviewed and current implementation

Inspected code paths:

- `execution/process_lock.py`: operating-system lock keyed to the execution state directory.
- `application/settlement_cli.py`: discovery is locked; `reconcile`, `confirm-redemption`, `mark-manual` and `ack-loss-pause` do not use that guard consistently. Ownership repair has its own guarded path.
- `execution/settlement/ledger.py`: append-only JSONL, deduplication and recovery; instances hold their own in-memory sequence and accounting state. Append mode alone does not make competing writers correct.
- `market_data/book_store.py`, `market_data/book_feed.py`, `adapters/polymarket/sdk_public.py`: live reconstruction, readiness, recovery and discarded venue best prices.
- `data_warehouse/reconstruction/books.py`: historical reconstruction rejects locked books as well as crossed books, unlike live.
- `research/strategy_replay/capture_replay.py`: reconstructs observation decisions; executes no intents and assumes flat position context. REST book captures are currently audit-only.
- `persistence/execution_journal.py`, `execution/evidence.py`, `execution/session_state.py`: authoritative execution evidence, ordering, session identity and reducer state. The SQLite journal is shared across runs and can contain older sessions.
- `persistence/run_evidence_journal.py`: reporting events are not execution authority and some decision records are droppable.
- `strategies/settlement_value/{config,schema,plugin,driver,economics}.py`: managed-exit mechanics, value exit with point or upper bound, thesis stop, bid-depth walk, partial exits and pending-exit exclusion already exist.
- `runtime/trading_runtime.py`: state ownership, session detachment, account gates, protection, mandatory flatten and residual hold on shutdown.

Current evidence establishes observation reconstruction, not complete trading reconstruction. Exit mechanics have tests; the proposed exit's investment performance is unqualified. The DESYNC probe measurements and speed claims are reported evidence, not newly reproduced benchmarks in this planning pass.

## 3. Delivery order and review gates

1. Preserve existing work and manifests; inventory active processes before any later implementation. Do not edit code used by an active experiment. Isolate implementation if required, carrying necessary uncommitted work explicitly rather than silently losing it.
2. Implement and verify **T1** first. It closes an accounting-integrity gap without changing the strategy.
3. Implement **T2 correctness**; measure old/new behavior before its performance refactor. Version the resulting market-data semantics.
4. Develop **T3** in parallel with T2 where practical; complete its integrated proof on the selected, versioned T2 implementation. Serialise performance measurements: no concurrent replay batch.
5. Specify T4 and prepare fixtures while T2/T3 proceed. Complete T4's integrated paper/reconstruction and research checks only after the relevant foundations pass.
6. Review measured changes and research conclusions. Live enablement is a separate user decision, not a consequence of finishing these tasks.

Do not automatically launch another full grid. Begin with focused deterministic cases, followed by paired runs on a fixed development set. Any rerun of the already-used P7 evaluation is a diagnostic before/after comparison, not a new independent qualification.

## 4. T1 — one owner for the settlement ledger

### Problem

Two commands can read the same ledger into separate in-memory objects and append incompatible transitions or sequence numbers. Two configs can also point at the same ledger while using different execution state directories, bypassing the existing directory lock. RPC read-only means no blockchain signing; it does not mean no local accounting writes.

### Proposed implementation

1. Reuse the OS locking primitive, extending its API where needed. Add a **ledger-specific lock derived from the canonical ledger path**, stored beside that ledger. Do not rely solely on `state_directory`. Document local-filesystem support and path canonicalisation; reject unsupported alias arrangements rather than pretending they are independent ledgers.
2. The runner holds its existing execution-directory lock and the ledger lock for its lifetime. Operations requiring both acquire **execution lock first, ledger lock second**, release in reverse, and clean up on partial acquisition or startup failure. Ledger-only CLI commands need only the ledger lock. No reverse acquisition anywhere.
3. Create a single guarded writable-ledger entry point. Acquire the guard **before constructing/recovering a writable ledger**. Reuse one authoritative in-process instance; prevent multiple stale writable projections in the same process. Enforce ownership at mutation boundaries, not merely through a voluntary CLI convention. Never acquire/release a file lock for every decision.
4. Route all production writers through it: runner, resolution/redemption worker, reconciliation, confirmation, manual intervention, loss-pause acknowledgement, discovery apply path, repair/migration tools and any additional writer found in the call-site audit. Avoid nested reacquisition when the runner's worker already owns the ledger.
5. Fail promptly with a clear owner/busy message when another writer owns it. No forced unlocking, PID-file deletion or concurrent override. Network verification and ledger commit must operate under one ownership scope, with bounded RPC timeouts.
6. Preserve read-only `status` and `worklist`: read a captured, complete-line prefix without calling mutating recovery. Label the snapshot's sequence/time and concurrent-tail condition. Truncating or repairing a torn tail requires exclusive writer ownership; never silently repair an interior corruption.
7. Keep schemas, event history, dedupe semantics, cash accounting and risk allowances unchanged. Preserve atomic append/fsync semantics. Any required tail repair is backed up and audited before appending again.

### Acceptance

- Subprocess tests, including Windows: runner vs CLI; CLI vs CLI; same ledger/different state directories; same state directory/different ledgers; independent stores; clean release after error or process death.
- Acquiring a writer after another exits loads the newest sequence and totals. Duplicate verified redemption remains idempotent; conflicting evidence still fails.
- Cover every mutating command, including discovery and ownership repair. A refused command writes nothing. Read-only inspection can run beside the owner without modifying bytes.
- Existing ledger recovery/accounting tests pass; historical allowances and outstanding holdings survive unchanged. Lock acquisition occurs at startup/command boundaries, adding no network call or per-evaluation locking to the trading path.

## 5. T2 — book correctness first, performance second

### Correctness implementation

1. Retain optional reported best bid/ask in the SDK adapter, normalized delta contract and captured formats. Validate field meaning and empty-side sentinels against actual SDK/venue samples. Distinguish absent, malformed, empty and genuine prices.
2. Extract a shared, deterministic Polymarket reconciliation function for live and warehouse reconstruction. Apply the complete message's changes per token in received order. Preserve connection/session/binding identity and reject obsolete epochs. Select the applicable reported best for that token without mixing intermediate and final references.
3. When valid current-message venue best prices explain obsolete better-priced local levels, remove only those levels, then require reconstructed top agreement. Do not add fabricated levels or quantities. A missing expected best, conflicting reports, unexplained mismatch or a genuine cross leaves the token unavailable and initiates recovery.
4. Explicitly define the missing-reported-best fallback and locked-book policy, identically for both paths. Retain legacy behavior behind the old reconstruction identity for historical reproducibility; do not silently treat missing evidence as verified top agreement. Treat a venue-confirmed lock separately from a cross and document its trading eligibility.
5. Matching top prices establishes top-price consistency only. Quantities and deeper levels retain their continuity assurance; unsupported depth must not be promoted to verified depth. Compare complete snapshots at aligned boundaries, accounting for intervening events. Continue investigating the reported 46 mismatches; do not assume either SDK loss or venue refresh is proven.
6. Preserve snapshot cross validation, stale-age checks, token/epoch filters, rejection while genuinely desynced, REST version guards and bounded logs. Preserve separate receipt time and last meaningful mutation where necessary.
7. Add configurable, non-blocking REST recovery debounce; **250 ms is a proposed test value**, not a guarantee of websocket recovery. While waiting, inconsistent books remain unavailable. Cancel pending recovery after valid snapshot repair, rollover, epoch change or shutdown. Retry within existing bounded recovery rules if no snapshot arrives.

### Performance implementation, in a separate change

- Maintain incremental per-token level maps and best-price tracking; defer sorted immutable snapshots until a consumer reads them and cache by complete book version.
- Every size/depth mutation invalidates the relevant cache even when best prices do not change. Update top quantities as well as prices. Preserve immutability for consumers retaining a previous snapshot.
- Reduce duplicate projection and unused publication only after auditing subscribers and consumers. A gate asking for depth must receive the latest depth, not a cached top-only view. Preserve freshness handling on no-op messages and connection changes.
- Compare the optimized implementation with the corrected, unoptimized reference. Do not change strategy scheduling merely to obtain a faster benchmark.

### Acceptance and versioning

- Focused fixtures: crossing insert followed by removals, locked/stale top, genuine cross, missing best fields, empty side, duplicate/no-op, quantity-only/depth-only change, multi-token and repeated-token message, missing delta, reordered/old-epoch input and recovery races.
- Measure READY-top mismatches, depth/quantity discrepancies, DESYNC duration, recovery calls and eligibility at actual evaluation/gate instants. Include replay assurance/invalidations, not warning count alone.
- Run both captures and compare all 919 previously reconstructed decisions. Investigate and classify any change; do not force equality if the old book was wrong. Exclude gaps or failures explicitly from exactness claims.
- On focused development cases compare entries, fills, sizes, fee-net economics and skip reasons before broader frozen-candidate reruns. Version reconstruction, normalized schema/parser where changed, cache keys, scenario identity and manifests; keep old results addressable.
- Benchmark per-message work and actual p50/p95/p99 loop delay, gate/order-path latency, CPU, memory and recovery rate. The reported 30x is not an end-to-end promise. Use comparable bounded paper runs, report concurrent collection/load and sample counts, and agree numerical acceptance limits before the qualification measurement.

## 6. T3 — reconstruct a captured trading run's actual context

### Scope

Add a distinct **recorded-execution reconstruction mode** beside observation reconstruction. It replays what the trader knew, including real recorded execution outcomes. It must never sign, contact the order endpoint, simulate replacement fills or modify production state. Counterfactual strategy backtests remain a separate mode with their own simulated fills.

### Proposed implementation

1. Define a versioned trading-capture manifest: run/process segment IDs; full effective config; code, clock, execution, reference and book-policy identities; raw/decision/explanation files; relevant execution/settlement event ranges; hashes; completeness and gaps. Include the startup state and prior held episodes affecting capacity, not just positions opened during this run.
2. Export a consistent read-only snapshot of the SQLite evidence database, including committed WAL contents, using SQLite snapshot/backup facilities. Do not copy an actively changing database file without its WAL. Record the included sequence boundaries and settlement prefix. Scope sessions by recorded run/window/attempt identities; unrelated historical sessions must not become this run's trades.
3. Reuse the authoritative execution evidence deserializer/reducer. Reconstruct requested/pending entry from queue time, submissions, acknowledgements, ambiguous outcomes, partial/confirmed fills, sellable quantities, partial exits, complete closure, committed holds, detachment and restart restoration. Replay strategy outcome callbacks and lifecycle changes in their actual order; a final position snapshot alone is insufficient.
4. Reconstruct the host context used by decisions and dispatch: account snapshots, cash/reservations, readiness and risk gates, metadata, ownership, ledger transitions and active/prepared market binding. Audit what is already journalled before adding fields. Do not use final balances, final reconciliation or later chain evidence at an earlier decision.
5. Preserve **knowledge/application order**, not exchange timestamps alone. Record run-segment IDs and applied sequence/cursors linking each evaluation to market data, execution and account/lifecycle state. A journalled event not yet reduced at evaluation must not appear early. Monotonic clocks reset across restarts; cross-table sequence numbers alone do not establish global order. Missing historical ordering is reported as incomplete/unsupported, never guessed into an exact match.
6. Reuse existing required evidence and bounded optional recording. Any additional low-volume lifecycle links reuse established journal boundaries; do not synchronously serialize whole account/book state on each tick. Optional diagnostics remain asynchronous and cannot silently drop authoritative execution state. Measure the overhead of any added links.
7. Extend the reconstruction CLI with explicit mode detection/selection and structured completeness checks. Snapshot evidence into a diagnostic destination; do not open production journals through constructors that create schemas or write metadata. Unsupported older captures retain observation support and an honest explanation of trading reconstruction gaps.
8. Add a generic result bundle readable by the existing explorer: captured vs reconstructed decisions, lifecycle context, per-field mismatches and first divergence. Compare evaluations by stable identity, not merely truncating and zipping two lists. Omitted/extra decisions count as failures.

### Acceptance

- Keep the existing observation proof as a regression on its original policies.
- Deterministic recorded-paper fixtures: no dispatch, zero fill, partial fill, late/uncertain submission, duplicate/out-of-order execution evidence, retry budget, hold/detach/next window, early sale, partial sale, restart and pre-existing holds. Build rare paths deterministically rather than waiting for them to occur in public feeds.
- A bounded captured paper run with an actual simulated fill and hold, followed by a restart; later repeat for managed exit. Use only simulated account funds and no signing credentials.
- Exact actions, reasons, held side, intent semantics, pending/position state, counters and gate decisions where full fidelity is supported. Match K, sigma, probabilities and quotes using declared tolerances; any existing numerical timing tolerance is disclosed, not relabelled bit-identical.
- Report per-window and per-segment coverage, equality counts, unsupported fields and capture faults. A replay is complete only if every expected evaluation and state dependency is accounted for. No missing/raw-faulted run receives a global exact-reconstruction claim.

## 7. T4 — value exit with optional downward-trend confirmation

### Exact proposed behavior

For the **held token**, not the currently leading token:

- UP holding: `p_hold = p_up_point`.
- DOWN holding: `p_hold = 1 - p_up_point`.
- A share held to resolution has model-estimated value `p_hold` in payout units. This rule uses the point estimate, deliberately not the entry's conservative bound.
- Reuse `exit_walk`: sell only bid depth whose executable price minus estimated sell fees is at least `p_hold + margin`. The margin has units of collateral/share. Do not compare with the entry price, ask, midpoint or last trade; entry cost remains relevant to reported P&L but is sunk for this forward-value comparison.
- Optional additional condition: the held token's evaluated best bid has fallen by at least `min_drop_per_share` over a declared lookback, and the negative-trend condition remains confirmed for a declared duration. New exit authorisation requires both the economic and trend conditions at that evaluation.

Example only: probability 0.55, acceptable net bid 0.62, configured margin below 0.07, and confirmed downward trend -> exit eligible. If probability remains 0.9995 while the token drops to 0.12, this value rule does **not** trigger. It is not an independent detector of model overconfidence and is not automatically a stop-loss.

### Configuration and integration

1. Reuse `exit.policy: managed_exit`, `value_exit_enabled`, `value_exit_basis: point`, the existing margin, minimum price, attempt limit and residual policy. Keep the current `upper` basis available; never change it globally for existing configs.
2. Add an off-by-default, versioned trend-confirmation setting under strategy exit parameters. Proposed fields: enabled, policy ID, lookback seconds, minimum downward move per share, confirmation seconds and maximum sample gap. Final numeric values are research choices, not embedded live defaults.
3. Define trend sampling as **best-bid observations at shared strategy evaluation instants**. Use a bounded reusable helper in the shared strategy path, called once per evaluation for each valid token quote. It is deliberately not a tick-by-tick momentum estimate. For evaluation time `t` and lookback `W`, use the latest valid sample at or before `t-W`, no older than the configured gap tolerance relative to that boundary. Require `bid_at_boundary - bid_now >= min_drop_per_share`; confirmation means this trend condition holds at every observed valid evaluation over the confirmation duration, with no over-limit gaps. The economic value condition must also hold at submission authorisation. Use an explicit elapsed-time clock (live monotonic time and its recorded/replay equivalent), not source timestamps or uncontrolled wall-clock jumps. No interpolated or future sample. Quotes must be fresh and book-ready; a gap, unavailable book, token/epoch change or restart resets confirmation. A restarted policy waits for sufficient fresh history before authorising this trend-gated exit. The observation/lookback convention and all thresholds enter provenance.
4. Record the signal history from the active window, including before entry, so the first post-fill decision may use already observed valid prices. Require complete lookback and confirmation; do not silently relax them immediately after entry. Persist enough evidence to reconstruct that history. No extra feed, RPC or probability recomputation.
5. Record `p_hold`, held token, trend sample times/prices, drop, confirmation age, net acceptable bid/depth, margin, quantity, fees and reason through the existing schema/recording framework. Distinguish no signal, trend warming up, model/book unavailable, no executable size and exit pending. Inspector charts show entry, exit requests, partial/complete fills and residual hold.
6. Keep one exit owner. Strategy/protection/lifecycle requests must be arbitrated before submission. Default research variants disable thesis stop and SL/TP, leaving the new rule isolated. No top-up or re-entry; an exit never grants another trade in that window in this phase.
7. Handle partial sells through existing accounting: manage only confirmed remaining sellable inventory; do not treat an exit request as a completed sale. Uncertain SELL outcomes reconcile before another order that could oversell. Late fills, dust and exhaustion retain explicit states and risk capacity.
8. **Resolve cutoff precedence before activation.** Current strategy defaults include a 15-second residual-hold cutoff; the host also has mandatory flatten/manual deadlines (20/5 seconds in the comparison template). Audit and align both paths using the declared managed policy. For this proposal, the selected residual action is hold to resolution, not an implicit deadline sale. The host must enforce that decision even if model inputs are unavailable or no strategy evaluation arrives. Reconcile/cancel any in-flight exit before committing residual inventory; uncertainty must not be disguised as a successful hold. Preserve explicit emergency/manual behavior. Confirm shutdown, restart and replay use the same precedence.
9. A hold committed under the old policy remains a hold; switching configs cannot retroactively make it sellable. Create separate research/paper profiles, preserving comparison + legacy hold unchanged. New non-default settings change config identity; default-off settings preserve existing candidate IDs under the established compatibility rules.

### Research and acceptance

Compare fixed entry rules with only three exit policies:

- **E0:** comparison + legacy, hold to resolution.
- **E1:** point-value exit alone, using existing economic-depth logic.
- **E2:** the same E1 settings plus downward-trend confirmation.

Begin with a small, versioned case set: ordinary winners/losers, large disagreement correct/incorrect, reversal after entry, temporary dip then recovery, low-price large-payout winner, thin/wide book, stale/model-unavailable inputs, partial exit, cutoff and uncertain SELL. Selection criteria and all windows are disclosed; include winners potentially harmed by early selling. The unrecorded old live losses cannot be reconstructed retrospectively.

First prove disabled-feature regression, UP/DOWN symmetry, exact threshold boundaries, no lookahead, gap/restart handling, bid-depth fees, partial-fill accounting, exit ownership and lifecycle cutoff agreement. Both live-paper and replay must exercise the same production rule; no notebook-only injection.

Then paired development replay, initially friction with favorable as sensitivity, using the current versioned book reconstruction and unchanged volatility/reference/execution settings. Report entry changes caused by infrastructure separately from exit changes. Metrics: net P&L, drawdown/loss distribution, entry and exit counts, time exposed, fees/slippage, remaining holdings, profit concentration, rescued losers, prematurely sold winners and failed exits. Distinguish trade win rate from settled-token correctness. Validate sell-fee assumptions; estimated fees are not provider qualification.

Use existing notebook 06 case tools and the shared experiment/explorer loaders for E0/E1/E2, with experiment selection, saved probability/quote timelines and reasons. Avoid a separate result store. Add a dedicated exit section rather than rewriting P7 history. If aggregation or visualization cannot account for partial exits, extend it before interpreting results.

Freeze a small policy set on development evidence, then assess once on genuinely untouched newly collected windows. Existing P7 evaluation windows are already used. Any chosen numeric trend/margin/cutoff values and risk/performance acceptance criteria must be declared before that evaluation. No broad parameter search or automatic promotion of whichever variant wins a selected case set.

## 8. Deliverables, decisions and stopping points

Each implementation task should finish with a reviewable change and a short report: files/contracts changed, exact verification, observed behavior changes, limitations and enablement status. Required artifacts:

- T1 writer/lock audit, subprocess test evidence and unchanged-accounting report.
- T2 old/new reconstruction comparison, version manifest, focused economics report and measured performance report. Correctness and optimization remain separately reviewable.
- T3 versioned trading-capture contract, read-only reconstruction command, recorded-paper bundle and complete mismatch/coverage report visible in the explorer.
- T4 rule/config documentation, deterministic mechanics tests, E0/E1/E2 saved case and development results, and a later independent evaluation plan. Qualification/live enablement remain separately gated.

Decisions for review before implementation: accept ledger-keyed ownership; shared versioned book repair with conservative fallback; evaluated-best-bid trend definition; point-basis economic exit; hold-to-resolution residual policy with host precedence aligned. Numeric margin/lookback/drop/confirmation/cutoff and benchmark limits remain explicitly provisional until a bounded protocol is agreed; they must not be tuned against fresh evaluation results.

Suggested milestone order: **T1 -> T2 correctness -> T2 optimization and T3 proof -> T4 paper/cases -> broader exit research -> fresh evaluation -> separate live decision**. T3 implementation can overlap T2; paper latency benchmarks and replay batches cannot overlap.

The user's subsequent implementation authorisation covers code changes and bounded
offline/paper checks. It does not enable real-money trading, alter production risk
limits, redeem funds, repair a live position, commit or merge this work. The status
report distinguishes completed checks from outstanding qualification.
