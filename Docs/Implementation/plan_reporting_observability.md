# Plan: Reporting and observability — platform run intelligence

**Status:** Full-scope implementation plan (replaces the earlier narrow “post–Phase C / MVP reporting” draft).  
**Scope:** Canonical **structured evidence** for the **trading platform / strategy runtime**—not a guru-only validation add-on. Logs remain for raw forensics; **the run dataset is the canonical source** for answering business and operational questions, comparing guru vs follower, and tuning strategies.

---

## Critical review of the prior (narrow) plan

### A) What remains valid and should be kept

- **Separation of concerns:** Reporting is **not** trading policy; it must not move gates, sizing, or execution decisions into telemetry code.
- **Facts at runtime, aggregates after:** Capture **atomic, timestamped facts** during the run; compute distributions, rollups, guru-vs-us deltas, and PnL breakdowns in **post-run** tooling (or scheduled recompute)—avoid heavy graph algorithms on the hot path.
- **Stable identifiers and schema versioning:** Every fact carries `run_id`, `fact_schema_version`, monotonic or wall-clock timestamps, and join keys (`correlation_id`, order ids, instrument/token ids as applicable).
- **Codebase-grounded integration map (conceptually):** Ingest (`guru_ingest_pipeline.py`, `guru_stream_actor.py`, …), strategy (`copy_strategy.py`), risk (`risk/configured.py`), execution (`nautilus_guru_exec.py`, `polymarket_policy.py`), compose (`runtime/guru_compose.py`), entrypoint (`scripts/run_guru.py`) remain the right *classes* of hook points—extended now to **Nautilus order events**, **cache/portfolio readers**, and **reconciliation**.
- **Dual log files reality:** `runtime/guru_run_logging.py` / `run_guru.py` Tyrex vs Nautilus sinks stay **auxiliary**; reporting must **not** depend on regex as the primary ingest path.
- **Future UI/IHM:** Design for **list runs → drill run → query facts → time series**; SQL-friendly and/or columnar exports are still appropriate.

### B) What was insufficient, misleading, or too narrow

- **Framing as “post–Phase C” and “C1/C2/C3 reporting”** tied the system to a **closed milestone** instead of the **platform lifecycle**. Strategy, ingest, risk, and execution will evolve; the reporting model must be **strategy-agnostic at the core** with **guru-specific extensions**.
- **“MVP”, “minimum implementation”, “optional facts”, “implied by joins”, “defer fills/PnL/lifecycle”** directly conflict with the goal of **explainable, comparable run truth**. Acceptances implied by downstream rows **break** when execution fails mid-flight, shadow mode omits venue, or crashes truncate the log—**explicit facts** are required for **accepted** strategy paths and **risk approvals**.
- **Low-overhead-first bias** risked **permanent information loss**. Efficiency is a **hard constraint** (bounded queues, batching, selective denormalization), not the **optimization target**—**completeness of observability** for decisions and outcomes comes first.
- **Centering summaries on phase labels** (C1/C2/C3) instead of **business questions** (guru vs us, capital, execution quality) made the plan wrong for PMs, operators, and researchers.
- **SQLite “optional materialization”** as the primary story understated the need for a **queryable, join-heavy** dataset across orders, fills, positions—**post-run** materialization is still fine, but the **target artifact** should be sized for **full lifecycle**, not “minimal facts.”

### C) What facts/metrics are missing today for a full reporting system

