# A0.5 Observe-Only Live Report (Post Timing Fix)

**Date (UTC):** 2026-07-10  
**Phase:** A0.5 observe_only (timing/PTB operational fixes)  
**Preflight:** `scripts/preflight_clock_sanity.py` (OS drift informational + TimeAuthority authoritative)  
**Batch:** `scripts/run_z_gap_observe_batch.py --prestart-seconds 60 --min-prestart-seconds 20`  
**Sidecar:** `scripts/chainlink_tick_logger.py` (feed=`chainlink`)

---

## 1. Executive Summary

| Metric | Pre-fix (2026-07-09) | Post-fix (2026-07-10) |
|--------|----------------------|------------------------|
| Windows attempted | 4 (1 partial + 3 full) | **3 full** (time-triggered batch) |
| Windows completed | 4 (`exit_code=0`) | **3** (`exit_code=0`, all `operational_pass=true`) |
| Dominant skip reason | `z_gap_clock_drift_exceeded` (100% windows) | **Downstream gates** (basis/feed/jump — **no clock drift skip**) |
| PTB K captured (usable) | 1/3 full windows | **3/3** (`ptb_lag_ms=0`, status=`observed`) |
| Late window joins | 2/3 started ~2 min late | **0** — prestarted 60s, overlapping Popen scheduler |
| TimeAuthority sync at run | N/A (static OS drift artifact) | **Flaky under startup load** (see §5) → **Fixed v2 (SNTP pre-feed)** |
| **A0.6 recommendation** | Blocked | **Not started** (A0.5 scope complete; operator may approve A0.6 separately) |
| **A0.5 timing fix** | — | **Accepted (observe-only)** — **clock_sync blocker closed** |

**Verdict:** A0.5 timing/PTB operational fixes are **accepted for observe-only**. Clock drift no longer wastes windows. PTB capture is reliable when prestarted. **Clock_sync at live startup is now reliable** (SNTP primary, pre-feed orchestration). **Do not start A0.6** in this change set — operator may proceed separately after review.

---

## Clock Sync Hardening Rerun (v2)

**Batch ID:** `z_gap_observe_clocksync_20260710`  
**Command:**

```bash
python scripts/preflight_clock_sanity.py --output var/reporting/z_gap/clock_sanity.json
python scripts/run_z_gap_observe_batch.py --windows 3 --prestart-seconds 60 --min-prestart-seconds 20 --name-prefix z_gap_observe_clocksync
```

### Preflight (2026-07-10T10:12:45Z, isolated)

| Field | Value |
|-------|-------|
| `sync_status` | **synced** |
| `source` | **sntp_time_cloudflare_com** |
| `samples_kept` | **3** |
| `time_authority_uncertainty_ms` | **3.25** |
| `enforce_gate_pass` | **true** |
| `sntp_offset_ms` | 249.5 |
| `binance_offset_ms` | 250.7 |
| `offset_disagreement_ms` | 1.2 |
| `os_drift_ms` | -561.4 (informational only) |

### 3-window result

| Run | Window (UTC) | exit | operational_pass | PTB status | PTB lag |
|-----|--------------|------|------------------|------------|---------|
| `z_gap_observe_clocksync_1783678500` | 10:15→10:20 | 0 | true | observed | **0ms** |
| `z_gap_observe_clocksync_1783678800` | 10:20→10:25 | 0 | true | observed | **0ms** |
| `z_gap_observe_clocksync_1783679100` | 10:25→10:30 | 0 | true | observed | **0ms** |

### Clock_sync facts (startup, all 3 runs)

| Run | sync_status | samples_kept | uncertainty_ms | source | SNTP offset | Binance offset | disagree_ms |
|-----|-------------|--------------|----------------|--------|-------------|----------------|-------------|
| 8500 | **synced** | **3** | **2.75** | sntp_time_cloudflare_com | 251.0 | 252.5 | 1.5 |
| 8800 | **synced** | **3** | **2.75** | sntp_time_cloudflare_com | 257.1 | 257.7 | 0.6 |
| 9100 | **synced** | **3** | **3.25** | sntp_time_cloudflare_com | 263.0 | 265.2 | 2.2 |

All runs: `sync_attempts=1`, `enforce_gate_pass=true`, `offset_disagreement_warning=false`. Logs confirm `TimeAuthority sampled before feeds` precedes CLOB bootstrap and signal feeds.

