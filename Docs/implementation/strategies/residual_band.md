# Implementation plan: residual-band digital strategy

Strategy kind: `residual_band`.

Date: 30 September 2026; repository inspection began 29 September. Status: implementation specification, not implemented trading behavior. Proposed files, facts, configuration fields, and execution capabilities are identified below. All `src/` paths are relative to the repository root.

Companion and canonical shared foundation: [settlement-value implementation plan](settlement_value.md). Research basis: [review of the residual proposal](../../research/btc5m_peer_research_review_2026-09-29.md). Project authorities: [architecture](../../latest/architecture.md), [extension pattern](../../latest/extending.md), [input contracts](../../latest/strategy_inputs.md), [protection](../../latest/protection.md).

## 1. Delivery contract and economic identity

Implement a production-quality, maker-first strategy that acquires a discounted binary outcome and seeks to resell it as the executable market price improves. It continuously updates fair value, expected convergence, order attractiveness, and confirmed inventory. A strictly cost-aware taker override and a separate complete-set acquisition module are supported modes with explicit capability requirements.

The residual is a diagnostic input, not a tradable security. Define r_j=m_j-p_j for each outcome's observed book estimator m_j and model fair value p_j. A negative residual identifies a potential discount. Entry requires evidence that the actual token's executable future value is attractive after fill selection, fees, slippage, and forced-exit risk. A residual shrinking because the fair-value model falls is not a successful convergence trade.

Implement the full operating behavior before profitability tuning: real quote lifecycle, partial fills, order races, bounded inventories, restart recovery, low-latency scheduling, data quality, and deterministic explanations. Mechanics and failure tests are implementation gates. No claim is made that OU residuals, profitable maker fills, or $3 opportunities have been established.

Initial operating scope:

- One directional episode per market, with bounded replacements of its working entry quote.
- At most one taker entry attempt that can have been dispatched per window; no second taker add after a partial fill or uncertain POST. A proven local pre-dispatch rejection may be reconsidered within the attempt budget.
- Only confirmed owned shares can be offered for sale; no synthetic naked short.
- Default exit objective is resale before the configured deadline. An explicit hold fallback is a distinct mode and requires the companion's complete resolution capability.
- Complete-set mode is separately selected and uses a parent condition-level risk ledger. The initial configuration rejects simultaneous directional and pair modes; support concurrent modes only after ownership/allocation tests are specified and implemented.
- Default target is $3 expected net episode profit when feasible, not a guaranteed gain or permission to increase risk after losses.

## 2. Current framework and reuse

The runtime already owns feed ingestion, feature graphs, readiness, protection, execution, journal persistence, and reporting. Strategy code must continue to supply private calculations and generic intents only.

Existing pieces worth reusing:

- `indicators/ewma_volatility.py`, `reference_alignment.py`, `reference_basis.py`, `imbalance.py`, `ofi.py`, and `realized_vol.py`.
- `indicators/graph.py` for declared subscriptions; do not instantiate another graph or raw-data store inside the driver.
- `market_data/book_store.py`, `book_view.py`, and `executable.py` for book truth, freshness, and executable depth walking.
- `strategies/z_gap/assemble.py`, `valuations.py`, and `policies.py` as examples of private snapshot assembly and pure economic decisions. Extract common arithmetic into shared owners; do not make `residual_band` import Z-Gap policies.
- `core/intents.py` has maker entry and cancellation requests; `execution/planner.py` supports post-only GTC entry and a resting TP sell path.
- Existing protection resolves reactive stops and resting TP cancellation through the execution lifecycle.

Important limitations:

1. A posted maker entry does not provide a complete continuous market-making controller.
2. Generic `ExitIntent` currently plans a market sell; it is not a dynamic maker-sell instruction.
3. Runtime cancellation/terminal paths can stop the run. Quote withdrawal/replacement must not be confused with completed strategy execution.
4. The strategy context describes one selected instrument and does not expose a full immutable quote/reservation portfolio. Paired inventory needs generic multi-instrument support.
5. L2 consume-time freshness/generation work is documented as deferred. This strategy must close that gap for any L2-dependent live feature.
6. Existing `microprice_gap` uses the generic external-asset depth path. Polymarket's two bounded outcome books need explicit adapter/feature integration; they cannot be assumed already subscribed.
7. The current source scheduler can override declared coalescing, and fallback evaluation timers are serviced from a loop sleeping one second. Millisecond reaction requires framework changes, not merely a smaller strategy constant.

## 3. Mandatory strategy conditions

### 3.1 Supported fair value

