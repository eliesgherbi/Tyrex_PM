# Implementation plan: settlement-aware value strategy

Strategy kind: `settlement_value`.

Date: 30 September 2026; repository inspection began 29 September. Status: implementation specification; no strategy implementation or live configuration change is made by this document. All new modules, facts, intents, configuration fields, and capabilities below are proposed unless explicitly called existing. Paths beginning `src/` are relative to the repository root.

Companion: [Residual-band strategy](residual_band.md). Research basis: [settlement-aware analysis](../../research/btc5m_settlement_aware_strategy_analysis_2026-09-29.md). Design authorities: [architecture](../../latest/architecture.md), [extension pattern](../../latest/extending.md), [strategy inputs](../../latest/strategy_inputs.md), [protection](../../latest/protection.md).

## 1. Delivery contract

Implement a production-quality strategy that buys an outcome only when its conservative expected settlement payout exceeds its executable acquisition cost by a configured margin. Its primary policy is one directional entry episode per window, followed by holding confirmed shares through official resolution. It must support skipped windows and must not manufacture exposure to meet a dollar target.

Production quality means correct accounting, bounded resource use, validated data contracts, deterministic decisions, recoverable execution, and measurable latency. It does not mean a proven profitable model. Forecast calibration and profitability assessment follow implementation; mechanical correctness, integration tests, failure tests, and latency qualification are part of implementation, not postponed until tuning.

The initial economic target is $3 expected net profit per selected episode, subject to loss, capital, liquidity, and model-uncertainty constraints. It is not a guaranteed realized gain. A $5 account debit limit must remain a $5 limit unless explicitly configured otherwise; never replace it with a larger research example.

The basic route is taker entry and settlement. Passive entry is a separately configured extension using the shared quote controller specified by the companion plan. Do not silently turn this strategy into a residual-reversion exit strategy. Completed-pair acquisition is a separate optional module, not a prerequisite to directional entry.

## 2. Current implementation and specific gaps

The inspected working tree has these reusable components:

- `facts/contract.py`, `facts/ids.py`, `facts/producers.py`, and `facts/mappings.py`: declared input closure, readiness, and feed/timer triggers.
- `indicators/graph.py`: runtime-owned incremental features; the strategy receives `FeatureBundle`.
- `indicators/binary_fair_value.py`: endpoint-style probability using log price ratio, percentage volatility per square-root second, and remaining time.
- `market_data/executable.py`: `executable_vwap` already walks depth; extend its use instead of creating another book walker.
- `strategies/z_gap/valuations.py`: useful separation of entry cost, settlement value, and liquidation value, currently within the Z-Gap package.
- `strategies/context.py`: immutable confirmed exposure and `ReferenceFacts`, currently only latest Chainlink value, freshness, and sealed K.
- `core/intents.py`: entry, exit, cancel, flatten, and hold-to-resolution contracts. Existence of a type does not establish working execution.
- `execution/planner.py`, `coordinator.py`, `reducer.py`, `final_gate.py`, and `polymarket/gateway.py`: one execution authority and durable evidence.
- Existing resolution evidence and capability types, startup settlement inspection, and `execution/settlement_cache.py`.

The following are actual gaps, not configuration-only tasks:

1. Endpoint pricing cannot represent accumulated averaging history. A latest TWAP observation is not automatically the constituent history required to forecast the next TWAP.
2. `market_data_runtime.py` passes `evaluation_interval_s` as a scheduler override; it also checks timers from a loop sleeping up to one second. Declaring a 5 ms timer alone does not produce a 5 ms service.
3. The existing Z-Gap final feature gate primarily handles EWMA readiness; broader feature generations and reference validity need a generic contract.
4. `HoldToResolutionIntent` is currently recorded by the live runtime, not executed as a complete hold/settle/redeem lifecycle. The Z-Gap plugin rejects enabling automatic resolution.
5. Existing position/session views describe a selected instrument; unresolved holdings across windows require explicit accounting and capacity handling.
6. Entry intents request notional. The planner applies a fee reserve and derives size. A model's desired share count cannot be assumed to equal the eventual position.
7. `lab/` evaluates fixtures. It is not a historical fill or profitability simulator.

