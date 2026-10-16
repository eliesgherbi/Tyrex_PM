# Phase B — Data module implementation and expected usage

Prepared: 30 September 2026. Status: implementation plan; the commands and new modules below are proposed, not available functionality. Repository review: `599ea94` (`release: freeze the Ask70 inspection baseline`), with separate working-tree changes left untouched.

## 1. Decision and scope

Build a separate, continuously running recorder that turns delivered market messages into a durable, inspectable research dataset. Then build an offline replay consumer of that dataset. This allows us to investigate both BTC five-minute strategies using evidence collected before their implementation is complete.

The [existing warehouse specification](live_market_data_warehouse_btc5m_implementation_plan.md) remains the authority for collection profiles, schemas, storage guarantees, source handling, and W0–W8 acceptance. This companion explains the delivery sequence, module responsibilities, research handoff, and expected operator workflow. Its explicit scope extension is the separate B5 replay consumer; it does not put simulation inside the collector.

Phase A trading qualification remains with the operator. The [Phase A report](../phaseA_report.md) is context, not evidence that this new data module works. Collector development does not depend on Phase A live-order tests. The data module requires its own offline, fault-injection, read-only live, and capacity evidence. Later replay latency calibration can incorporate the operator's Phase A findings.

The desired outcome is three independently useful deliveries:

1. **Recording:** retain raw inputs across successive windows and report interruptions honestly.
2. **Research datasets:** reconstruct what was observable at each moment, identify unsuitable intervals, and export reproducible market histories.
3. **Execution replay:** evaluate hypothetical orders under explicit delay, liquidity, cost, and fill assumptions.

None of these deliveries establishes a profitable strategy. They provide the evidence needed to test one.

## 2. Review of Phase B against the existing plan

### B1 — Verify the contracts we are collecting

Covered principally by W0/W1. Confirm market identity, token mapping, exact settlement rule, opening threshold, reference feed, timestamps, fees, tick size, minimum size, and resolution meaning. Store the provider evidence and version these interpretations.

**Required clarification:** subscribing to a reference feed does not establish that its value is the official opening threshold or that it exposes all constituents needed to model settlement. Verify these separately on selected markets. Preserve unknowns instead of manufacturing an endpoint or average from convenient ticks.

### B2 — Record continuously

Covered by W2–W5. Collect both Polymarket outcome books and market events, metadata, the required reference stream, Binance trades and declared depth, and lifecycle/resolution observations. Follow successive windows without launching a strategy.

**Required clarification:** the existing trading adapters are not lossless recorders. The current Binance depth adapter consumes top-20 snapshots; a recorder using diff depth needs its own synchronization logic. Start collecting useful raw data as soon as the durable recording slice works; downstream normalization can catch up.

### B3 — Make storage survive failure

Covered by W2, W3, W6, and W7. Recovery includes raw files, publication state, expected-market coverage, and unfinished discovery/resolution tasks. An application restart must not silently erase a missed interval or an unresolved market.

**Required clarification:** “received,” “queued,” “durable,” and “normalized” are separate watermarks. The baseline's 250 ms fsync cadence is a batching setting, not a promise that every received message survives a sudden power loss.

### B4 — Produce usable datasets

Covered by W6/W7 and qualified by W8. Provide normalized observations, valid book epochs, versioned metadata, boundary evidence, resolution labels, quality reports, and as-observed queries/exports.

**Required clarification:** usability is question-specific. A market may support a spread study while lacking the official boundary evidence required for a settlement model. Exclude and count unsuitable windows under a versioned research policy; do not discard their raw data.

### B5 — Replay historical execution

The warehouse specification explicitly excludes a fill simulator. The existing [fixture evaluator](../../../src/tyrex_pm/lab/evaluate_replay.py) evaluates strategy decisions; it is not an execution backtester.

**Delivery decision:** implement B5 as a separate offline research package after the export contract is stable. It consumes pinned datasets and emits simulated orders, fills, inventory, costs, and results. Neither the collector nor the live trader depends on it. Its generic engine can be tested with scripted intents before either new strategy exists; strategy-specific adapters and calibration follow their implementation plans.

## 3. Provider verification and unresolved facts

Official documentation checked for this planning review on 30 September 2026 supports the transport direction in the baseline:

- Polymarket's market channel publishes outcome-token book and market-event messages. Retain message batches and reconstruct books from their actual protocol, rather than imposing another venue's sequence rules. [Market channel specification](https://docs.polymarket.com/api-reference/wss/market).
- PolyBolt documents authenticated subscriptions using CLOB credentials and a Chainlink BTC 60-second TWAP channel. A generic crypto-price subscription is not an interchangeable settlement feed. [PolyBolt specification](https://docs.polymarket.com/api-reference/live-data/overview).
- The resolution endpoint returns condition-level state, accepts one selector family, and limits condition selectors to 20 distinct IDs per request. This is a separate batching limit from order-book reads. [Resolution endpoint](https://docs.polymarket.com/api-reference/markets/get-resolution-state).
- Binance diff-depth reconstruction requires buffering, REST bootstrap, update-ID synchronization, and recovery after discontinuity. The initial 5,000-level snapshot does not establish knowledge of every deeper level. [Binance stream specification](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md).

These are documentation checks, not authenticated live qualification. W0 must still establish:

1. The actual rules and boundary interpretation for the selected BTC markets, including averaging/equality semantics and supported rule variants.
2. Whether the authoritative threshold is exposed through a supported structured source, with its observation time.
3. Whether the reference data includes constituent observations required by the planned averaged-settlement model. Published rolling averages alone do not uniquely recover their constituent prices.
4. Exact fee parameters, units, rounding, order constraints, and changes over time. Do not hard-code the fee formula from earlier research into archival records.
5. Actual CLOB seeding/continuity behavior, reference acknowledgements, decimal precision, timestamp meanings, and resolution status/payout units.
6. Access, TLS, source limits, retry behavior, and capacity on the intended recording machine. An unavailable provider remains an unpassed gate; preserve certificate verification.

Deliver `provider_contract_report.md` in this folder during implementation, plus sanitized fixtures labelled as live, documentation-derived, or synthetic. It must name supported, unsupported, and unresolved capabilities. Documentation examples alone cannot prove live semantics.

## 4. Data inputs and what each enables

### Market identity and contract history

Collect event/market/condition/token identifiers, outcome labels and indices, scheduled window boundaries, market description and resolution source, rule fingerprint, fee/order constraints, activity/closure status, and metadata observation history.

Purpose: select the right market, match Up/Down correctly, interpret the payoff, and apply constraints actually known at a simulated decision. A corrected rule observed tomorrow must not silently replace yesterday's decision inputs.

### Polymarket books and market messages

Retain both tokens' original snapshots, price changes, trades, quote/tick/lifecycle events, control observations, unknown messages, and independent REST audit responses. Record connection generation, reception order, provider timestamps where present, and reconstruction validity.

Purpose: study spreads, depth, imbalance, residuals, short-term changes, executable taker prices, and uncertainty in maker fills. A displayed bid is not evidence that our hypothetical buy filled.

### Official reference observations and boundary evidence

Retain the actual source/channel/symbol/window, exact values, source times, arrival times, history/live distinction, sequence/drop evidence, and field provenance. Store derived boundary candidates separately from official attestations and from later resolution evidence.

Purpose: check the relationship between the traded contract and the model. Both strategies need verified settlement semantics; the settlement-value strategy additionally needs enough observable history for its selected probability backend. Missing history produces an unsupported input state, not an interpolated fact.

### External BTC observations

Collect Binance BTCUSDT individual trades and synchronized diff depth at the baseline's requested 100 ms cadence, including bootstrap evidence and depth scope. Store the external stream once and bind it to all relevant markets.

Purpose: measure price moves, volatility inputs, source disagreement, order flow, and possible quote-response delays. BTCUSDT and an official BTC/USD reference have different units/source conventions; any basis conversion is an explicit later research model.

### Resolution and operational evidence

Keep payout vectors, status, identifiers, available transaction/block references, observation times, audit history, and contradictions. Separately retain connection events, clock diagnostics, expected windows, queue/disk health, parse outcomes, and gap intervals.

Purpose: produce qualified outcome labels and explain dataset exclusions. A window ending or a token trading near $1 does not establish a terminal payout. Provider-terminal evidence is distinct from independently verified chain finality.

### Inputs deliberately outside this collector

Private order acknowledgements, the account's positions, realized fills, wallet state, and transaction submission remain execution-system responsibilities. Public data cannot reconstruct our private queue position or prove actual execution latency. Later research may import a sanitized, versioned execution-evidence export; the collector must not inspect the execution journal or account.

## 5. Module ownership and project integration

All paths in this section are proposed unless described as existing. Use the package layout in the baseline; add a file only when it owns real behavior.

### Collector package: `src/tyrex_pm/data_warehouse/`

- `contracts.py`, `config.py`, `families/`: typed identities, strict profile contracts, family classification, supported rule policies, and immutable configuration versions. Generic storage must not contain BTC slug logic.
- `connectors/`: source-specific HTTP/WebSocket protocols, authentication, subscriptions, and capture of original inbound bodies. Use a read-only method/path allowlist, including read-only batch-book POST requests. No order gateway or credential provisioning.
- `capture/`: reception envelopes, byte-bounded queue, sequence allocation, durable writer, segment rotation/compression, manifests, locks, recovery, and quarantine. This layer owns durability; no strategy facts or indicators.
- `catalog/`, `discovery.py`, `lifecycle.py`: metadata relationships and durable work scheduling. SQLite is a coordination/index layer; persisted operation evidence must allow recovery of obligations.
- `normalize/`: pure source decoding, exact schemas, deterministic record identities, per-payload processing outcomes, and versioned publication. Live lifecycle projections and offline normalization share the same decoders.
- `reconstruction/`: source-specific book-epoch rules and evidence-based boundary derivation. Never use an arbitrary REST response to conceal an unbridgeable WebSocket gap.
- `quality/`: interval coverage, continuity assurance, semantic readiness, settlement readiness, and operational diagnostics. Feature/model failure must not prevent a quality report.
- `query/`: manifest-pinned reads, as-observed views, provenance, and export. These APIs are offline consumers, never called from the trading hot path.
- `supervisor.py`, `cli.py`: process wiring, lifecycle, signals, exit codes, and status. Keep business rules out of command parsing.

Supporting ownership: `config/data_collection/btc_5m.yaml`; `tests/unit/data_warehouse/`; `tests/integration/data_warehouse/`; `tests/fixtures/data_warehouse/`. Package the built-in profile so it works outside the repository working directory.

### Share pure semantics, not runtime services

Audit existing `core/ids.py`, `core/clock.py`, `core/ingress.py`, and `domain/polymarket/outcome_map.py` for reuse, including transitive imports. Preserve the warehouse's distinction between missing source time and actual reception time. The in-memory `AppendOnlyIngressLog` does not satisfy durable storage.

For research cost calculations, audit `domain/polymarket/fees.py`; extend a shared pure contract only when the verified metadata can be represented faithfully. Keep provider parameter collection in connectors/normalization and strategy entry thresholds in strategy policies. Do not duplicate a fee formula in each strategy or silently reinterpret historical metadata.

The collector must not import execution coordinators, strategy drivers, trading runtime, or execution journals. Existing trader startup must work without warehouse dependencies. Add optional `[warehouse]` dependencies with tested constraints and keep `polymarket-client==0.5.0` unchanged. Do not import archived `old/` code.

### Separate research consumer: `src/tyrex_pm/research/`

Use one offline research namespace, consistent with the proposed `research/residual_calibration.py` in the residual-band plan. B5 adds `research/replay/` with ownership for dataset validation, event scheduling, latency/fill policies, simulated accounting, and reports; `research/cli.py` owns its command entry point. Calibration/training modules are later work.

Its input boundary is an immutable export. Its output is a separate experiment directory. It may call existing pure strategy evaluation/indicator interfaces through an offline adapter after capability validation. It must never import or instantiate the live gateway, mutate a collection, or make venue calls. Keep the existing fixture lab fast and independent.

## 6. Delivery sequence and acceptance gates

These packages group the existing W milestones into useful releases. They do not waive any baseline requirement. Begin with W0/W1; do not create a temporary recorder that will later need a different format.

### Package 1 — Contracts and offline skeleton (B1; W0/W1)

Implement verified source mappings, required-feed policy, identity/schema contracts, strict configuration, dependency isolation, entry points, and fixture plumbing. Freeze the export envelope early, even though publication follows later.

Acceptance: offline profile validation; huge IDs and exact decimals round-trip; source time can remain null; no serialized secrets; no collector mutation path; existing trader imports without extras. Every unresolved provider fact has an explicit unsupported representation. Record provider evidence rather than marking all assumptions complete.

### Package 2 — Durable capture core (B3 foundation; W2)

Implement one root owner, raw queue/writer, checksums/manifests, durability watermarks, atomic publication primitives, recovery, and storage-failure handling. Add durable collector-operation events before persistent scheduling is introduced.

Acceptance: acknowledged-durable fixture records survive restart; torn tails and corruption are visible; duplicate ownership is rejected; queue/disk exhaustion cannot silently discard messages. Prove Windows file replacement, locks, and open-reader behavior.

### Package 3 — Continuous source collection (B2; W3/W4/W5)

Add expected-window discovery, both-token subscriptions, source connectors, external/reference bindings, reconnects, and persisted pending obligations. Deliver source slices incrementally using the same raw format and decoder contracts. Run the minimum lifecycle/health projections from durable events; no second parsing implementation.

Use the baseline's 24-hour discovery lookahead, 15-minute external warm-up requirement, and tiered metadata/audit schedule as initial configurable values. Budget each provider's actual request weights and batching limits, including delayed resolution work and independently running trading traffic. Report overload and missed windows rather than widening limits silently.

Acceptance: successive fixture windows capture all declared feeds, interruptions create visible epochs/gaps, and restart restores obligations. Once offline durability and connector checks pass, bounded read-only raw capture can begin while Packages 4/5 are developed. Such a collection is provisional, not yet a qualified research dataset.

### Package 4 — Meaning, outcomes, and quality (B1/B4; W6)

Implement boundary candidate provenance, official-attestation states, resolution mapping/polling, contradiction handling, follow-up audits, and the four independent quality dimensions. Preserve coverage requirements before the window and delayed labels after it.

Acceptance: unavailable official thresholds remain unavailable; an empty resolution response stays unresolved; contradictions supersede assessments without overwriting evidence. Late data cannot become an earlier feature. Quality reports explain missing required feeds, warm-up, unknown continuity, and unsupported rules separately.

### Package 5 — Publication and reproducible research reads (B4; W7)

Implement versioned Parquet builds, payload processing ledger, idempotent normalization, manifest-pinned readers, compaction, exports, and a schema-generated field dictionary. SQLite checkpoints advance only against committed durable publication evidence.

Acceptance: repeat builds have equal logical checksums; compaction never doubles records; a new parser does not overwrite the old dataset; every normalized/derived value has lineage. Test as-observed joins against deliberately late metadata/history and nanosecond ordering. Export opens in a clean process without a running collector.

Keep the baseline's exact Arrow/Decimal path. Its Decimal256 precision exceeds DuckDB's native decimal range: the supported SQL projection represents those values as exact strings unless a numeric cast is explicitly proven lossless. Do not silently convert prices or IDs through floating point. SQL consumers must not treat string ordering as numeric ordering.

### Package 6 — Qualify the warehouse release (W8)

Run offline end-to-end fault injection, a read-only live smoke covering at least three complete consecutive windows after warm-up, controlled restart/reconnect, and a 24-hour soak including UTC midnight. Follow pending resolutions beyond the soak where necessary. A stopped collector cannot finish its later audits until resumed.

Acceptance: baseline definition of done, actual source-access evidence, resource measurements, recovery proof, pinned sample export, and operator instructions. Preserve unsupported semantics and unresolved markets in the report. Raw capture success does not imply every captured window is suitable for both strategies.

### Package 7 — Historical execution replay (B5; new separate consumer)

Implement export validation, a deterministic observation clock, scripted intents, configurable delays, conservative taker fills, maker scenarios, partial fills, cancellation races, accounting, and experiment manifests. Add focused replay tests under `tests/unit/research/replay/` and end-to-end offline fixtures under `tests/integration/research/`.

Acceptance: the same dataset/config/seed yields the same logical results; no observation is available before its allowed time; cash and inventory reconcile; cost changes and gaps cannot be silently ignored. An end-to-end scripted scenario works before strategy integration. The new strategies' adapters must reject missing capabilities instead of pretending their execution lifecycle has been implemented.

Packages 1–6 produce the data module. Package 7 completes the generic Phase B research foundation. Model fitting, threshold tuning, strategy development, live quoting, and deployment remain subsequent work.

## 7. Storage, timing, and performance contract

Use the baseline root layout: original raw segments, catalog, immutable normalized builds, manifests, quarantine, collection reports, and exports. Separate roots/processes from live trading; one writer and one normalizer owner per root. Raw data is never automatically deleted to make space.

For every observation preserve these distinct concepts:

- **Source time:** when the provider says the event occurred; nullable with original units/precision.
- **Reception time and sequence:** when this collector first received it, including per-process monotonic order and connection generation.
- **Durability time:** when storage acknowledged it under the configured fsync policy.
- **Publication time:** when a normalized build containing it became available to research readers.

As-observed replay uses reception availability and the declared ordering policy. It does not wait until Parquet publication, nor does it backdate history received later. Across sessions, record the merge policy and clock uncertainty; there is no universal cross-provider sequence. Collection receive order is not automatically the live trader's receive order.

The fast path is timestamp/sequence assignment and bounded enqueue of original application data. JSON interpretation, SQL, compression, Parquet, reports, feature calculation, and model fitting stay off it. The normalizer runs in a supervised separate process by default.

**Performance gates:** measure receipt-to-enqueue p50/p95/p99/max, event-loop lag, queue bytes, durable-write latency, normalization lag, and CPU/memory on the target machine. Add an initial local receipt-to-enqueue p99 target of 1 ms under the declared benchmark load; this is an engineering target to prove, not a provider latency claim. Retain the baseline's p99 receipt-to-fsync target of at most 2 seconds, normalization lag at most 120 seconds after startup under supported load, twice measured peak input for 30 minutes, and bounded memory across the soak. Measure contention with normalization enabled.

The 250 ms flush cadence and 60-second/size-based segment rotation intentionally mean that durable writes and published datasets are not millisecond services. Network arrival cadence is source-controlled. No warehouse read belongs in a millisecond trading decision loop; later strategies use incremental in-memory inputs and load versioned model artifacts outside that loop.

## 8. Research export contract

An export is a self-describing, immutable bundle containing:

1. Manifest: export ID, selected raw/build versions, schemas/parsers, profile/rule-policy versions, interval, ordering policy, checksums, and creation metadata.
2. Market/instrument identities and time-versioned metadata, including field provenance and fee/order-constraint evidence.
3. Ordered market observations and external/reference bindings over the required warm-up, window, and tail. Shared observations retain global feed IDs.
4. Book-epoch validity, source/depth scope, gap intervals, boundary candidates/attestations, and clock uncertainty.
5. Separate resolution/label history and its observation times, status, and audit horizon.
6. Coverage report and payload-processing status summary, including exclusions and unknown message types.

A dataset-selection manifest pins one or more market exports, an ordered market list, selection interval, and a versioned quality predicate. This composition belongs to the offline research consumer; it must not concatenate shared external rows into duplicate events. Deduplicate only by stable observation identity, never by equal payload values alone.

Require every experiment to publish the number of expected, discovered, exported, eligible, excluded, traded, and unresolved windows, with exclusion reasons. Distinguish a failed collection from an absent listing. Preserve bad/partial windows for engineering studies while excluding them from questions they cannot support.

The eventual calibration workflow uses chronological train/validation/test manifests, with overlap controls for shared warm-up/reference windows. It does not randomly split neighboring ticks. Late resolution labels may be used for retrospective scoring, but cannot enter the decision inputs or claim to have been available to earlier model training. Training/optimization code is outside this implementation scope.

## 9. B5 replay logic and limits

### Algorithm

```text
load pinned export set and scenario; validate checksums and required capabilities
initialize simulated clock, cash, orders, inventory, and pending actions
merge observations under the export's declared reception-order policy
for each next observation or scheduled action:
    advance simulated clock
    apply newly available observations, metadata, and validity changes
    process order arrivals, fills, and cancel arrivals under scenario rules
    build only the state observable by the decision time
    obtain scripted intents, or call a supported offline strategy adapter
    validate sizes, prices, constraints, cash, and outstanding reservations
    schedule hypothetical actions with explicit delays
    append decisions, state changes, costs, and assumption identifiers
score outcomes separately using qualified labels
write manifest, order/fill ledger, metrics, exclusions, and unresolved exposure
```

Define deterministic tie-breaking among observations, decisions, order arrivals, fills, and cancels. Include a conservative alternative for ambiguous ties; do not award a fill because two coarse timestamps happened to compare equal. Randomized policies require a pinned seed and version.

### Execution policies

- **Taker:** use the eligible reconstructed book at simulated arrival, walk prices and quantities, enforce limit/minimum/tick constraints, and apply order-lifetime semantics verified in B1. Partial liquidity produces a partial fill or the appropriate unfilled/rejected result. Missing valid depth is not an unlimited fill at the last best price.
- **Historical liquidity:** prevent multiple hypothetical orders from repeatedly consuming the same displayed size. Document replenishment and market-impact assumptions; cap participation and stress slippage because the historical market did not react to our orders.
- **Maker:** provide conservative and alternative fill scenarios using available trades/depth and assumed queue position. A touch or a disappearing level alone is not a guaranteed fill. If required public trade size/aggressor information is unavailable, report reduced fill-model support instead of synthesizing it. Simulated fills and profitability remain conditional on the selected model; scenario ranges are not mathematical profit bounds.
- **Cancellation:** a requested cancel takes effect after its modeled delay. Fills can occur before that time. Replacements do not retroactively remove exposure; enforce aggregate cash/inventory reservations.
- **Costs:** version fee inputs, rounding, spread/depth execution, and any supported position-operation costs. Default unverified rebates to zero. Separate documented parameters from stress assumptions. Unknown fees block an authoritative net-cost result rather than silently becoming zero.
- **Accounting:** buys debit cash; sells credit proceeds; fees follow their actual units; inventory cannot be sold twice. Track realized P&L, marked inventory, payout receivables, and available cash separately. Recognition of a payout does not make cash immediately reusable; configure and report its settlement/release policy. No venue transactions are submitted.
- **Resolution/gaps:** retain pending or conflicting outcomes. Do not treat unresolved holdings as a realized loss or profit. Crossing an unusable market-data interval invalidates the affected execution path under the default policy; report it rather than silently liquidating at a stale mark.

The initial ledger supports long Up/Down inventories, buys/sells, and qualified payout scoring. Pair merge/claim execution and richer quote-controller behavior need explicit simulator capability extensions alongside the relevant strategy lifecycle. They must not be claimed as supported merely because the dataset has two token books.

Latency scenarios distinguish feed availability delay, local decision delay, order-arrival delay, and cancel delay. Existing collector receipt times already include the collector's real feed delay: add only declared incremental delay unless an independently evidenced venue timeline is available. Do not subtract guessed latency to invent an earlier observable price.

Validate with hand-checkable cases: a delayed taker misses the old ask; limited depth prevents full execution; a touched maker does not necessarily fill; cancel/fill ordering changes exposure; a late reference never leaks backward; fees change mid-recording; a payout is credited once; settlement receivables do not fund another trade prematurely; gaps/contradictions leave results explicitly unqualified. Economic outputs use exact arithmetic where required.

## 10. Expected operator workflow

These examples specify the intended interface after implementation. They are not instructions to run unimplemented commands now. Paths are relative to the repository in these examples; commands also accept an explicit absolute root.

### Install and validate

```text
python -m pip install -e ".[warehouse]"
run_data_collection_live --profile btc-5m --validate-config
```

Set already provisioned credential environment variables named in the baseline: `TYREX_DATA_PM_API_KEY`, `TYREX_DATA_PM_API_SECRET`, and `TYREX_DATA_PM_API_PASSPHRASE`. Do not print their values or write them into the profile/export. Configuration validation is offline; a valid profile does not prove provider access.

### Start recording and inspect health

```text
run_data_collection_live --profile btc-5m --root var/warehouse --duration 45m
tyrex-data status --root var/warehouse --collection <collection-id>
```

The first command prints its collection ID and starts the normalizer unless disabled. Run status from another terminal. Forty-five minutes is an initial smoke duration: the default 15-minute warm-up plus alignment, gaps, and reconnects can reduce eligible windows. Acceptance counts actual complete windows, not elapsed minutes.

Expected status includes source connections/freshness, discovered and missing windows, durable/normalized watermarks, pending resolutions, queue size, gaps, unknown messages, disk space, and normalized-data lag. A healthy connection does not prove semantic readiness. A clean duration stop returns exit code 0 even if quality is incomplete; use the quality report to assess usability.

For ongoing collection, omit `--duration`. Graceful interruption drains/seals according to the baseline's bounded shutdown contract and leaves unfinished follow-up tasks for resume.

### Resume and normalize

```text
run_data_collection_live --resume <collection-id> --root var/warehouse
tyrex-data normalize --root var/warehouse --collection <collection-id> --follow
```

Resume uses the saved effective profile and creates a new session with visible discontinuity. Start a new collection for a different effective profile. The standalone normalization command is for a collection without an active normalizer, such as capture started with `--no-normalize`; concurrent ownership is rejected. Neither command backfills an unrecorded interval.

### Verify and export

```text
tyrex-data verify --root var/warehouse --collection <collection-id>
tyrex-data export-market --root var/warehouse --market <market-id> --output var/research/exports/<export-id>
```

Verification reports archive integrity and quality separately. Integrity failure returns nonzero; an explicitly requested quality predicate must also fail nonzero when unmet. Default verification success is not a universal certificate for every strategy. Export pins the chosen committed builds into its manifest and preserves incomplete/unsupported states. Existing immutable export destinations are rejected rather than overwritten.

Use `list_markets(...)` and `coverage(...)` from the baseline's query API to select markets, then pin the list of resulting export manifests into a research dataset manifest. These operations must not depend on a strategy process.

### Run a replay experiment

```text
tyrex-research replay --dataset var/research/datasets/btc5m-study.json --scenario config/research/replay_conservative.yaml --output var/research/experiments/study-001
```

This is a proposed B5 entry point and example configuration path. Its typed scenario must specify initial cash, supported intent/strategy adapter, required quality predicate, feed/action/cancel delays, execution-policy versions, fee policy, participation constraints, payout recognition/cash-release policy, seed, and ambiguity handling. Reject unknown keys, unsupported capabilities, and inconsistent units. The command performs no network calls.

Results include the dataset/scenario/code versions, decisions, simulated orders/fills, cash/inventory ledger, gross/net results, unresolved exposure, excluded windows, and sensitivity across explicitly selected scenarios. A $3 expected-value sizing target is reported separately from a $3 realized outcome.

## 11. How the two strategies use the foundation

### Settlement-value strategy

Consume verified payoff/threshold evidence, reference history available at each decision, external observations, both token books, and historical constraints/costs. First measure how many windows have adequate semantic/history coverage. Then test whether independently estimated probabilities differ from executable prices enough to survive realistic costs and delays.

Warehouse output supplies observations and labels. Shared probability/features, model artifacts, sizing, and actual hold/claim execution belong to the [settlement-value implementation plan](../strategies/settlement_value.md). A missing authoritative threshold or missing averaging history blocks the affected model backend; extra Binance ticks do not resolve it.

### Residual-band strategy

Use the same contract/reference foundation, plus book/trade histories and temporal quality, to study residual duration, spread/depth, markouts, and sensitivity to action delay. Once a common fair-value model exists, estimate residual behavior on training data and test it on later windows.

Fill-conditioned maker performance needs calibrated fill assumptions or actual execution evidence. Public replay alone cannot prove the proposed quoting policy earns its displayed spread. Feature/band fitting, quote control, and optional complete-set acquisition remain in the [residual-band implementation plan](../strategies/residual_band.md). A pair of displayed prices summing below $1 does not establish that both legs could be acquired.

For both strategies, publish opportunity frequency, eligible-window coverage, capital/depth feasibility, costs, and scenario dependence alongside performance. Do not hide skipped windows when assessing the original objective of earning money in each five-minute market.

## 12. Completion record and next implementation action

Record each package as `NOT_STARTED`, `IN_PROGRESS`, `IMPLEMENTED_UNQUALIFIED`, or `QUALIFIED`, with commit, commands, evidence paths, and outstanding limitations. All packages in this companion are currently planned; no collector, export, or replay capability is certified by writing this document.

The warehouse handoff must contain the provider-contract report, effective profile/schema dictionary, fault/recovery evidence, live/soak measurements, one pinned sample export, quality/exclusion reports, and an installation/recovery runbook under `Docs/latest/`. B5 adds a replay capability matrix, hand-checkable accounting fixtures, scenario definitions, and a reproducible example experiment.

The next implementation action is **Package 1: W0 provider/repository contract verification and W1 typed contracts/configuration**. Follow it with the durable writer before adding continuous connectors. This establishes a recording format that can accumulate useful history while the research readers and replay consumer are completed.