| Gap | Notes |
|-----|--------|
| **Run manifest + config_snapshot** | No durable `run_id` or frozen **effective config** (strategy + risk + runtime + env knobs that affect behavior). |
| **Explicit strategy_accept / sizing_fact** | Acceptances are **not** logged at INFO uniformly (`copy_conviction_diag` is DEBUG); **pre-risk target qty/notional** per signal is not a first-class fact row. |
| **Explicit risk_allow** | Approvals are **silent** except via downstream intent; need **allow** rows with gate inputs/outputs for replay. |
| **Normalization / book_constraint facts** | C3 steps mostly appear as log lines; need **structured** pre/post qty, price, book top, clip bounds, guard verdict. |
| **Order lifecycle stream** | `CopyStrategy.on_order_event` forwards only to `NautilusGuruExecutionPort.notify_order_event` for **timer cancel**—no Tyrex-wide recording of accept, fill, cancel, reject. |
| **Fill facts** | Not emitted by Tyrex; Nautilus `OrderEvent` / fill events contain the data. |
| **Position / exposure facts** | `NautilusPortfolioExposureAggregator` / readers compute `E_pending`, `E_filled_net`, `E_portfolio` at decision time—not persisted as **time series** or per-signal context. |
| **Account / allowance facts** | `AccountSnapshot`, `AllowanceSnapshot` in `state_readers.py`—used for gates, not durably reported. |
| **Reconciliation / divergence** | No fact when **cache vs venue** or **intent vs resting** mismatch is detected (future adapter hooks). |
| **Guru benchmark row** | Guru `size_raw`, `price_raw`, `notional` should sit in a **single benchmark fact** joined to every downstream decision for **delta** math. |
| **report_pipeline_health** | Backpressure, drop counts, disk errors, incomplete run end—**telemetry on the reporter itself**. |

### D) What was previously optional / implied / deferred but must become first-class

| Old framing | New requirement |
|-------------|-----------------|
| Approvals “implied by intent” | **`strategy_decision_fact` (accept)** + **`risk_decision_fact` (allow)** with numeric fields. |
| `guru_stream_would_emit` optional / sampled | **Always record** in shadow/comp modes where the platform emits it (bounded payload); needed for **ingest truth vs guru**. |
| Order lifecycle “MVP partial” | **`order_lifecycle_fact`** for **all** state transitions Tyrex observes (from Nautilus events + submit edge). |
| Fill facts “deferred” | **`fill_fact`** for every fill event; basis for **fill quality** and **realized PnL** attribution. |
| Portfolio snapshots “optional low-rate” | **`portfolio_snapshot_fact`** and **`exposure_fact`** on a defined cadence **and** on **material events** (risk deny, submit, fill, significant mark change—policy TBD to cap volume). |
| Realized / unrealized PnL deferred | **`pnl_fact` / derived metrics** from framework fills + positions; unrealized when **marks** exist (`portfolio_exposure.py` / `Cache` price priority). |
| Slippage / time-to-fill deferred | **First-class execution-quality metrics** in §9 from lifecycle + fill timestamps. |
| “Heavy online computation” deferred | **Online** = only atomic facts; **heavy** analytics stay post-run—but **inputs** to those analytics must **not** be omitted. |

---

# 1. Reframed objective

Build the **reporting and observability system** for Tyrex so that **each run** produces a **complete, linked, versioned factual dataset** that is the **canonical evidence** for:

- what happened end-to-end (ingest → strategy → risk → execution → venue feedback → portfolio),
- **why** each skip, resize, clip, delay, or failure occurred,
- **guru vs follower** comparison on size, timing, and outcomes,
- **execution quality** and **capital deployment** truth aligned with **Nautilus-native state** (`Docs/Implementation/current_state.md`, roadmap),
- tuning **any** strategy running on this runtime—not only guru-follow.

Reporting **does not** replace logs; it **subsumes** them for structured questions. Trading logic stays out of the reporting package; reporting **records** decisions and framework truth **as observed**.

---

# 2. Business questions the system must answer

After a run, operators, PMs, researchers, and developers must answer **at least** the following from the dataset (not from ad-hoc grep):

**Guru and mirror**

- **What did the guru do?** (per trade: side, token/market, size, price, event time, source id)
- **What did we do?** (per signal: follow, skip, resize, submit, cancel, fill)
- **Where did we differ from the guru?** (qty, notional, price, timing)
- **Why did we differ?** (machine-readable reason taxonomy, §5)

**Sizing and execution**

- **How much did we intend to copy** (pre-risk target)? **Post-risk?** **Post-normalization / post book-constraint?**
- **How much did we actually submit** (qty/notional, price)?
- **How much was acknowledged, resting, partially filled, fully filled, canceled, rejected, expired?**
- **What was fill quality?** (avg fill px vs intent, vs book/mid when measurable, partial-fill sequence)

**Capital**

- **What capital was available, reserved, pending (open orders), filled (positions), and blocked?**
- **Which gates or controls had the most effect?** (denials by gate, **lost notional** by reason)
- **Stale or unresolved** risk state—how often and with what effect?

**Config and markets**