The working tree includes ongoing inspection, reconciliation, and finality changes. Build on those changes; do not revert them or revive earlier latency designs as if they were the current implementation.

## 3. Strategy invariants and decision logic

### 3.1 Payoff and reference

For a verified binary contract, Up pays Y and Down pays 1-Y. The probability model estimates the physical probability of the official outcome, not a generic Binance price direction and not an option-implied probability assumed to be physical.

Before trading, bind an immutable `SettlementContractSpec` containing:

- Market, condition, window, token, chain, and outcome-index identities.
- Exact opening threshold and provenance, or an explicit not-ready status.
- Reference stream identity, units, precision, source timestamp semantics, and publication delay semantics.
- Endpoint, rolling-average, or other supported payoff-functional identifier.
- Averaging length, sample/weight convention, boundary selection, comparison operator, rounding, and tie behavior where applicable.
- Rules revision/hash, provenance references, and verification status.

Do not infer the functional solely from the market title, a feed name, or a generic FAQ. Unsupported or ambiguous rules mean no entry. Archive the provider evidence during implementation. A change in rule version invalidates dependent models and pending entries.

### 3.2 Probability engine

Implement a model interface with two explicitly selected backends:

1. `endpoint_diffusion`: preserve the existing endpoint formula as a benchmark for contracts whose verified payoff matches it.
2. `average_diffusion`: conditional moments of the verified weighted average, including already observed history and reference-estimation error.

For the continuous arithmetic-diffusion benchmark, when t is inside the last L seconds and h=T-t:

\[
m_A=(I_t+hS_t+\mu h^2/2)/L,\qquad
v_A=\sigma_d^2h^3/(3L^2),\qquad
p_U=\Phi((m_A-K)/\sqrt{v_A}).
\]

Before that averaging interval, h>=L:

\[
m_A=S_t+\mu(h-L/2),\qquad v_A=\sigma_d^2(h-2L/3).
\]

These are model backends, not hard-coded claims about current market rules. For discrete reference weights, use the actual weighting/covariance functional. With uncertain threshold or history, model A-K jointly; include covariance instead of adding unrelated variances by convenience. Never average already averaged reports again unless that is the verified payoff.

Units are typed: sigma_d is dollars/sqrt(second); existing EWMA sigma is a log-return scale/sqrt(second). Any approximation sigma_d approximately S*sigma must be explicit and versioned. Equality at zero variance uses the verified comparison and precision, not a tau floor pretending that unresolved uncertainty remains.

Maintain separate physical-model and calibrated/combined probability outputs. An optional fitted combination with the book is permitted only through a versioned artifact; its training target, clipping, uncertainty method, and regime must be known. A raw point estimate or arbitrary haircut must not be presented as a statistically guaranteed lower bound.

### 3.3 Entry and size

Given an Up interval [p_low,p_high], use p_low for Up and 1-p_high for Down. For N net payoff-bearing shares of side j, define C_j(N) as conservative executable total debit, including depth prices, fees at their actual levels, explicit slippage/delay assumptions, and any attributable settlement/claim cost.

\[
G_j(N)=N p_{j,conservative}-C_j(N).
\]

Search only feasible sizes on the venue's validated quantity lattice. Reject if metadata, cost parameters, or minimum-size units are unknown. Use the existing fee function and actual market parameters. Never double-count spread already embedded in asks. Account explicitly for any fee charged in shares so N is the net payout quantity.

The entry policy requires positive conservative edge above the configured minimum, acceptable reference quality, supported regime, and feasible exposure. `target_mode` must be explicit:

- `require_target`: choose the smallest feasible size with G_j(N)>=target_profit; otherwise skip.
- `cap_only`: choose a risk-constrained size and report its implied EV even when below target.

Both modes enforce maximum total debit, maximum full-loss exposure, bankroll allocation, account-wide reserved capital, usable depth, and fractional robust-Kelly limits when enabled. For flat unit cost c, N_K=B(p_low-c)/(c(1-c)), clipped at zero, is a diagnostic/reference solution. For depth-dependent cost use a bounded numeric search. Do not silently use directional Kelly for a different payoff distribution.

