# Tyrex_PM — Phase 2B: Event-Driven Data Backbone and Replay Lab

**Status:** implementation-ready plan (final) · **Supersedes:** `tyrex_pm_phase2b_plan_rev2.md` for milestone execution  
**Scope:** document-only. No code implemented, no strategy behavior modified, no live trading run, no enforcement enabled, no adaptive advisor implementation started.

**Companion docs (created during implementation):**

- `Docs/EVENT_MODEL.md` — canonical event contract (draft in M2B.0-A)
- `Docs/DATA_LAKE.md` — Parquet schemas and quality scorecard (M2B.3)
- `Docs/Implementation/phase2b_data_backbone/milestones_index.md` — per-milestone status tracker

---

## 1. Executive summary

Phase 2B builds the **event-driven data and replay foundation** required before any adaptive edge work. The goal is to stop tuning survival parameters from live anecdotes and instead **record, replay, label, and statistically analyze** Polymarket BTC 5-minute markets — every day of operation compounding a research dataset instead of evaporating.

The production trading spine remains unchanged:

```text
MarketStateStore
  → DataQualityGate
  → DecisionSnapshot
  → strategy / survival
  → RiskEngine
  → ExecutionPlanner
  → validate_planned_order
  → SingleWriterOMS
  → facts
```

Phase 2B's centerpiece is a canonical `MarketEvent` contract. Today market data arrives as *mutations* to `MarketStateStore`; "an event happened" exists only implicitly as a WS wake in `paired_binary_run`. Because the event stream is never reified, nothing is recordable, nothing is replayable, and every parameter decision has been calibrated on a handful of live runs.

Phase 2B introduces:

1. A typed, sequenced `MarketEvent` contract behind compatibility flags
2. A non-blocking recorder and `tyrex-pm record` mode (no trading)
3. An external BTC data feed (read-only, data-only venue)
4. A Parquet data lake and offline lab
5. A replay engine with golden replay-diff validation
6. FeatureBuilder, survival case labeler, and advisory-only ParameterResolver (late 2B, non-enforcing)

**Boundary statements:**

- Phase 2B does **not** replace Phase 1 survival. Mechanical survival keeps running unchanged.
- Phase 2B does **not** implement alpha. No edge signals, regime prediction, or profitability claims.
- Phase 2B does **not** enforce adaptive advisors. `ParameterResolver` and advisors compute, log facts, and change nothing.
- Phase 2B prepares Phase 3, where adaptive modules become live behavior — one at a time, gated on replay and shadow validation.

**First implementation task:** M2B.0-A — event contract skeleton + sequencer tests (pure additive, no live wiring).

---

## 2. Phase boundary and scope

| Phase | Content | Status |
|---|---|---|
| **Phase 2 (historical)** | WS-primary live execution backbone (M8 cutover, M9 baseline) | **Complete** |
| **Phase 1** | Mechanical survival / damage control (floor, recovery, trailing, order policy, retries) | Implemented; enforce opt-in via scenarios |
| **Phase 2B (this document)** | Event recorder + replay + offline lab + feature foundation | **Implementation-ready** |
| **Phase 3** | Adaptive edge / dynamic parameter **enforcement** | Future; designed here only as targets |

### In scope (Phase 2B)

- `MarketEvent` contract, per-market `Sequencer`, store projection behind flags
- Non-blocking `EventSink` recorder; record-only CLI
- External BTC feed (data-only venue)
- Normalizer → Parquet data lake; offline lab notebooks
- Replay engine feeding actual strategy/survival code
- FeatureBuilder (live/replay parity); survival case labeler
- Advisory-only `ParameterResolver` (pass-through mode)
- Shadow-live validation of advisors

### Out of scope (Phase 2B)

- Any change to live trading decisions (entry, exit, survival enforce, risk gates)
- Alpha / edge signals / regime prediction
- Adaptive advisor **enforcement** (Phase 3.0)
- `SessionRunner` extraction from `paired_binary_run.py` (deferred; see §8)
- Survival legacy module quarantine (`survival/legacy/`) — separate cleanup, not blocking
- Phase 1 hard floor → enforce promotion (separate track; §18)
- Touching `RiskEngine`, `ExecutionPlanner`, `SingleWriterOMS`

---

## 3. Codebase feasibility verdict

**Verdict: feasible with scoped corrections.** The revised plan aligns with the live spine. The codebase already has WS-primary ingestion, quality gates, a feature stub, fact reporting, and import-isolation test patterns.

### Repo-grounded findings (preserve throughout implementation)

| Finding | Implication |
|---|---|
| `ingestion/market_ws_ingest.py` is the real hot WS path | Emit `MarketEvent` at recv boundary; do not only modify `market_stream.py` |
| `ingestion/market_stream.py` is mutation/projection helper logic | Add `project_event()` here; keep `apply_market_message` as legacy path |
| `MarketStateStore` caps books at `store_top_n_levels` (default 5) | Recorder must preserve **raw event payloads**; projection uses capped store as today |
| Polymarket uses `book_hash` lexicographic ordering, not integer `venue_seq` | Sequencer must preserve `_handle_sequence` semantics; do not assume numeric sequence |
| `core/events.py` exists with unused internal types (`MarketBookUpdated`, etc.) | Repurpose file (recommended) or use `core/market_events.py`; move dead types first |
| `RawMarketEvent` exists in `market_data/models.py` | Precursor envelope only; not the canonical contract |
| `JsonlSink` is synchronous with per-write `flush()` | Unsuitable for hot-path recording; recorder needs async buffered writer |
| `paired_binary_run.py` (~2300 lines) is the production loop | Do not touch in M2B.0-A, M2B.0-B, or M2B.1-A |
| No `research/`, no `zstandard`, no `pandas`/`pyarrow` today | Add only when milestone requires |
| `scripts/replay_survival_advisory.py` replays facts only | Full event replay is new work (M2B.5) |

### Risky files (touch only behind flags + equivalence tests)

| File | Risk |
|------|------|
| `ingestion/market_ws_ingest.py` | High — hot WS recv loop |
| `ingestion/market_stream.py` | High — single-writer book mutation |
| `state/market_store.py` | Medium — all consumers read through here |
| `runtime/paired_binary_run.py` | High — production loop (defer until M2B.0-C) |
| `runtime/app.py` | Medium — CLI + supervisor wiring (M2B.1-A for `record` only) |
| `strategies/paired_binary/facts.py` | Medium — large fact surface (M2B.0-C) |

---

## 4. Confirmed assumptions