- **Which config values materially changed behavior between runs?** (diff on normalized `config_snapshot`)
- **Which markets/tokens are systematically problematic?** (reject rate, guard skips, missing book/instrument, mark gaps)

**Strategy outcomes**

- **What explains under-following or over-following** vs guru notionals?
- **What is the realized impact on PnL and deployment?** (realized; unrealized when marks allow)

**Platform**

- **Was the reporting pipeline healthy?** (drops, flush failures, incomplete run)

---

# 3. Canonical run data model

Every fact row is a **vertex** on a shared graph; exports and UI use the same **join keys**.

### 3.1 Primary identifiers

| Id | Definition |
|----|------------|
| **`run_id`** | UUID (or ULID) assigned at process start; **one** per `run_guru.py` (or future entrypoints). |
| **`strategy_run_id`** | Optional sub-id if one process hosts **multiple** strategy instances serially; default **`run_id`** for guru-follow today. |
| **`strategy_name`** | Logical name, e.g. `guru_follow` (from config or code registry). |
| **`strategy_instance`** | **Stable disambiguator** within a host: e.g. `trader_id` + `strategy_name` + index. |
| **`correlation_id`** | **Business trace id** for guru-follow: `GuruTradeSignal.source_trade_id` (equals `OrderIntent.correlation_id`). **Required** on all guru-sourced facts. |
| **`guru_trade_id` / `source_trade_id`** | Same as `correlation_id` for guru stack; alias field for clarity in exports. |
| **`token_id`** | Polymarket outcome token string from guru / intent. |
| **`market_id`** | Gamma/market slug or id when resolvable (may be null early-run). |
| **`instrument_id`** | Nautilus `InstrumentId` string when resolved. |
| **`client_order_id`** | Nautilus `ClientOrderId` from `_client_order_id_from_guru_correlation` (`nautilus_guru_exec.py`) or legacy path equivalent. |
| **`venue_order_id`** | From order events / `OrderSnapshot.venue_order_id` (`state_readers.py`). |
| **`fill_event_id`** | Stable id from Nautilus fill event (or hash of venue_order_id + trade_id + ts if needed). |
| **`position_id`** | If framework exposes; else **synthetic** `(instrument_id, run_id)` for reporting. |
| **`account_snapshot_id`** | Monotonic sequence per run for `Portfolio.account` captures. |
| **`balance_allowance_snapshot_id`** | Monotonic sequence for py-clob snapshot (`AllowanceSnapshot`). |

### 3.2 Join model (normative)

- **`guru_signal_fact`** is the **root** for a guru trade: `correlation_id` **=** `source_trade_id`.
- **`strategy_decision_fact`**, **`sizing_fact`**, **`risk_decision_fact`** link **`run_id` + `correlation_id`**.
- **`execution_intent_fact`** (Tyrex handoff: post-risk intent, pre-venue) links **`correlation_id` + `token_id`**.
- **`normalization_fact`**, **`book_constraint_fact`** link **`correlation_id`** (and `client_order_id` once minted if created before submit).
- **`execution_outcome_fact`** (submit/skip/error at Tyrex boundary) links **`correlation_id`**; includes **`client_order_id`** when submit succeeds.
- **`order_lifecycle_fact`** links **`client_order_id`** (+ `venue_order_id` when known); **must** also carry **`correlation_id`** and **`run_id`** for guru drill-down (from order tags `guru_cid=` or deterministic COID mapping in `state_readers.py`).
- **`fill_fact`** links **`fill_event_id`**, **`client_order_id`**, **`venue_order_id`**, **`correlation_id`**.
- **`position_fact` / `exposure_fact`** link **`instrument_id`** and optionally **`correlation_id`** when the change is attributable; portfolio-wide rows use **`run_id` + snapshot_ts** only.
- **`account_snapshot_fact`** / **`portfolio_snapshot_fact`** link **`account_snapshot_id`** / aggregate sequence id.
- **`reconciliation_fact`** links whichever ids the reconciler compares (e.g. `client_order_id` + venue state hash).

**Robustness:** If **`venue_order_id`** arrives late, lifecycle rows use **`client_order_id`** as spine; **upsert** or **second-pass linking** in post-run ETL is allowed; facts must allow **nullable** `venue_order_id` on early rows.

---

# 4. Always-on fact model