### Skip histogram (aggregate)

| Reason | 8500 | 8800 | 9100 |
|--------|------|------|------|
| `z_gap_feed_stale` | 2 | 2 | 2 |
| `z_gap_basis_exceeded_fresh_chainlink` | 3 | 11 | 2 |
| `z_gap_chainlink_stale_basis_untrusted` | 2 | 8 | 1 |
| `z_gap_jump_guard` | 0 | 2 | 0 |
| **`z_gap_clock_drift_exceeded`** | **0** | **0** | **0** |

### No-OMS proof

```text
oms_submit: 0
intent_created: 0
allocation_ledger: 0
z_gap_clock_drift_exceeded skips: 0
```

**Analysis artifact:** `var/reporting/z_gap/a0_5_observe_analysis.json`

---

## Timing Fix Rerun

**Batch ID:** `z_gap_observe_fix_20260710`  
**Command:**

```bash
python scripts/run_z_gap_observe_batch.py --windows 3 --prestart-seconds 60 --min-prestart-seconds 20 --name-prefix z_gap_observe_fix
```

### 3-window result

| Run | Window (UTC) | exit | operational_pass | PTB status | PTB lag |
|-----|--------------|------|------------------|------------|---------|
| `z_gap_observe_fix_1783675800` | 09:30→09:35 | 0 | true | observed | **0ms** |
| `z_gap_observe_fix_1783676100` | 09:35→09:40 | 0 | true | observed | **0ms** |
| `z_gap_observe_fix_1783676400` | 09:40→09:45 | 0 | true | observed | **0ms** |

### Skip histogram (aggregate)

| Reason | 5800 | 6100 | 6400 |
|--------|------|------|------|
| `z_gap_feed_stale` | 1 | 2 | 2 |
| `z_gap_basis_exceeded_fresh_chainlink` | 3 | 2 | 3 |
| `z_gap_chainlink_stale_basis_untrusted` | 1 | 0 | 2 |
| `z_gap_jump_guard` | 2 | 0 | 0 |
| **`z_gap_clock_drift_exceeded`** | **0** | **0** | **0** |

### Clock_sync summary (per run)

| Run | sync_status | samples_kept | offset_ms | uncertainty_ms | enforce_gate_pass | observe clock skip |
|-----|-------------|--------------|-----------|----------------|-------------------|-------------------|
| 5800 | failed | 0 | null | null | false | **0** |
| 6100 | failed | 0 | null | null | false | **0** |
| 6400 | failed | 0 | null | null | false | **0** |

**Preflight (2026-07-10T10:00:32Z, isolated):**

| Field | Value |
|-------|-------|
| `os_drift_ms` | -555.8 (informational) |
| `time_authority_offset_ms` | **247.7** |
| `time_authority_uncertainty_ms` | **124.5** |
| `sync_status` | **synced** |
| `samples_kept` | ≥1 (enforce_gate_pass=true) |
| `enforce_gate_pass` | **true** (TimeAuthority) |
| preflight `pass` | false (OS drift >500ms blocks enforce only) |

**Feed freshness:** ages computed from venue `source_ts` vs `TimeAuthority.corrected_now()` in `SignalStateStore` and `book_read.py`. Observe loop uses corrected epoch for τ and event-end cutoff.

### Sidecar validation (post-fix, 2026-07-10T10:00Z)

| Check | Result |
|-------|--------|
| Feed name | `chainlink` (correct) |
| Connects | yes (`wss://ws-live-data.polymarket.com`) |
| Writes `var/state/chainlink_ticks.jsonl` | yes (**79+ ticks** in ~90s) |
| Row schema | `source_ts`, `recv_ts`, `price` |
| Path handling | fixed (`Path` coercion) |
| Crash after startup | **no** |
| Retention | prune loop every 300s, `retention_s=7200` default |

Sample row:

```json
{"price": "64356.00739662453", "recv_ts": "2026-07-10T10:00:15.064365+00:00", "source_ts": "2026-07-10T10:00:14+00:00"}
```

### File-derived PTB validation

**pytest:** `tests/test_price_to_beat_tracker.py` — **9 passed**

