# Implementation plan: `settlement_value` from research to supervised live execution

> **History.** settlement_value now runs in observation, paper and hold-to-resolution trials; see `Docs/latest/running.md`. This plan records how it got there.

Date: 11 October 2026 (revision 4). Status: **S0 second pass, S1/M1, S2 tooling and experiments, S3 (proxy backend), S4 observation infrastructure and S5 minimum lifecycle implemented in code. No observation, recording or trading has been run. S6 trials are an operator step.** Nothing has been recorded or traded under this plan. Module paths are relative to `src/tyrex_pm/` unless they start with `Docs/`, `config/`, `tests/` or `var/`.

## 0. Implementation progress — 11 October 2026

**Delivered (offline-ready for one window, M1):**

- Virtual time: optional `Clock` in `indicators/graph.py` and `market_data/book_store.py`, plus `MarketSessionRuntime.create(freshness_clock=...)`; `FakeClock.advance_to_epoch_ns`. Live defaults are unchanged. One direct host-time read remains, the book-desync log throttle in `book_store.on_delta`, which affects logging only.
- `runtime/market_event_router.py`: the live `on_cl`/`on_bn` handlers and the replay share one reference ingest path. Indicators update only inside `ingest_binance`.
- `strategies/settlement_value/` plugin, registered:
  - endpoint backend; `average_diffusion` rejected by the schema;
  - conservative band (sigma ± band, S ± bps); greedy depth walk on the marginal conservative edge (`cap_only` / `require_target`); share-denominated fees;
  - one attempt per window; hold intent after a confirmed fill; stable reason codes;
  - model state recorded on every evaluation;
  - `validate_run_config` rejects every live run profile.
- Engine: `Engine.run_closed_loop` with the section 7 timeline and priorities, `schedule` / `schedule_order`, feedback events, `adverse_price_ticks`, and settlement shared with scripted `run()`. The scripted CLI rejects closed-loop scenarios.
- `research/strategy_replay/`: `WindowCatalog` (identity rows read once per collection), time-bounded `load_window`, `StrategyFeed`, the closed-loop agent, proxy labels, and results with config hash and decision digest.
- `research/strategy_lab/`: notebook 00 (S0 diagnostic) and notebook 01 (M1). Both execute from a clean kernel.
- Scenarios `config/research/scenarios/{favorable,friction}.yaml`.
- Tests: 27 new; full suite 596 passed.

**Deviations from the plan text, and why:**

1. `model.spot_input` (`binance_spot` default, or `aligned_reference`) and a generic `ReferenceFacts.trading_reference_raw`. The aligned estimate maps Binance into the lagged TWAP level through the basis, so right after the seal S equals K; the raw spot is needed to test the alternative. The live runtime fills the new field too.
2. Books are reconstructed for the strategy view by the warehouse `Books` (per-socket streams, gap and crossed-book invalidation) and published to `MarketStateStore` as full snapshots, bounded to 64 levels per side. Replaying raw deltas into the store would splice sockets. Validated `BookLevel` objects are cached; this halved replay time.
3. Intent translation lives in `strategy_replay/run.py`, not a separate `translate.py`. Context building is the agent's `_context`.
4. The replay seals K through the live PTB capture engine without attestation (mirroring the live no-SSR path). Its quality is therefore `PROVISIONAL`, accepted explicitly by research configs.
5. The plan's S0 "verify suspected defects" step found one real defect (recorded fee-schedule shapes), now fixed with a regression test. Payout units and the minimum-size unit stay unchanged and unqualified.

**Findings that shape S2:**

- The S0 diagnostic supports a TWAP-60 endpoint rule: 54 of 54 windows (see [s0_data_interpretation.md](s0_data_interpretation.md)).
- In the M1 window the endpoint model's P(Up) sat well below the book's (about 0.67 vs 0.88 at entry). The model therefore bought the cheap side and lost against the proxy outcome. One window proves nothing, but it points at the averaging/lag treatment and at the sigma floor as the first S2 questions.
- With `friction`, an adverse tick plus a limit equal to the worst walked level leaves FAK orders unfilled. A strategy-side limit buffer is an S2 candidate parameter.
- Replay speed is about 20 s per window plus 25 s to load. A prepared-window disk cache, or publishing books only before decisions, is S2 work.

Scope: **`settlement_value` only.** `residual_band` is out of this sequence. Its documentation ([residual_band.md](residual_band.md)) stays as it is for later work.

Companions: [settlement-value specification](settlement_value.md) (target production specification), [strategy reminder](strategy_reminder.md). This plan picks the smallest subset of that specification that leads, in order, to research results, live observation and supervised small-money execution. Where it defers part of the specification, it says so.

## 0b. Implementation progress — second batch (11 October 2026)

**Performance** (AMD Ryzen, 16 logical CPUs, 32 GiB, Python 3.12; collection `380e4487…`, one 5.5-minute window with 300 s warm-up = 108k rows):