| # | Assumption | Evidence |
|---|------------|----------|
| 1 | `MarketStateStore` can become a projection without breaking consumers | `apply_book` is the single mutation API; reads are pure (`capture`, `best_bid`, etc.) |
| 2 | Projection equivalence testable from fixture streams | `tests/fixtures/ws/market_book.json`, `market_price_change.json` |
| 3 | `paired_binary_run` unchanged in first recorder milestones | Recorder wires in `cmd_record` / optional tap only |
| 4 | `tyrex-pm record` addable without wallet/OMS | Pattern: separate code path in `app.py` like `live-attest` |
| 5 | `research/` may import live; live never imports `research/` | `test_v2_import_isolation.py` is the template |
| 6 | Recorder can be non-blocking | Async queue + dedicated writer task; never await disk in ingest |
| 7 | BTC feed as data-only venue | Follow `venue/polymarket/` adapter pattern minus OMS/wallet |
| 8 | Facts can gain optional correlation fields | Additive payload fields; schema_version unchanged |
| 9 | Existing WS/market tests provide regression gate | `test_market_stream_ingest`, `test_ws_primary_event_ordering`, etc. |

---

## 5. Rejected or corrected assumptions

| Original assumption | Correction |
|---|---|
| Emit from `market_stream` only | **Emit** at `market_ws_ingest` recv boundary; **project** via `market_stream` helpers |
| `venue_seq` is an integer from WS | Polymarket populates from `book_hash` (string, lexicographic). Field is generic `venue_cursor`; see §10 |
| `core/events.py` is empty | File has unused internal types — must relocate before adding `MarketEvent` |
| JSONL.zst available now | No `zstandard` in `pyproject.toml`; plain JSONL in M2B.1-A |
| Live fact-identical run required in M2B.0 | Deferred to staged acceptance; golden replay-diff in M2B.5 is canonical |
| `monitor_trigger=None` is the latency gap | Code always sets `monitor_trigger`; real gap is missing `trigger_event_id` linkage |
| Move `survival/legacy/` in M2B.0 | **Rejected for early milestones** — separate cleanup, not blocking recorder |
| `LatencyTracker.decision_ts` is wall-clock | It uses `monotonic_s()`; add separate `decision_wall_ts` in M2B.0-C |
| `JsonlSink` suitable for recorder | Rejected — sync flush blocks; use `reporting/event_sink.py` |

---

## 6. Production spine preserved

Non-negotiables, unchanged and unweakened:

```text
MarketStateStore
  → DataQualityGate (market_data/quality.py)
  → DecisionSnapshot (market_data/decision_snapshot.py)
  → PairedBinaryMonitor / survival.advisory
  → process_intent_work_unit (runtime/pipeline.py)
  → RiskEngine (risk/engine.py)
  → ExecutionPlanner (execution/planner.py)
  → validate_planned_order
  → SingleWriterOMS (execution/oms.py)
  → facts (reporting/facts.py → JsonlSink)
```

Also preserved:

- Single OMS writer; duplicate-decision protections (`pending_survival_exit_intent`, `submit_fingerprint`)
- Fail-closed risk with stable reason codes; fill-finality; reconcile machinery
- Quality-gate contexts (`ENTRY` / `STOP` / `URGENT_EXIT`); survival quality-reject retry semantics
- Facts remain decision logs; **book data never enters facts**
- Advisory→enforce promotion pattern (reused in Phase 3 deployment discipline)
- `Decimal` / `core.time` / `core.ids` / reason-code conventions

### Current wake path (unchanged in M2B.0-B)

```text
market_ws_ingest recv
  → apply_market_message (market_stream)
  → MarketStateStore.apply_book
  → set_on_token_update callback
  → MarketUpdateCoordinator.notify_token_update
  → paired_binary_run coordinator.wait_for_update
  → monitor_trigger = "ws_book_update" | "poll"
```

Phase 2B adds a **parallel** event path behind a flag. The strategy continues consuming `MarketStateStore` exactly as today until SessionRunner extraction (deferred).

---

## 7. Main architecture risks and mitigations

| Risk | Severity | Why it matters | Mitigation | Test |
|------|----------|----------------|------------|------|
| Event allocation in hot WS path | High | Latency on every `ws.recv()` | Frozen dataclasses; flag default off; no disk in ingest | p99 recv→apply unchanged flag off |
| Projection ≠ legacy mutation | High | Silent book drift breaks survival | Dual-path fixture test; legacy default | `test_market_event_projection_equivalence.py` |
| `book_hash` ordering semantics change | High | Breaks M8 reconnect_gap behavior | Sequencer delegates to `_handle_sequence` logic initially | Port `test_ws_primary_event_ordering.py` |
| Store level cap (5) vs research depth | Medium | Projection loses levels 6+ | Event `payload` stores raw WS message | Assert payload has full levels |
| Recorder backpressure stalls trading | High | Data loss or latency if miswired | Bounded queue; `put_nowait`; overflow → manifest gap | `test_event_sink_never_blocks.py` |
| `JsonlSink` sync flush | Medium | Blocks event loop if reused | Separate async `EventSink` | Slow-disk writer mock |
| Fact schema drift | Medium | Breaks validators | Optional fields only; absent when flag off | Validators tolerate missing fields |
| `research/` import leak | High | Offline code pulls live behavior | CI grep under `src/` | `test_research_import_isolation.py` |
| Record CLI pulls OMS/wallet | High | Unattended box safety | Dedicated `record_run.py`; forbidden-import test | `test_record_mode_import_isolation.py` |
| Replay drift from live | High | False parameter confidence | Golden replay-diff in CI (M2B.5) | `test_replay_fact_diff_golden.py` |
| SessionRunner premature extraction | Medium | 2300-line refactor risk | Defer until prerequisites met (§8) | N/A until M2B.5 |

---

## 8. Revised milestone map

```text
M2B.0-A  Event contract skeleton + Sequencer tests
M2B.0-B  WS ingest emits MarketEvent behind flag
M2B.0-C  Fact telemetry fields + shadow parity
M2B.1-A  Recorder MVP — plain JSONL, one configured market
M2B.1-B  Full recorder phase gate — JSONL.zst, heartbeat, 24h coverage
M2B.2    External BTC feed
M2B.3    Normalizer + Parquet data lake
M2B.4    Offline lab notebooks
M2B.5    Replay engine + golden replay-diff
M2B.6    FeatureBuilder
M2B.7    Survival case labeler
M2B.8    ParameterResolver advisory-only
M2B.9    Shadow-live validation
Phase 3.0 Controlled adaptive enforcement (future only)
```

### Safer acceptance sequence

| Milestone | Acceptance gate |
|-----------|-----------------|
| **M2B.0-A** | Sequencer + serialization tests green |
| **M2B.0-B** | Projection equivalence on fixtures green; existing WS/market tests green flag off **and** on |
| **M2B.0-C** | Optional event-correlation fields in shadow mode; fact sequence unchanged except new optional fields |
| **M2B.1-A** | Record-only smoke for one BTC 5m market; manifest complete; no trading imports |
| **M2B.1-B** | 24h record-only run with target coverage; heartbeat and coverage report |
| **M2B.5** | Golden replay-diff becomes canonical validation (subsumes live fact-identical requirement) |

**Do not** require a live fact-identical run in M2B.0.

### SessionRunner extraction (deferred)