Side selection maximizes the configured conservative utility among feasible candidates. Ties resolve deterministically. Both-positive opportunities are recorded, but the initial directional strategy does not accidentally submit both outcomes as two independent positions.

### 3.4 Entry episode and hold

- One logical entry episode per window. Unfilled canceled attempts can be retried only within a bounded attempt budget and after proven termination; a confirmed fill consumes the directional episode.
- A partial fill is a position, not an instruction to top up until EV reaches $3. Report actual expected value and keep the existing exposure unless an explicitly tested completion policy permits more.
- The primary strategy has no profit-price target or residual exit. Emergency/risk actions remain framework-owned.
- The live profile must declare a resolution-compatible lifecycle. Existing mandatory pre-expiry flatten settings cannot remain active accidentally or be bypassed by strategy code.
- Hold intent is a durable lifecycle commitment, not a venue order. New entries require the full resolution capability before acquisition, not only after a fill.
- Before commitment, capability loss causes the documented pre-commit recovery/exit policy. After commitment or market closure, stop new entries and reconcile; do not promise that emergency liquidation remains possible.

## 4. Data inventory and typed interfaces

### 4.1 Required external data

**Polymarket market metadata and rules:** identity mapping, start/end times, exact threshold/reference evidence, fee schedule, tick, minimum order constraints, trading/closure state, and relevant venue delay flags. Preload and validate during preparation; refresh through background adapters. Tick/rule changes invalidate affected order candidates.

**Separate Up and Down books:** snapshot/delta provenance, sorted depth, original timestamp when supplied, local receive time, connection generation, local update version, tick, synchronization status, and depth coverage. Do not synthesize executable Down quotes as 1-Up. Use continuity checks supported by the actual feed; do not invent sequence guarantees from hashes.

**Reference reports/history:** exact decimal value, stream identity, source observation interval, published/source time if available, local receive time, provenance, and whether a message is historical replay. If constituent history is inaccessible, implement an explicitly uncertain state estimator or remain not ready. Missing history is never silently zero.

**Binance spot trades:** price, size, event/trade id, source time, receive time, and connection generation. Use for volatility and an optional fast reference estimate. Current BTCUSDT and a USD resolution reference have a currency basis; include that difference in the reference model rather than assuming identical units.

**Authenticated execution/account evidence:** balances, allowances, confirmed fills, open orders, fees, holds/reservations, and evidence completeness. Read from the authority's current immutable views, never by network calls inside `evaluate()`.

**Resolution and redemption evidence:** condition/outcome mapping, payout vector, finality/provenance, claim transaction identity and receipt, and credited collateral. Read asynchronously. Scheduled expiry is not proof of resolution or cash receipt.

### 4.2 Optional data

Binance L2, book imbalance, jump/tail predictors, calibrated market probability, maker markout features, and rebates. Declare only features actually used. Enabling an optional model makes its required inputs mandatory for that mode; an absent optional feature is not converted to zero. Perpetual/funding sources remain unsupported until real adapters exist.

### 4.3 Shared immutable outputs

Introduce typed, versioned contracts, with compatibility defaults for existing consumers:

- `SettlementReferenceState`: contract id/hash, K and uncertainty, current reference estimate, known weighted history, remaining weights, basis state, coverage, quality, as-of receive time, connection generations, and rejection reasons.
- `BinaryProbabilitySnapshot`: point probability, conservative interval, predictive moments, model/artifact version, regime, validity horizon, parameter-uncertainty method, and all upstream epoch identifiers.
- `AcquisitionCostCurve`: bounded depth breakpoints, net share/debit mapping, fee provenance, quantity constraints, and depth sufficiency.
- `SizingDecision`: permitted net shares/debit, risk usage, expected profit range, target-feasibility flag, and binding constraint.
- `DecisionValidity`: market/rule/model versions, source generations, decision receive time, expiry/age budget, selected side, maximum economic price, and capital reservation identity.

Bundle aligned immutable observations at one evaluation boundary. As-of joins use information received before the decision; never use a future nearest tick. Distinguish source age, transport delay, local residence time, and cross-feed skew.

