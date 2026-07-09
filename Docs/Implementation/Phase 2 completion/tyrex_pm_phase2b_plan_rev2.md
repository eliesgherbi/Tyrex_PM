# Tyrex_PM — Phase 2B: Event-Driven Data Backbone and Replay Lab

**Status:** planning document, revision 2 · **Supersedes:** "Phase 2 Plan: Event-Driven Backbone, Recorder, Replay, Adaptive Survival" (rev 1)
**Scope of this revision:** document-only. No code implemented, no strategy behavior modified, no live trading run, no enforcement enabled, no adaptive advisor implementation started.

---

## 1. Executive summary

Phase 2B builds the **event-driven data and replay foundation** required before any adaptive edge work. The goal is to stop tuning survival parameters from live anecdotes and instead **record, replay, label, and statistically analyze** Polymarket BTC 5-minute markets — every day of operation compounding a research dataset instead of evaporating.

The production trading spine remains unchanged:

```text
Intent → RiskEngine → ExecutionPlanner → validate_planned_order → SingleWriterOMS → facts
```

Phase 2B's centerpiece is a canonical `MarketEvent` contract. Today market data arrives as *mutations* to `MarketStateStore`; "an event happened" exists only implicitly as a WS wake in `paired_binary_run`. Because the event stream is never reified, nothing is recordable, nothing is replayable, and every parameter decision has been calibrated on a handful of live runs. Phase 2B introduces the event contract behind compatibility flags, taps it with a recorder, normalizes recordings into research tables, and builds a replay engine that feeds the *same events* through the *same strategy/survival code*.

Boundary statements, explicit:

- **Phase 2B does not replace Phase 1 survival.** The mechanical survival layer keeps running unchanged throughout.
- **Phase 2B does not implement alpha.** No edge signals, no regime prediction, no profitability claims.
- **Phase 2B does not enforce adaptive advisors.** The `ParameterResolver` and any advisor built late in Phase 2B are advisory-only: they compute, log facts, and change nothing.
- **Phase 2B prepares Phase 3**, where adaptive/edge modules become live behavior — one module at a time, gated on replay and shadow validation.

---

## 2. Phase naming and boundary

The previous draft's "Phase 2" title collided with project history. Corrected mapping:

| Phase | Content | Status |
|---|---|---|
| **Phase 2 (historical)** | WS-primary live execution backbone (M8 cutover, M9 baseline) | **Complete** |
| **Phase 1** | Mechanical survival / damage control (floor, recovery, trailing, order policy, retries) | Implemented; enforce opt-in via scenarios |
| **Phase 2B (this document)** | Event recorder + replay + offline lab + feature foundation | Proposed |
| **Phase 3** | Adaptive edge / dynamic parameter **enforcement** | Future; designed here only as targets |

Anything in this document that changes live behavior is either (a) explicitly deferred to Phase 3, or (b) separated into the Phase 1 final-hardening section (§18), which proceeds on its own track and does not block or belong to the data backbone.

---

## 3. Current architecture assessment

(Condensed from rev 1; conclusions unchanged.)

| Module | Today | Phase 2B relevance |
|---|---|---|
| `runtime/pipeline.py` + intent path | Fail-closed spine, `process_intent_work_unit` | **Untouched.** All Phase 2B code sits upstream or offline |
| `runtime/paired_binary_run.py` (+recovery/shutdown) | Production hybrid loop: WS wake + timer tick | Bespoke per-strategy runtime loop, contradicting developer_guide §4.2. Generalization is desirable but **staged** (§16, 2B.0) — must not delay the recorder |
| `ingestion/market_stream` | Thin WS→`MarketStateStore` writer; event-driven but contract-less | Emits `MarketEvent`s behind a flag; store becomes a projection |
| `market_data/` | Quality gate contexts, executable book, decision snapshots, `features.py` stub | Reused verbatim; `features.py` is the FeatureBuilder home (2B.6) |
| `state/market_store.py` | Current book only; no history | Correct as-is. History is captured at the ingestion boundary by the recorder — **explicit non-goal:** no history buffers inside the store |
| `survival/` | Simplified flow live-validated; floor advisory; legacy modules parked | Consumed unchanged by replay; floor→enforce moved to §18; legacy quarantine is low-risk cleanup in 2B.0 |
| `risk/`, `execution/`, `state/` (wallet/order/allocation/fill_state/reconcile) | Fail-closed gates, SingleWriterOMS, finality, reconcile machinery | **Do not touch.** `liquidity_guard.py`/`slippage.py` stubs deprecated on paper now, folded into FeatureBuilder later |
| `reporting/` | facts.jsonl, schema_v2, dedup | Facts stay a *decision* log; market data goes to a separate event stream; facts gain `trigger_event_id` |
| Validators | Bespoke per-phase fact-grep scripts | Long-term subsumed by replay-diff (2B.5); kept as smoke checks meanwhile |
| External BTC data / recorder / replay | Do not exist | The substance of Phase 2B |