`runtime/session_runner.py` extraction from `paired_binary_run.py` is architecturally desirable but **not part of early Phase 2B**.

**Prerequisites before SessionRunner extraction:**

1. M2B.0-B projection equivalence proven on fixtures
2. M2B.0-C `trigger_event_id` fields working on material decisions
3. M2B.5 replay-diff green on at least one real recorded run
4. `developer_guide.md` updated with session-strategy contract

Until then, `paired_binary_run.py` remains the production loop. SessionRunner may slip to Phase 3 without harming 2B deliverables.

### Survival legacy quarantine (deferred)

Moving `reachability.py`, `stall_exit.py`, `target_policy.py` to `survival/legacy/` is a **separate cleanup task, not blocking Phase 2B recorder**. Do not include in M2B.0-A or M2B.0-B. Reason: first milestones must be additive and low risk.

---

## 9. Detailed milestone specifications

### M2B.0-A — Event contract skeleton + Sequencer tests

| Field | Detail |
|-------|--------|
| **Objective** | Canonical `MarketEvent` + per-market `Sequencer` with ordering, dedup, gap detection on fixtures |
| **Current reality** | `RawMarketEvent` in `market_data/models.py`; unused types in `core/events.py`; hash dedup in `market_ws_ingest._handle_sequence` |
| **Files to create** | `src/tyrex_pm/core/runtime_events.py`, `src/tyrex_pm/ingestion/sequencer.py`, `tests/test_market_event_sequencer.py`, `tests/test_market_event_serialization.py`, `tests/fixtures/events/ws_book_sequence.jsonl`, `tests/fixtures/events/ws_gap_sequence.jsonl`, `Docs/EVENT_MODEL.md` (draft) |
| **Files to modify** | `src/tyrex_pm/core/events.py` (repurpose for `MarketEvent`) |
| **Config changes** | None |
| **Fact/schema changes** | None |
| **Tests** | Sequencer ordering, dedup, gap, coalescing stub; event_id determinism; JSON round-trip |
| **Acceptance** | All M2B.0-A tests green; no live imports wired |
| **Out of scope** | Live wiring, `event_factory` (unless test-only), facts, recorder, `paired_binary_run`, survival legacy move |
| **Rollback / flag** | N/A — no runtime wiring |
| **Dependencies** | None |
| **Risk** | **Low** |

---

### M2B.0-B — WS ingest emits MarketEvent behind flag

| Field | Detail |
|-------|--------|
| **Objective** | Flagged dual path: event projection and legacy mutation produce identical `MarketStateStore` |
| **Current reality** | `market_ws_ingest.run_market_ws_ingest` calls `apply_market_message` directly; `on_raw_event` hook exists but unused in `app.py` |
| **Files to create** | `src/tyrex_pm/ingestion/event_factory.py`, `tests/test_market_event_projection_equivalence.py` |
| **Files to modify** | `ingestion/market_ws_ingest.py`, `ingestion/market_stream.py`, `venue/polymarket/book_snapshot.py` (REST recovery events), `runtime/config.py` |
| **Config changes** | `runtime.market_data.event_backbone.enabled: false` (default); env `TYREX_EVENT_BACKBONE` |
| **Fact/schema changes** | None |
| **Tests** | Projection equivalence; full pytest flag off/on; `test_market_ws_ingest`, `test_ws_primary_*` |
| **Acceptance** | Fixture equivalence 100%; pytest green both flag states; **no changes to `paired_binary_run.py`** |
| **Out of scope** | Fact fields, recorder, SessionRunner, survival legacy move |
| **Rollback / flag** | `event_backbone.enabled: false` restores exact legacy path |
| **Dependencies** | M2B.0-A |
| **Risk** | **Medium–High** |

---

### M2B.0-C — Fact telemetry fields + shadow parity

| Field | Detail |
|-------|--------|
| **Objective** | Join material decisions to causing events via optional fact fields |
| **Current reality** | `monitor_trigger` is `"poll"` \| `"ws_book_update"`; `LatencyTracker.decision_ts` is monotonic float; no `trigger_event_id` |
| **Files to create** | `tests/test_event_correlation_facts.py` |
| **Files to modify** | `strategies/paired_binary/observability.py`, `latency.py`, `facts.py`, `survival/facts.py`, `runtime/paired_binary_run.py` (pass last wake event id only), `reporting/schema_v2.py` (comments), `Docs/reporting_fact_model.md` |
| **Config changes** | `runtime.observability.emit_event_correlation: false` (default) |
| **Fact/schema changes** | Optional: `trigger_event_id`, `event_recv_ts`, `decision_wall_ts` on `latency_chain`, `decision_snapshot`, material survival facts |
| **Tests** | Shadow run fact diff (optional fields only); existing validators pass |
| **Acceptance** | Flag off: byte-identical fact payloads (excluding timestamps). Flag on: material decisions have non-null `trigger_event_id` in shadow |
| **Out of scope** | Behavior change, floor enforce, SessionRunner |
| **Rollback / flag** | `emit_event_correlation: false` |
| **Dependencies** | M2B.0-B |
| **Risk** | **Medium** |

---

### M2B.1-A — Recorder MVP (plain JSONL, one market)

| Field | Detail |
|-------|--------|
| **Objective** | Non-blocking event persistence; `tyrex-pm record` for one configured market lifecycle |
| **Current reality** | No recorder; `JsonlSink` sync; no `record` CLI |
| **Files to create** | `reporting/event_sink.py`, `runtime/record_run.py`, `config/scenarios/record_btc5m_single.yaml`, `config/profiles/record/` (optional), `tests/test_event_sink_never_blocks.py`, `tests/test_event_sink_segments.py`, `tests/test_event_sink_manifest.py`, `tests/test_record_mode_import_isolation.py` |
| **Files to modify** | `runtime/app.py` (`cmd_record` subparser only) |
| **Config changes** | `runtime.recording.enabled`, `output_dir`, `segment_max_mb`, `segment_max_s` |
| **Fact/schema changes** | None |
| **Tests** | Non-blocking writer; segment rotation; manifest; import isolation |
| **Acceptance** | One full BTC 5m market recorded; manifest complete; readable JSONL; no forbidden imports |
| **Out of scope** | zstd, multi-market discovery, in-trading tap, `paired_binary_run` |
| **Rollback / flag** | `recording.enabled: false`; do not register `record` tap in live `cmd_run` |
| **Dependencies** | M2B.0-B |
| **Risk** | **Medium** |

---

### M2B.1-B — Full recorder phase gate

| Field | Detail |
|-------|--------|
| **Objective** | 24h unattended record-only; ≥95% BTC 5m market coverage; heartbeat alerting |
| **Files to create** | `ingestion/market_discovery.py` |
| **Files to modify** | `reporting/event_sink.py` (zstd), `pyproject.toml` optional `[record]` extra, `Docs/OPERATIONS.md` |
| **Config changes** | Discovery poll interval; heartbeat thresholds |
| **Dependencies** | M2B.1-A; adds `zstandard>=0.22` optional dep |
| **Acceptance** | 24h run; manifests complete; coverage report |
| **Risk** | **Medium** |