## 5. Module ownership: shared foundation G0–G8

Implement this foundation once. The residual-band plan reuses the same interfaces and work packages. Neither strategy imports the other strategy's private modules.

### G0 — Provider semantics and contract binding

- Extend `domain/polymarket/resolution.py` and add `domain/polymarket/settlement_contract.py` for pure verified rule and payoff specifications.
- Reuse `domain/polymarket/ptb.py`, existing attestation/alignment helpers, and outcome mapping where their semantics fit. Do not weaken exact threshold readiness to enable a new model.
- `adapters/polymarket/` owns transport and provider parsing. Add a current-reference adapter only after verifying the existing transport against the required stream. Keep SDK/provider details out of indicators.
- `runtime/market_family.py` binds rules and identity; `runtime/composition.py` starts declared supported adapters. Unsupported combinations fail at composition.
- Register proposed derived facts such as `settlement.contract` and `settlement.reference_state` in `facts/ids.py` and `producers.py`; choose upstream source facts only after provider verification. These names are additions, not existing catalog entries.

Acceptance: exact supported semantics demonstrated with sanitized provider examples; mismatched stream, unknown rule, incorrect currency, or unverified threshold cannot produce an eligible entry.

### G1 — Reference and probability features

- `market_data/reference_store.py` and `settlement_store.py`: bounded source observations and provenance; add `reference_history.py` for ring-buffer/weighted-history state when needed.
- `indicators/settlement_probability.py`: reusable pure endpoint/average probability and moments.
- `indicators/reference_state.py`: incremental reference estimator, basis/uncertainty update, and generation invalidation.
- `indicators/reference_basis.py`, `reference_alignment.py`, and `causal_pairing.py`: reuse pure arithmetic and causal joins where appropriate.
- Extend `indicators/graph.py`/`spec.py` with declared reference-update inputs and readiness metadata. Keep one runtime-owned graph; do not make a private history store or private EWMA in the strategy.
- Extend `strategies/context.py` reference facts with an optional immutable reference-state view. Preserve the generic evaluate signature; no ad hoc Chainlink kwargs.

Acceptance: probability/units/averaging boundary tests, reconnect/history gaps, uncertain K, and exact settlement tie behavior. Both strategies consume the same snapshot type.

### G2 — Cost and sizing primitives

- Extend `market_data/executable.py` to expose bounded depth cost segments using the existing walker.
- Add pure provider-independent `domain/binary/valuation.py` and `domain/binary/sizing.py`; use `domain/polymarket/fees.py` as an injected fee implementation at assembly. Extract reusable arithmetic from Z-Gap only behind compatibility tests; leave its private policies and public behavior unchanged.
- `execution/planner.py`: introduce an optional explicit net-share/debit sizing contract in a backward-compatible intent extension; old notional-only intents retain their current meaning. Reject mutually contradictory quantity/notional requests.
- `execution/account_state.py`/coordinator: reserve capital atomically for pending orders; confirmed fills transfer reservation into inventory. Unknown dispatch retains its reservation.
- Final planning recomputes attainable size, fees, and worst price. If rounding or reserve changes make a strict target infeasible, reject or return a clearly classified result before POST; never pretend requested shares are guaranteed filled.

Acceptance: fees across price levels, share-fee treatment, minimum size, tick rounding, insufficient depth, partial fills, and debit invariant under concurrent requests.

### G3 — Fast scheduling and snapshots

- `strategies/registry.py`: add a generic optional configured-input-contract factory/accessor so coalescing and enabled-feature dependencies are derived from typed config. Preserve static contracts for existing plugins and use the same accessor in live and lab paths.
- `runtime/scheduler.py`: dirty/latest-snapshot scheduling, bounded coalescing, deadline timers, and explicit execution-state wake support. Add a declared `OnExecution` trigger or equivalent typed generic trigger; do not pretend execution updates already are public source facts.
- `runtime/market_data_runtime.py`: replace the one-second strategy timer service with scheduled monotonic deadlines independent of maintenance ticks. Resolve the existing `default_coalesce_s` override explicitly. Do not accelerate account/startup REST polling to millisecond cadence.
- `runtime/market_runtime.py`: versioned immutable snapshot assembly and deduplicated evaluations; every ingest still updates stores.
- Execution fills, cancel outcomes, risk blocks, and rule invalidation must wake affected policy even if no public tick arrives. Cancel/risk processing must not wait for PTB/model readiness.