Findings carried forward unchanged: incomplete decision-latency chain (`monitor_trigger=None` symptom); config sprawl in survival legacy fields; no clocks/seq discipline at the market-data boundary; facts must not carry book data.

---

## 4. What is preserved from the existing live spine

Non-negotiables, unchanged and unweakened by this revision:

- `MarketState → DataQualityGate → DecisionSnapshot → strategy/survival → RiskEngine → ExecutionPlanner → validate_planned_order → SingleWriterOMS → facts` remains the only trading path.
- **Single OMS writer** and the existing duplicate-decision protections (`pending_survival_exit_intent` latch, `submit_fingerprint`).
- Fail-closed risk with stable reason codes; fill-finality classification; reconcile/adoption/tombstone machinery.
- Quality-gate contexts (`ENTRY` / `STOP` / `URGENT_EXIT`) and the survival quality-reject retry semantics.
- Facts remain decision logs, deduped, with the schema_v2 envelope; **book data never enters facts**.
- The advisory→enforce promotion pattern — reused later as the deployment discipline for advisors (Phase 3).
- `Decimal` / `core.time` / `core.ids` / reason-code conventions.

---

## 5. What Phase 2B adds

Seven additions, all upstream of or parallel to the trading path, none altering it:

1. **`MarketEvent` contract** — typed, timestamped, sequenced events at the ingestion boundary, behind flags.
2. **Recorder (EventTap)** — non-blocking persistence of the event stream; standalone `tyrex-pm record` mode covering all BTC 5m markets.
3. **External BTC feed** — read-only Binance data venue, recorded alongside.
4. **Normalizer + Parquet data lake** — analysis-ready tables with quality scorecards.
5. **Offline lab** — notebooks that end in decisions (honest stop/trail distances, lead-lag verdicts), not just plots.
6. **Replay engine** — recorded events through the actual monitor/survival/risk code, with counterfactual parameter sweeps and synthetic episode generation.
7. **FeatureBuilder + case labeler + advisory-only ParameterResolver** — late-stage, strictly non-enforcing; the hand-off surface for Phase 3.

---

