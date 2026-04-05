# C1 closeout note — Time-to-Follow

Concise closure artifact for **Phase C / C1**: event-driven guru ingestion replacing poll-bound detection as the primary path.

---

## 1. Objective

C1 was meant to **materially reduce time-to-follow** by moving guru detection off the **`guru_poll_interval_seconds` ceiling** and onto **RTDS `activity` / `trades`**, while keeping downstream **signal → sizing → risk → execution** behavior unchanged. Polling remains as **shadow, fallback, and recovery**, not as the latency architecture.

---

## 2. Final architecture

- **RTDS primary:** `GuruStreamActor` (`src/tyrex_pm/data/guru_stream_actor.py`) subscribes unfiltered `activity`/`trades`, client-filters by **`proxyWallet`** vs `guru_wallet_address`, normalizes via `guru_rtds_parse` / `guru_parse`, and participates in shared dedup + ingest (`guru_ingest_pipeline.py`).
- **Poll:** `GuruMonitorActor` (`guru_monitor.py`) unchanged role — **shadow** (`rtds_shadow`: poll publishes, stream `guru_stream_would_emit` only), **fallback** (`guru_ingest_fallback_*` when primary stalls), **recovery** / gap-fill (`guru_gap_fill.py` after reconnect).
- **Stable contract:** `GuruTradeSignal` (`core/types.py`) and **`GURU_TRADE_TOPIC`** (`guru_monitor.py`) — strategy consumes the same topic and type; **`CopyStrategy`** does not absorb ingest logic.
- **Dedup identity:** `ingest_source_trade_id` (`guru_parse.py`) uses **`transactionHash:asset`** when `transactionHash` is present so multi-leg txs are not collapsed; ingest uses `GuruDedupStore` + optional persistence (`RuntimeSettings.guru_dedup_state_path` / strategy override).
- **Operational resilience:** RTDS **liveness**, **reconnect** backoff, **fallback** activation/clear, **gap-fill** after reconnect — wired in runtime compose (`guru_compose.py`) and stream worker config.

---

## 3. Validation evidence

Artifacts **in this repo** (concrete):

| Stage | What | Where |
|-------|------|--------|
| **Spike** | Connection, subscribe envelope, message rate (~83 msg/s sample), payload keys incl. `proxyWallet` / `transactionHash`, **`tx:asset` dedup** rationale | `Docs/Implementation/spike_C1_rtds_report.md` (2026-04-04); runner `scripts/spike_rtds_activity.py` |
| **Wallet identity** | Operator procedure: match YAML `guru_wallet_address` to RTDS **`proxyWallet`**; spike with `--wallet`; compose log `guru_rtds_wallet_identity` / stream start | `Docs/Implementation/c1_shadow_run_guide.md` § *Wallet identity*, § A |
| **Shadow comparison** | Poll publishes vs stream `guru_stream_would_emit`; correlation id alignment; post-run summary | `c1_shadow_run_guide.md` § C; **`python scripts/guru_shadow_report.py logs/<mode>/run_nautilus.log`**; isolated state preset `config/runtime/rtds_shadow.yaml` |
| **Primary canary** | RTDS/signal emissions, duplicate detection, fallback/gap-fill/latency summaries | `c1_shadow_run_guide.md` § E; **`python scripts/guru_primary_report.py logs/<mode>/run_nautilus.log`** (exit **2** on duplicate/mixed-source issues documented in script) |
| **Long primary soak** | Hours-long **operator** runs; stall/recovery and duplicate behavior validated from **local** Nautilus logs (e.g. `logs/live/run_nautilus.log` from `run_guru.py`) — **not** committed as binary evidence | Same report script + grep per `Docs/OPERATIONS.md` |

**Truthful boundary:** The spike report records **environment-level** RTDS behavior and Tyrex parser alignment; **guru-specific** wallet match and **multi-hour** soak are **operator-attested** using the guides and log paths above, not a single frozen log file in git.

---

## 4. Operational conclusion

**Consider validated (for this guru / deployment) when** wallet spike, shadow coverage/timing, healthy duplicate semantics, and **long `rtds_primary` soak** with acceptable fallback/reconnect/gap-fill have been performed per `c1_shadow_run_guide.md` and primary/shadow report scripts.

**Operational watch items (carry forward):**

- **Unfiltered ingest rate** — CPU/queue/drain; tune `guru_stream_queue_drain_interval_ms` and host capacity (`spike_C1_rtds_report.md`, stream actor).
- **Multi-leg same tx** — dedup is **`tx:asset`**; venue semantics and follow-policy edge cases documented as residual risk in spike report § *Duplicate `transactionHash`*.
- **Stall-with-open-socket** — short spike did not stress; production relies on **liveness timeout + reconnect**; monitor `guru_rtds_*` / fallback lines in Nautilus log.

**Normal operating mode:** **`guru_ingest_mode: rtds_primary`** on production runtime YAML (see `config/runtime/live_polymarket.yaml` pattern and `Docs/OPERATIONS.md` § Guru ingestion).

**Polling’s role:** **Timer-driven REST** for shadow comparison, **publisher of last resort** on stream stall (fallback), **gap-fill** after reconnect, and **`poll_only`** rollback / dev baseline.

---

## 5. Handoff to C2

C1 is **no longer the blocking Phase C workstream** for teams that have completed the evidence chain above. **C2 — Capital Allocation** (conviction-weighted sizing + minimum-follow-notional policy gate) is the **active next** Phase C slice on top of stable `GuruTradeSignal` ingress; see `plan_C2_Capital-Allocation.md` and `c2_validation_readiness_review.md`.