| Step | Before | After | Change |
|---|---|---|---|
| Window load | 23.5 s (`Reader.iter_rows`) | 5.5 s cold (Arrow-filtered, identical events) / 1.1 s warm (prepared-window cache) | `strategy_replay/loader.py` `method="arrow"`, `cache.py` |
| Catalog | 342 × full metadata scan (~15 min) | 5.9 s cold / 0.8 s warm | `WindowCatalog`, `cached_catalog` |
| Replay | 18.7 s | 7.6 s (decision-identical) | Exact lazy book publication with a store-only clock |
| Cold cache build, 65 windows | — | 28 min with 4 workers (parquet I/O contention; 14 workers was slower) | one-off |

- **Cache:** zlib, about 11 MB per window. The key covers loader and cache versions, collection profile hash, overlapping builds and part checksums, and the market's late metadata and resolution rows.
- **Remaining replay hotspot:** `select_latest_binance_at_or_before`, live code, about 45% of replay time. Left unchanged: making it faster needs an ordering invariant the live history does not guarantee.

**Execution assumptions:**
- `entry.limit_buffer_ticks`: the limit sits above the worst walked level, bounded by `max_entry_price` and by non-negative marginal conservative value. Fills never exceed the limit (tested).
- Scenario overrides through `Lab.scenario(name, overrides)`.
- Recorded `tick_size_change` events are applied to the replayed store with live semantics.

**Model (S3):**
- `twap_proxy_diffusion` is implemented as an explicitly named **proxy**, with assumptions in `strategies/settlement_value/twap_proxy.py`. The official `average_diffusion` stays disabled.
- Both backends are computed and recorded on every evaluation.
- New generic indicator `spot_path`.
- `ReferenceFacts` gains `chainlink_source_ts`.
- The research sigma-floor seed is lowered to 1e-5, with evidence in S0.

**S4 — observation:**
- `runtime/observation.py` and `tyrex-pm observe` (requires `--connect-public-feeds`, refuses `--live`).
- Its import closure contains no execution modules (tested).
- Shared decision records (`runtime/decision_records.py`, schema `settlement_decision_record_v1`) are used by replay and observation.
- Promotion to the next window now goes through the market family instead of the hard-coded `prepare_next_btc_5m`.

**S5 — minimum supervised-live lifecycle:**

| Piece | Where |
|---|---|
| Durable event-sourced ledger: reserved, spent, receivable and realized buckets, each counted once | `execution/settlement/ledger.py` |
| Trial governor: realized losses + worst-case open loss + pending and uncertain reservations + the proposed order | `governor.py` |
| Verified live constraints: Gamma `feeSchedule` equal to the strategy curve, tick, minimum size | `constraints.py` |
| Finalized-block CTF payout reader and attributable `PayoutRedemption` receipt verifier | `chain.py` |
| Resolution manager and operator worklist | `resolution.py` |
| Runtime hooks: entry gate and reservation, fill outcome, hold commitment, flatten/manual/shutdown exemption for committed holdings only, hold-aware startup reconciliation (`COMMITTED_HOLD_AWAITING_RESOLUTION`) | `runtime/settlement_hooks.py` |
| Opt-in profile `lifecycle.mode: hold_to_resolution` with a required `settlement:` block | run config |
| Plugin lifecycle declarations | `StrategyPlugin.lifecycle_modes` |
| Trial profile and operator commands | `config/runs/settlement_value_trial.yaml`, `tyrex-pm settlement status/reconcile/worklist/confirm-redemption/mark-manual` |

Legacy profiles are unchanged; all existing tests pass.

**Deliberate limits of S5:**
- One entry episode per process, so trials are a supervised sequence of single-window runs. Startup reconciliation blocks a new run while an owned holding awaits resolution, so holdings are serialised.
- A `DISPATCH_UNKNOWN` episode left by a crash blocks entries until the operator resolves it with `mark-manual` after inspecting the venue.
- Redemption is operator-assisted only.

**S2 results (development windows only; holdout untouched).** See [settlement_value_candidate_ranges.md](settlement_value_candidate_ranges.md).

- **Probability quality:** the TWAP proxy model matches the book (pooled Brier 0.123 vs 0.123); the endpoint model is worse (0.184).
- **Selected-trade economics:** negative point estimates, with 95% intervals straddling zero (TWAP proxy −$11.50 on $71.61 debit, `favorable`).
- **Bias in earlier bundles:** two replay defects were found and fixed during validation. The batch runner's memory growth and a CPython 3.12 worker-recycling deadlock affected tooling only; the collector-clock skew (S0) affected results. The `vol/` and partial `grid/` bundles predate the clock fix and are biased.
- **Not done:** no shortlist and no holdout evaluation.

**Readiness gates.** In code: offline-ready; observation-ready (infrastructure). Still requiring operator evidence:

- an observation run;
- RTDS/PolyBolt identity;
- attested settlement semantics;
- exercising the fee-schedule read, payout reader and receipt verifier against real chain/provider data;
- the first supervised trial.

## 1. Journey and readiness levels

```text
hypothesis → grounded data/model → historical tests and notebooks → provisional parameter ranges
          → signal-only live observation → minimum live lifecycle → supervised small-money trials
```

There are three readiness levels. Each is a gate, and each stage below says which one it reaches.