Acceptance: a last event suppressed by coalescing causes a deferred evaluation; no perpetual postponement under continuous flow; no duplicate decisions on unchanged state; existing ask70 contract remains free of Binance/Chainlink dependencies.

### G4 — Execution validity and observability

- `core/intents.py`: optional generic `DecisionValidity`/sizing metadata with typed validation and default compatibility; it carries constraints, not provider calls.
- `runtime/capabilities.py` and `execution/final_gate.py`: recheck declared input readiness, generations, model validity, current book, reservation, rule version, and economic limit immediately before durable dispatch. Do not rerun arbitrary strategy code from the gate.
- If reference/model state has materially invalidated the candidate, reject and schedule a new decision; a still-valid limit is not a license to send stale alpha. Protective exits do not require a healthy alpha model.
- Keep prepare -> sign -> newest view -> final gate -> durable authorization -> POST. Preserve uncertain-dispatch reconciliation. Do not introduce a second mutation writer.
- Extend evidence and reporting with stage latencies and source/model versions. `None` is missing; zero is a valid measurement.

### G5 — Model artifacts and bounded inference

- Proposed `domain/models/artifact.py`: immutable schema, model id/hash, training cutoff, rule regime, feature schema, units, calibration range, uncertainty method, and validity limits.
- Artifact loading/validation happens in runtime composition or preparation, not evaluation. Do not load pickle or execute artifact-supplied code; use validated data-only serialization.
- Fit model/variance/calibration parameters offline. Runtime applies bounded arithmetic or small frozen lookup tables. Model updates are atomic at a declared safe boundary and invalidate older pending candidates.
- Engineering fixtures may use synthetic parameters and must be labeled non-validated. A live profile needs a compatible artifact and explicit operational limits; no fabricated confidence intervals or live profitability claims.

### G6 — Execution-state projections and persistence

- Extend `strategies/context.py` with immutable order/reservation/position summaries supplied by the execution authority. Strategies do not query journals or infer fills.
- `execution/reducer.py` is the sole derivation of confirmed order/inventory state; `coordinator.py` remains its sole mutation authority.
- Persist enough model/strategy state to reproduce episode consumption, but reconstruct monetary truth from execution evidence after restart.
- Keep critical execution authorization and evidence durable. Telemetry can be batched; durability cannot be removed to improve latency numbers.

### G7 — Performance harness and replay interfaces

- Add focused benchmarks under `tests/performance/` and an explicit runner under `scripts/`; no provider/network calls in deterministic local benchmarks.
- Extend `lab/evaluate_replay.py` fixtures for reference history, generations, immutable execution events, model artifacts, and both new plugins using their real configured input contracts.
- A separate offline research replay engine may consume recorded events; it must not import the live gateway. Keep it distinct from fixture lab, live runtime, and the independently planned warehouse process.

### G8 — Documentation and shared compatibility

Update `Docs/latest/architecture.md`, `strategy_inputs.md`, `extending.md`, `config_guide.md`, `execution_lifecycle.md`, and relevant operator docs after features actually ship. Update current docs, not historical implementation claims. Preserve existing plugin defaults and execution behavior unless a documented generic change requires a migration.

## 6. Strategy package and configuration

Create `src/tyrex_pm/strategies/settlement_value/` with:

- `config.py` and `schema.py`: immutable typed config; unknown keys rejected.
- `plugin.py`: input contract, indicator specs, driver factory, required lifecycle capability validation.
- `driver.py`: generic evaluate adapter; consumes shared snapshots and context.
- `snapshots.py` and `assemble.py`: private atomic decision view, with no I/O or ownership of raw stores.
- `policies.py`: side selection, feasibility, one-entry policy, and hold preference.
- `strategy.py`: private state transition and intent creation.
- `state.py`: window id, consumed episode, pending candidate identity, hold-request state, model version; not monetary authority.
- `reasons.py` and `calibration.py`: stable explanations and compact evidence.