---

### M2B.2 — External BTC feed

| Field | Detail |
|-------|--------|
| **Objective** | Read-only Binance WS normalized to `external_btc_tick` events; recorded alongside PM |
| **Files to create** | `venue/binance_data/`, `ingestion/external_btc.py` |
| **Files to modify** | `record_run.py`, sequencer (cross-source ordering by `recv_ts`) |
| **Config** | `runtime.external_btc.enabled`, stream list |
| **Tests** | `tests/test_binance_data_isolation.py`, reconnect fixtures |
| **Acceptance** | 24h joint PM+BTC recording; `clock_sync` drift measurable |
| **Out of scope** | Multi-venue; BTC-led trading |
| **Risk** | **Medium** |

---

### M2B.3 — Normalizer + Parquet data lake

| Field | Detail |
|-------|--------|
| **Objective** | Raw JSONL → Parquet tables + per-market quality scorecards |
| **Files to create** | `research/normalize/`, `Docs/DATA_LAKE.md` |
| **Dependencies** | `pandas`, `pyarrow` as optional `[research]` extra |
| **Tests** | `tests/test_normalize_golden.py` |
| **Acceptance** | Full recorded day normalized; `coverage_pct` populated |
| **Risk** | **Low–Medium** |

---

### M2B.4 — Offline lab notebooks

| Field | Detail |
|-------|--------|
| **Objective** | Notebooks 01–02 (+07 first pass) with written decision outputs |
| **Files to create** | `research/lib/`, `research/notebooks/` |
| **Acceptance** | Memo: honest stop/trail/floor distances; cadence verdict; PM–BTC lag estimate |
| **Out of scope** | Live parameter changes from notebook outputs |
| **Risk** | **Low** (depends on M2B.3) |

---

### M2B.5 — Replay engine + golden replay-diff

| Field | Detail |
|-------|--------|
| **Objective** | Recorded events through actual monitor/survival code; fact-diff vs live `facts.jsonl` |
| **Files to create** | `research/replay/` (`EventLogReader`, `SimClock`, `SimRiskContext`, `FillModel`, `ReplayRunner`, sweep harness) |
| **Tests** | `tests/test_replay_fact_diff_golden.py` (CI golden recordings) |
| **Acceptance** | ≥2 recorded live runs replay-diff green (fills excepted; fill decisions included) |
| **Out of scope** | Managed-rest queue modeling; adaptive policy |
| **Risk** | **High** |

---

### M2B.6 — FeatureBuilder

| Field | Detail |
|-------|--------|
| **Objective** | Incremental per-event `FeatureVector`; identical code live and replay |
| **Files to modify** | `market_data/features.py`; deprecate `execution/liquidity_guard.py`, `execution/slippage.py` |
| **Tests** | `tests/test_feature_live_replay_parity.py`; <1ms/tick budget |
| **Acceptance** | Byte-identical feature vector on same events in live vs replay |
| **Out of scope** | Live decisions consuming features |
| **Risk** | **Medium** |

---

### M2B.7 — Survival case labeler

| Field | Detail |
|-------|--------|
| **Objective** | Rule-based labels from facts + recorded path + resolution → `survival_cases` table |
| **Files to create** | `research/labeler/` |
| **Acceptance** | ≥500 labeled episodes; notebook 06 go/no-go memo |
| **Risk** | **Low–Medium** |

---

### M2B.8 — ParameterResolver advisory-only

| Field | Detail |
|-------|--------|
| **Objective** | Pass-through `ParameterResolver`; at most one advisory table computing and logging only |
| **Files to create** | `advisor/resolver.py`, bounds schema in config |
| **Tests** | `tests/test_parameter_resolver_passthrough.py`, bounds/fallback goldens |
| **Acceptance** | Zero live behavior change; fact-diff resolver on vs off |
| **Out of scope** | Enforcement; other advisors |
| **Risk** | **Medium** |

---

### M2B.9 — Shadow-live validation

| Field | Detail |
|-------|--------|
| **Objective** | Advisors advisory in live ≥1 week; nightly diff vs static outcomes |
| **Acceptance** | Shadow report matches replay within tolerance; no fallback storms |
| **Risk** | **Low** |

---

### Phase 3.0 — Controlled adaptive enforcement (future only)

One module at a time via experiment scenarios (Phase 1 advisory→enforce pattern). Not part of Phase 2B.

---

## 10. M2B.0-A implementation spec

### File path decision: `core/events.py` (repurpose)

**Decision:** Repurpose `src/tyrex_pm/core/events.py` for the Phase 2B `MarketEvent` contract.

**Rationale:** Grep shows zero imports of `core/events.py` in production code. The existing types (`MarketBookUpdated`, `UserOrderUpdated`, `EventPayload`, etc.) are dead code from an earlier internal-event sketch.

**Migration:**

1. Move existing types to `src/tyrex_pm/core/runtime_events.py` (preserve for potential future `EventBus` use)
2. Implement `MarketEvent`, `EventType`, `compute_event_id` in `src/tyrex_pm/core/events.py`
3. No import changes required elsewhere (nothing imported the old file)

**Alternative (if team prefers):** `core/market_events.py` — valid but duplicates plan naming; only use if repurpose is rejected.

### EventType enum

```python
class EventType(str, Enum):
    # Book / WS
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE_PRINT = "trade_print"
    BOOK_QUALITY_CHANGED = "book_quality_changed"
    WS_SEQ_GAP = "ws_seq_gap"
    WS_RECONNECT = "ws_reconnect"
    REST_RECOVERY_USED = "rest_recovery_used"
    OUT_OF_ORDER_EVENT = "out_of_order_event"

    # Lifecycle (stub — emitters added in later milestones)
    MARKET_DISCOVERED = "market_discovered"
    MARKET_METADATA_UPDATED = "market_metadata_updated"
    MARKET_PHASE_CHANGED = "market_phase_changed"
    SESSION_TIMER_TICK = "session_timer_tick"
    MARKET_CLOSE_APPROACHING = "market_close_approaching"
    MARKET_CLOSED = "market_closed"
    RESOLUTION_OBSERVED = "resolution_observed"

    # External / clock (stub in 0-A)
    EXTERNAL_BTC_TICK = "external_btc_tick"
    CLOCK_SYNC = "clock_sync"
```

Include `EXTERNAL_BTC_TICK` as enum member in M2B.0-A (no emitter until M2B.2).

### MarketEvent dataclass fields

Use **frozen dataclass** (match `market_data/models.py`, `reporting/facts.py` discipline — not Pydantic).

```python
@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    event_type: EventType
    market_id: str | None
    token_id: TokenId | None
    venue_cursor: str | None      # generic; see book_hash note below
    source_ts: datetime | None    # venue timestamp (WS ms → UTC)
    recv_ts: datetime             # local receive (core.time.utc_now)
    payload: Mapping[str, Any]    # typed per event_type; raw WS preserved for book events
    schema_version: int = 1
    connection_id: str | None = None
    local_counter: int | None = None
```