| Level | Meaning | Allows |
|---|---|---|
| **Offline-ready** | The plugin runs through the closed-loop research runner on recorded windows, deterministically, with labelled execution assumptions and qualified or explicitly proxy outcomes | Notebooks, parameter-range studies |
| **Observation-ready** | The same plugin runs live with submission disabled, over successive windows, recording decisions comparable with offline output | Live signal studies; no orders |
| **Supervised-live-ready** | The minimum settlement lifecycle (S5) is implemented and tested, settlement semantics are established, and trial limits are configured | Small-money trials under operator supervision |

## 2. Strategy behavior (v1)

- Estimate the probability of the official settlement outcome with a supported backend (section 6).
- For Up and Down, compare the conservative expected payout with the executable all-in acquisition cost. The conservative probability is `p_low` for Up and `1 - p_high` for Down.
- Choose the deterministic best feasible side and size within the caps, or skip with a stable reason.
- One entry episode per window. A confirmed fill, or any order that may have been dispatched, consumes the episode.
- Hold confirmed inventory through supported resolution. There is no profit-price exit, and no top-up after a partial fill to recover the dollar target.
- The $3 figure is an expected-profit research target. It never forces position size in mechanics trials: trial profiles use `target_mode: cap_only` and report the resulting expected value (S6).

The offline runner and the live runtime call the same decision code (`driver.evaluate()` and its private policy modules). Notebooks configure experiments and visualize results. They never reimplement decision logic.

## 3. Invariants and relaxable assumptions

These are never relaxed, offline or live:

- **Contract meaning.** Only established semantics with qualified labels count as actual-contract settlement results. Everything else carries a proxy label.
- **Chronology.** A decision sees only information available by its decision time.
- **Accounting.** No spending of unavailable or reserved cash. No sale of shares not owned. A payout receivable is not cash.
- **Episode discipline.** No duplicate entries. Pending orders count immediately.
- **No fabricated evidence.** No invented attestation, provider policy or metadata, and no silent reinterpretation of unknown fields.

Relaxable, but each must be named in every result (section 8): fill quantity and price within the limit, delays, minimum-size assumptions while unverified, and fee scenario values.

## 4. Component inventory

### 4.1 Reused without behavioral change

| Component | Role |
|---|---|
| `strategies/registry.py`, `protocol.py`, `evaluation.py`, `decisions.py`, `context.py` | Plugin contract, evaluate signature, decisions, `DecisionContext` / `ReferenceFacts` |
| `strategies/z_gap/` | Package-layout template; its valuations are a reference, not a dependency |
| `runtime/market_runtime.py` `MarketSessionRuntime` (`create`, `ingest_binance`, `ingest_chainlink`, `attest_and_seal`, `prepare_aligned_eval`) | Snapshot assembly. It already takes a `core.clock.Clock` |
| `domain/polymarket/ptb_capture.py`, `ptb.py`, `boundary_candidates.py` | K capture, with K quality carried into decisions |
| `indicators/binary_fair_value.py`, `ewma_volatility.py`, `indicators/graph.py` | Endpoint probability and volatility (graph gets virtual time in S1) |
| `market_data/executable.py` `executable_vwap`, `market_data/book_view.py` `executable_buy` | Depth-walking acquisition cost |
| `domain/polymarket/fees.py` | Taker fee curve phi(p) = r(p(1-p))^e |
| `data_warehouse/query/reader.py` `Reader.iter_rows(..., start, until, entities)`; `research/tuto_btc5m/data.py` Arrow-filtered reads | Time-bounded per-window loading |
| `research/replay/engine.py` `Engine`: reservations, tick/minimum checks, taker depth walk, participation, fees, ledger, payouts; `research/replay/config.py` `Scenario` | Execution assumptions and accounting |
| `lab/evaluate_replay.py` | Deterministic decision fixtures |
| `domain/polymarket/resolution_evidence.py`, `resolution_capability.py`, `execution/settlement_cache.py`, `adapters/polymarket/ctf_payout.py`, `execution/startup_reconciliation.py` | Resolution evidence, payout reads, existing settlement-only classification |
| `execution/reducer.py` `SETTLEMENT_ONLY` disposition and `settlement_holding_shares` (around lines 190-210, 685-695) | Starting point for unresolved-holding accounting |

### 4.2 Modified

| Module | Change | Stage |
|---|---|---|
| `indicators/graph.py` (lines 137, 228) | Optional injected `Clock` for freshness. Default is the system clock | S1 |
| `market_data/book_store.py` (direct `time.monotonic_ns()` / `datetime.now()` reads) | Same injection | S1 |
| `runtime/market_data_runtime.py` (handler closures `on_cl`, `on_bn`, `on_book_snap`, `on_book_delta`, lines ~592-655) | Move the event-to-runtime routing into a reusable router so live and replay share one ingest path. Live behavior unchanged | S1 |
| `research/replay/engine.py` | Unified timeline with scheduled decisions, timers, order arrivals and execution feedback. `Engine.run(events, intents, labels)` keeps its scripted semantics | S1 |
| `strategies/registry.py` `ensure_strategies_registered()` | One registration line | S1 |
| `runtime/run_config.py`, `runtime/trading_runtime.py` | Observe mode (S4); resolution-compatible lifecycle profile (S5) | S4, S5 |
| `execution/reducer.py`, `session_state.py`, `coordinator.py`, `account_state.py`, `persistence/execution_journal.py`, `execution/startup_reconciliation.py` | Hold commitment, unresolved holdings, reserved capital, resolution/payout states | S5 |