All types below are **required for every run** in which the subsystem is **active** (e.g. no `venue_order_id` in pure shadow without live submit—**explicit `run_mode`** + **absence** is recorded, not silence).

| Fact type | Purpose | Key fields (non-exhaustive) |
|-----------|---------|------------------------------|
| **`run_manifest`** | Anchor | `run_id`, `started_at_utc`, `ended_at_utc`, `git_sha`, host, `trader_id`, `execution_mode`, `guru_ingest_mode`, log paths, **`reporting_schema_version`** |
| **`config_snapshot`** | Frozen effective config | Normalized JSON/hash of strategy + risk + runtime + **material env** (`TYREX_MIN_BUY_NOTIONAL_USD`, etc.); **diff-friendly** |
| **`guru_signal_fact`** | Guru-side truth | `correlation_id`, `source` (poll/rtds/gap_fill), `side`, `token_id`, `guru_size_raw`, `guru_price_raw`, `guru_notional_usd`, `ts_event_ms`, `ts_ingest_ms`, ingest metadata |
| **`guru_trade_benchmark_fact`** | One row per signal for comparisons | Denormalized **guru reference** for joins: same as above plus `market_id` if known—optimizes §5 queries |
| **`strategy_decision_fact`** | Entry/exit policy | `decision` accept/skip, `reason_code`, branch, **`pre_sizing_qty`** if accept path—**every** evaluated signal |
| **`sizing_fact`** | Numeric trace | `base_scale`, `effective_scale`, `conviction_ratio`, **target_qty** post-size, **`target_notional_usd`**, guru notionals—**on accept path** |
| **`risk_decision_fact`** | Gate truth | `allowed`, `reason_code`, `gate`, `e_portfolio`, `intent_notional`, `cap`, `balances_snippet`, **`inputs_hash`** optional |
| **`execution_intent_fact`** | Handoff to execution | `correlation_id`, `token_id`, `side`, **risk-approved** qty/price, `signal_kind`, timestamps through risk |
| **`normalization_fact`** | Venue shape | Pre/post qty, pre/post price, skip flag, `ReasonCode`, min_notional/tick constraints |
| **`book_constraint_fact`** | Book-aware C3 | Book top snapshot ids, `visible_liquidity`, clip_from/to, guard bands, `book_source` (cache/rest/none) |
| **`execution_outcome_fact`** | Tyrex submit boundary | submit/skip/error, `client_order_id`, `instrument_id`, final submitted qty/price, **`latency_ms`** stages |
| **`order_lifecycle_fact`** | Framework state machine | `status` transition, `prev_status`, `ts_event`, `leaves_qty`, `cum_qty`, `avg_px`, venue/framework reason if any |
| **`fill_fact`** | Execution fill | `last_px`, `last_qty`, `liquidity_side`, commission if available, **`ts_fill`** |
| **`position_fact`** | Position updates | `instrument_id`, `net_position`, `avg_px`, `ts` |
| **`exposure_fact`** | B1-style scalars | `E_pending`, `E_filled_net`, `E_portfolio`, `complete`, per-eval or snapshot |
| **`account_snapshot_fact`** | Portfolio.account | balances summary, `captured_at_utc`, `account_present` |
| **`portfolio_snapshot_fact`** | Rolling capital view | Derived headroom, cap usage, unresolved flags—feeds §8 |
| **`health_anomaly_fact`** | Ingest/connectivity | RTDS reconnect, stall, fallback, gap-fill, poll errors |
| **`component_status_fact`** | Bootstrap | cache warmup, dynamic instrument cap, adapter ready |
| **`reconciliation_fact`** | Drift detection | what was compared, outcome, severity |
| **`report_pipeline_health_fact`** | Reporter reliability | queue depth peaks, flush errors, **facts_dropped**, disk full |

**Strategy-agnostic rule:** Names like `guru_*` are **extensions**; **`strategy_decision_fact`**, **`risk_*`**, **`execution_*`**, **`order_*`**, **`fill_*`** must remain **generic** for non-guru strategies (different `correlation_id` semantics).

---

# 5. Guru-vs-us comparison model

### 5.1 Required columns (per `correlation_id` or per benchmark row)

