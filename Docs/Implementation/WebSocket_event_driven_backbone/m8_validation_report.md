# M8 WS-primary validation report (Group E)

**Date:** 2026-06-30 (updated Group E4)  
**Status:** `M8_VALIDATED` — E4 `m8_validation_002c` passes all hard gates with split freshness metrics  
**M9:** Complete — see [baseline_vs_ws_primary_report.md](baseline_vs_ws_primary_report.md)

---

## Run artifacts

| Field | Value |
|-------|--------|
| Run name | `m8_validation_001` |
| Run ID | `1ff93bcf-0270-4510-90e2-b7fc4c18d0d0` |
| Scenario | `config/scenarios/m8_ws_primary_validation.yaml` |
| Strategy | `config/strategies/paired_binary.yaml` (params unchanged) |
| Strategy YAML SHA256 (prefix) | `cf2b1b7c2dbe15d9` |
| Scenario YAML SHA256 (prefix) | `1c094653f0ccd4a5` |
| Git commit | `ac5191d76aa26eea0c93cf8f004c0ba3e6cdf44e` |
| Execution mode | `live` |
| Artifacts dir | `var/reporting/runs/m8_validation_001/` |

Collected files:

- `facts.jsonl` (7,683 facts)
- `manifest.json`
- `run_summary.json`
- `m8_validation_summary.json` / `.md` (analyzer output)
- Runtime logs (process stdout; not archived separately)

---

## Step 1 — Pre-run verification

Preflight (`scripts/m8_preflight_check.py`) **PASSED**:

| Check | Result |
|-------|--------|
| `websocket.primary_enabled: true` | OK |
| `websocket.shadow_enabled: false` | OK |
| `rest.poll_enabled: false` | OK |
| `rest.bootstrap_on_startup: true` | OK |
| `rest.recovery_on_reconnect: true` | OK |
| `quality.enforcement_mode: enforce` | OK |
| `quality.require_ws_primary_for_entry: true` | OK |
| `quality.allow_rest_recovery_for_entry: false` | OK |
| `position_size: 5` (tiny/safe) | OK |
| Wallet readiness | OK (~$26 USDC at preflight) |

Base scenario `config/scenarios/live_paired_binary_ws_primary.yaml` exists and matches the WS-primary posture above. Validation used overlay `m8_ws_primary_validation.yaml` (adds `max_runtime_s: 1900`, active CLOB token IDs, tiny risk caps).

---

## Step 2 — Live validation run

**Observed duration:** ~152 s (run stopped during analysis; target was ≥1,800 s or one lifecycle).

**Infrastructure observed:**

- `ws_primary_cutover` fact emitted; WS writes authoritative store.
- `rest_poll_disabled` fact emitted (`ws_primary_healthy_mode`).
- Readiness chain: `starting → rest_bootstrapped → ws_connected → ws_book_received → both_legs_ready → quality_pass → trading_enabled` within ~1 s.
- All steady-state `decision_snapshot` books sourced from `websocket` with quality `pass` (p95 book age 82 ms).
- **Zero** `oms_submit` / paired-binary entry submits.
- **64** preflight rejections: `notional_below_min` on YES leg (ask ~0.112 × 5 shares = $0.56 < `min_usd: 1`).

**Anomaly (fixed post-run):** Immediately after `trading_enabled`, readiness transitioned to `paused` / `rest_recovery` without a WS disconnect. Root cause: duplicate WS book-hash events incorrectly invoked REST gap recovery. See Hardening below.

---

## Step 3 — M8 acceptance criteria