### 4.3 Added

| Module | Purpose | Stage |
|---|---|---|
| `strategies/settlement_value/` (`config.py`, `schema.py`, `plugin.py`, `driver.py`, `assemble.py`, `economics.py`, `policies.py`, `state.py`, `reasons.py`) | The plugin. `economics.py` holds cost curve, conservative edge and size search, built on `executable_vwap` and `fees.py` | S1 |
| `runtime/market_event_router.py` | Shared event routing extracted from `market_data_runtime.py` | S1 |
| `research/strategy_replay/` (`loader.py`, `feed.py`, `run.py`, `context.py`, `translate.py`, `results.py`) | Closed-loop runner | S1 (minimal), S2 (cache, grids) |
| `research/strategy_lab/` (`plots.py`, `experiments.py`, notebooks) | Thin notebooks | S1, S2 |
| `config/research/scenarios/favorable.yaml`, `friction.yaml` | Named execution assumptions | S1 |
| `config/runs/settlement_value_observe.yaml`, later `settlement_value_trial.yaml` | Observe and trial profiles | S4, S5 |
| `execution/resolution_manager.py` | Minimal hold, resolution and redemption lifecycle | S5 |

Shared extraction stays limited to this list. Moving economics out of the plugin into a shared `domain/binary/` package, and migrating Z-Gap onto it, are deferred until a second strategy needs them.

## 5. What can proceed while semantics are unresolved

| Can proceed now | Must wait for established semantics (S0) |
|---|---|
| Plugin engineering, schema, fixtures | Describing any result as actual-contract settlement validation |
| Virtual time, router extraction, unified timeline | Enabling the `average_diffusion` backend |
| M1 notebook with proxy K and proxy outcome labels | Final candidate parameter ranges used to configure live trials |
| Multi-window runs and grids, labelled proxy | Enabling live order submission (S6) |
| Observe-mode engineering and live observation | `recorded` fee mode (until the key-shape question is verified) |
| S5 lifecycle engineering against fakes and failure injection | Treating the warehouse TWAP series as interchangeable with the live RTDS input |

## 6. Probability and reference inputs

- `SettlementModelSpec` lives in plugin config at first. It records the functional id, comparison, tie convention, reference series identity, units, timestamp meaning and provenance status (`VERIFIED` / `ASSUMED` / `PROXY`). Every result and decision record carries it.
- **`endpoint_diffusion` (implemented).** It wraps `binary_fair_value` on a declared reference series. Volatility is log-return sigma per square-root second from `ewma_vol` on Binance trades, with any scaling named in config. Under an averaged payoff this is a proxy: the reported TWAP is smoother than spot, so an endpoint diffusion on it misstates variance. Results are labelled accordingly.
- **`average_diffusion` (disabled).** The config schema rejects it until S0 shows the needed inputs exist: the averaging window, the weighting convention, the constituent history accumulated so far, and the remaining contribution. The latest TWAP report alone is not enough. A proxy constituent history (for example Binance spot over the averaging window) is allowed only as an explicitly named proxy backend, never as the official functional.
- **Conservative interval.** This is a configured parameter-uncertainty band (for example sigma scaled up and down), labelled as a method rather than a statistical guarantee.
- **K.** The capture engine's output with its quality (`PROVISIONAL` unless attested). The plugin config lists the accepted K qualities. Offline, K from boundary candidates is a proxy.
- **Reference series.** The warehouse records PolyBolt `price.crypto.twap` (`chainlink:btcusd:twap:60`). The live runtime consumes RTDS `crypto_prices_chainlink`. Until S0 shows they are the same series, every offline result names the series it used, and offline/live comparisons treat them as different inputs.

## 7. Historical timing model

The runner merges everything into one heap-ordered timeline on virtual time:

| Event kind | Time | Effect |
|---|---|---|
| Market observation | `received_at` | Updates the engine's market-truth book (the existing engine behavior) |
| Strategy observation | `received_at + information_delay` | The same row routed once through the shared router into `MarketSessionRuntime` and the indicator graph |
| Timer | Configured cadence on virtual time | Freshness checks and evaluations still run during quiet periods |
| Decision | When a declared input changes (with coalescing) or a timer fires | Calls `driver.evaluate()` with a context built from the runner's current account and pending view |
| Order arrival | `decision_time + order_delay` | Matched against the market-truth book as of that instant, even if no later update has arrived |
| Execution feedback | `arrival + feedback_delay` | Fill or reject becomes visible to the strategy context |

Rules:

- **Ties at the same nanosecond:** market observation, then order arrival, then execution feedback, then strategy observation, then timer, then decision. This keeps the existing engine's "observations before actions" rule.
- **Pending immediately.** An order counts as pending, with its cash reserved, at decision time, before it arrives. While anything is pending or unconfirmed, the context reports `entry_allowed = False`.
- **Two delays, kept separate.** `information_delay` (data availability to the strategy) and `order_delay` (submission to venue arrival) are distinct scenario parameters. A delayed strategy never fills at prices it could not have reached.
- **No double updates.** `MarketSessionRuntime.ingest_binance` already updates the indicator graph (`runtime/market_runtime.py:317`). The replay feed calls only the shared router, never the graph directly.
- **Virtual clock.** It advances before each event is applied, and is the clock passed to `MarketSessionRuntime`, `IndicatorGraph` and `MarketStateStore`.

## 8. Execution assumption register (taker v1)

Every result records the scenario id and all of these values.

| Parameter | `favorable` | `friction` |
|---|---|---|
| `information_delay` / `order_delay` / `feedback_delay` | 0 / 0 / 0 ms | initial 100 / 300 / 300 ms |
| Fill quantity | displayed depth up to the limit (participation 1.0) | participation 0.25 |
| Fill price | depth walk at displayed prices, never beyond `max_price` | depth walk plus configured adverse ticks, never beyond `max_price` |
| Order type | FAK BUY at `max_price`; remainder canceled | same |
| Tick and quantity lattice | from metadata; orders off-lattice rejected | same |
| Minimum size | assumed 5 shares, labelled as an assumption until S0 | same |
| Fees | scenario curve r=0.07, e=1, labelled scenario (not historical) | same |
| Invalid book at arrival | rejected | rejected |
| Settlement | qualified label if available, else `proxy_outcome` in a separate field | same |

Values marked "initial" are configurable, uncalibrated friction assumptions; sensitivity to them is explored in S2. The five-share minimum is research-only until verified for the market concerned. `favorable` explores model behavior under generous fills. It is not a profitability bound. If results disappear under `friction`, that is reported as a finding.

## 9. Stages

### S0 — Data interpretation audit (parallel; no gate on S1/S2 engineering)

Audit representative completed windows from `var/warehouse_btc5m/` and `var/warehouse_phase_b_validation/` for:

1. **Settlement rule and threshold provenance:** rules text and fingerprints in `metadata_versions`, boundary candidates, recorded page `openPrice` if present, and authoritative provider documentation.
2. **Reference compatibility:** PolyBolt TWAP vs RTDS identity, units, timestamp meaning and cadence, and whether constituent history is available.
3. **Payout units and outcome mapping:** recorded rows such as `[1000000, 0]`, read against provider documentation and the instruments' outcome indices.
4. **Fee metadata:** the CLOB `{"e","r","to"}` and Gamma `{"exponent","rate","rebateRate","takerOnly"}` shapes against `Engine.apply_metadata`.
5. **Order constraints:** minimum size is applied only when the unit is `"shares"`, and recorded rows carry `UNKNOWN`.
6. **Required-feed gaps:** `binance_trades` reported as `GAP` in every published report.

Deliverables:

- `Docs/implementation/strategies/s0_data_interpretation.md`, with one row per item: verified value with evidence reference, assumption, or open.
- Notebook `00_settlement_diagnostic.ipynb`, showing how often each candidate rule and K agrees with recorded winners. **Diagnostic only, not proof of the rule or the tie convention.**
- Fixes only for defects reproduced on sanitized real rows, each with a regression test. Raw evidence and readiness predicates stay unchanged. Missing attestation stays missing.

Acceptance: every item classified; no readiness weakened; each fix has a failing-then-passing test.

### S1 — First vertical slice → **Milestone M1** (offline-ready, one window)

Goal: one recorded window, run through the actual `settlement_value` driver under the `favorable` and `friction` scenarios, shown in a notebook with inputs, probabilities, decisions, simulated acquisition, and a qualified or explicitly proxy outcome.

Work:

1. **Virtual time** in `indicators/graph.py` and `market_data/book_store.py`. Tests: a recorded gap longer than `max_trade_age_ms` gives `stale_tape`; outputs do not depend on real elapsed time; existing live tests are unchanged.
2. **Router extraction** from `market_data_runtime.py` into `runtime/market_event_router.py`; the live runtime uses it. Tests: existing runtime tests pass; a test asserts each trade updates the graph exactly once.
3. **Plugin v1**, endpoint backend only:
   - Reason codes: `REFERENCE_UNVERIFIED`, `K_NOT_ACCEPTED`, `MODEL_NOT_READY`, `BACKEND_UNSUPPORTED`, `OUTSIDE_ENTRY_BAND`, `EDGE_BELOW_COST`, `TARGET_INFEASIBLE`, `DEPTH_INSUFFICIENT`, `EPISODE_CONSUMED`, `PENDING_ORDER`.
   - After a fill it emits HOLD with `HoldToResolutionIntent`.
   - `validate_run_config` rejects any order-enabled profile until S5 capabilities exist, with an explicit message.
   - Tests: schema rejects unknown keys and `average_diffusion`; fixture decisions for entry, each skip reason, stale reference, unaccepted K, partial fill without top-up, and episode consumed; the Down side uses `1 - p_high`; fee and lattice rounding; insufficient depth.