| Measure | Source |
|---------|--------|
| Guru side / price / qty / notional / ts | `guru_trade_benchmark_fact` |
| Pre-risk target qty / notional | `sizing_fact` |
| Post-risk qty / notional | `risk_decision_fact` + `execution_intent_fact` |
| Post-normalization qty / notional | `normalization_fact` |
| Submitted qty / notional | `execution_outcome_fact` |
| Resting / ack qty | `order_lifecycle_fact` |
| Filled qty / notional | `fill_fact` agg |
| Avg fill price | `fill_fact` |
| Δ qty, Δ notional vs guru | **derived** in post-run |
| **Copy ratio** | our_notional / guru_notional (define 0/∞ guards) |
| **Under-copy / over-copy** | threshold policy vs guru leg |

### 5.2 Reason-for-delta taxonomy (`delta_reason_code`)

Machine-readable enum (extend in schema; examples):

`conviction_sizing` · `min_follow_notional` · `min_follow_price_missing` · `token_filter` · `entry_exit_policy` · `portfolio_cap` · `token_notional_cap` · `portfolio_exposure_unresolved` · `position_exposure_unresolved` · `concurrent_guru_resting_cap` · `collateral_reserve` · `min_collateral` · `min_allowance` · `kill_switch` · `order_qty_limit` · `notional_per_order` · `normalization_min_size` · `normalization_min_notional` · `entry_guard_slippage` · `book_unavailable` · `depth_clip` · `limit_timeout_cancel` · `venue_reject` · `venue_error` · `instrument_unmapped` · `instrument_not_in_cache` · `dynamic_resolve_failed` · `activation_cap` · `operator_config_change` · `shadow_mode_no_submit` · `unknown`

**Maps to:** skip/execution `ReasonCode` (`core/reason_codes.py`) where applicable; reporting layer **normalizes** legacy and Nautilus reasons into this taxonomy.

---

# 6. Strategy decision and sizing trace

For **every** `GuruTradeSignal` processed (per branch):

1. **Record `guru_signal_fact` + benchmark row** (even if later skipped—**guru did X** is independent).
2. **Record `strategy_decision_fact`** for **skip OR accept** (no silent accept).
3. On accept: **`sizing_fact`** with **all** numbers needed to recompute qty (guru size, scales, caps, floor).
4. **`risk_decision_fact`** for **deny OR allow** with gate label and **numeric inputs** that justified the decision (where privacy/size allows).
5. **`execution_intent_fact`** after risk approve.
6. **`normalization_fact`** / **`book_constraint_fact`** for each shaping step (including **no-op** with “passthrough” flag—proves path taken).
7. **`execution_outcome_fact`** at submit/skip/error.
8. **`order_lifecycle_fact`** + **`fill_fact`** from Nautilus events.

**Shadow runs:** Steps 6–8 may terminate at **shadow intent**; `execution_outcome_fact` records **`shadow_mode`** / **no venue** explicitly.

---

# 7. Execution truth

### 7.1 Lifecycle states to capture

**Submit**, **ack**, **rest** (open working), **partial fill**, **full fill**, **cancel request**, **canceled**, **reject**, **timeout cancel** (Tyrex-initiated, `EXEC_LIMIT_TIMEOUT_CANCEL`), **expiry** if venue/TIF produces it, **reconciliation corrections** when detected.

### 7.2 Source of truth

**Primary:** Nautilus **`OrderEvent`** stream visible to the strategy (`CopyStrategy.on_order_event` today only forwards timer cancel to `NautilusGuruExecutionPort.notify_order_event`). **Change:** introduce **`ReportingOrderEventSink`** (or extend port) that **dispatches** `OrderAccepted`, `OrderRejected`, `OrderCanceled`, `OrderFilled`, `OrderUpdated`, etc., to **`order_lifecycle_fact` + `fill_fact`**.

**Secondary validation:** `Cache.order(client_order_id)` snapshots at **key boundaries** (post-submit timer schedule—optional; post-fill—for reconciliation facts).

**Legacy py-clob path (`polymarket_policy.py`):** If HTTP response exposes ids and fills, emit **same fact types** with **adapter** `source=legacy_http`.

### 7.3 Integration work (explicit)