**Field naming note:** Use `venue_cursor` as the canonical field name. Alias/document as `venue_seq` in `EVENT_MODEL.md` for cross-venue generality. For Polymarket, populated from `book_hash`.

### book_hash handling (critical)

> **MarketEvent has a generic `venue_cursor` field (documented alias: `venue_seq`), but for Polymarket this is populated from `book_hash` and must preserve current ordering/gap semantics from `market_ws_ingest._handle_sequence`. It must not be assumed to be a numeric sequence.**

Sequencer M2B.0-A must port these rules from `_handle_sequence`:

- No `venue_cursor` → always forward (apply)
- `venue_cursor == last` → duplicate; drop
- `venue_cursor < last` (lexicographic) → out-of-order; emit `WS_SEQ_GAP`; do not forward book event
- `venue_cursor > last` → in-order; forward

### Deterministic event_id algorithm

```python
def compute_event_id(
    *,
    event_type: str,
    token_id: str | None,
    venue_cursor: str | None,
    source_ts: datetime | None,
    payload_digest: str,  # sha256 hex of canonical JSON subset
) -> str:
    source_ms = int(source_ts.timestamp() * 1000) if source_ts else 0
    key = f"{event_type}|{token_id or ''}|{venue_cursor or ''}|{source_ms}|{payload_digest}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]
```

- **No `uuid4()`** in `event_id` (breaks replay determinism)
- For `book_delta` without hash: digest over normalized change list `(side, price, size)`

### Serialization format

JSON lines (one event per line), compatible with future normalizer:

```json
{
  "schema_version": 1,
  "event_id": "a1b2c3...",
  "event_type": "book_snapshot",
  "market_id": null,
  "token_id": "93600127...",
  "venue_cursor": "0xabc123...",
  "source_ts": "2026-07-03T12:00:00+00:00",
  "recv_ts": "2026-07-03T12:00:00.001+00:00",
  "payload": { "raw": { "...": "full WS message" } },
  "connection_id": "uuid",
  "local_counter": 42
}
```

- Datetimes: ISO8601 UTC (match `make_fact` in `reporting/facts.py`)
- `payload.raw` retains full WS message for book events (levels 2–5+ for research)
- Round-trip: `to_dict()` / `from_dict()` with golden vectors in tests

### Timestamp handling

| Field | Source |
|-------|--------|
| `source_ts` | `state/market_store.parse_exchange_ts(msg["timestamp"])` |
| `recv_ts` | `core.time.utc_now()` at ingest boundary |
| Replay "now" | Event's `recv_ts` or `source_ts` (documented in `EVENT_MODEL.md`) |

### Sequencer API

```python
# ingestion/sequencer.py

class MarketSequencer:
    def __init__(
        self,
        market_id: str,
        *,
        reorder_buffer_ms: float = 75.0,
        dedup_capacity: int = 4096,
    ) -> None: ...

    def ingest(self, event: MarketEvent) -> list[MarketEvent]:
        """Return 0+ events ready for consumers (ordered, deduped).
        May synthesize WS_SEQ_GAP events."""

    def flush(self) -> list[MarketEvent]:
        """Drain reorder buffer at end of stream / test teardown."""
```

One `MarketSequencer` per market (paired YES/NO tokens share it).

### Dedup strategy

- Ring set of `event_id` (capacity `dedup_capacity`)
- On duplicate `event_id`: drop silently
- Pattern mirrors `guru_stream` watermark generalized to event IDs

### Gap detection strategy

- Port `_handle_sequence` book_hash rules (see above)
- Synthesize `MarketEvent(event_type=WS_SEQ_GAP, ...)` with payload `{token_id, last_venue_cursor, received_venue_cursor, reason}`
- Reorder buffer: hold events with inverted `source_ts` within `reorder_buffer_ms` before emitting

### Coalescing (stub in 0-A)

Document coalescing policy in `EVENT_MODEL.md`; implement in M2B.0-B or M2B.1:

> On bounded queue overflow: coalesce contiguous `book_delta` → synthetic `book_snapshot` + `book_quality_changed{coalesced}`. Never drop trades, quality, or lifecycle events.

0-A may include a unit test with synthetic fixture only.

### Tests and fixtures

| Test file | Fixture | Asserts |
|-----------|---------|---------|
| `test_market_event_sequencer.py` | `fixtures/events/ws_book_sequence.jsonl` | In-order emission |
| same | duplicate hash row | Dedup drop |
| same | `fixtures/events/ws_gap_sequence.jsonl` | `WS_SEQ_GAP` synthesized |
| same | inverted source_ts within buffer | Reorder before emit |
| `test_market_event_serialization.py` | golden vectors | `event_id` stable; JSON round-trip |

Build fixture events in tests directly (no `event_factory` module required unless convenient for test DRY).

### Docs to create in M2B.0-A

- `Docs/EVENT_MODEL.md` (draft): envelope, `venue_cursor`/book_hash, ordering, dedup, gap, replay time discipline
- `Docs/Implementation/phase2b_data_backbone/milestones_index.md` (stub)

### M2B.0-A explicit exclusions

- No wiring into `market_ws_ingest.py`
- No changes to `paired_binary_run.py`
- No fact field additions
- No recorder / `event_sink`
- No `event_factory.py` in production tree (test helpers OK)
- No survival legacy module moves

---

## 11. M2B.0-B implementation spec

### Integration point: `ingestion/market_ws_ingest.py`

Inside `run_market_ws_ingest`, in the recv loop (~line 267), after `_parse_messages`, **before** `_handle_sequence` / `apply_market_message`:

```text
for msg in _parse_messages(raw):
    received_ts = utc_now()

    if event_backbone_enabled:
        event = build_market_event_from_ws(
            msg, received_ts=received_ts,
            connection_id=connection_id,
            local_counter=local_counter,
        )
        for out_event in sequencer.ingest(event):
            if is_book_event(out_event):
                project_event(write_store, out_event)
            elif out_event.event_type == WS_SEQ_GAP:
                apply_gap_side_effects(write_store, out_event, ...)
    else:
        # LEGACY PATH — unchanged
        tid = extract_token_id(msg)
        if tid and not _handle_sequence(write_store, tid, book_hash=msg.get("hash"), ...):
            continue
        apply_market_message(write_store, msg, source=BookSource.WEBSOCKET)

    # on_book_applied, readiness, shadow compare — unchanged
```

**Secondary path:** `venue/polymarket/book_snapshot.py` REST bootstrap → emit `REST_RECOVERY_USED` + `BOOK_SNAPSHOT` when flag on.

### `event_factory.py`

```python
# ingestion/event_factory.py

def build_market_event_from_ws(
    msg: dict,
    *,
    received_ts: datetime,
    connection_id: str,
    local_counter: int,
    market_id: str | None = None,
) -> MarketEvent:
    """Parse WS dict → MarketEvent. payload.raw = full msg."""
```