Register through `ensure_strategies_registered()` only. Do not add strategy-kind branches to generic runtime/config modules. Add generic plugin capability declarations if needed instead of an `if settlement_value` exception.

Required configured input closure: PM metadata/books, clock, account, exact sealed threshold/reference contract, reference-state sources, and the selected variance source. Wake on PM books, reference updates, volatility-changing inputs, declared execution updates, and time deadlines. Optional L2 is added only when its model is enabled.

Configuration groups must cover:

- `model`: payoff backend, artifact id/path/hash, variance source, basis method, coverage and uncertainty requirements.
- `entry`: time band, minimum conservative edge, target profit/mode, route, max episode/attempt count, candidate age and validity.
- `sizing`: bankroll definition, fractional Kelly when used, maximum loss, liquidity participation, quantity limits.
- `quality`: per-source freshness/skew/clock limits, reconnect warmup, supported regimes.
- `schedule`: event coalescing and deadline service, using G3 rather than assuming existing YAML keys exist.
- `holding`: explicit settlement mode, pre-commit fallback, outstanding-resolution count/debit cap, claim policy.
- Shared risk/lifecycle/protection fields stay in their existing framework owners; do not duplicate them under strategy parameters.

Deliver a validate-only example and a production configuration template with explicit operator-supplied limits. Do not label synthetic parameters as tuned. A config that simultaneously demands hold-to-resolution and unconditional pre-expiry flatten must fail with a precise explanation.

## 7. Generic hold, resolution, and claim lifecycle

This is mandatory for the primary strategy, not a follow-up left to tuning.

### 7.1 Ownership and state

Add `execution/resolution_manager.py` for a generic controller invoked by the coordinator, with provider work in adapters/gateway. Extend existing evidence, reducer, session state, journal migrations, and capabilities. Reuse resolution evidence types and the inspected finality rules; never treat a cached `latest` chain read as finalized clearance.

Proposed durable lifecycle:

`POSITION_OPEN -> HOLD_REQUESTED -> HOLD_COMMITTED -> AWAITING_RESOLUTION -> PAYOUT_PROVEN -> CLAIM_PENDING -> CASH_CONFIRMED -> COMPLETED`

Add explicit `RESOLUTION_UNCERTAIN`, `CLAIM_UNCERTAIN`, and `MANUAL_INTERVENTION` paths. A zero-payout position may complete after proven resolution and inventory/account reconciliation without a cash claim; fractional/exceptional payouts must be handled by actual payout evidence or classified unsupported, never relabeled Up/Down by guess.

On hold request, verify capability, ownership/quantity, settlement-spec identity, capital reserve, and outstanding orders. Cancel conflicting resting sells and resolve any cancel/fill race before committing. Never acknowledge a hold while an unknown sell could still fill.

### 7.2 Resolution and collateral

- Maintain bounded asynchronous resolution observations after the market ends. No claim transaction or balance read is part of probability evaluation.
- Track payout receivable separately from spendable cash. Scheduled close, a UI winner, a payout claim, and credited collateral are distinct events.
- Verify condition/token/outcome mapping, finality and reorg handling. Unknown status or missing evidence stays unresolved.
- Automatic redemption requires supported chain/wallet/provider capabilities, nonce management, idempotent transaction tracking, receipts/finality, fees, and restart reconciliation. Implement it as a generic execution operation through the authority, not strategy-owned RPC. Do not release the feature as complete if it only records an intent.
- If redemption is configured manual, report receivable and manual work explicitly, retain capital reservations, and cap unresolved inventory. Such a profile is not the unattended production acceptance profile.

### 7.3 Rollover and exits

Extend runtime episode management so an ended market can remain in resolution management while the next market prepares. Aggregate all pending orders and unresolved settlement exposure in account risk. A maximum unresolved count/debit cap stops further entries. Preserve old terminal-stop behavior for existing profiles; opt into a generic continuous-episode capability rather than a strategy-name branch.