## 6. Target event-driven data architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│ L1 INGESTION (live)                                                    │
│  market_discovery (poll, low cadence)  market_ws  user_ws  btc_ws     │
│            └──────────────┬─── emit MarketEvent ───┬──────────┘        │
├───────────────────────────▼────────────────────────▼───────────────────┤
│ L2 EVENT BACKBONE   per-market Sequencer (order, dedup, gap detect)    │
│        ├── projection → MarketStateStore  (existing consumers as-is)   │
│        ├── tap        → Recorder                                       │
│        └── dispatch   → existing paired_binary_run wake path (flagged) │
├────────────────────────────────────────────────────────────────────────┤
│ L3 RECORDER    events/*.jsonl.zst per market + manifests               │
│ L4 NORMALIZER + DATA LAKE   Parquet tables (offline batch)             │
│ L5 OFFLINE LAB + REPLAY ENGINE  (research/, never imported by live)    │
│ L6 FeatureBuilder + Labeler + advisory ParameterResolver (late 2B)     │
│ L7 RISK → PLANNER → SingleWriterOMS      (unchanged, not shown wired)  │
└────────────────────────────────────────────────────────────────────────┘
```

One event stream, four consumers — projection, recorder, live dispatch, replay — reading the identical contract. That identity is what makes offline research live-transferable by construction.

Live dispatch note: in Phase 2B the existing `paired_binary_run` wake path is **kept working**. Events feed the projection; the strategy keeps consuming the store exactly as today. Rewiring the strategy to consume events directly (via a generalized SessionRunner) happens only after projection equivalence is proven, and may slip to Phase 3 without harming any 2B deliverable.

---

## 7. MarketEvent contract

`core/events.py`, mirroring the facts discipline (frozen dataclass, `schema_version`):

```python
@dataclass(frozen=True)
class MarketEvent:
    event_id: str            # deterministic hash(source, venue_seq|payload, source_ts)
    event_type: EventType
    market_id: str | None
    token_id: TokenId | None
    venue_seq: int | None    # WS sequence when the venue provides one
    source_ts: datetime|None # venue timestamp
    recv_ts: datetime        # local receive (core.time.utc_now)
    payload: Mapping         # typed per event_type
    schema_version: int = 1
```

Initial event types:

```text
market_discovered, market_metadata_updated, market_phase_changed
book_snapshot, book_delta, trade_print
book_quality_changed          # stale / crossed / parity anomaly enter+exit
ws_seq_gap, ws_reconnect, rest_recovery_used
external_btc_tick
clock_sync                    # NTP offset sample
session_timer_tick, market_close_approaching, market_closed, resolution_observed
```

Decision *outcomes* stay in facts; facts gain `trigger_event_id`, `event_recv_ts`, `decision_ts` so every material decision is joinable to the exact event that caused it (closing the `monitor_trigger=None` gap).

**Ordering:** one `Sequencer` per market (paired YES/NO tokens share it — the only ordering the strategy needs). Orders by `venue_seq` when present, else `(source_ts, recv_ts)`; small reorder buffer (default 50–100 ms); emits `ws_seq_gap` on holes, unifying today's ad-hoc `reconnect_gap` handling. Cross-market ordering is not guaranteed and must not be assumed.

**Dedup:** by `event_id`, ring set sized to the reorder window — the guru watermark pattern generalized.

**Backpressure:** bounded per-market queue; on overflow, coalesce contiguous `book_delta`s into a `book_snapshot` + `book_quality_changed{coalesced}`; never drop trades, quality, or lifecycle events. The recorder writes on its own buffered task — **trading never waits on disk**; recorder overflow degrades recording (gap noted in manifest), never trading.

**Determinism (replayability requirement):** consumers of events must be pure functions of `(state, event)`; "now" is the event's timestamp; timer needs become recorded `session_timer_tick` events. This discipline is introduced progressively — enforced strictly in replay-facing code from day one, and adopted by the live monitor when it moves onto the event path.

---

## 8. Recorder design

**Two modes, one code path.**

*Record-only:* `tyrex-pm record --scenario record_btc5m.yaml` — market discovery + market WS for every active BTC 5m market + BTC feed + recorder. **No OMS, no RiskEngine, no wallet, no trading.** Safe to run unattended on a separate box; this is the data-lake workhorse (~288 markets/day).

*In-trading tap:* live trading runs record their own markets' events automatically, guaranteeing the research view equals what the strategy saw — the property replay-diff validation depends on. (Enabled after the record-only mode is proven.)

**Discovery** (`ingestion/market_discovery.py`): poll Gamma/CLOB every 20–30 s for upcoming BTC 5m markets; parse strike + event window at discovery; subscribe pre-open; hold through resolution + 60 s. Polling is appropriate here — discovery is inherently low-frequency; the books are event-driven.

**Recorded per market (both tokens):** all §7 event types verbatim, including raw book deltas (research needs levels 2–5), trade prints, quality events with **gap price displacement** (mid before vs after), lifecycle markers with BTC price attached, clock-sync samples. In trading mode, the run's `facts.jsonl` is cross-referenced by `run_id` in the manifest.

**Files:** `var/recordings/<date>/<market_id>/events-<seg>.jsonl.zst`, size/time rotation; per-market `manifest.json` (schema versions, coverage window, gap summary, git SHA, scenario, linked run_ids). **Heartbeat + coverage alerting ships with the recorder phase** — a silently dead recorder loses irreplaceable data.

---

## 9. External BTC feed

`venue/binance_data/` + `ingestion/external_btc.py`: read-only WS client for Binance perp (`bookTicker`, `aggTrade`, `depth5@100ms`, `forceOrder`, mark/funding) plus spot `bookTicker` for basis, normalized to `external_btc_tick` events on the same backbone and recorded identically. Follows the venue-addition recipe minus OMS/wallet — documented as the first "data-only venue". One venue first; multi-venue only if lead-lag research later demands it. No credentials, no order path, no wallet interaction — this package can never trade by construction.

---

## 10. Storage / Parquet schemas

Normalizer batch job (`research/normalize/`), raw JSONL → Parquet, partitioned `date/market_id`. Raw JSONL is the archive; every table is regenerable.

```text
markets:        market_id, condition_id, question, strike_price, event_start_ts,
                event_end_ts, yes_token_id, no_token_id, discovered_ts, tick_size,
                min_order_size, resolution_outcome, resolution_ts, btc_at_close,
                coverage_pct, schema_version

book_deltas:    market_id, token_id, venue_seq, source_ts, recv_ts, event_type,
                side, price, size                       # raw truth, replay source

book_snapshots: market_id, token_id, snap_ts (250ms grid), bid_px_1..5, bid_sz_1..5,
                ask_px_1..5, ask_sz_1..5, depth_{bid,ask}_{1,2,3,5}c, mid, spread,
                microprice, imbalance_top1, imbalance_5c, tte_s, staleness_ms

trades:         market_id, token_id, source_ts, recv_ts, price, size,
                aggressor_side, trade_id

ws_quality:     ts, market_id, event_type, duration_ms, gap_len,
                mid_before, mid_after, displacement

btc_ticks:      ts (100ms grid), perp_bid/ask/mid, spot_mid, basis, ret_{1,3,5,10,30}s,
                rv_{10,30,60}s, signed_vol_1s, ob_imbalance, liq_notional_1s, funding

strategy_events: run_id, market_id, fact_type, ts, trigger_event_id, event_recv_ts,
                decision_ts, submit_ts, ack_ts, fill_ts, state, intent, limit_px,
                fill_px, qty, quality_verdict, raw_fact (json)

resolutions:    market_id, outcome, resolution_ts, final_yes_mid, final_no_mid,
                pin_speed_s

survival_cases: episode_id, market_id, run_id|synthetic, entry/loser/survivor
                path stats, labels (built by the labeler, §14)
```

Per-market **quality scorecard** at normalize time: coverage %, gap count/duration, max staleness, crossed-book count, sustained YES+NO parity violations, timestamp drift vs `clock_sync`, duplicate rate, resolution mismatch — joined into `markets.coverage_pct` and used to filter research samples.

---

## 11. Offline lab

`research/notebooks/` importing a thin `research/lib/` (loaders, episode iterator, plot helpers). Each notebook must end with a **decision output**:

```text
01_coverage_quality        → which days/markets are research-grade
02_book_dynamics           → jump distribution of best bid over 100/250/500/1000ms
                             by tte × BTC-rv bucket. OUTPUT: minimum honest
                             trail/stop/floor distances per state; event-driven
                             vs fixed-cadence verdict
03_survivor_recovery       → P(touch breakeven | distance, tte, rv); time-to-peak.
                             OUTPUT: reachability table (Phase 3 arming input)
04_trailing_counterfactual → giveback + missed-upside per trail policy grid
05_liquidity_vacuum        → P(depth_2c −50% within 1s | features); FAK ladder calib
06_missed_continuation     → frequency + predictability; go/no-go for any future
                             runner module (Phase 3 gate)
07_pm_btc_leadlag          → PM mid vs digital model price cross-correlation.
                             OUTPUT: is BTC-led triggering worth building in Phase 3
08_feature_label_ic        → univariate IC of features vs labels
09_replay_sweeps           → parameter grids, walk-forward, CVaR reports
```

---

## 12. Replay engine

`research/replay/` — offline package; may import live modules; **live modules never import it** (import-isolation test, same pattern as `test_v2_import_isolation`).

Components: `EventLogReader` (segments → ordered events); `SimClock` (time = event ts; timer ticks replayed from recorded `session_timer_tick`s or synthesized for counterfactual cadences); reuse of the **actual** Sequencer → projection → `PairedBinaryMonitor` + `survival/` code; `SimRiskContext` (full RiskEngine in permissive-wallet mode so gate-interaction bugs surface); `FillModel` implementing `OMSBackend` — FAK fills at recorded resting depth at limit-or-better, evaluated at `decision_ts + latency` drawn from the measured live decision→ack distribution. GTC/managed-rest is **not modeled** in v1 (no queue-position model) — documented blind spot; managed-rest evaluation deferred.

**Keystone acceptance test:** replay the recorded events of an actual live run and diff the emitted fact sequence against the live `facts.jsonl` (fills excepted, fill *decisions* included). One test simultaneously proves replay correctness, backbone determinism, and eventually subsumes the bespoke fact-grep validators. Runs in CI on committed golden recordings (trimmed segments).

**Counterfactual surface (static Phase 1 parameters only in 2B):** stop levels and detection cadence, floor enforce on/off and buffer, trail_distance grid, arm policy variants, FAK ladder ticks, entry pair-cost threshold, flatten timing. **Synthetic episode generation:** run the entry rule over every recorded market to spawn episodes the live bot never traded — the sample-size multiplier (~50–150 episodes/day vs ~5 live).

**Outputs per sweep point:** PnL distribution, CVaR-5%, mean loss on losers, giveback decomposed (peak→floor / floor→trigger / trigger→fill), missed upside, slippage, FAK reject rate, recovery-capture %, vacuum-failure %, time in market, never-armed-and-lost count.

---

## 13. FeatureBuilder

Late-2B module: flesh out the existing `market_data/features.py` stub into an incremental, O(1)-per-event builder producing a `FeatureVector` — identical code consumed live (attached to decision snapshots, logged in facts for decisions that used it) and in replay (feature live/replay parity is a golden test).

Feature groups (computation targets; *no live decision consumes them in 2B*):

- **State vector:** `d_sigma = (btc − strike)/(σ√τ)`, `tte_s`, `rv_10s/30s`, digital model price, `dislocation = pm_mid − model_price`, rolling PM–BTC lag estimate.
- **PM microstructure:** spread, depth 1/2/3/5c, depth-evaporation rate, imbalance, microprice, update rate, trade intensity, seq-gap rate, synthetic pair cost / parity deviation, cross-leg lead.
- **Execution risk:** book age, spread/depth at decision, estimated FAK fill probability and slippage (from the calibrated surface), recent reject rate, latency percentile.
- **Strategy state:** pair cost, loser severity, floor/breakeven distances (cents *and* σ√τ units), phase duration, peak, giveback so far.

`execution/liquidity_guard.py` and `execution/slippage.py` stubs are absorbed here and deprecated.

---

## 14. Survival case labeler

`research/labeler/` — rule-based, from facts + future recorded path + resolution only (never subjective). Populates `survival_cases`.

| Case | Rule sketch | Informs |
|---|---|---|
| `clean_recovery_trailing_exit` | armed → triggered → filled, no retries | trail_distance, arm policy |
| `quality_gap_recovered` | `retry_scheduled` → `retry_attempted{quality_passed}` → filled | retry backoff, phantom-trigger re-check |
| `liquidity_vacuum_after_trigger` | trigger → FAK reject(s) or slippage > x, depth collapse within 1 s | execution policy, FAK ladder |
| `profit_lock_missed_continuation` | trailing exit ∧ resolved in survivor direction ∧ missed upside > x | Phase 3 runner go/no-go |
| `failed_before_recovery` | survivor MAE breaches floor before breakeven ever touched | floor enforce, ratchet |
| `strong_survivor_preclose_flatten` | no trailing exit, flatten near close, high terminal px | flatten timing |
| `execution_abandoned` | `survival_enforce_exit_abandoned` present | order-policy hardening |
| `never_armed_and_lost` | trailing never armed ∧ episode PnL < 0 | arm gate — the invisible loss bucket |

Target labels: `touch_breakeven{, time}`, survivor MFE/MAE, `resolved_in_favor`, `giveback(θ)`/`missed_upside(θ)` per grid point, `vacuum_1s`, `fak_fillable`, realized slippage, hindsight `best_exit_ts` (regret analysis only — never a training target).

---

## 15. ParameterResolver and advisors — future / advisory-only

**Design targets for Phase 3.** In Phase 2B, at most the following is built, and only after the data lake, replay, and labels exist (2B.8):

A pure `ParameterResolver`:

```text
EffectiveParams = clamp(static_config ⊕ advisor_overrides, config_bounds)
```

wired into the monitor **in pass-through mode**: with no advisors registered it returns static config verbatim, emitting an `advisor_decision` fact only when an advisor proposes something. Operator-set bounds (`{value, min, max}` per parameter) mean even a future buggy advisor can only ever emit a bounded parameter. Missing/stale inputs → static defaults + `advisor_fallback` fact.

The advisor modules — `DynamicStopPolicy`, `DynamicTrailingPolicy`, `ExecutionRiskPolicy`, `EntryQualityAdvisor`, `PreCloseFlattenPolicy`, `ContinuationRunnerPolicy` — are **specified, not implemented**, in Phase 2B. Their calibration tables are notebook outputs; their promotion ladder (replay-validated → shadow-advisory live → controlled enforce) is Phase 3. `ContinuationRunnerPolicy` additionally requires a pre-registered positive result from notebook 06 before any implementation begins — it is the highest-overfit-risk module and is last by design.

**Hard rule: no advisor changes live behavior until replay + shadow validation proves value, and enforcement of any advisor is Phase 3.0, not Phase 2B.**

---

## 16. Implementation phases

Sequential unless noted. Every phase lists what is explicitly **out of scope** to keep the wave small.

**Phase 2B.0 — Minimal MarketEvent contract**
Objective: reify market events behind flags with zero behavior change.
Touches: `ingestion/market_stream` (flagged emit), decision facts (+`trigger_event_id`, `event_recv_ts`, `decision_ts`), `survival/` legacy quarantine (move to `survival/legacy/`, deprecate config fields in CONFIG_MODEL).
New: `core/events.py`, `ingestion/sequencer.py`.
Tests: sequencer ordering/dedup/gap goldens; **projection equivalence** (event path vs legacy mutation path produce identical `MarketStateStore` states on fixture streams); paired-binary regression suite green with flag on and off.
Acceptance: one live run with the flag on is fact-identical to the legacy path; `monitor_trigger` never None.
Risks: touching the wake path → mitigated by the flag + legacy fallback retained for at least one release.
**Out of scope:** SessionRunner extraction (desirable, but staged behind the equivalence proof and must not delay the recorder — see note below), floor enforce (§18), any advisor code, BTC feed.

*SessionRunner staging note:* extraction of `runtime/session_runner.py` from `paired_binary_run` remains architecturally desirable (it also removes the developer-guide contradiction), but it is deferred until event-path equivalence is proven live, and may slip into Phase 3 without harming any 2B deliverable. The current `paired_binary_run` path keeps working throughout Phase 2B.

**Phase 2B.1 — Recorder MVP**
Objective: persist the event stream; record-only CLI.
New: `reporting/event_sink.py` (buffered writer task, segments, rotation, manifests), `runtime/record_run.py` + `cmd_record`, `ingestion/market_discovery.py`, heartbeat/coverage alerting.
Tests: tap-never-blocks (slow-disk fake); segment integrity + manifest completeness; discovery fixture test.
Acceptance: 24 h unattended record-only session covering ≥95 % of BTC 5m markets with complete manifests. (24 h is the **phase** acceptance criterion; the first milestone targets a single-market smoke test — §17.)
Risks: silent recorder death → heartbeat/alerting ships in this phase, not later.
**Out of scope:** in-trading tap (enabled after record-only is proven), BTC feed, normalization.

**Phase 2B.2 — External BTC feed**
Objective: BTC context recorded alongside PM events.
New: `venue/binance_data/`, `ingestion/external_btc.py`; `clock_sync` sampling.
Tests: normalizer fixtures; reconnect behavior; isolation test (package imports no OMS/wallet code).
Acceptance: 24 h joint PM+BTC recording; timestamp drift measurable from `clock_sync`.
**Out of scope:** multi-venue, any BTC-led trading logic.

**Phase 2B.3 — Normalizer + Parquet data lake**
Objective: analysis-ready tables + quality scorecards (§10).
New: `research/normalize/`, schema registry doc.
Tests: golden normalization of fixture recordings; scorecard flags a synthetically corrupted fixture.
Acceptance: a full recorded day normalized end-to-end; `coverage_pct` populated.
**Out of scope:** features, labels.

**Phase 2B.4 — Offline lab notebooks** *(parallel with 2B.5 once data flows)*
Objective: notebooks 01–02 (+07 first pass) with decision outputs.
New: `research/lib/`, notebooks.
Acceptance: written memo — honest stop/trail/floor distances per (tte, rv) bucket; cadence verdict; first PM–BTC lag estimate.
**Out of scope:** parameter changes to live scenarios based on these outputs (that's §18 / Phase 3 territory, done deliberately, not as a side effect).

**Phase 2B.5 — Replay engine**
Objective: §12 in full.
New: `research/replay/` (reader, SimClock, SimRiskContext, FillModel, sweep harness, synthetic episode generator); import-isolation test.
Tests/acceptance: **golden replay-diff of ≥2 recorded live runs green in CI**; ≥50 synthetic episodes/day generated from recordings; sweep harness produces the §12 output metrics.
**Out of scope:** managed-rest/queue modeling; any adaptive policy.

**Phase 2B.6 — FeatureBuilder**
Objective: §13; identical code live and replay.
Touches: `market_data/features.py`; recorder optional 250 ms feature dumps in record mode; deprecation of `liquidity_guard`/`slippage` stubs.
Tests: live/replay feature-parity golden; <1 ms/tick budget test.
Acceptance: full vector computed in both contexts, byte-identical on the same events.
**Out of scope:** any live decision consuming features.

**Phase 2B.7 — Survival case labeler**
Objective: §14; auto-label all historical live runs + synthetic episodes; notebooks 03–06.
New: `research/labeler/`, `survival_cases` table.
Acceptance: ≥500 labeled episodes; runner go/no-go memo from notebook 06.
**Out of scope:** acting on labels.

**Phase 2B.8 — ParameterResolver, advisory-only**
Objective: §15 pass-through resolver + at most one advisory table (DynamicStopPolicy candidate) computing and logging only.
New: `advisor/` package, resolver wiring, `advisor_decision`/`advisor_fallback` facts + validator rules, bounds schema in config.
Tests: clamp/bounds goldens; fallback-on-missing-feature; replay-diff still green with resolver active in pass-through.
Acceptance: **zero live behavior change**, verified by fact-diff of runs with resolver on vs off; replay sweep shows the advisory table ≥ static baseline on holdout CVaR-5% (evidence for Phase 3, not a trigger to enforce).
**Out of scope:** enforcement, all other advisors, runner.

**Phase 2B.9 — Shadow-live validation**
Objective: advisors advisory in live scenarios ≥1 week; nightly job diffs recommendations vs static outcomes on the day's recordings.
Acceptance: shadow report matches replay expectations within tolerance; no fallback storms; latency budget held.
**Out of scope:** enforcement.

**Phase 3.0 — Controlled adaptive enforcement** *(future phase, listed for boundary clarity only)*
One module at a time via experiment scenarios (the Phase 1 advisory→enforce pattern), starting with pure damage-control (`DynamicStopPolicy`), each with a scenario kill switch reverting to static instantly, each promotion requiring validator + replay-diff green on the promotion run itself. Not part of Phase 2B.

---

## 17. First two-week milestone (reduced)

**M2B.0-A — Event contract skeleton** (days 1–4)
`core/events.py`: envelope, event types, deterministic `event_id`, `source_ts`/`recv_ts`. `ingestion/sequencer.py`: ordering, dedup, gap detection. Golden tests for ordering/dedup/gap/coalescing on fixture streams. No wiring into live yet.

**M2B.0-B — Market stream emits MarketEvent behind a flag** (days 4–8)
`market_stream` emits events when flagged; projection populates `MarketStateStore`; **equivalence test**: event path and legacy mutation path produce identical store states on recorded fixture streams. Flag default off. No behavior change; `paired_binary_run` untouched.

**M2B.1-A — Event recorder MVP** (days 8–12)
`event_sink` with buffered non-blocking writer, JSONL.zst segments, rotation, per-market manifest. `cmd_record` CLI skeleton wiring discovery-lite (single configured market acceptable) + market WS + sink. No OMS, no RiskEngine, no wallet.

**M2B.1-B — Record-only smoke test** (days 12–14)
Record one full BTC 5m market lifecycle (pre-open → resolution + 60 s). Confirm: events written and readable back through `EventLogReader`-style iteration, manifest complete and accurate, zero trading-path imports (isolation check). Write the findings memo (event rates, segment sizes, any WS surprises) that sizes the full 2B.1 phase.

The 24-hour / ≥95 %-coverage recorder target **remains the acceptance criterion for Phase 2B.1 as a whole**, deliberately not part of this first milestone. SessionRunner extraction, floor enforce, BTC feed, and all cleanup beyond legacy quarantine are likewise out of this milestone.

---

## 18. Phase 1 final hardening items (kept separate)

These change live *survival* behavior and therefore belong to Phase 1's own hardening track — validated with the existing Phase 1 scenario/validator machinery, on their own schedule. They must not block the recorder/data backbone and are not Phase 2B implementation milestones:

1. **Hard floor → enforce.** Live runs proved the advisory floor is decorative (a trigger printed below the "never breached" floor). Flip `survivor_floor.enforcement_mode: enforce` in a dedicated experiment scenario, extend `validate_paired_binary_phase2_live_run.py` with floor-enforce rules, and validate over a handful of live runs — exactly the process trailing enforce already went through. Once the replay engine exists (2B.5), replay-validate the floor+trailing profile retroactively as additional evidence.
2. **Floor + trailing combined enforce profile.** After (1), a scenario with both enforced, validated as its own experiment.
3. **Decision-latency instrumentation on the loser-stop path.** The stop overshoot observed live is a detection-latency issue; the new fact fields from 2B.0 (`trigger_event_id`, `decision_ts`) make it measurable, but any cadence/parameter change to the stop path is a Phase 1 hardening decision, made explicitly — not a side effect of backbone work.

Rationale for separation: Phase 2B must remain a zero-live-behavior-change program. Mixing enforcement changes into it would blur exactly the phase boundary this revision establishes.

---

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Over-refactoring the live runtime too early** | Event path behind flags; projection-equivalence tests; legacy fallback retained; SessionRunner extraction staged after equivalence proof and allowed to slip to Phase 3 |
| **Building advisors before data exists** | Advisors are design-only until data lake + replay + labels exist (2B.8 earliest, one advisory table, pass-through resolver) |
| **Confusing Phase 2B with Phase 3 alpha** | §2 boundary table; every phase lists explicit out-of-scope items; Phase 3.0 listed only as a boundary marker |
| **Recorder blocks live trading** | Recorder buffer is non-blocking on its own writer task; trading never waits on disk; overflow degrades recording (manifest gap), never trading; tap-never-blocks golden test |
| **Research code leaks into live runtime** | Live modules never import `research/`; enforced by an import-isolation test (pattern: `test_v2_import_isolation`) |
| Silent recorder death losing irreplaceable data | Heartbeat + coverage alerting ships inside Phase 2B.1, not later |
| Sim-to-live drift | Golden replay-diff of real live runs in CI; re-run on fresh runs monthly; measured latency re-sampled |
| Small-n seduction | No parameter trusted from live anecdotes; ≥500 labeled episodes before any table is believed; walk-forward by day; frozen holdout |
| Venue microstructure drift | Rolling KS test on the jump distribution in the quality job |
| Config sprawl worsening | Bounds schema documented before any advisor exists; legacy survival fields deprecated with validator warnings in 2B.0 |

---

## 20. Documentation and organization

```text
docs/
  EVENT_MODEL.md            # event contract, ordering, dedup, replay guarantees
  DATA_LAKE.md              # recording layout, Parquet schemas, quality scorecard
  modules/{recorder,replay,research,advisor}/README.md   (as each lands)
Implementation/phase2b_data_backbone/    # milestone specs derived from this plan
```

Boundaries added to developer_guide §1: `research/` may import anything; **nothing imports `research/`**. Future `advisor/` may import `core` + `market_data` (read) + config only. The session-strategy vs signal-strategy contract distinction is documented when SessionRunner lands (resolving the current developer-guide contradiction explicitly rather than silently).

Config: `config/profiles/{live,record,research,experiment}/…`; scenario naming convention `<mode>_<strategy>_<experiment>_vNN.yaml`; CONFIG_MODEL gains the bounds schema and legacy-field deprecations.

Phase naming in all docs updated to the §2 table so "Phase 2" unambiguously means the completed WS backbone and "Phase 2B" means this program.

---

## 21. Final recommended next action

Start **M2B.0-A** (event contract skeleton + sequencer tests) immediately — it is pure additive code with no live wiring — and in parallel draft the `EVENT_MODEL.md` contract doc from §7 so the recorder and replay teams build against a written contract. Schedule the Phase 1 floor-enforce hardening run (§18.1) on its own track with the existing validator machinery; it needs no Phase 2B deliverable to proceed.

**End state of Phase 2B:** Tyrex_PM can (1) record complete Polymarket BTC 5m event streams, (2) record external BTC context, (3) normalize into research tables, (4) replay recorded markets through the same strategy/survival code, (5) label survival cases automatically, (6) run parameter counterfactuals offline, (7) produce advisory parameter recommendations, and (8) enter Phase 3 adaptive enforcement safely. Phase 2B itself produces no alpha and changes no live trading behavior — that is its definition of success.