Consume the shared settlement contract/reference/probability interfaces from G0/G1 in the companion plan. Use endpoint probability only for a verified endpoint contract; use reference-history-aware probability for a supported averaged payoff. Both strategies must operate on the same official settlement meaning.

Keep point probability, conservative range, terminal payoff uncertainty, local probability sensitivity, and reference quality separate. Reject unsupported regimes, unverified thresholds, incomplete history, stale models, or excessive basis uncertainty. A basis error cannot be monetized by pretending that the exchange and settlement stream are identical.

### 3.2 Residual and convergence model

For each token retain observed bid, ask, size, midpoint, selected book estimator, fair value, residual, time remaining, source epochs, and model version. Do not derive executable Down prices from Up prices.

Support two clearly named estimators:

- `weighted_mid`: simple depth-weighted price; no claim that it is Stoikov's calibrated micro-price.
- `fitted_microprice`: optional conditional future-price correction with a compatible offline artifact and stated prediction horizon.

The reference fair-value estimate may optionally incorporate book calibration. If it does, record the combination weights and fit the residual model against exactly that anchor; changing the anchor changes the residual distribution. Avoid treating a mechanically shrunken residual as evidence of better convergence.

OU is an optional calibrated residual backend:

\[
dr=-\kappa(r-\bar r)dt+\xi dW,\qquad
E[r_{t+h}|r_t]=\bar r+(r_t-\bar r)e^{-\kappa h}.
\]

Require positive supported kappa, explicit time units, sample/regime coverage, out-of-sample fit metadata, and finite-horizon limits. A nonstationary, sparse, or incompatible regime does not get a default positive kappa. Prefer a versioned lookup of finite-horizon entry/exit bands computed offline rather than solving a first-passage problem on each tick.

The tradable object is m, so any exit forecast must include movement in fair value as well as r: delta m=delta r+delta p. Include fill-conditioned entry selection, future executable bid/ask, partial fills, and the forced-exit tail. OU convergence alone cannot authorize an order. If no defensible episode-value artifact exists, a validate-only diagnostic profile may compute signals; the live profile must not silently invent an expected capture.

### 3.3 Maker entry and inventory-aware exits

For outcome j, produce a reservation value by adjusting its fair value for confirmed directional exposure and risk. Positive Up inventory should reduce willingness to acquire more Up; its sign must translate consistently for Down. Use cash, matched pairs, gross shares, pending reservations, and net exposure, not net exposure alone.

A simple initial quote policy can center around the reservation value and use a calibrated half-width. The half-width includes uncertainty, observed adverse-selection cost, execution/exit costs, time-to-exit, and quote-latency risk. Do not substitute current instantaneous probability variance times tau for a proven integrated risk measure.

- Maker BUY: a post-only price below the side's conservative value by a sufficient margin, subject to executable book/tick constraints and inventory caps.
- Maker SELL: a post-only price for confirmed sellable inventory that balances expected capture and completion probability before the deadline.
- Tick rounding must preserve economic bounds. Post-only rejection does not trigger an automatic taker order.
- Requote only after a meaningful price/size change, hysteresis, or expiry of the allowed quote age. Fixed epsilon and time limits are typed config, not hidden constants.
- Risk invalidation can cancel immediately despite ordinary requote throttling. Rate budgets still apply; if an exchange cancellation is delayed, retain exposure and reservations until evidence resolves it.
- A small price discount is insufficient if the likelihood of adverse fills/forced liquidation makes expected episode profit nonpositive.

### 3.4 Taker override

Retain the intent of the original conditions, with precise meanings:

1. The executable all-in edge clears a calibrated cost and uncertainty margin. Use actual ask for entry, not absolute micro-price residual with an automatically added half-spread.
2. The configured standardized-distance gate passes. Original |z|>0.8 is a named candidate threshold, not a theorem. Record whether z means endpoint log-distance or the settlement functional's standardized distance; never interchange them silently.
3. Fresh exchange and official reference estimates pass the configured agreement/basis check. When the original sign-agreement rule is selected, a disagreement blocks the override; it does not affect a completed matched pair's contractual payout.
4. Time remaining, sensitivity, feed/POST latency, and available depth permit a credible capture/exit. The optional original high-confidence override near p=0.90 is bounded by the same cost, freshness, and risk tests.
5. No prior taker dispatch or acquired directional episode in this window, no unresolved mutation, and no working maker entry that could create duplicate exposure.

Before switching a working maker quote to a taker entry, request cancellation and wait for complete evidence. If any shares fill in the race, the override is no longer a fresh one-shot entry. Taker economics use the strategy's actual exit policy; settlement edge by itself cannot justify a pre-expiry resale trade. A configured hold variant must use the full resolution lifecycle and be evaluated separately.