Define precedence: emergency/risk control owns tradable exits before commitment; a successful durable hold changes the lifecycle policy only for that position. After closure, risk action prevents more exposure and escalates unresolved claims; it cannot fabricate a sell. Define point-of-no-return from actual supported lifecycle semantics and reject contradictory config.

On restart, discover journal-owned pending holdings/claims before authorizing any new purchase. An uncertain claim is reconciled using its existing identity, not sent again as a new transaction.

## 8. Simplified algorithm

```text
on declared fact, execution event, or deadline:
    snapshot = read bounded immutable state
    process framework-owned risk/lifecycle precedence
    if we already own a pending or filled episode:
        request/maintain supported hold lifecycle as appropriate
        return explanation without a duplicate buy
    if reference contract, required features, or account are not ready:
        return blocked
    probability = shared settlement model + compatible calibration
    candidates = []
    for side in [UP, DOWN]:
        lower_probability = p_low if UP else 1 - p_high
        costs = bounded acquisition curve for that side
        size = solve configured target/utility within every risk cap
        if size is feasible and its conservative edge clears margin:
            append candidate with immutable validity envelope
    if no candidate:
        return no_trade with binding reasons
    emit one EnterIntent for deterministic best candidate

on authoritative fill:
    record actual exposure through reducer
    consume episode; do not top up to recover a missed dollar target
    request hold only if lifecycle preconditions still hold

on official resolution/claim evidence:
    coordinator reconciles payout, transaction, cash, and completion
```

## 9. Millisecond implementation budget

These are proposed engineering acceptance targets measured on declared target hardware, not achieved benchmarks or guarantees about the exchange. The two strategies share these targets.

- Normalized event ingestion plus incremental feature update: p95 <=0.5 ms, p99 <=1 ms per processed event.
- Pure evaluation, including two bounded cost/size searches: p95 <=1 ms, p99 <=3 ms.
- Relevant local receive-to-intent publication, including configured coalescing: p95 <=5 ms, p99 <=10 ms in the qualified load envelope.
- High-priority invalidation to local cancel/risk-command enqueue: p99 <=5 ms; it bypasses ordinary alpha coalescing.
- Dirty-event coalescing: initial maximum 2 ms; fallback strategy deadline 10 ms, independent of the one-second maintenance loop. These are scheduling budgets, not alpha thresholds.
- Warm local planning/preparation/signing/final checks, excluding contention, external SDK reads, and durable storage: initial target p95 <=50 ms, p99 <=150 ms. Measure each component; a slow SDK is a release issue, not hidden inside the 3 ms evaluation number.
- Critical durable authorization append: target p99 <=10 ms on the qualified disk. No POST before durable authorization. Exceeding it rejects/invalidates stale candidates rather than bypassing the journal.

A stage target does not bound total execution. Record receive -> decision -> queue -> prepare -> durable authorization -> POST -> venue acknowledgement -> match -> confirmation. External network, venue delays, claim transactions, and finality have separate observed distributions and may take much longer. A 100 ms feed cannot reveal fresh information every millisecond.

Performance design:

1. Use incremental ring buffers, cached sufficient statistics, bounded book depth, and at most a configured number of size candidates; initial bound 32 depth breakpoints per side and 64 size-search iterations/candidates. Reject or conservatively cap capacity beyond observed depth.
2. No pandas, model fitting, Monte Carlo, optimizer startup, database queries, HTTP, signing, or synchronous file logging inside `evaluate()`.
3. Float64 for probability/variance math with finite/range checks; Decimal or validated integer units for prices, shares, fees, and order constraints. Convert only at explicit boundaries.
4. One single-writer asyncio loop owns state and execution. CPU-heavy fitting runs offline. Do not move mutable strategy state between worker threads to claim speed.
5. Coalesce redundant evaluations only after ingesting every needed book delta/trade. Never drop execution evidence or required history. Bound pending evaluations to the latest dirty state per market; use a priority queue with finite capacity for mutation commands and preserve critical events durably.
6. Slow inspection/history/reporting cannot hold the execution SDK slot. Preserve the existing separate inspection-client design. No outer cancellation of in-flight SDK HTTP operations that could leave the transport or dispatch uncertain.
7. Excess local lag, queue saturation, or unavailable durable storage blocks new entry and invokes documented protective handling. All work has explicit memory/CPU/depth limits.