| Test | Case |
|------|------|
| `test_derive_from_log_usable_k` | boundary_lag ≤5000 → `observed_from_log`, K non-null |
| `test_derive_from_log_late_k` | boundary_lag >5000 → `late` |
| `test_derive_from_log_no_matching_tick_returns_none` | no tick ≥ boundary → None |
| `test_register_no_log_tick_stays_pending` | no match → pending, no null-K observed |
| `test_never_emit_observed_with_null_k` | invariant: observed ⇒ K not None |
| `test_register_applies_log_derivation` | register loads K from log |
| `test_late_k_not_calibration_usable` | late excluded from calibration |
| `test_observed_from_log_is_calibration_usable` | log-derived usable K → calibration OK |

**Live log integration:** derived K=64356.01 from sidecar file (status `late` for 10:00:00 boundary because first tick was +14s — expected).

### No-OMS proof (3-window batch)

```text
oms_submit: 0
intent_created: 0
allocation_ledger: 0
z_gap_clock_drift_exceeded skips: 0
z_gap_clock_sync_failed skips: 0 (observe mode — gate passes with warning)
```

### Calibration sample handling

- 3 new rows in `calibration_samples.jsonl` with `calibration_usable=true`, `ptb_status=observed`
- 0 rows in `observe_infra_debug_samples.jsonl`

### Remaining blockers

None for A0.5 observe-only. Optional host hygiene (see §7 Host checklist).

**Analysis artifact:** `var/reporting/z_gap/a0_5_observe_analysis.json`

---

## 2. Run Inventory (Post-Fix Batch)

| Run name | Event window (UTC) | Prestart | market_id | PTB K | ptb_lag_ms | operational_pass |
|----------|-------------------|----------|-----------|-------|------------|------------------|
| `z_gap_observe_fix_1783675800` | 09:30 → 09:35 | ~60s | `btc_5m_20260710_0935` | 64374.75 | **0** | true |
| `z_gap_observe_fix_1783676100` | 09:35 → 09:40 | ~60s | `btc_5m_20260710_0940` | 64369.20 | **0** | true |
| `z_gap_observe_fix_1783676400` | 09:40 → 09:45 | ~60s | `btc_5m_20260710_0945` | 64401.34 | **0** | true |

**Artifacts:**

| Run | facts.jsonl |
|-----|-------------|
| `z_gap_observe_fix_1783675800` | `var/reporting/runs/z_gap_observe_fix_1783675800/facts.jsonl` |
| `z_gap_observe_fix_1783676100` | `var/reporting/runs/z_gap_observe_fix_1783676100/facts.jsonl` |
| `z_gap_observe_fix_1783676400` | `var/reporting/runs/z_gap_observe_fix_1783676400/facts.jsonl` |

**Aggregates:** `var/reporting/z_gap/calibration_samples.jsonl` (3 new usable rows)  
**Clock sanity:** `var/reporting/z_gap/clock_sanity.json`  
**Skip report:** none (no `z_gap_window_skipped_late_start` — first planned 09:25 window skipped implicitly because batch launched after its wake time)

---

## 3. Fix 1 — TimeAuthority Results

### Implementation (v2 — SNTP primary)

- Module: `src/tyrex_pm/runtime/time_authority.py`
- **Primary:** SNTP client to `time.cloudflare.com` (UDP/123, no new dependency)
- **Cross-check:** Binance `/api/v3/time` via persistent HTTP session (warm-up discarded)
- **Sampling:** best-3-of-7 by RTT; `uncertainty_ms = max(kept RTT)/2`; retry with backoff (3 attempts)
- **Orchestration:** `sample_offset()` runs in `run_once.py` **before** CLOB bootstrap / signal feeds; `mark_feeds_started()` guards feed entry points
- `clock_sync` fact emits `sntp_offset_ms`, `binance_offset_ms`, `offset_disagreement_ms` (+ warn if >250ms)
- Preflight / enforce gate: `sync_status` + `uncertainty_ms` (authoritative); `os_drift_ms` informational only

### Live evidence (v2 batch)

| Run | sync_status | samples_kept | uncertainty_ms | source | enforce_gate_pass |
|-----|-------------|--------------|----------------|--------|-------------------|
| 1783678500 | **synced** | **3** | **2.75** | sntp_time_cloudflare_com | true |
| 1783678800 | **synced** | **3** | **2.75** | sntp_time_cloudflare_com | true |
| 1783679100 | **synced** | **3** | **3.25** | sntp_time_cloudflare_com | true |

**v1 root cause (resolved):** Binance-only sampling under concurrent startup pushed RTT >250ms hard discard → `samples_kept=0`. SNTP (~5ms RTT) + pre-feed sequencing eliminates contention.