| # | Check | Status | Detail |
|---|--------|--------|--------|
| 1 | Run duration ≥30m OR complete lifecycle | **NOT_OBSERVED** | `duration_s≈152`; no lifecycle. **Requires another run** (≥30 min or lifecycle). |
| 2 | p95 decision `book_age_ms` < 750 ms | **PASS** | p95 = 82 ms (n=1,900) |
| 3 | p99 decision `book_age_ms` < 1500 ms | **PASS** | p99 = 90 ms |
| 4 | Zero REST-sourced **new entries** | **PASS** | 0 OMS/submit facts from `rest_bootstrap` / `rest_poll` / `rest_recovery`. Two startup `entry_eval` observability snapshots on `rest_bootstrap` only (no risk opened). |
| 5 | Material decisions carry id/snapshot/source/age/verdict | **PASS** | No missing fields on material snapshots |
| 6 | FAK planner evidence complete | **NOT_OBSERVED** | No entries → no FAK plans. **Requires lifecycle run** on a market that passes preflight notional. |
| 7 | REST poll disabled in healthy WS-primary mode | **PASS** | `rest_poll_disabled` fact present; no steady-state poll loop |
| 8 | WS disconnect blocks new entries | **NOT_OBSERVED** | No live disconnect. Entry blocks seen were `readiness_not_trading_enabled` from spurious `rest_recovery` pause (bug, now fixed). **Requires controlled disconnect test or longer run with fix.** |
| 9 | Reconnect: REST_RECOVERY + fresh WS both legs → TRADING_ENABLED | **NOT_OBSERVED** | Spurious pause observed; no resume to `trading_enabled` after real gap. **Requires disconnect/reconnect drill on fixed build.** |
| 10 | Rollback via config | **PASS** | `tests/test_m8_rollback_drill.py`, `tests/test_cutover_acceptance_criteria.py` |
| 11 | `paired_binary_tick_source`: `event_wake` + `timer` | **NOT_OBSERVED** | Only `event_wake` (1,900) in ~152 s — expected when WS is very active. **Acceptable for short run; verify timer on ≥30 min run or quiet market.** |
| 12 | `latency_chain` on venue ack/fill/user-WS events | **NOT_OBSERVED** | No fills. **Requires lifecycle with live orders.** |

### NOT_OBSERVED acceptability

| Criterion | Acceptable without re-run? |
|-----------|---------------------------|
| 1 | **No** — hard gate |
| 6, 12 | **No** — need at least one paired-binary lifecycle |
| 8, 9 | **No** — need disconnect/reconnect observation (live or simulated integration test) |
| 11 | **Partially** — timer ticks may be absent under heavy WS activity; still verify on long run |

---

## Step 4 — Analyzer metrics (`scripts/validate_m8_ws_primary_run.py`)

```
python scripts/validate_m8_ws_primary_run.py var/reporting/runs/m8_validation_001 \
  --json-out var/reporting/runs/m8_validation_001/m8_validation_summary.json \
  --md-out var/reporting/runs/m8_validation_001/m8_validation_summary.md
```

| Metric | Value |
|--------|--------|
| Decision snapshots | 1,900 |
| Book age p50 / p95 / p99 | 0 / 82 / 90 ms |
| Source distribution | `websocket`: 1,898; `rest_bootstrap`: 2 (startup observability only) |
| Quality verdicts | `pass`: 1,898; `reject_decision`: 2 (startup bootstrap) |
| REST OMS entry violations | **0** |
| OMS submit count | 0 |
| Tick sources | `event_wake`: 1,900; `timer`: 0 |
| Planner evidence facts | 0 |
| Latency chain facts | 0 |
| Readiness transitions | 7 |
| Entry health blocks | 1,836 (`readiness_not_trading_enabled` after spurious pause) |

---

## Step 5 — Rollback drill

Config-only rollback verified in unit tests (no code change required):

```yaml
runtime:
  market_data:
    websocket:
      primary_enabled: false
      shadow_enabled: true
    rest:
      poll_enabled: true
    quality:
      enforcement_mode: observe_only
```

**Result:** REST authoritative again; WS shadow-only; quality does not block live decisions. Tests: `tests/test_m8_rollback_drill.py` (2 passed).

---

## Step 6 — Hardening (M8 infrastructure)

### Fixed: duplicate WS hash triggered REST recovery pause

**Symptom:** `trading_enabled → paused (rest_recovery)` on duplicate book-hash WS messages, blocking all entries.

**Root cause:** `market_ws_ingest.py` invoked `on_gap_recovery` for any `_handle_sequence` `False` return, including benign duplicates.

**Fix:** Invoke REST recovery only when `on_sequence_gap` fires (true out-of-order gap), not on duplicate hash skip.

