# C1 validation review — event-driven guru ingestion

**Date:** 2026-04-04  
**Parent plan:** `Docs/Implementation/plan_C1_Time-to-Follow.md`  
**Purpose:** Decide whether C1 moves from **implementation-complete** to **operationally trusted**.

---

## Executive verdict

| Readiness level | Supported by this review? |
|-----------------|----------------------------|
| Not ready | No — core path is implemented and RTDS spike succeeded. |
| **Ready for shadow only** | **Yes** — with mandatory operator follow-ups (wallet match, soak, shadow log correlation). |
| Ready for RTDS-primary canary | **Partially** — spike + code review support a **limited** canary; **not** fully proven (no live shadow correlation); multi-leg dedup addressed in code (`tx:asset`). |
| Ready for broader primary use | **No** — rate (~83 msg/s observed) and lack of long-run / shadow coverage evidence block this. |

**Recommended next step:** Run **`guru_ingest_mode: rtds_shadow`** with real guru + logs; reconcile `guru_signal_emitted` (poll) vs `guru_stream_would_emit` (dedup id is **`transactionHash:asset`** when tx present); then **short** `rtds_primary` canary with tight monitoring. Operator steps: **`Docs/Implementation/c1_shadow_run_guide.md`**.

---

## Code review checklist (architecture)

| Item | Result |
|------|--------|
| `GuruTradeSignal` unchanged | ✅ `src/tyrex_pm/core/types.py` |
| `GURU_TRADE_TOPIC` unchanged | ✅ `guru_monitor.GURU_TRADE_TOPIC` reused by stream + pipeline |
| `CopyStrategy` thin | ✅ Only subscription, policies, risk, port + latency logging |
| No C2/C3 in C1 modules | ✅ No sizing/execution-quality policy added to ingest |
| Guru not via Nautilus MARKET/USER WS | ✅ RTDS separate client (`guru_rtds_ws.py`) |
| Dedup identity: `transactionHash:asset` then composite | ✅ `guru_parse.ingest_source_trade_id` — same tx, different `asset` distinct ids |
| Mode state machine | ✅ `GuruIngestRuntimeState` — `poll_only` / `rtds_shadow` / `rtds_primary`; fallback only activates in primary; shadow does not call `activate_fallback_poll` (mode guard) |
| Single publisher in primary when healthy | ✅ `stream_should_publish` xor `poll_should_publish` for primary path |
| Queue / drain latency | ⚠️ Default **50 ms** drain timer adds small bounded delay vs raw WS; acceptable for C1 if documented |
| Stream shutdown | ✅ `GuruStreamActor.on_dispose` stops worker + `stop_join` |

---

## Gate 0 — Poll baseline

### Executed

- **Tests:** `pytest` subset — `test_guru_actor_mocked`, `test_copy_strategy_shadow`, `test_guru_rtds_parse`, `test_guru_ingest_state`, `test_compose_shadow_builds` — **20 passed**.  
- **Code inspection:**
  - `GuruSignalPipeline.try_publish` logs `event=guru_signal_emitted` with `source=poll|rtds|gap_fill`, `ts_recv_ms`, `ts_emit_ms`, `ts_event_ms`, `detection_to_emit_ms` (stream/gap path extras).  
  - `CopyStrategy` logs `shadow_order_intent` / `live_order_intent` with `ts_event_ms`, `ts_signal_received_ms`, `ts_submit_ms`, `detection_to_submit_ms`, `signal_to_submit_ms`.

### Not executed here

- Full **`run_guru.py`** session with real Data API + production logs (no guru activity captured in this environment).

### Baseline summary

- **Instrumentation:** present in code and exercised by unit/integration tests.  
- **Obvious defects:** none found in poll + pipeline wiring for `poll_only` default.

---

## Gate 0.5 — RTDS spike

### Executed

- **Script:** `scripts/spike_rtds_activity.py --duration 15`  
- **Evidence:** see populated **`Docs/Implementation/spike_C1_rtds_report.md`**.

### Findings (high level)

- Connect + unfiltered subscribe **work**.  
- **~1242 messages / 15 s** (~**83/s**).  
- **`proxyWallet`**, **`transactionHash`**, **`asset`**, **`side`**, **`price`**, **`size`**, **`timestamp`** present — sufficient for `GuruTradeSignal` mapping.  
- **Same `transactionHash`, different `asset`** observed — addressed by **`transactionHash:asset`** `source_trade_id` when tx present.  
- Filtered subscription **not** re-verified in automation (manual).

### Manual remaining

- Spike with **`--wallet <guru>`** during known guru activity.  
- Optional **filtered** subscribe experiment.  
- **Hours-long** reconnect/stall characterization.

---

## Gate 1 — Shadow (`rtds_shadow`)

### Executed (partial)

- **Static analysis:**
  - `GuruIngestRuntimeState.stream_should_publish()` is **False** for `rtds_shadow`.  
  - `GuruStreamActor._handle_drain`: only **`pipe.try_publish`** when `stream_should_publish()`; shadow branch logs **`guru_stream_would_emit`** only — **no** `msgbus.publish` from stream.  
  - `poll_should_publish()` is **True** in shadow — **`GuruMonitorActor`** remains sole publisher to `GURU_TRADE_TOPIC`.

### Not executed here

- Live **`node.run()`** with `guru_ingest_mode: rtds_shadow`, real guru, and log correlation (coverage %, timing delta).

### Shadow summary

- **Architecture:** meets “poll publishes, stream compares only.”  
- **Operational proof:** **pending** operator run.  
- **Canary justification:** **premature** until shadow logs show acceptable coverage; **early RTDS vs poll timing** not measured here.

---

## Gate 2 — RTDS primary canary

### Executed

- **Not run** — depends on Gate 1 live evidence and dedup policy.

### Preconditions (from plan + this review)

Before canary:

1. Acceptable **spike report** for purpose — **conditional** (see dedup + rate).  
2. Shadow **coverage** acceptable.  
3. **No double publish** — code review OK; **runtime** confirm under fallback transitions.  
4. **Gap-fill + dedup** — `gap_fill_resilient` uses same `GuruSignalPipeline` / `ingest_source_trade_id`; duplicate risk same as poll if REST returns overlapping rows (mitigated by dedup store).

---

## Follow-up fixes / decisions (before broader rollout)

1. **Dedup key:** Implemented as **`f"{tx}:{asset}"`** when `tx` present; composite fallback unchanged.  
2. **Ops:** CPU / queue depth monitoring under **~80+ msg/s** drain.  
3. **Manual:** Shadow run + grep correlation; soak primary.  
4. **Optional:** Run **filtered** RTDS spike from bash or `cmd` to avoid PowerShell JSON escaping issues.

---

## Evidence index

| Artifact | Path |
|----------|------|
| Spike detail | `Docs/Implementation/spike_C1_rtds_report.md` |
| Stream actor | `src/tyrex_pm/data/guru_stream_actor.py` |
| Ingest state | `src/tyrex_pm/data/guru_ingest_state.py` |
| Pipeline / logs | `src/tyrex_pm/data/guru_ingest_pipeline.py` |
| Copy latency logs | `src/tyrex_pm/strategy/copy_strategy.py` |
| Compose wiring | `src/tyrex_pm/runtime/guru_compose.py` |
| Spike script | `scripts/spike_rtds_activity.py` |