### Skip histogram (v2)

| Run | Skip reasons (count) |
|-----|---------------------|
| 8500 | feed_stale:2, chainlink_stale:2, basis_exceeded:3 |
| 8800 | feed_stale:2, basis_exceeded:11, chainlink_stale:8, jump_guard:2 |
| 9100 | feed_stale:2, basis_exceeded:2, chainlink_stale:1 |

### Feed freshness (corrected time)

- `SignalStateStore.snapshot()` ages now use **source_ts** vs corrected `now` passed from `TimeAuthority`
- Book ages in `book_read.py` use `time_authority.corrected_now()`
- τ loop uses `corrected_epoch()` for event-end cutoff and fair-value input

---

## 7. Host Checklist (Operator — not code)

Before live runs on Windows, align OS clock with NTP peers (reduces informational `os_drift_ms` warnings):

```powershell
# 1. Start Windows Time service and set auto-start
sc.exe config w32time start= auto
net start w32time

# 2. Configure NTP peers (example: Windows defaults + Cloudflare)
w32tm /config /manualpeerlist:"time.cloudflare.com time.windows.com" /syncfromflags:manual /update
w32tm /config /update

# 3. Force resync
w32tm /resync /force

# 4. Verify + run preflight; attach output to run log
w32tm /query /status
python scripts/preflight_clock_sanity.py --output var/reporting/z_gap/clock_sanity.json
```

**Current host state (2026-07-10):** `w32tm /query /status` reports service not started; TimeAuthority SNTP gate still passes (`uncertainty_ms` ≈ 3ms). OS drift vs Binance (~560ms) is informational only.

---

## 3b. Fix 1 — TimeAuthority Results (v1 archive)

---

## 4. Fix 2 — PTB & Scheduler Results

### Time-triggered scheduler

- `scripts/run_z_gap_observe_batch.py` launches at `event_start - 60s` via `Popen` (overlapping allowed)
- Skips if `now > event_start - 20s` with `z_gap_window_skipped_late_start` JSONL row
- Batch waited for scheduled wakes (not sequence-after-exit)

### Startup timing (representative)

| Stage | Run 5800 | Run 6100 | Run 6400 |
|-------|----------|----------|----------|
| total_startup_ms | 1563 | 3984 | 4047 |
| rtds_connect_ms | 0 | 0 | 15 |
| ptb_tracker_registered_ms | 0 | 0 | 15 |

**Previous ~2 min late-start cause:** sequence-triggered batch (next window started only after prior process exited) + short `prestart_seconds=25`. Fixed by time-triggered Popen scheduling + `prestart_seconds=60`.

### PTB capture

| Run | ptb_status | K | boundary_lag_ms | calibration_usable |
|-----|------------|---|-----------------|-------------------|
| 5800 | observed | 64374.75 | 0 | true |
| 6100 | observed | 64369.20 | 0 | true |
| 6400 | observed | 64401.34 | 0 | true |

- **Atomicity:** observed facts always carry non-null K (pending fact with null K still emitted once at registration — acceptable lifecycle; terminal never shows `observed` + null K)
- **Late/missing PTB:** none in this batch; infra-debug file unused

### Chainlink sidecar

- Validated post-fix (2026-07-10T10:00Z): feed=`chainlink`, **79+ ticks** written to `var/state/chainlink_ticks.jsonl`, no crash, retention prune every 300s (`retention_s=7200`)
- Initial batch run used pre-fix sidecar (failed); live PTB captured via RTDS ingest; file-derivation path now proven

---

## 5. Model / Edge / Calibration

| Criterion | Result |
|-----------|--------|
| σ ready | Yes (~320s all windows) |
| Fair value / z | Yes (`last_z` populated) |
| Fee model | `polymarket_dynamic_fd_v1` resolved |
| Edge evaluated | Yes (155–290 facts/window) |
| Calibration usable rows | 3 appended with `calibration_usable=true`, PTB observed |
| No OMS | **0** `oms_submit`, `intent_created`, `allocation_ledger` |

---

## 6. Success Criteria vs A0.6