| Item | Current code | Needed |
|------|--------------|--------|
| Order events | `copy_strategy.py` `on_order_event` | Fan-out to reporting sink + keep timer behavior |
| Submit edge | `nautilus_guru_exec.py` `submit_intent` | Already logs `LIVE_ORDER_SUBMIT`; add **`execution_outcome_fact`** + link `client_order_id` |
| COID ↔ correlation | `_client_order_id_from_guru_correlation`, `guru_cid=` tags | Reporting must decode both **tag-first** per `is_guru_resting_order` / `OrderSnapshot.tags` |
| Venue ids | Adapter fills in over time | Lifecycle rows **update** `venue_order_id` when seen |

**Hard limitation today:** If the Polymarket adapter **drops** tags or does not emit certain events, facts must record **`data_quality_flag`** on affected orders; **adapter fixes** are tracked as engineering tasks, not “defer reporting.”

---

# 8. Capital / portfolio / account truth

Expose **what risk and execution actually used** (aligned with `state_readers.py`, `portfolio_exposure.py`, `configured.py`):

- **Available USDC / buying power** — `AccountSnapshot` (`Portfolio.account`).
- **Reserve / min collateral / allowance** — `AllowanceSnapshot` + gate evaluations; **per-deny** facts already carry snippets.
- **Pending notional** — `E_pending` from open orders (`leaves_quantity` × price); `NautilusExecutionStateReader`.
- **Filled exposure** — `net_exposure` / position readers; `NautilusPortfolioExposureAggregator`.
- **Per-token and portfolio exposure** — `aggregate()` result fields; **headroom** to caps = `cap - e_portfolio - n` at deny time.
- **Stale snapshot** — `captured_at_utc` vs `max_*_age_seconds` from **risk settings**; record **`stale=true`** on decisions influenced by stale data.
- **Unresolved** — `complete=false`, `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED` paths; count + time-under-unresolved.

**Facts:** `account_snapshot_fact`, `portfolio_snapshot_fact` (with `e_portfolio`, legs, `omitted_instruments_*`), **`exposure_fact`** pinned to **risk evaluation events** and periodic snapshots (interval configurable; **must** include **start/stop** and **any risk denial burst** window).

---

# 9. PnL and execution-quality metrics

Computed **post-run** (and optionally incremental ETL) from facts; definitions fixed in schema doc.

| Metric | Definition (high level) |
|--------|-------------------------|
| **Realized PnL** | From closed trades / fills + Nautilus position reductions—**venue truth**; attribute to `correlation_id` when fill links to guru order. |
| **Unrealized PnL** | Positions × **marks** from `Cache` / `MarkPriceUpdate` when available; flag **mark_source**. |
| **Fill ratio** | filled_qty / submitted_qty per order or per run. |
| **Cancel / reject ratio** | terminal cancels / rejects / submits. |
| **Slippage vs intended price** | `avg_fill_px - intent_price_ref` (signed by side). |
| **Slippage vs book/mid** | When `book_constraint_fact` captured mid—**else** null with `not_applicable`. |
| **Time to intent** | `ts_submit_intent - ts_guru_event` (already partially in logs—formalize). |
| **Time to submit** | `ts_submit_order - ts_intent`. |
| **Time to ack / first fill / full fill** | from `order_lifecycle_fact` / `fill_fact` timestamps. |
| **Notional attempted / submitted / filled** | rollup per `correlation_id` and run. |
| **Lost notional by reason** | sum of would-have-been fills gated by **`delta_reason_code`** (skip/deny). |

**Hard limitations today (must be documented + closed):**

- **`notify_order_event`** does not record fills—**instrumentation gap**, not a reporting “defer.”
- **Unrealized** requires reliable marks for all non-flat instruments; same as Phase B ops (`phase_b_operational_validation.md`).
- **Legacy py-clob** may lack rich event stream—may need **polling** reconciliation facts or **mandatory** migration to framework path for full parity.

---

# 10. Summary outputs

Reports are organized by **question domain**, not phase:

| Section | Contents |
|---------|----------|
| **Run overview** | manifest, duration, mode, data quality flags, pipeline health |
| **Strategy behavior** | signals seen, accepts/skips, sizing distribution, policy reasons |
| **Guru-vs-us comparison** | §5 tables, under/over-copy, timing skew |
| **Execution quality** | slippage, time-to-fill, partial fills, timeout cancels, rejects |
| **Capital deployment** | snapshots, headroom, utilization, stale/unresolved exposure |
| **Risk / gate impact** | denials by gate, **lost notional**, concurrent cap hits |
| **Health and incidents** | RTDS, poll, gap-fill, cache warmup, adapter errors |
| **Market / token breakdown** | top problematic `token_id` / `instrument_id` |
| **Config deltas across runs** | diff of `config_snapshot` |
| **Anomaly summary** | duplicates, multi-source correlation, reconciliation failures |

**Artifacts:**

- **`summary.json`** — machine-readable full rollup + section stubs for UI.
- **`summary.md`** — operator narrative.
- **`facts.sqlite`** (or Parquet partitions) — **normalized tables** for SQL/Python; **CSV extracts** per facet.
- **Cross-run index** — optional `runs_index.sqlite` with manifest pointers for multi-run compare.

---

# 11. Storage and query design

### 11.1 Recommendation

**Hybrid (justified by completeness, not minimalism):**

1. **Durability during run:** **append-only JSONL** (`facts.jsonl`) or **WAL SQLite** per run—**writer thread + bounded queue**; both support crash-partial recovery. JSONL maximizes **ingest simplicity**; SQLite maximizes **mid-run query** (usually unnecessary).

2. **Canonical analytical store (post-run default):** **`{run_id}/run.sqlite`** with **typed tables** per §4 (indexes on `correlation_id`, `client_order_id`, `ts`, `token_id`, `instrument_id`). ETL: stream `facts.jsonl` → load SQLite (or write SQLite directly if single-writer proven stable).

3. **Scale / multi-strategy:** **Partition** by `run_id` directory; **global catalog** table with `strategy_name`, `started_at`, hashes; **retention policy** by age/size; **schema migrations** via `reporting_schema_version` + Alembic-like SQL scripts for the query DB only.

4. **Optional warehouse path:** export **Parquet** from SQLite or JSONL for heavy research—**not** required for platform truth.

### 11.2 Schema versioning

- **`run_manifest.reporting_schema_version`** — major contract.
- **`fact_schema_version`** per row or per table—increment on **breaking** field renames.
- **Retention:** operator-configured; **never** silently delete without manifest archive.

### 11.3 Backpressure

Bounded queue; **block with timeout** vs **drop**: **never drop** lifecycle/fill/risk/strategy facts—**block** risks latency; **recommended** separate **high-priority** queue lane for execution-critical facts and **soft cap** only on volumetric `portfolio_snapshot_fact` (sample with `sampled=true` flag if under extreme load—with **counter** of suppressed snapshots).

---

# 12. Codebase integration plan

| Fact family | Emit / ingest location |
|-------------|------------------------|
| `run_manifest`, `config_snapshot` | `scripts/run_guru.py`, `config/loaders.py`, `runtime/guru_compose.py` after validation |
| `guru_signal_fact`, benchmark | `data/guru_ingest_pipeline.py`, `guru_stream_actor.py` (would_emit), `guru_gap_fill.py` |
| `health_anomaly_fact` | `guru_rtds_ws.py`, `guru_stream_actor.py`, `guru_monitor.py`, `data_api_client.py` |
| `component_status_fact` | `guru_cache_warmup.py`, `guru_instrument_dynamic.py`, compose startup |
| `strategy_decision_fact`, `sizing_fact` | `strategy/copy_strategy.py` — **INFO-level facts**, not DEBUG-only |
| `risk_decision_fact` | `risk/configured.py` — allow + deny |
| `execution_intent_fact` | `copy_strategy.py` after `evaluate` approve |
| `normalization_fact`, `book_constraint_fact`, `execution_outcome_fact` | `execution/nautilus_guru_exec.py` (+ C3 helpers); `execution/polymarket_policy.py` |
| `order_lifecycle_fact`, `fill_fact` | **NEW:** `CopyStrategy.on_order_event` → reporting + `notify_order_event` |
| `position_fact`, `exposure_fact` | On **event** or periodic reader pull from `portfolio_exposure.py` / position reader |
| `account_snapshot_fact` | When `NautilusAccountSnapshotProvider.snapshot()` runs (risk or reporting tick) |
| `portfolio_snapshot_fact` | After `NautilusPortfolioExposureAggregator.aggregate` on schedule/trigger |
| `reconciliation_fact` | NEW small task: compare intent vs cache vs last known venue (define in adapter milestone) |
| `report_pipeline_health_fact` | `tyrex_pm/reporting/` writer |