### Config flag

```yaml
# runtime.market_data.event_backbone (new block)
event_backbone:
  enabled: false              # DEFAULT — zero behavior change
  reorder_buffer_ms: 75
  emit_rest_recovery: true
```

Environment override (mirror existing pattern):

```text
TYREX_EVENT_BACKBONE=1|0|true|false
```

Parser in `runtime/config.py` → `MarketDataEventBackboneConfig` nested under `MarketDataConfig`.

### Projection function

Add to `ingestion/market_stream.py`:

```python
def project_event(
    store: MarketStateStore,
    event: MarketEvent,
    *,
    source: str = BookSource.WEBSOCKET,
) -> str | None:
    """Apply MarketEvent to store via existing apply_book internals.
    Returns snapshot_id or None if skipped."""
```

- `BOOK_SNAPSHOT` → extract bids/asks from `payload.raw` → `apply_book_message` logic
- `BOOK_DELTA` → `apply_price_change` logic
- `WS_SEQ_GAP` → `store.set_reconnect_gap(token_id, True)` + existing fact emission unchanged
- Event `payload` retains full raw WS; store projection still capped at `store_top_n_levels`

### Legacy mutation path preserved

When `event_backbone.enabled: false`:

- Exact current code path
- No `MarketEvent` allocation (unless debug `on_raw_event` sampling)
- No sequencer instantiation

### Projection equivalence test

`tests/test_market_event_projection_equivalence.py`:

```python
def test_fixture_stream_legacy_vs_projection_identical():
    store_legacy = MarketStateStore()
    store_event = MarketStateStore()
    sequencer = MarketSequencer(market_id="test")

    for raw_msg in load_ws_fixture_stream("combined_session.jsonl"):
        # Legacy
        apply_market_message(store_legacy, raw_msg, source=BookSource.WEBSOCKET)

        # Event path
        ev = build_market_event_from_ws(raw_msg, ...)
        for out in sequencer.ingest(ev):
            project_event(store_event, out)

    for token in ALL_TOKENS_IN_FIXTURE:
        assert_capture_equal(store_legacy.capture(token), store_event.capture(token))
```

Compare: `best_bid`, `best_ask`, `bids`, `asks`, `book_hash`, `reconnect_gap`, `source_quality`, `snapshot_id` presence.

### Flag on/off testing

**Required regression gate for M2B.0-B:**

```bash
# Flag off (default)
pytest tests/test_market_stream_ingest.py tests/test_market_ws_ingest.py \
  tests/test_ws_primary_event_ordering.py tests/test_ws_primary_reconnect_gap.py \
  tests/test_market_state_store.py tests/test_paired_binary_runtime.py

# Flag on (env or pytest fixture monkeypatch)
TYREX_EVENT_BACKBONE=1 pytest ...  # same suite, all green
```

### No changes to `paired_binary_run.py`

M2B.0-B must not modify `runtime/paired_binary_run.py`. Wake path continues via `set_on_token_update` → `MarketUpdateCoordinator` unchanged.

---

## 12. M2B.0-C implementation spec

### Objective

Close the decision-latency join gap: every material decision optionally carries the causing market event identity. **No trading behavior change.**

### New optional fact fields

| Field | Type | Meaning |
|-------|------|---------|
| `trigger_event_id` | `str \| null` | `event_id` of the market event that caused the monitor wake / decision |
| `event_recv_ts` | ISO8601 `str \| null` | `recv_ts` of that event |
| `decision_wall_ts` | ISO8601 `str \| null` | Wall-clock time when decision was taken (`utc_now()` at emit) |

### Where to attach `trigger_event_id`

| Location | Change |
|----------|--------|
| `runtime/paired_binary_run.py` | Track `last_wake_event_id: str \| None` set by ingest tap when `emit_event_correlation` on; pass to monitor tick |
| `strategies/paired_binary/monitor.py` | Accept optional `trigger_event_id`, `event_recv_ts`; forward to observability |
| `strategies/paired_binary/observability.py` | `emit_material_decision(...)` adds fields to `decision_snapshot` and `latency_chain` payloads |
| `strategies/paired_binary/latency.py` | `LatencyTracker` gains optional `trigger_event_id`, `event_recv_ts`, `decision_wall_ts`; **keep** existing `decision_ts` (monotonic float) |
| `strategies/paired_binary/facts.py` | `emit_latency_chain`, decision snapshot facts include optional fields |
| `survival/facts.py` | Material survival facts (`survivor_trailing_stop_triggered`, etc.) include optional correlation fields |

### Wiring last wake event

In `market_ws_ingest` (when correlation enabled), after successful projection:

```python
# Callback registered by paired_binary_run or app.py wiring in M2B.0-C
on_market_event_applied: Callable[[MarketEvent], None]
```

`paired_binary_run` sets `last_wake_event_id = event.event_id` on wake-driving book events only.

### Keep existing monotonic latency fields

**Do not remove or rename:**

- `LatencyTracker.decision_ts` (monotonic float)
- `trigger_to_submit_ms`, `submit_to_ack_ms`, etc.
- `monitor_trigger` (`"poll"` \| `"ws_book_update"`)

Add `decision_wall_ts` as **supplementary** wall-clock field for event-stream joins.

### Validator backward compatibility

- `validate_paired_binary_phase2_live_run.py` and phase1 validators: **must not require** new fields
- New fields appear only when `runtime.observability.emit_event_correlation: true`
- When flag off: fact payloads byte-identical to pre-2B.0-C (modulo existing timestamp variance)

### Config

```yaml
runtime:
  observability:
    emit_event_correlation: false   # DEFAULT
```

### Prove no behavior change

1. Shadow paired-binary run with flag off vs on: same state transitions, same intent sequence, same risk decisions
2. Fact diff: only additive optional fields differ when flag on
3. Full pytest green both settings
4. No edits to `pipeline.py`, `risk/engine.py`, `execution/planner.py`, `execution/oms.py`

---

## 13. M2B.1-A recorder MVP implementation spec

### `reporting/event_sink.py`

```python
class EventSink:
    """Non-blocking market event recorder. Never awaited from ingest hot path."""

    def __init__(
        self,
        output_dir: Path,
        market_id: str,
        *,
        queue_maxsize: int = 10_000,
        segment_max_mb: int = 64,
        segment_max_s: int = 300,
        batch_size: int = 100,
        batch_flush_ms: int = 50,
    ) -> None: ...

    async def start(self) -> None:
        """Start background writer task."""

    def emit(self, event: MarketEvent) -> None:
        """Non-blocking: queue.put_nowait. Never awaits."""

    async def stop(self) -> None:
        """Flush queue, finalize manifest."""
```

### Async non-blocking writer design