| # | Criterion | Met? |
|---|-----------|------|
| 1 | TimeAuthority sync succeeds | **Yes** (v2: 3/3 SNTP pre-feed) |
| 2 | uncertainty_ms ≤ 50 at startup | **Yes** (2.75–3.25ms) |
| 3 | Observe no longer skips on OS drift | **Yes** |
| 4 | PTB K in prestarted windows | **Yes (3/3)** |
| 5 | K boundary lag ≤ 5000ms | **Yes (0ms)** |
| 6 | Late/missing excluded from calibration | **Yes** (no such samples) |
| 7 | σ ready | **Yes** |
| 8 | Fair value computes | **Yes** |
| 9 | Fee model resolves | **Yes** |
| 10 | Edge evaluates | **Yes** |
| 11 | Skip histogram beyond clock drift | **Yes** |
| 12 | No OMS path touched | **Yes** |

**Recommendation: proceed to A0.6?** **Not in this change set** — A0.5 complete including clock_sync blocker. Operator may approve A0.6 separately.

---

## 8. A0.5 Timing Fix Final Summary

### Files changed (clock sync v2)

| Area | Files |
|------|-------|
| TimeAuthority | `src/tyrex_pm/runtime/time_authority.py` (SNTP primary, best-3-of-7, pre-feed guard) |
| Orchestration | `src/tyrex_pm/runtime/run_once.py`, `market_data_runtime.py`, `signal_feed_runtime.py` |
| Preflight gates | `src/tyrex_pm/runtime/z_gap_clock_sanity.py`, `z_gap_preflight.py`, `scripts/preflight_clock_sanity.py` |
| Observe loop | `src/tyrex_pm/runtime/z_gap_run.py` (no post-feed resync fallback) |
| Tests | `tests/test_time_authority.py`, `tests/test_clock_sanity.py`, `tests/test_z_gap_config.py` |

### Commands run (v2)

```bash
python -m pytest tests/test_time_authority.py tests/test_clock_sanity.py tests/test_z_gap_config.py tests/test_z_gap_entry_eval.py -q   # 39 passed
python scripts/preflight_clock_sanity.py --output var/reporting/z_gap/clock_sanity.json
python scripts/run_z_gap_observe_batch.py --windows 3 --prestart-seconds 60 --min-prestart-seconds 20 --name-prefix z_gap_observe_clocksync
python scripts/analyze_z_gap_observe_runs.py z_gap_observe_clocksync_1783678500 z_gap_observe_clocksync_1783678800 z_gap_observe_clocksync_1783679100 --output var/reporting/z_gap/a0_5_observe_analysis.json
```

### Files changed (v1 timing fix)

| Area | Files |
|------|-------|
| TimeAuthority | `src/tyrex_pm/runtime/time_authority.py` |
| Runtime | `src/tyrex_pm/runtime/z_gap_run.py`, `run_once.py`, `coordinator.py` |
| Gates / facts | `src/tyrex_pm/strategies/z_gap/entry_eval.py`, `reporting/schema_v2.py` |
| Feed ages | `src/tyrex_pm/state/signal_state_store.py`, `market_data/book_read.py` |
| Preflight | `src/tyrex_pm/runtime/z_gap_clock_sanity.py`, `scripts/preflight_clock_sanity.py` |
| PTB | `src/tyrex_pm/ingestion/price_to_beat_tracker.py`, `runtime/signal_feed_runtime.py` |
| Sidecar | `src/tyrex_pm/runtime/chainlink_tick_logger.py`, `scripts/chainlink_tick_logger.py` |
| Calibration | `src/tyrex_pm/strategies/z_gap/calibration_samples.py` |
| Scheduler | `scripts/run_z_gap_observe_batch.py` |
| Tests | `tests/test_time_authority.py`, `tests/test_price_to_beat_tracker.py` (+ updates) |

### Commands run

```bash
python -m pytest tests/test_price_to_beat_tracker.py tests/test_time_authority.py -q   # 18 passed
python scripts/preflight_clock_sanity.py
python scripts/chainlink_tick_logger.py                                              # sidecar validation
python scripts/run_z_gap_observe_batch.py --windows 3 --prestart-seconds 60 --min-prestart-seconds 20 --name-prefix z_gap_observe_fix
python scripts/analyze_z_gap_observe_runs.py z_gap_observe_fix_1783675800 z_gap_observe_fix_1783676100 z_gap_observe_fix_1783676400 --output var/reporting/z_gap/a0_5_observe_analysis.json
```

*Stop here. A0.6 not started. A0.5 clock_sync blocker closed.*