### 3.5 Sensitivity exclusion and deadline exits

Preserve the proposed `tau<45s and abs(z)<1` cancellation rule as an explicitly configured candidate gate. Its thresholds are research defaults only and must be attached to a defined model/regime. Do not call it mathematically optimal or universally valid for TWAP contracts.

In addition, implement a reusable probability-move/latency risk estimate under the selected payoff model. Cancel/abstain when plausible adverse movement during the measured cancel/replace round trip exceeds the quote's risk budget. This general criterion is necessary even when a fixed time gate does not fire.

On model failure or deadline, remove entry quotes and follow the deterministic inventory exit policy. A resting exit may remain only when the configured risk/lifecycle rules permit it; at mandatory liquidation, the framework's protective exit takes precedence. A stop uses the held token's executable bid and does not guarantee the trigger price.

### 3.6 Sizing the $3 target

For resale strategy sizing, estimate the entire episode distribution, not N*(p-entry_cost). Define conservative expected episode profit including acquisition, maker/taker exits, unfilled/partially filled paths, and forced exits. Maintain separate expected value per attempted episode and per acquired position.

Use target mode `require_target` or `cap_only` as in the companion plan, but inject the resale payoff evaluator. The primary cap is full-debit exposure for unhedged shares, plus bounded operating costs and capital reserved by all potentially live orders. Directional binary Kelly is inappropriate for a resale episode unless its payoff assumptions really match; use a bounded scenario-based utility allocation or a simpler hard exposure cap initially.

N=3/expected_net_capture_per_share is a diagnostic only when capture does not change with size. Recompute execution probability, capacity, and cost at the requested size. A $3 expected profit target never overrides risk or causes topping up after a partial fill.

## 4. Data and inputs

### 4.1 Required facts

Use the shared contract/reference/account/clock inputs from the companion plan plus:

- Full validated PM Up and Down depth at the configured bounded number of levels; local receive time, source metadata, book generation/version, tick/minimum size, depth coverage, and synchronization state.
- Reference/spot updates and incremental variance/jump features needed by the fair-value and quote-risk models. Freshness is per source; a slow reference feed does not inherit the fast exchange's freshness.
- Authoritative order lifecycle: our order ids, limit, remaining quantity, posted/cancel-requested/cancel-proven states, fills/fees, sellable quantity, open-order reconciliation quality, and risk reservations.
- Compatible residual/band and execution-value artifacts with expiry/regime/feature metadata.
- Actual timing measurements for submission, cancellation, confirmation, and stale quote age; use conservative initial configured bounds until observed estimates are qualified.

### 4.2 Conditional/optional facts

- Binance spot L2/OFI if the selected toxicity model uses it; otherwise do not start it. The current top-20 snapshot transport has its own cadence and is not a reconstructed full diff-depth feed.
- PM public trades for a selected fill/flow estimator, with an explicitly verified source and trade-direction semantics. Add a fact/adapter if the trading feed contract does not currently expose them; do not pretend `polymarket.books` proves trade history or aggressor identity.
- A Hawkes arrival model can be added behind the shared artifact/feature interfaces. Simple recent-jump, flow, and markout features are the initial production implementation; no mandatory online Hawkes fitting.
- Maker rebates are separate accounting items. Base-case eligibility/capture excludes unconfirmed rebates; never book an estimated rebate as cash.

### 4.3 Feature and policy interfaces

Shared typed snapshots:

- `BookPriceEstimate`: token identity, estimator kind/horizon/version, price, uncertainty, depth source/epoch, and readiness.
- `ResidualSnapshot`: fair-value id, book-estimate id, signed gap, standardized distance with definition, regime, band/artifact id, and convergence-quality flags.
- `FillRiskSnapshot`: entry/exit fill estimates, adverse markout scenarios, cancel-latency budget, support/uncertainty flags, and as-of validity.
- `QuoteRiskSnapshot`: probability sensitivity, jump/flow state, latency-adjusted loss estimate, and entry/quote-cancel eligibility.
- `QuoteDecision`: desired owned quote slots with side/token/price/quantity/lifetime, expected episode economics, and immutable validity envelope.

Use shared G1 `BinaryProbabilitySnapshot`, G2 cost/sizing contracts, and G4 validity metadata. Strategy-specific action thresholds stay in the private policy. Shared features describe state and risk; they do not decide that every strategy should trade.

## 5. Module ownership and shared implementation