```text
emit() → asyncio.Queue.put_nowait (main thread / ingest task)
         ↓ on QueueFull
         increment manifest.dropped_events; optionally coalesce last N book_deltas

_writer_loop() → batch up to batch_size or batch_flush_ms
              → write JSONL lines to current segment file
              → rotate segment when size/time threshold exceeded
              → update manifest.json atomically (write temp + rename)
```

**Critical:** `emit()` must never call `flush()`, `write()`, or `await` I/O.

### Queue size and overflow behavior

| Parameter | Default | On overflow |
|-----------|---------|-------------|
| `queue_maxsize` | 10,000 | `put_nowait` raises `QueueFull` → catch; `dropped_events += 1`; log warning |
| Coalescing (optional 1-A) | off | Document for 1-B: merge contiguous book_deltas in overflow handler |

Overflow degrades **recording only** (gap noted in manifest). Trading never waits.

### Segment file format (plain JSONL)

```text
var/recordings/<YYYY-MM-DD>/<market_id>/
  events-00001.jsonl
  manifest.json
```

Each line: serialized `MarketEvent` JSON (§10 format). **No compression in M2B.1-A.**

### Manifest schema

```json
{
  "schema_version": 1,
  "market_id": "btc_5m_20260703_1200",
  "yes_token_id": "...",
  "no_token_id": "...",
  "condition_id": null,
  "recording_started_ts": "2026-07-03T11:55:00+00:00",
  "recording_ended_ts": null,
  "segments": [
    {
      "path": "events-00001.jsonl",
      "event_count": 4521,
      "first_recv_ts": "...",
      "last_recv_ts": "...",
      "bytes": 1234567
    }
  ],
  "gaps": [],
  "dropped_events": 0,
  "event_types_seen": {"book_delta": 4000, "book_snapshot": 12},
  "git_sha": "abc123",
  "scenario": "record_btc5m_single.yaml",
  "linked_run_ids": []
}
```

### Plain JSONL choice

M2B.1-A uses plain JSONL to avoid new dependencies. M2B.1-B adds `zstandard` as optional `[record]` extra for `.jsonl.zst`.

### Record-only CLI path

```python
# runtime/app.py
p_rec = sub.add_parser("record", help="Record market events (no trading)")
p_rec.add_argument("--scenario", required=True)
p_rec.add_argument("--repo-root", default=None)

# runtime/record_run.py
async def cmd_record(args) -> int:
    app = load_app_config(..., scenario_file=args.scenario)
    # MUST NOT import: LiveOMS, SingleWriterOMS, WalletStore, RiskEngine,
    #   process_intent_work_unit, paired_binary_run
    sink = EventSink(...)
    await run_market_ws_ingest(..., on_market_event=sink.emit, ...)
    await stop.wait()
```

CLI invocation:

```bash
tyrex-pm record --scenario config/scenarios/record_btc5m_single.yaml
```

### Scenario config: `config/scenarios/record_btc5m_single.yaml`

```yaml
# Minimal record-only scenario (sketch)
runtime:
  execution_mode: shadow          # no live OMS
  market_data:
    enabled: true
    event_backbone:
      enabled: true
    websocket:
      primary_enabled: true
  recording:
    enabled: true
    output_dir: var/recordings
    segment_max_mb: 64
    segment_max_s: 300
strategy:
  kind: paired_binary             # for token_ids / market_id metadata only
  paired_binary:
    market_id: "<configured>"
    yes_token_id: "<required>"
    no_token_id: "<required>"
    # OR event_url for metadata resolution (reuse paired_binary_metadata)
```

No risk block required for record mode (or minimal stub that record_run ignores).

### No-trading import isolation test

`tests/test_record_mode_import_isolation.py`:

Scan `runtime/record_run.py` and all transitive imports under `src/tyrex_pm/` for forbidden patterns:

```text
FORBIDDEN: SingleWriterOMS, LiveOMS, WalletStore, RiskEngine,
           process_intent_work_unit, paired_binary_run,
           execution.oms, risk.engine, state.wallet_store
```

Pattern: extend `test_v2_import_isolation.py` approach (text scan of import lines).

### One-market smoke test

**Manual / scripted acceptance:**

1. Configure one upcoming BTC 5m market (pre-open → resolution + 60s)
2. Run `tyrex-pm record --scenario record_btc5m_single.yaml`
3. Verify:
   - `events-00001.jsonl` exists and is parseable line-by-line
   - `manifest.json` complete with segment stats
   - Event types include `book_snapshot`, `book_delta`
   - `import isolation test` passes in CI

---

## 14. Later milestones summary

| Milestone | Key deliverable | New deps | Canonical test |
|-----------|-----------------|----------|----------------|
| M2B.1-B | `market_discovery.py`, zstd, 24h coverage | `zstandard` | Coverage report |
| M2B.2 | `venue/binance_data/`, `external_btc_tick` | `websockets` (existing) | Isolation test |
| M2B.3 | `research/normalize/` → Parquet | `pandas`, `pyarrow` | Golden normalizer |
| M2B.4 | `research/notebooks/` 01, 02, 07 | jupyter (dev) | Written memo |
| M2B.5 | `research/replay/` + sweep harness | — | Golden replay-diff |
| M2B.6 | Incremental `FeatureBuilder` | — | Live/replay parity |
| M2B.7 | `research/labeler/` → `survival_cases` | — | ≥500 episodes |
| M2B.8 | `advisor/resolver.py` pass-through | — | Fact-diff on/off |
| M2B.9 | Nightly shadow diff job | — | Weekly report |

### Target event-driven architecture (end state)

```text
┌────────────────────────────────────────────────────────────────────────┐
│ L1 INGESTION (live)                                                    │
│  market_discovery  market_ws  user_ws  btc_ws                          │
│            └──────────────┬─── emit MarketEvent ───┬──────────┘        │
├───────────────────────────▼────────────────────────▼───────────────────┤
│ L2 EVENT BACKBONE   per-market Sequencer (order, dedup, gap detect)    │
│        ├── projection → MarketStateStore  (existing consumers as-is)   │
│        ├── tap        → Recorder (EventSink)                           │
│        └── dispatch   → paired_binary_run wake (unchanged)             │
├────────────────────────────────────────────────────────────────────────┤
│ L3 RECORDER    events/*.jsonl[.zst] per market + manifests             │
│ L4 NORMALIZER + DATA LAKE   Parquet tables (offline batch)             │
│ L5 OFFLINE LAB + REPLAY ENGINE  (research/, never imported by live)    │
│ L6 FeatureBuilder + Labeler + advisory ParameterResolver (late 2B)     │
│ L7 RISK → PLANNER → SingleWriterOMS      (unchanged)                   │
└────────────────────────────────────────────────────────────────────────┘
```

### Import boundaries

| Package | May import | Must not import |
|---------|------------|-----------------|
| `research/` | any `tyrex_pm.*` | — |
| `src/tyrex_pm/**` | — | `research.*` |
| `advisor/` (M2B.8) | `core`, `market_data` (read), config | `execution`, `risk`, `venue` OMS paths |
| `record_run.py` | `ingestion`, `reporting/event_sink`, `market_data` | OMS, wallet, risk, pipeline, `paired_binary_run` |