**Files:** `src/tyrex_pm/ingestion/market_ws_ingest.py`, `tests/test_ws_primary_event_ordering.py`

### Analyzer fix: criterion 4 REST entry counting

Count REST violations only on OMS/submit fact types, not observability-only `entry_eval` snapshots on bootstrap.

**Files:** `scripts/validate_m8_ws_primary_run.py`, `tests/test_validate_m8_ws_primary_run.py`

---

## Step 7 — Tests run (post-hardening)

```
pytest tests/test_ws_primary_event_ordering.py \
       tests/test_validate_m8_ws_primary_run.py \
       tests/test_m8_rollback_drill.py \
       tests/test_ws_primary_cutover.py \
       tests/test_rest_fallback_policy.py \
       tests/test_cutover_acceptance_criteria.py \
       tests/test_ws_primary_reconnect_gap.py -q
```

**Result:** 26 passed

---

## Confirmations

| Item | Status |
|------|--------|
| WS authoritative in WS-primary mode | **Yes** — steady-state snapshots from `websocket` |
| REST did not open new risk | **Yes** — 0 OMS submits from REST sources |
| Readiness blocks until WS books valid | **Yes** — chain to `trading_enabled` only after both-leg WS_PRIMARY + quality PASS |
| Decision evidence on snapshots | **Yes** — id, snapshot_id, source, book_age_ms, verdict present |
| REST poll disabled (healthy mode) | **Yes** — `rest_poll_disabled` fact |
| Rollback works | **Yes** — config-only, tested |
| Strategy logic/params unchanged | **Yes** — strategy hash `cf2b1b7c2dbe15d9`; only scenario overlay (tokens, runtime window) |

---

## Blockers for `M8_VALIDATED`

1. **Duration:** Run stopped at ~152 s; need ≥30 min **or** complete lifecycle on fixed build.
2. **Market selection:** Current validation tokens (Spain WC) fail `notional_below_min` on YES leg at `position_size: 5`. Choose an active paired market where **both** legs satisfy `min_usd: 1` without changing strategy params (scenario token overlay only).
3. **Lifecycle / latency / planner:** No fills → criteria 6 and 12 not observed.
4. **Disconnect / reconnect:** Criteria 8–9 not live-observed; run controlled drill after duplicate-hash fix.
5. **Timer tick mix:** Re-check on long run (criterion 11).

---

## Group E2 — `m8_validation_002` (2026-06-30)

### Market selection (Step 1)

| Field | Value |
|-------|--------|
| Market | Will bitcoin hit $1m before GTA VI? |
| Slug | `will-bitcoin-hit-1m-before-gta-vi-872-424` |
| YES token | `105267568073659068217311993901927962476298440625043565106676088842803600775810` |
| NO token | `91863162118308663069733924043159186005106558783397508844234610341221325526200` |
| YES bid/ask @ preflight | 0.495 / 0.496 (depth 11,140; notional@5 = $2.48) |
| NO bid/ask @ preflight | 0.504 / 0.505 (depth 122,444; notional@5 = $2.53) |
| Scenario | `config/scenarios/m8_ws_primary_validation_002.yaml` |
| Scenario SHA256 (prefix) | `dbc775ed4f4a2a20` |
| Strategy SHA256 (prefix) | `cf2b1b7c2dbe15d9` (unchanged) |
| Git commit | `ac5191d76aa26eea0c93cf8f004c0ba3e6cdf44e` |

Scanner: `scripts/m8_market_preflight.py` (Gamma filter + CLOB verify). Candidate JSON: `var/m8_market_candidate_002.json`.

### Run artifacts

| Field | Value |
|-------|--------|
| Run name | `m8_validation_002` |
| Run ID | `c8081d7c-5ed9-4277-b759-b995cd7b4727` |
| Wall time | ~161 s (process exit after lifecycle DONE + user-WS disconnect) |
| Parseable facts | 158 rows (`facts.jsonl` truncated mid-write at line 78) |
| Lifecycle | `paired_binary_done` + `final_state: DONE` observed |

### E2 acceptance table (analyzer on saved facts)