4. **Minimal runner:**
   - `loader.py` does an uncached, time-bounded read of one window plus warm-up, starting from the latest valid book seed.
   - `feed.py` turns recorded rows into the router's normalized events.
   - `translate.py`: TAKER `EnterIntent` becomes a FAK BUY at `max_price`; `HoldToResolutionIntent` becomes no order, with the position held to the label. Unsupported intents are rejected and counted.
   - `context.py` builds `DecisionContext` from the engine ledger and the pending set.
   - The engine timeline follows section 7.
   - **Design check to do first:** confirm that `MarketStateStore` can consume replayed book events through its normal attach path. If it cannot, write down the smallest adapter.
5. **Notebook `01_settlement_value.ipynb`, single-window view:**
   - reference and Binance prices, K with quality, time remaining;
   - model probability with interval against book-implied probability;
   - per-side edge, decisions with reasons, order, fill and inventory;
   - costs and outcome;
   - side-by-side `favorable` vs `friction`.

Runner tests:

- A decision never sees a row before `received_at + information_delay`.
- An order arriving between updates matches the book at its arrival.
- A timer fires during a quiet period, and staleness is detected.
- A pending order blocks re-entry.
- A partial fill does not top up.
- The fill price never exceeds `max_price`.
- A size off the lattice or below the minimum is rejected.
- Cash reservation prevents overspend.
- The scripted `Engine.run` regression is unchanged.

Acceptance: M1 notebook runs from a clean kernel; the decision digest (section 10) is identical across reruns.

Not required for M1: caching, grids, holdout, S0 completion, shared module extraction, observe mode.

### S2 — Multi-window research and provisional parameter ranges (offline-ready)

Work:

- **Prepared-window cache** in `var/research/strategy_replay/cache/`. The key covers: collection id; checksums of the normalized parts and session manifests the window reads; reader, reconstruction and quality-policy versions; loader version; and the effective profile hash. Any change rebuilds the entry. Strategy config is not part of the key, because the cache holds data before any strategy runs.
- **Grid runner** in `experiments.py`, rerunning only router, graph, driver and engine over cached windows. Small grids over:
  - entry-time band;
  - minimum conservative edge;
  - volatility settings (EWMA half-life, sampling, floor, uncertainty band);
  - exposure limits (max debit, participation).
- **Results store** in `var/research/strategy_runs/<run_id>/`, holding:
  - dataset provenance (collection, markets, cache keys);
  - plugin config hash and `SettlementModelSpec`;
  - scenario id;
  - label type;
  - code version.
- **Notebook `01_settlement_value.ipynb`, cross-window sections:**
  - probability quality of model vs book baseline: reliability curves, Brier score, log loss;
  - separately, results of the opportunities the strategy selected, after costs;
  - attempt-level vs fill-level results, skip counts by reason;
  - stability of results across neighboring parameter values.
- **Chronological split.** Earlier windows are for development; later windows are a holdout, evaluated once per shortlisted configuration. With the current data (about 200 windows over a few days) the holdout is indicative only. That limit is stated in the notebook.

Deliverable: a short candidate-ranges note (`Docs/implementation/strategies/settlement_value_candidate_ranges.md`). It states parameter ranges, sample sizes and label types, and does not claim optimal settings or profitability.

Acceptance:

- A cache invalidation test: a changed source checksum or version rebuilds the entry.
- A reproducibility test compares decision digests and economic outputs across reruns.
- A grid of the agreed size completes in a working session; the measured seconds per window are recorded.

### S3 — (reserved for S0 follow-through)

If S0 establishes the averaging functional and its inputs, add the `average_diffusion` backend with its tests (boundary of the averaging window, inside the window, zero remaining time, weighting) and rerun S2. Otherwise S2 results stay labelled endpoint/proxy. This stage does not block S4.

### S4 — Signal-only live observation (observation-ready)

Work:

- Run-config `execution.mode: observe`.
  - The coordinator, planner, signer and gateway are not constructed. Intents become decision records.
  - No credentials or account facts are required. The context reports a flat position.
  - Recommendations never create positions or fills.
- Successive windows in observe mode. Roll over through the market family instead of the hard-coded `discovery.prepare_next_btc_5m()` (`runtime/trading_runtime.py:852`), and do not stop on per-window terminal states.
- `trigger` reports the real cause (`feed` / `timer`). Today live always passes `"feed"` (`trading_runtime.py:1055`).
- Decision records use the same schema as offline results, with bounded batched writes. Each recommendation also records the executable book at decision time, so a hypothetical fill can be labelled as hypothetical.
- The observe profile documents the reference series it uses (RTDS) and whether S0 found it compatible with the offline series.

**What observation validates:**

- indicator warm-up and freshness on real feeds;
- reference and K alignment with offline inputs;
- signal frequency;
- local decision latency;
- quote staleness at decision time;
- similarity between offline and live inputs and decisions;
- hypothetical outcomes of recommendations.

**What it cannot validate (needs execution):**

- real fills and partial fills;
- venue latency and acknowledgement;
- fees actually charged;
- order rejection behavior;
- the hold commitment;
- resolution and payout evidence for our own position;
- redemption and cash crediting;
- restart with a live holding.