G0–G8 are specified in [the companion plan](settlement_value.md#5-module-ownership-shared-foundation-g0g8) and implemented once. This strategy adds the following reusable capabilities rather than duplicating the host.

### 5.1 Book price, residual, and quote-risk features

- Add `indicators/book_price.py` for weighted midpoint and validated fitted micro-price inference. Transform PM normalized levels into the existing provider-independent feature input shape through `market_data/`; do not import Polymarket `BookSnapshot` into a generic indicator's logic/tests.
- Add `indicators/mean_reversion.py` for bounded residual statistics and frozen OU forecasts. It owns numerical state only, not band entry decisions or orders.
- Add `indicators/quote_risk.py` for reusable probability-move and fill-risk feature evaluation. Reuse existing OFI/imbalance/volatility producers.
- Extend `indicators/graph.py`/`spec.py` with subscriptions keyed by source, instrument, level/horizon, and parameters. Up and Down instances must not collide or reset each other.
- Extend `facts/ids.py`, `producers.py`, and `mappings.py` for the required derived book/reference facts and any truly new source. Composition validates that every enabled model has supported live inputs.
- Offline band/fill-model fitting belongs in research tooling, proposed `research/residual_calibration.py`, separate from `lab/` fixture evaluation and from live imports. Data-only artifact schema and loading are shared G5.

There is no live optimization solver in this path. A finite-horizon band surface can be precomputed as a bounded lookup over regime/time/risk state, with explicit interpolation error/support behavior. Outside supported cells, do not extrapolate aggressive quotes.

### 5.2 Strategy-private package

Create `src/tyrex_pm/strategies/residual_band/` with `config.py`, `schema.py`, `plugin.py`, `driver.py`, `snapshots.py`, `assemble.py`, `policies.py`, `strategy.py`, `state.py`, `reasons.py`, and `calibration.py`.

- Driver assembles immutable book/probability/residual/execution views through the existing evaluate signature.
- Policies choose side, mode, target quote, taker override, time/sensitivity exclusion, and economic exit.
- Private state retains episode id, taker-dispatch consumption, last desired quote generation, active mode, and model-regime identity.
- Execution-confirmed quantities, cash, order status, and reservations come exclusively from context. Persist/recover private state without replacing execution truth.
- Plugin declares configured sources/triggers and supported execution capabilities; registration uses the open registry. No strategy-kind branches in the host.

### 5.3 Generic quote controller

Add `execution/quote_controller.py` under the existing coordinator. It reconciles desired quotes with confirmed actual quotes; it never independently signs or POSTs.

Extend `core/intents.py` with a proposed generic `QuoteIntent`/desired-quote contract containing market/token, owned slot id, BUY/SELL, limit, desired total exposure or remaining order size with explicit semantics, local expiry, decision generation, and validity envelope. Add typed cancellation scope when canceling a set of owned slots. Use strategy/condition/slot ids for idempotency; do not deduplicate different desired generations as identical orders.

Extend `execution/planner.py` for post-only entry and exit limits using authoritative ticks and quantity constraints. Keep generic `ExitIntent` semantics unchanged for existing strategies. A quote SELL is not the same request as flattening at market.

Controller operations:

1. If an identical valid quote is live, do nothing.
2. If a change falls below configured hysteresis and risk has not worsened, retain it.
3. To replace a live quote, request cancel and mark `CANCEL_PENDING`.
4. While cancellation is unresolved, reserve its entire possible remaining exposure. Keep only the latest desired replacement; do not create a growing queue of obsolete quote changes.
5. When cancellation and intervening fills are reconciled, recalculate actual inventory and residual allowed quantity. A cancel acknowledgement alone is insufficient if match/fill evidence remains incomplete.
6. Recheck latest data, economic bounds, deadline, tick, capability, and rate budget before creating the replacement through the coordinator.
7. If POST/cancel status is uncertain, reconcile the same attempt; no blind new mutation.

The initial controller permits only one potentially live entry quote per directional episode. A partial entry normally cancels its remainder before managing the acquired position; it does not assume the original requested size was filled. More concurrent quote inventory is an explicit future mode, not an accidental side effect.

SELL inventory is reserved across resting exits. Before a protective FAK, resolve/cancel conflicting resting sells and recompute the actual sellable balance through the existing lifecycle pattern. Preserve protection precedence. Dynamic strategy exit and static TP must share one owned exit slot or be rejected as incompatible; two controllers must not independently sell the same shares.

A local quote TTL schedules cancellation but is not exchange-enforced expiry. Use venue-native expiry only when its documented minimum/semantics permit it. Existing GTD fallback behavior does not establish short-lived exchange quotes. On process/network failure, orders may remain live until the venue or recovered runtime cancels them; cap that exposure explicitly.

### 5.4 Runtime and generic episode support

- `strategies/context.py`: immutable summaries of owned quote slots, confirmed fills, reservations, and pending mutation status.
- `execution/orders.py`, `evidence.py`, `reducer.py`, `session_state.py`, and `persistence/execution_journal.py`: quote generations, lineage, cancel/fill reconciliation, reserved quantities, and crash recovery.
- `runtime/trading_runtime.py`: consume quote intentions through the coordinator; replace unconditional terminal-stop assumptions with declared generic episode completion rules. Quote withdrawal without a fill is not automatically end of run, while legacy profiles preserve current behavior.
- `runtime/capabilities.py`/`final_gate.py`: validate BUY and resting SELL quote constraints appropriately. A stale alpha model blocks a new purchase but must not block an authorized risk-reducing exit.
- `execution/polymarket/gateway.py`: venue adaptation only; preserve single-writer submission, metadata warming, inspection isolation, and SDK-owned timeout behavior.
- `protection/`: retain framework-owned hard stop/TP/trailing semantics. Extend order-slot arbitration generically where dynamic quotes interact with protection; no second stop implementation in the strategy.

## 6. State machines and race handling

Directional strategy view:

`FLAT -> ENTRY_DESIRED -> ENTRY_WORKING -> INVENTORY_ACTIVE -> EXIT_WORKING -> FLAT_DONE`

Additional states: `ENTRY_CANCEL_PENDING`, `EXIT_CANCEL_PENDING`, `FORCED_EXIT`, `RECOVERING`, and `BLOCKED`. The execution authority determines actual exposure in every state. No public price event alone changes a position state.

Quote state:

`ABSENT -> PREPARING -> DISPATCH_AUTHORIZED -> POST_UNKNOWN / LIVE -> CANCEL_PENDING -> CANCEL_UNKNOWN / TERMINATED`

Fills can arrive during LIVE, CANCEL_PENDING, or apparent termination. Represent fill accounting separately from the order's transport state and process duplicate/out-of-order evidence idempotently. A late fill triggers immediate quantity/risk recomputation even after a newer model update.

Persist taker consumption and episode lineage at durable dispatch/fill boundaries. A restart cannot turn an already dispatched taker into a fresh allowed override. A proven no-dispatch attempt can be retried within the configured policy; unknown is never treated as zero.

Required precedence is framework emergency/kill, mandatory exit, hard stop, strategy invalidation/exit, trailing, then take-profit. Cancel obsolete entry exposure before ordinary requoting. Do not hold a coordinator mutex across a slow independent read and block every risk command; preserve serialized mutations while modeling external operations as pending state.

## 7. Separate complete-set module

This module preserves the original paired acquisition idea. Implement its lifecycle as a shared capability so the settlement-value strategy can use it later. Its configuration remains off until the entire capability is available; enabling it must fail validation if only the quote half is implemented.

### 7.1 Conditions and arithmetic

Acquire equal net Up/Down shares in the same verified complementary condition. For N completed pairs and all-in total cost C, locked payoff surplus is N-C, minus any additional claim/merge costs not already included.

The original 0.97 combined price cap is a candidate configuration. Use actual net shares and actual costs, including price-dependent fees for any completion taker, rounding, and bounded merge/claim charges. Two posted bids summing to 0.97 are not an executed arbitrage.

For an orphan Up lot costing c_U per net share, the maximum Down completion cost is pair_cap-c_U after allocating other costs. If completion is unavailable below the cap, do not buy at any price to preserve the illusion of a pair. Follow the explicitly configured orphan policy and record its loss.

### 7.2 Shared ownership

- Proposed `domain/binary/complete_set.py`: complement validation, matched net quantity, cost basis allocation, and completion-price budget.
- Proposed `execution/position_ledger.py`: multi-instrument position/reservation projection under the same coordinator/reducer; a parent condition groups child token sessions. Account-wide capital includes both submitted and possibly filled legs.
- Proposed `execution/pair_controller.py`: pair acquisition and orphan deadlines through the shared quote controller, with no direct provider calls.
- Proposed `execution/position_operations.py`: idempotent merge/claim requests and receipts; venue/chain encoding in adapters. Use the shared transaction/finality machinery from settlement lifecycle, not another nonce manager.
- `strategies/residual_band/pair_policy.py`: the strategy's pair economics and orphan choices, not order truth.

### 7.3 Lifecycle

`PAIR_FLAT -> QUOTES_WORKING -> ORPHAN / MATCHED -> MERGE_REQUESTED -> MERGE_CONFIRMED -> COMPLETE`

Add `ORPHAN_EXIT`, `MERGE_UNCERTAIN`, and `MANUAL_INTERVENTION`. Atomic dual fills cannot be assumed. If supported batch submission is used, treat each result independently.

- Reserve worst-case debit of both live legs before submission.
- Match only confirmed net shares; account for unequal/partial fills and price lots.
- An orphan has explicit maximum quantity, debit, age, and liquidation policy. Completion is attempted only inside its remaining all-in budget.
- Pending merges reserve both token quantities so no sell/second merge can reuse them.
- A merge request is not collateral. Reconcile receipts/finality and balances before releasing reservations or reporting realized profit.
- Loss on an orphan is directional episode loss. Include failed attempts and no-fills when reporting expected pair profitability.

Default recovery for an orphan is a bounded liquidation policy before market closure. Optional hold-to-resolution fallback requires that capability and full-debit risk allowance; it is never implied by failure to complete the pair.

## 8. Simplified algorithm

```text
on declared market event, execution update, or deadline:
    snapshot = read immutable books, features, confirmed orders/inventory
    apply framework risk/protection precedence
    if mode == COMPLETE_SET:
        evaluate pair budget, matched lots, and orphan deadlines
        emit desired quotes / cancellation / completion / merge preference
        return

    if entry data are stale or model is unsupported:
        withdraw entry quotes
        manage confirmed inventory through its risk/exit policy
        return

    compute shared fair value and uncertainty
    compute book estimator, residual, calibrated bands, and fill risk
    compute latency/sensitivity and inventory adjustment

    if an entry mutation or cancellation is unresolved:
        update latest desired quote only; do not submit another order
    else if we own inventory:
        if thesis invalid, time limit, or risk exit:
            request cancel/exit through the framework
        else:
            publish a sell quote for confirmed available inventory
    else if directional episode remains unused:
        assess maker episode EV and feasible size
        if maker conditions pass:
            publish a post-only buy quote
        else if all taker override conditions pass:
            require no potentially live maker entry
            emit one bounded taker entry
        else:
            withdraw unattractive entry quotes and wait

quote controller:
    reconcile latest desired slots against confirmed actual orders
    apply rate budget, hysteresis, reservations, and cancel/replace protocol
    submit only through coordinator after final validity checks
```

Maker first is an explicit route priority in the initial policy. If future research optimizes route choice by expected utility, version that policy rather than silently changing priority while tuning a threshold.

## 9. Configuration and research parameters

Create validate-only and complete production templates under `config/runs/`, parsed through typed strategy config plus existing shared runtime sections. No live file or risk cap changes are part of writing this plan.

Strategy configuration must expose:

- `mode`: directional residual or complete-set acquisition.
- `model`: shared reference probability artifact, book estimator, residual anchor id, supported regime.
- `bands`: backend/artifact, minimum discount, convergence horizon, residual uncertainty, maximum hold time.
- `maker`: quote width/risk multiplier, minimum improvement, minimum requote interval, local TTL, post-only, episode/attempt cap.
- `taker_override`: enabled flag, one-shot policy, minimum executable edge, standardized-distance definition/threshold, sign-agreement policy, minimum time, and optional high-confidence override.
- `sensitivity`: fixed candidate time/z gate plus modeled latency-risk cap and stale-feed/jump cooldown rules.
- `inventory`: maximum directional shares/debit, full-loss cap, risk aversion, target mode/profit, and selected sizing objective.
- `exit`: model invalidation rule, resting-exit policy, deadline, forced-exit behavior, and optional explicitly supported resolution fallback.
- `pair`: all-in cap, max matched/orphan quantities, orphan age/loss limits, completion route, merge/claim policy, and costs.
- `quality` and `schedule`: per-input ages/skew/generations, parameter freshness, execution-event triggers, coalescing, and deadline budgets.

Shared account caps, SDK timeouts, protection specs, and execution rate budgets stay in their framework-owned configuration. Validate contradictory combinations: dynamic sell and independent TP without arbitration; pair mode without a two-token ledger; live residual mode without a compatible episode-value artifact; required L2 without current freshness; unsupported hold fallback; too-short native GTD semantics.

Every numeric threshold has a unit and provenance: operational limit, illustrative research seed, or fitted artifact. Original 0.8/45s/1/0.90/0.97 candidates must remain labeled unvalidated until tested. Start with simple jump/flow features; HAR-RV, fitted micro-price, OU bands, and Hawkes refinements are independently swappable model choices, not mandatory prerequisites disguised as established alpha.

## 10. Millisecond budget and load behavior

Adopt the companion plan's G3/G4 scheduling and section 9 performance specification. Required local targets are:

- Incremental normalized-event/feature processing p99 <=1 ms.
- Strategy evaluation p95 <=1 ms, p99 <=3 ms.
- Local receive-to-intent p95 <=5 ms, p99 <=10 ms with maximum initial 2 ms alpha coalescing.
- Critical stale-quote/risk invalidation to local cancel-command enqueue p99 <=5 ms.
- A 10 ms fallback decision deadline served independently of maintenance/account tasks.
- Separate warm signing/planning, durability, queue, network, venue acceptance, cancellation, and fill-confirmation metrics. No claim that an exchange-confirmed cancel or fill finishes within the local evaluation budget.

These are qualification targets, not measured current performance. Measure both plugins on the same specified hardware/load, including 2,000 mixed events/sec sustained and 10,000/sec bursts, representative payloads, active/next-window subscriptions, and fake asynchronous execution races. Record full latency distributions and event-loss counters.

Implementation constraints:

- Incremental bounded windows; O(1) rolling updates where possible, O(configured bounded depth) book summaries and costs; no rescanning an entire window or fitting OU on every tick.
- Frozen small band/fill lookup tables and bounded interpolation. No general-purpose optimizer, Monte Carlo, pandas, SQL, disk reads, or network calls in the evaluator.
- Latest desired quote per owned slot; do not queue every micro-update as an order. Limit quote mutations separately from signal-evaluation rate.
- Per-provider rate budget and cancel reserve, configured from verified limits. Continuous 1 ms calculations do not authorize 1,000 orders/sec. Cancel/risk work takes precedence over new entries within the provider's actual limits.
- Ingest necessary book deltas/trades before coalescing evaluations. Public-stream gaps invalidate relevant features; execution evidence is never sampled away.
- Generation checks at consume and final gate for PM books, optional L2, references, and model artifacts. The existing deferred L2 gate is mandatory for enabled L2-dependent modes here.
- Use bounded queues and nonblocking telemetry. No suppression of durable dispatch authorization to improve a timing statistic. On overload, withdraw/disable new quoting according to the risk policy, retain possible-live order reservations, and surface explicit health failure.
- Protective processing cannot be starved by repeated alpha evaluations or slow inspection. Keep the same execution authority, SDK clients, and documented safe timeout semantics.

Optimizations follow profiling. Keep Python's current architecture initially; only move a proven CPU bottleneck into a native/vectorized implementation behind the same pure interface if the measured targets cannot be met. Replacing the entire runtime is outside this plan.

## 11. Telemetry and later tuning dataset

For each evaluation and quote generation, record the probability/model/rule ids, observed book estimate, residual, band, sensitivity, inventory skew, predicted fill/markout values, expected episode profit, selected size, time remaining, and reasons. Record the executable alternative even when a passive quote is chosen so route selection can later be compared honestly.

For each order, record decision/dispatch/acknowledgement/cancel/fill timing, queue proxy if available, venue evidence completeness, initial and updated quote reasons, final actual fees, and partial quantities. Record post-fill markouts at configured horizons using only observations actually received after the fill. Missing horizons remain missing; after expiry distinguish settlement payoff from an absent tradable midpoint.

Separate:

- Expected capture, realized spread, and adverse-selection markout.
- Price movement in the market from movement in model fair value.
- Attempt-level, filled-position-level, and full-window results.
- Matched-pair gains, orphan losses, merge costs, and unresolved receivables.
- Hypothetical missed quotes from actual orders; no claim that price touch means fill.

Suggested stable reasons: `DISCOUNT_TOO_SMALL`, `RESIDUAL_MODEL_UNSUPPORTED`, `CONVERGENCE_NOT_TRADABLE`, `FILL_RISK_TOO_HIGH`, `QUOTE_STALE`, `CANCEL_UNRESOLVED`, `TAKER_ALREADY_DISPATCHED`, `SENSITIVITY_EXCLUSION`, `TARGET_INFEASIBLE`, `ORPHAN_BUDGET_EXCEEDED`, and `EXIT_DEADLINE`.

Persist compact decision evidence through the existing reporting/journal separation. Heavy feature archives and calibrations use the independently planned warehouse/offline pipeline, never synchronous writes or queries in the hot path. Hash model artifacts and retain causation/correlation ids for exact decision replay.

## 12. Implementation work packages

1. **RB0 — Contract and capability specification.** Resolve exact payoff/reference semantics and quote/execution provider contracts using G0. Specify supported order lifetimes, post-only behavior, cancellation evidence, tick/minimum units, and pair merge availability with sanitized fixtures. Unknown semantics remain explicit blockers.
2. **RB1 — Shared foundation.** Implement/reuse G1–G6 once with the companion work. Do not copy probability, fee, sizing, scheduler, or readiness code into this package. Preserve old strategies' default behavior.
3. **RB2 — Features and artifact interfaces.** Add PM book estimators, residual state, supported OU/band evaluation, sensitivity, and fill-risk snapshots. Tests distinguish Up/Down, endpoint/average, fitted/weighted book estimator, and source/model generations.
4. **RB3 — Directional plugin.** Implement config, driver, policies, state, evidence, and generic desired quotes/taker override. Use synthetic labeled fixtures first. Verify that residual convergence via falling fair value does not manufacture a profitable-trade label or authorize an uneconomic entry.
5. **RB4 — Generic quote lifecycle.** Implement desired/actual reconciliation, post-only sells, bounded replacement, reservations, slot arbitration, runtime episode continuation, and restart recovery. This is required to call maker-first implementation complete.
6. **RB5 — Exit integration.** Complete dynamic exits with framework protection, stale-model behavior, deadline liquidation, partial sells, and optional supported hold fallback. No duplicated stop engine and no overselling.
7. **RB6 — Taker route.** Test all configured gates against actual executable cost, one-shot lineage, maker-to-taker cancel races, dispatch uncertainty, tick/fee changes, and depth-dependent size. No automatic crossing on post-only rejection.
8. **RB7 — Pair capability.** Implement the generic condition ledger, pair/orphan controller, merge/claim state and receipts, and recovery. Both outcomes and uncertain transactions count against aggregate risk. This stage is required for the original proposal's complete-set feature; without it the release must explicitly advertise directional-only support and reject pair config.
9. **RB8 — Qualification and release.** Complete deterministic fixtures, execution race/integration tests, performance/load tests, operational runbook, schema migrations, versioned artifacts, and configuration templates. Update `Docs/latest/` only after corresponding capabilities exist.

Dependency order: RB0 -> shared G0/G1; RB2/RB3 require probability and economics interfaces; RB4 depends on shared persistence/reservations/validity; RB5/RB6 require RB4; RB7 additionally requires generic multi-token accounting and settlement transaction support; RB8 validates the assembled release. Work can be organized independently by module once shared contracts are fixed, but no second execution authority is permitted.

## 13. Required tests and completion criteria

**Pure model/economics tests:** side complements without synthetic quotes; residual sign; fair-value change vs book change; positive/zero/negative reversion estimates; finite-horizon band support; probability units; cost at each book level; fee/tick/minimum constraints; inventory skew direction; target feasibility; loss-cap and quantity rounding. Check down-side conservative probability uses 1-p_high.

**Decision fixtures:** maker buy, no fill, partial fill, no attractive exit, model collapse after entry, stale feed, jump exclusion, fixed time/z exclusion, taker allowed/blocked for each reason, pair matching, and orphan completion failure. Lab uses the same configured driver/features as live and cannot import the gateway.

**Quote/execution race tests:** duplicate desired generation; superseded candidate before sign and before POST; post-only rejection; simultaneous fill/cancel; missing acknowledgement; stale cancellation response; partial fill during replacement; SELL reservation conflict; emergency stop racing TP; process crash at each durable boundary; uncertain POST at restart; manual external activity; end-of-window order still live. Assert no duplicate exposure, no oversell, and no premature reservation release.

**Pair tests:** only one leg fills; uneven net fills/fees; second-leg price moves above cap; parent risk exceeded by two pending legs; merge request timeout; reorg/provisional receipt; duplicate receipt; token/outcome mismatch; restart with orphan; mixed directional/pair config rejected. Matched inventory cannot be reused while merge is pending.

**Host compatibility tests:** existing ask70/Z-Gap/feature_probe fixtures remain valid; no unrelated adapters start; old notional sizing and stop precedence unchanged; rule-driven capability checks replace strategy-name branches; public feed loss does not erase confirmed position truth.

**Performance tests:** include a last update inside coalescing, constant event bursts, no-public-tick execution wake, stale quote priority, slow account inspection, slow durable append, metadata cache miss, disconnect/reconnect, and bounded memory over long runs. Report local and provider latency separately.

The implementation is complete only when live-declared capabilities operate end to end, failure paths are recoverable, latency targets are measured, and each decision is explainable. Actual trading returns and parameter selection are subsequent empirical work. Production-quality software is the prerequisite for measuring the strategy honestly, not a substitute for demonstrating its edge.