| # | Status | Notes |
|---|--------|-------|
| 1 | **PASS** | Complete lifecycle (`paired_binary_done`) |
| 2 | **PASS** | p95 book_age 264 ms (n=3 decision snapshots) |
| 3 | **PASS** | p99 279 ms |
| 4 | **PASS** | 0 REST OMS submits |
| 5 | **PASS** | Material decision evidence complete |
| 6 | **PASS** | 2 FAK planner evidence facts, all fields present |
| 7 | **PASS** | `rest_poll_disabled` |
| 8 | **NOT_OBSERVED** | No live market-WS disconnect drill |
| 9 | **NOT_OBSERVED** | Simulated reconnect drill only (see below) |
| 10 | **PASS** | Config rollback tests |
| 11 | **PASS** | `event_wake`: 17, `timer`: 51 |
| 12 | **FAIL** | 3 latency chains; `submit_to_ack_ms` null despite matched OMS (fixed post-run) |

### E2 hardening fixes (infrastructure only)

| Fix | Module |
|-----|--------|
| PAUSED → TRADING_ENABLED resume after reconnect | `market_data/readiness.py` |
| BUY maker-notional ≤2 decimals (venue compliance) | `execution/order_builder.py` |
| WS_PRIMARY not regressed by REST bootstrap | `state/market_store.py` |
| Post-OMS `latency_chain` with ack/fill on matched submit | `runtime/pipeline.py` |
| Analyzer: lifecycle via `paired_binary_done`, skip corrupt JSONL lines | `scripts/validate_m8_ws_primary_run.py` |
| Simulated reconnect drill | `tests/test_m8_reconnect_drill.py` |
| Market preflight + CLOB notional checks | `scripts/m8_preflight_check.py`, `scripts/m8_market_preflight.py` |

### Reconnect drill (Step 5 — simulated)

`tests/test_m8_reconnect_drill.py`: gap → PAUSED → REST recovery → fresh WS both legs → TRADING_ENABLED. **Not a live market-WS disconnect.**

### Rollback (Step 5)

Unchanged — `tests/test_m8_rollback_drill.py` **PASS**.

---

## Recommended next run

```bash
python scripts/m8_preflight_check.py --scenario m8_ws_primary_validation_002
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/paired_binary.yaml \
  --scenario m8_ws_primary_validation_002 \
  --run-name m8_validation_002b
# Prefer ≥30 min steady state OR lifecycle; verify latency_chain after matched OMS
python scripts/validate_m8_ws_primary_run.py var/reporting/runs/m8_validation_002b
```

Delete stale `var/state/paired_binary/paired_binary/m8_ws_primary_validation_002.json` if prior run ended FAILED.

---

## Group E3 — `m8_validation_002b` (2026-06-30)

### Step 1 — Clean state and preflight

- Deleted stale paired-binary state before run (prior E2 FAILED reruns).
- Preflight **PASSED** on same balanced market (BTC vs GTA VI):
  - YES: bid=0.495 ask=0.496 depth=11,177 notional@5=$2.48
  - NO: bid=0.504 ask=0.505 depth=122,421 notional@5=$2.53
- Strategy SHA256 (prefix): `cf2b1b7c2dbe15d9` (unchanged)
- Scenario SHA256 (prefix): `dbc775ed4f4a2a20` (unchanged)
- Git commit at run time: `ac5191d76aa26eea0c93cf8f004c0ba3e6cdf44e`

### E3 infrastructure fix (criterion 12 root cause)

Entry `decision_id` from `emit_material_decision(entry_eval)` was not propagated into `intent_fact_extensions` for pair-entry OMS submits. Post-OMS `_emit_post_submit_latency_chain` requires `decision_id` in extensions.

**Files:** `src/tyrex_pm/runtime/paired_binary_run.py`, `src/tyrex_pm/runtime/pair_entry_saga.py`, `scripts/validate_m8_ws_primary_run.py` (enhanced criterion-12 field counts).

### Run artifacts