Tests: a fake gateway that would record calls asserts zero mutation calls; multi-window rollover; decision records load in the S2 notebook; the observe profile is rejected if it tries to enable submission.

Acceptance: a supervised multi-window observation run with the records compared offline. Running it is an operator action, decided separately.

### S5 — Minimum live settlement lifecycle (engineering towards supervised-live-ready)

This is a planned delivery stage. It is the minimum, not the full G-series of the companion specification.

1. **Resolution-compatible run profile.**
   - A declared lifecycle capability lets a committed position skip the mandatory pre-expiry flatten. Today `run_config.py:366` forces `mandatory_exit_before_end_s > manual_deadline_before_end_s`, and the runtime enqueues `MANDATORY_FLATTEN` (`trading_runtime.py:1565-1593`).
   - Shutdown `drain_open_session` (`execution/lifecycle.py:469-534`) must not sell committed holdings.
   - Profiles that combine hold with unconditional flatten fail validation with a precise message.
   - The entry-band constraint `tau_min >= mandatory_exit + evidence_timeout` is replaced, for this profile, by a hold-commitment timing constraint.
   - Existing profiles keep their current behavior.
2. **Durable hold commitment.**
   - The flow is `POSITION_OPEN → HOLD_REQUESTED → HOLD_COMMITTED`.
   - Before commitment: verify the capability, owned quantity, settlement-spec identity and capital reservation.
   - Cancel conflicting resting orders and resolve any cancel/fill race first. Never commit while an unknown sell could still fill.
   - Each transition is journaled before it takes effect.
3. **Unresolved holdings, reserved cash and spent capital.** These are three separate quantities, never netted against each other twice:
   - **Pending purchases reserve cash** in `execution/account_state.py` for their worst-case debit plus attributable fees, from the moment of decision until the order is proven terminal. An order whose dispatch is unknown keeps its reservation.
   - **Filled purchases have spent cash.** The fill converts the reservation into spent capital plus position exposure. The reservation is released by the amount actually spent, and the cash is not subtracted again.
   - **Unresolved exposure** (committed holdings awaiting resolution or redemption) is tracked in a cross-window ledger, extending the existing `SETTLEMENT_ONLY` / `settlement_holding_shares` accounting. It is reported by count, cost basis and worst-case loss. Caps on outstanding count and cost basis block new entries.
   - **Redemption confirmation needs evidence attributable to that holding:** the redemption transaction or claim identity for that condition and token, together with the credited amount. An unrelated or unexplained balance increase is not confirmation.
4. **Resolution and payout accounting.**
   - Flow: `AWAITING_RESOLUTION → PAYOUT_PROVEN`, with `RESOLUTION_UNCERTAIN` and `MANUAL_INTERVENTION` paths.
   - Uses `resolution_evidence.validate_resolution_evidence`, `ctf_payout` reads and `settlement_cache` proofs, with condition, token and outcome mapping verified.
   - Scheduled close, a displayed winner and a payout proof are distinct events. A zero payout completes after proof and reconciliation.
5. **Operator-assisted redemption (initial mode).**
   - `PAYOUT_PROVEN → REDEMPTION_PENDING_OPERATOR → CASH_CONFIRMED → COMPLETED`.
   - The runtime publishes a redemption worklist. The operator redeems.
   - The runtime confirms credited collateral from balance evidence before releasing reserved capital. Proceeds are unavailable until then.
   - Unattended redemption is deferred.
6. **Restart recovery and duplicate protection.**
   - On startup, journal-owned holdings and pending redemptions are rediscovered before any new purchase is authorized.
   - An uncertain dispatch is reconciled under its existing identity, never sent again.
   - The one-episode-per-window state persists across restarts.
7. **Continuation with outstanding holdings.** The runtime prepares and trades the next window while earlier holdings sit in resolution management, using an opt-in generic continuous-episode capability. Legacy profiles keep their terminal-stop behavior.
8. **Trial limits** (config, enforced by the framework before dispatch, not only after fills):
   - maximum debit per episode (the existing `maximum_total_debit ≤ 5` rule stays);
   - aggregate unresolved exposure;
   - maximum episodes per trial;
   - total trial loss budget. The remaining budget is checked immediately before durable dispatch authorization and counts: realized losses; worst-case loss on unresolved holdings; worst-case debit plus attributable fees of every order that could still fill (pending, dispatched, or dispatch-unknown); and the proposed order itself. Several individually acceptable orders therefore cannot together exceed the budget.
   - When any limit is reached, entries stop.
9. **Verified current trading constraints.** Scenario fees and the assumed five-share minimum are research-only. A live trial profile must use supported current fee parameters (from market metadata, with a parser that handles the observed schedule shapes), tick size and minimum order size for the market being traded. If any of them is unknown, unparsed or contradictory, entry is blocked with an explicit reason. Historical fee reconstruction stays deferred; live fee correctness does not.

Tests:

- Failure injection before and after every durable boundary (hold request, commit, resolution proof, redemption confirmation).
- A cancel/fill race during commitment.
- Restart with a committed holding, with a pending redemption, and with an uncertain dispatch.
- A contradictory profile is rejected.
- Each trial limit stops entries, including the case where several pending orders together plus a proposed order would exceed the loss budget.
- Fill accounting: reservation becomes spent capital exactly once; no double subtraction.
- Redemption is not confirmed by an unattributable balance increase.
- Unknown or unparsed live fee, tick or minimum-size information blocks entry.
- A trial profile with `require_target` is rejected.
- Zero-payout completion.
- Wrong outcome mapping is rejected.
- Legacy ask70 and Z-Gap regressions unchanged.
- No sale of a committed position by the mandatory-flatten or drain paths.

Acceptance: all tests pass against fakes; a documented operator runbook (start, monitor, redeem, confirm, stop, recover); the S5 profile validates.

### S6 — Supervised small-money trials (supervised-live-ready)

Preconditions:

- S0 has established the settlement semantics in use.
- Candidate ranges come from S2, with S3 where applicable.
- S4 observation shows offline/live input agreement.
- S5 is complete.
- Trial limits are agreed.
- The operator explicitly approves each trial.

Work:

- `config/runs/settlement_value_trial.yaml`, with `entry.target_mode: cap_only`: size is set by the trial budget and debit caps, and each decision reports the resulting expected value instead of demanding $3. A `require_target` trial profile is rejected, because under the tiny-live debit cap a $3 expected profit is usually infeasible, and the strategy would correctly skip every window while appearing broken. Minimum size, tick and fees come from verified current market constraints (S5 item 9).
- Measurement: decision-to-POST latency, acknowledgement, fill quantity against intended, fees actually charged, the hold commitment, resolution and payout timing, redemption and cash confirmation, and restart behavior.
- A post-trial comparison of the trials against offline scenario assumptions. The measured differences decide what improves next: execution assumptions, model, or infrastructure.

Acceptance: each trial ends with reconciled accounting (positions, receivables, cash) and a short evidence report.

## 10. Reproducibility

- **Decision digest:** the ordered tuple of (virtual decision time, action, reason codes, side, intended shares, `max_price`, model spec id, K quality), plus fills (arrival time, quantity, price, fee), plus economic outputs (debit, receivable, outcome, result). Reproducibility tests compare digests and economic outputs.
- Wall-clock run timestamps, durations, file paths and notebook metadata are excluded from the comparison.
- Cache entries and result bundles carry the provenance listed in S2. A changed input means a new cache key, never a silently reused entry.

## 11. Dependencies and genuine blockers

| Item | Blocks | Resolution |
|---|---|---|
| Settlement functional, K definition, tie convention | Actual-contract claims; live trials | S0 with authoritative evidence |
| Reference series identity (PolyBolt TWAP vs RTDS) | Offline/live comparability | S0 |
| Constituent history for averaging | `average_diffusion` | S0; else proxy or disabled |
| Payout units and outcome mapping | Qualified labels | S0 |
| `binance_trades` GAP | Trust in offline volatility inputs | S0 |
| Wall-clock freshness | Correct offline staleness | S1 |
| Engine open loop | Any strategy replay | S1 |
| Live single-window stop, hard-coded rollover | Multi-window observation | S4 |
| Mandatory flatten, log-only hold, drain-sells, no redemption path, cross-window holdings | Live orders | S5 |
| About 200 recorded windows | Meaningful holdout | Longer recording, decided separately |

Not blockers for this sequence: the ~1 s live evaluation cadence (measured in S4, improved only if evidence requires it), maker execution, queue modelling.

## 12. Deferred

`residual_band` (whole strategy); passive/maker entry for `settlement_value`; complete-set module; Hawkes or fitted-calibration models and the model-artifact registry; unattended redemption; share-count intent contract (v1 uses notional plus `max_price` and records intended shares); shared `domain/binary/` extraction and Z-Gap migration; millisecond scheduler and latency qualification from the companion specification; queue simulation; maker rebates; historical fee reconstruction beyond the scenario curve.

## 13. Summary

In plain terms:

- **S0** checks what the recorded data really means, especially the settlement rule, the reference series, payouts, fees and the Binance gap. It runs in parallel and fixes only proven defects.
- **S1** builds the thinnest end-to-end slice: the real plugin, correct historical timing, and one notebook on one window. That is milestone M1.
- **S2** scales it to many windows and small parameter grids, compares the model with the book, and produces candidate parameter ranges.
- **S3** adds the averaging model only if the data supports it.
- **S4** runs the same plugin live with no order submission, to check its inputs and signals on real feeds.
- **S5** builds the minimum real lifecycle for holding to settlement: hold commitment, unresolved holdings, payout proof, operator-assisted redemption, recovery, and trial limits.
- **S6** runs small, supervised money trials and compares them with the offline assumptions.

**Recommended first implementation batch** (all of S1, plus starting S0):

1. Virtual-clock injection in `indicators/graph.py` and `market_data/book_store.py`.
2. Router extraction into `runtime/market_event_router.py`.
3. The `settlement_value` plugin v1 with the endpoint backend.
4. The engine timeline extension and minimal `research/strategy_replay/`.
5. The two scenario files.
6. The single-window notebook (M1).

Start the S0 audit document at the same time, beginning with the reference-series and payout-unit questions, because they decide how M1's outcome is labelled.