---

## 15. Documentation plan

| Document | Purpose | Created |
|----------|---------|---------|
| `Docs/Implementation/Phase 2 completion/tyrex_pm_phase2b_plan.md` | This document — execution authority | **Now** |
| `Docs/EVENT_MODEL.md` | Event contract, venue_cursor/book_hash, ordering, replay guarantees | M2B.0-A |
| `Docs/Implementation/phase2b_data_backbone/milestones_index.md` | Milestone status tracker | M2B.0-A |
| `Docs/Implementation/phase2b_data_backbone/milestone_0_event_contract.md` | M2B.0 detail spec | M2B.0-A |
| `Docs/Implementation/phase2b_data_backbone/milestone_1_recorder_mvp.md` | M2B.1 detail spec | M2B.1-A |
| `Docs/DATA_LAKE.md` | Parquet schemas, scorecard, partition layout | M2B.3 |
| `Docs/reporting_fact_model.md` | Optional correlation fields | M2B.0-C |
| `Docs/developer_guide.md` §1 | `research/` import boundary | M2B.0-A |
| `Docs/OPERATIONS.md` | `tyrex-pm record` usage | M2B.1-A |
| `Docs/README.md` | Link to Phase 2B program | M2B.0 complete |

Repo convention: **`Docs/`** (capital D), lowercase snake_case filenames.

---

## 16. Testing plan

### Per-milestone test requirements

| Milestone | New tests | Regression gate |
|-----------|-----------|-----------------|
| M2B.0-A | `test_market_event_sequencer.py`, `test_market_event_serialization.py` | — |
| M2B.0-B | `test_market_event_projection_equivalence.py` | All market/WS tests flag off **and** on |
| M2B.0-C | `test_event_correlation_facts.py` | `test_paired_binary_runtime.py`, validators |
| M2B.1-A | `test_event_sink_*`, `test_record_mode_import_isolation.py` | — |
| M2B.1-B | Discovery fixture, heartbeat | 24h smoke (manual/ops) |
| M2B.3 | `test_normalize_golden.py` | — |
| M2B.5 | `test_replay_fact_diff_golden.py` | CI golden recordings |
| M2B.6 | `test_feature_live_replay_parity.py` | — |
| M2B.8 | `test_parameter_resolver_passthrough.py` | Replay-diff still green |

### Test suite classification

| Group | File | Suite |
|-------|------|-------|
| Sequencer ordering/dedup/gap | `test_market_event_sequencer.py` | unit |
| Event serialization | `test_market_event_serialization.py` | unit |
| Projection equivalence | `test_market_event_projection_equivalence.py` | integration / CI |
| Recorder non-blocking | `test_event_sink_never_blocks.py` | unit + async |
| Segment + manifest | `test_event_sink_segments.py`, `test_event_sink_manifest.py` | unit |
| Record import isolation | `test_record_mode_import_isolation.py` | CI |
| Research import isolation | `test_research_import_isolation.py` | CI (from M2B.3) |
| Replay-diff golden | `test_replay_fact_diff_golden.py` | CI-golden (M2B.5) |
| Feature parity | `test_feature_live_replay_parity.py` | CI-golden (M2B.6) |
| Resolver pass-through | `test_parameter_resolver_passthrough.py` | unit (M2B.8) |

### Fixtures

| Fixture | Used by |
|---------|---------|
| `tests/fixtures/ws/market_book.json` | Existing + equivalence |
| `tests/fixtures/ws/market_price_change.json` | Existing + equivalence |
| `tests/fixtures/events/ws_book_sequence.jsonl` | Sequencer (M2B.0-A) |
| `tests/fixtures/events/ws_gap_sequence.jsonl` | Gap detection (M2B.0-A) |
| `tests/fixtures/recordings/` (trimmed) | Replay-diff (M2B.5) |

---

## 17. Open human decisions

| # | Decision | Recommendation |
|---|----------|----------------|
| 1 | Repurpose `core/events.py` vs new `core/market_events.py` | **Repurpose** — file is unimported |
| 2 | Field name: `venue_cursor` vs `venue_seq` | **`venue_cursor`** in code; document `venue_seq` alias in EVENT_MODEL |
| 3 | M2B.1-A compression | **Plain JSONL**; zstd in M2B.1-B |
| 4 | Who emits `session_timer_tick` in live before SessionRunner | **Defer** to M2B.5 replay synthesis |
| 5 | In-trading recorder tap | Separate `runtime.recording.tap_in_live: false` until M2B.1-B proven |
| 6 | Golden replay recordings in git vs CI artifacts | Commit small trimmed fixtures (<5MB) |
| 7 | Survival legacy quarantine timing | **Separate cleanup PR** after M2B.1-A |

---

## 18. Separate Phase 1 hardening track

**Related Phase 1 hardening, independent of Phase 2B.**

These change live survival behavior and belong to Phase 1's own hardening schedule. They must not block the recorder/data backbone and are **not** Phase 2B implementation milestones.

| Item | Description | Phase 2B interaction |
|------|-------------|----------------------|
| **Hard floor → enforce** | Flip `survivor_floor.enforcement_mode: enforce` in dedicated experiment scenario; validate with `validate_paired_binary_phase2_live_run.py` | None, except M2B.0-C telemetry (`trigger_event_id`) helps measure stop latency |
| **Floor + trailing combined enforce** | Scenario with both enforced; separate validation | None |
| **Stop-path cadence changes** | Any parameter change to loser-stop detection | Phase 1 decision only; not a side effect of backbone work |

Once M2B.5 replay engine exists, floor+trailing profiles may be replay-validated retroactively as additional evidence — but that is validation, not a 2B deliverable.

---

## 19. Final recommended first implementation task

### Start M2B.0-A immediately

**Task list (implementation agent prompt):**

1. Create `src/tyrex_pm/core/runtime_events.py` — move unused types from `core/events.py`
2. Implement `EventType`, `MarketEvent`, `compute_event_id`, `to_dict`/`from_dict` in `src/tyrex_pm/core/events.py`
3. Implement `src/tyrex_pm/ingestion/sequencer.py` with book_hash-compatible ordering (port `_handle_sequence` rules)
4. Add `tests/test_market_event_sequencer.py` and `tests/test_market_event_serialization.py` with fixtures under `tests/fixtures/events/`
5. Draft `Docs/EVENT_MODEL.md` and stub `Docs/Implementation/phase2b_data_backbone/milestones_index.md`

**Explicit exclusions:**

- Do not wire into `market_ws_ingest.py`
- Do not modify `paired_binary_run.py`
- Do not add fact fields
- Do not add recorder
- Do not move survival legacy modules
- Do not add `zstandard`, `pandas`, or `pyarrow`

**M2B.0-A done when:** all new tests green; `pytest` overall green; no runtime behavior change.

---

*End of Phase 2B implementation-ready plan.*