**Package layout:** Implement core in **`src/tyrex_pm/reporting/`** (`RunContext`, `FactRecorder`, sinks, serializers). **Inject** `FactRecorder` from `build_guru_trading_node` into strategy, risk, execution port, and new event sink—**no globals**.

---

# 13. Gaps vs current implementation

| Category | Situation |
|----------|-----------|
| **Already in code, needs structured capture** | `event=` logs and **`ReasonCode`** enums—**promote** to facts without removing logs |
| **Logs only today** | Most skips; risk allows; C3 book details; conviction DEBUG |
| **Nautilus/framework visible, not recorded** | Order events, fills, cumulative qty, `OrderSnapshot` fields |
| **Exists in readers, not persisted** | `PortfolioExposureAggregate`, `AccountSnapshot`, `AllowanceSnapshot`, open order snapshots |
| **New instrumentation** | `on_order_event` reporting fan-out; explicit allow facts; normalization snapshots |
| **Adapter/runtime integration** | Reconciliation, venue reject reasons, guaranteed tag propagation—**may need adapter changes**; track per ticket |
| **Correlation** | COID ↔ `correlation_id` is **deterministic** on framework path—**document and test**; legacy path needs explicit mapping table in facts |

---

# 14. Deliverable shape

This document is the **parent implementation plan**. Deliverables include:

- **Retained:** fact/aggregate split, schema versioning, join keys, non-policy boundary, UI-oriented exports.
- **Replaced:** MVP framing, phase-centric summaries, optional lifecycle/fill/PnL, “implied accept” notion.
- **Architecture:** §3–4 canonical model, §5 guru-vs-us, §7 execution, §8 capital, §9 quality metrics, §11 storage.
- **Taxonomies:** §4 fact types, §5 `delta_reason_code`, §10 output sections.
- **Integration:** §12 map.
- **Validation:** Golden runs + fact-count contracts + cross-check **Nautilus Cache** samples on exit; compare rollups to **manual** spot checks until fully trusted.
- **Implementation order:** §15.

---

# 15. Prioritization rule

Order work by **information value and truthfulness**, not smallest diff:

1. **`run_manifest` + `config_snapshot` + `report_pipeline_health`** — without these, cross-run analysis is meaningless.
2. **`order_lifecycle_fact` + `fill_fact` + execution outcome** — **largest explanatory gap** between “we clicked submit” and “what the venue did.”
3. **`guru_trade_benchmark_fact` + `strategy_decision_fact` + `sizing_fact` + `risk_decision_fact` (allow+deny)** — full **why** chain vs guru.
4. **`normalization_fact` + `book_constraint_fact`** — execution-quality root causes.
5. **Capital **`portfolio_snapshot_fact` / `exposure_fact` / `account_snapshot_fact`**** — ties behavior to deployable capital.
6. **Guru ingest **`health_anomaly_fact`** + shadow **`guru_stream_would_emit`**** — platform feed truth.
7. **Reconciliation + PnL attribution refinements** — after core event spine is reliable.

---

# 16. Future extensibility

- **Core facts** (`order_*`, `fill_*`, `risk_*`, `execution_*`, `account_*`, `position_*`) use **`strategy_name` + `correlation_id`** as opaque business ids; **guru** is one importer.
- **Guru-specific** types: `guru_signal_fact`, `guru_trade_benchmark_fact`, ingest health tied to `GuruMonitorActor` / `GuruStreamActor`.
- **New strategies** supply their own **signal facts** implementing the same **join roles** (root signal row → decisions → orders).
- **Plugin boundary:** `FactRecorder.emit(kind: str, payload: dict)` with **registered validators** per `kind`—versioned independently.

---

**Related docs:** `Docs/Implementation/current_state.md`, `Docs/OPERATIONS.md`, `Docs/Implementation/logging_workflow_review.md`, `Docs/log_validation_playbook.md`, `Docs/CONFIG_MODEL.md`, `Docs/modules/reporting/README.md`, `Phase_B_planing.md` (B1 exposure), `plan_C3_Execution-Quality.md` (execution semantics reference—not reporting outline).