Benchmark at a minimum of 2,000 normalized mixed market events/sec sustained for 30 minutes and 10,000/sec bursts for 10 seconds, with active/next-window data, fixed subscribed features, and adverse execution events. Record CPU, memory, hardware, event sizes, full histograms, sample counts, event loss, and p99/max loop lag. These are initial qualification loads; revise upward from measured production rates. Include cold start, reconnect, model reload, GC, and disk contention separately. Do not claim a percentile target from mocks with no representative payload processing.

## 10. Evidence and operational reporting

Every decision record includes contract/reference/model versions; point/conservative probability; time remaining; book/source epochs and ages; considered costs/sizes; selected side; binding risk constraint; target feasibility; and stable reason codes. Suggested reasons include `REFERENCE_UNVERIFIED`, `HISTORY_INCOMPLETE`, `MODEL_REGIME_UNSUPPORTED`, `MODEL_UNCERTAINTY_HIGH`, `EDGE_BELOW_COST`, `TARGET_INFEASIBLE`, `DEPTH_INSUFFICIENT`, `RESOLUTION_CAPABILITY_UNAVAILABLE`, and `EPISODE_CONSUMED`.

Record predicted EV separately from actual realized profit, unrealized inventory, settlement receivables, and available cash. Report both selected-episode and all-window statistics. Carry execution correlation ids through fills and claims. Decision telemetry may use bounded batching with explicit dropped-telemetry counters; critical mutation/accounting evidence cannot be dropped.

Integrate with the [warehouse plan](../../implementation/pm_data_warhouse/live_market_data_warehouse_btc5m_implementation_plan.md) via versioned offline event exports only. No Parquet/warehouse query belongs on the trader's hot path. The collector's receive order is not automatically the trader's receive order.

## 11. Delivery sequence and acceptance

1. **SV0 / G0 — Semantics and capability design.** Produce sanitized provider-contract fixtures and the exact supported payoff/claim contract. Freeze unsupported behavior as explicit rejection cases.
2. **SV1 / G1, G5 — Reference model.** Implement incremental state and pure probability backends; validate moments analytically and against offline simulation, including zero time/variance, weighted samples, discontinuities, basis covariance, and currency units.
3. **SV2 / G2 — Economics.** Implement cost/size interfaces and notional/share compatibility; verify maximal debit, conservative Down probability, target infeasibility, rounding, and depth-dependent sizing.
4. **SV3 / G3, G4, G6 — Runtime foundation.** Deliver fast scheduler, immutable evidence, generation validity, reservations, and regression coverage for existing plugins.
5. **SV4 — Strategy package.** Add real plugin/config/driver/policies and deterministic lab fixtures for entry, skip, stale state, partial fill, duplicate callbacks, and window rollover. No live gateway in the lab.
6. **SV5 — Resolution lifecycle.** Deliver hold commitment, order cancellation race handling, payout proof, claims, cash reconciliation, bounded cross-window holdings, and restart recovery. Inject failures before/after each durable boundary and provider call.
7. **SV6 / G7 — Production qualification.** Run latency/load tests and recorded-event integration. Test network failure, delayed/duplicate/contradictory evidence, rule changes, fee/tick changes, stale features at final gate, and uncertain claims. Complete operator runbook and schema migration/rollback procedure.
8. **SV7 — Release package.** Ship validate-only and complete production templates, artifact schema, fixture/replay commands, diagnostic reports, and updated current docs. Production capability remains unavailable when any required feature is missing; do not substitute an unfinished branch with a TODO or silent fallback.

Completion criteria: no strategy I/O; same evaluate path in lab/live; no readiness bypass; no false fill/settlement inference; total-debit and unresolved-exposure limits maintained; recovery is idempotent; performance distributions meet declared local targets; existing strategy tests pass; all claimed capabilities work end to end. Profitability tuning begins from this mechanically complete implementation and does not excuse missing lifecycle or accounting work.