| Field | Value |
|-------|--------|
| Run name | `m8_validation_002b` |
| Run ID | `6faabd4c-9545-4f14-9348-6d5fc3c8d03a` |
| Wall time | ~347 s (lifecycle DONE via max-holding timeout exit) |
| Parseable facts | 799 rows (complete `facts.jsonl`) |
| Lifecycle | `paired_binary_done` + `final_state: DONE` |
| Exit path | `urgent_exit` (max holding time) — not take-profit |

Collected: `facts.jsonl`, `manifest.json`, `run_summary.json`, `m8_validation_summary.json`, `m8_validation_summary.md`.

### E3 acceptance table

| # | Status | Detail |
|---|--------|--------|
| 1 | **PASS** | `duration_s=346.7`, lifecycle complete |
| 2 | **FAIL** | p95 book_age **2036.8 ms** (n=5 decision snapshots) |
| 3 | **FAIL** | p99 book_age **2092.2 ms** |
| 4 | **PASS** | 0 REST OMS submits |
| 5 | **PASS** | Material decision evidence complete |
| 6 | **PASS** | 4 planner evidence facts; 4 matched OMS submits |
| 7 | **PASS** | `rest_poll_disabled` |
| 8 | **NOT_OBSERVED** | No live market-WS disconnect drill (1 bootstrap `readiness_not_trading_enabled` block only) |
| 9 | **NOT_OBSERVED** | **PASS_SIMULATED** via `tests/test_m8_reconnect_drill.py` |
| 10 | **PASS** | Config rollback tests |
| 11 | **PASS** | `event_wake`: 160, `timer`: 262 |
| 12 | **PASS** | 4 matched OMS → 4 post-submit chains with non-null `submit_to_ack_ms` + `trigger_to_fill_ms` |

### E3 analyzer metrics

| Metric | Value |
|--------|--------|
| Decision snapshots by type | `entry_eval`: 2, `activation`: 1, `urgent_exit`: 2 |
| Book age p50 / p95 / p99 | 191 / **2036.8** / **2092.2** ms (all decision snapshots) |
| WS-primary entry_eval only | 81 ms (pass verdict) |
| Source distribution | `websocket`: 4, `rest_bootstrap`: 1 |
| Quality verdicts | `pass`: 2, `reject_decision`: 2, `emergency_only`: 1 |
| REST OMS entry violations | **0** |
| OMS submit (matched) | **4** (2 entry FAK + 2 urgent-exit FAK) |
| Latency chains | 5 total; **4** with `submit_to_ack_ms` + `trigger_to_fill_ms` populated |
| `ack_to_user_fill_ms` non-null | 0 |
| `fill_to_sellable_ms` non-null | 0 |
| Tick sources | `event_wake`: 160, `timer`: 262 |
| Readiness transitions | 278 |

**Book-age failure drivers:** activation snapshot at 1760 ms (`book_age_reject`); timeout `urgent_exit` at 2106 ms (`book_age_emergency`). Entry WS-primary snapshot at 81 ms was within limits.

### Criterion 12 hard check (saved facts)

Example entry leg chain linked to `entry_eval` decision `9cbd1d07-…`:

```json
{"decision_id": "9cbd1d07-8aed-497e-b4fd-61809b9907be",
 "trigger_to_submit_ms": 813, "submit_to_ack_ms": 0, "trigger_to_fill_ms": 813,
 "source": "websocket", "market_book_age_ms": 292}
```

Matched OMS + latency_chain co-emitted for YES/NO entry and YES/NO urgent exit.

### Reconnect / soak (Steps 5–6)

| Item | Result |
|------|--------|
| Live market-WS disconnect drill | **Not run** (Option B — unsafe to force disconnect mid-live) |
| Simulated reconnect | **PASS** — `tests/test_m8_reconnect_drill.py` |
| 30-minute WS-primary soak | **Not run** — process exits after lifecycle DONE (~347 s); no idle post-DONE soak mode without extra scenario work |

User-WS disconnect observed at shutdown (`ConnectionClosedError`); not a market-WS reconnect criterion event.

### E3 tests

```
pytest tests/test_pair_entry_saga.py tests/test_paired_binary_runtime.py \
       tests/test_m8_reconnect_drill.py tests/test_m8_rollback_drill.py \
       tests/test_cutover_acceptance_criteria.py tests/test_ws_primary_cutover.py -q
```

**Result:** 38 passed

---

## Group E4 — decision freshness hardening + `m8_validation_002c` (2026-06-30)

### E3 root-cause analysis (002b stale snapshots)

| Decision | Category | book_age_ms | verdict | Root cause |
|----------|----------|-------------|---------|------------|
| entry_eval (WS) | ENTRY | 70 | pass | Fresh WS at entry — OK |
| activation | ACTIVATION | **1760** | **reject_decision** | Activation armed immediately after sellable-wait without freshness gate; `emit_material_decision` recorded stale pair while monitoring still activated |
| timeout urgent_exit | TIMEOUT_EXIT | **2106** | **emergency_only** | Correct tier for risk reduction, but counted in all-decision p95/p99; book had not updated for ~2s before timeout tick |

**Tick context:** activation followed ~13s after entry snapshot; timeout followed ~302s after activation — both on `event_wake` ticks with no intervening WS book update at decision instant.

### E4 infrastructure fixes

| Fix | Module |
|-----|--------|
| `DecisionContext.ACTIVATION` (strict, same as ENTRY) | `market_data/quality.py` |
| Activation deferred until fresh WS_PRIMARY pair (`activation_may_proceed`) | `market_data/decision_freshness.py`, `paired_binary_run.py` |
| Timeout exits use `trigger_to_context` + `timeout_exit` snapshots | `monitor.py`, `pipeline.py` |
| Split strict vs risk-reduction book-age metrics + per-snapshot diagnosis | `scripts/validate_m8_ws_primary_run.py` |

### Run `m8_validation_002c`

| Field | Value |
|-------|--------|
| Run ID | `04056d08-b3b2-475b-a7cf-6cbb0138a3b0` |
| Duration | ~336 s, lifecycle DONE |
| Activation deferrals | 3× `activation_book_age_stale` rechecks → activation at **1 ms** book age, verdict **pass** |
| Timeout exits | 2× `timeout_exit` at **89 ms**, verdict **pass** (fresh WS on event_wake) |

### E4 book-age metrics (002c)

| Bucket | p50 / p95 / p99 | samples |
|--------|-----------------|---------|
| **all_decisions** | 89 / 92.2 / 92.8 ms | 5 |
| **strict** (ENTRY/TP/ACTIVATION) | 70 / **90.7** / **92.5** ms | 3 |
| **risk_reduction** (TIMEOUT/STOP/URGENT) | 89 / 89 / 89 ms | 2 |

### E4 acceptance table (002c)

| # | Status |
|---|--------|
| 1 | **PASS** — lifecycle DONE |
| 2 | **PASS** — strict p95 90.7 ms |
| 3 | **PASS** — strict p99 92.5 ms |
| 4 | **PASS** — 0 REST OMS |
| 5 | **PASS** — decision evidence |
| 6 | **PASS** — FAK planner 4/4 |
| 7 | **PASS** — REST poll disabled |
| 8 | **NOT_OBSERVED** — live disconnect (simulated drill PASS) |
| 9 | **NOT_OBSERVED** — live reconnect (simulated drill PASS) |
| 10 | **PASS** — rollback tests |
| 11 | **PASS** — event_wake 107 + timer 283 |
| 12 | **PASS** — latency_chain 4/4 matched OMS |

### E4 tests

```
pytest tests/test_quality_entry_vs_exit.py tests/test_paired_binary_activation_freshness.py \
       tests/test_m8_reconnect_drill.py tests/test_m8_rollback_drill.py -q
```

**Result:** 14 passed

---

## Final recommendation

### `M8_VALIDATED`

Validated on **`m8_validation_002c`** (`04056d08-b3b2-475b-a7cf-6cbb0138a3b0`) after E4 freshness hardening.

**Evidence chain:** E1 infrastructure → E2 lifecycle + planner → E3 latency_chain fix → E4 strict freshness pass.

**Accepted without live drill:** Criteria 8–9 via `tests/test_m8_reconnect_drill.py` (Option B documented).

**M9 still blocked:** baseline artifacts missing per milestone plan — not an M8 code gate.
