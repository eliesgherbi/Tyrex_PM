# N3B / N4B / N5B live-input validation report

**Final verdict:** `PASS_READY_FOR_N6_PLANNING`

Caveat: Gate 3 naturally produced strategy `WAIT`/`SKIP` only (no simulated entry/fill). Offline N5A already accepts `shadow_depth_walk_v1` lifecycle. Live SHADOW composition and safety invariants are proven; full live fill/exit P&L path was not naturally exercised.

---

## 1. Starting / ending state

| Item | Start | End |
|------|-------|-----|
| Branch | `rest_project` | `rest_project` |
| HEAD | `6d1b202ff83e3603486519314008610d76271eb1` | (commit after this report) |
| Tests | 560 passed | **575 passed** |
| Python | 3.12.3 (conda-forge) | same |
| Worktree | dirty with N3B/N4B/N5B integration | committed integration only |

---

## 2. Gate table

| Gate | Inspected | Missing/defective | Changes | Command | Duration / windows | Expected | Actual | Verdict | Evidence |
|------|-----------|-------------------|---------|---------|--------------------|----------|--------|---------|----------|
| **1 N3B** | Capture/analyze only; no integrated `attest_and_seal` product path | Integrated live seal CLI; SSR comparison timing | `live_zgap_compose`, `run_n3_ptb_live.py`, SSR provider, seal contracts | `python tools/n3_ptb/run_n3_ptb_live.py --min-seals 3 --wait-for-boundary --prep-lead-s 40 --max-duration-s 1200 --out-dir var/reporting/n3b/gate1_live` | ~11 min; 3 EXACT seals (+1 mid-window miss) | Real EXACT K sealed, immutable, provenance | 3 EXACT seals, `immutable_check_ok=true`; live SSR INCOMPLETE at T+1s; **post-hoc SSR MATCH exact** for all 3 | **PASS** | `var/reporting/n3b/gate1_live/` |
| **2 N4B** | Live OBSERVE had `observations=0` (never sealed) | Shared seal→eval path; clock falsely DEGRADED; SSR float crash | Shared compose; clock offset semantics; attestation grace; `run_n4_observe.py --mode live` | `python tools/n4_observe/run_n4_observe.py --mode live --duration-s 1100 --max-windows 3 --prep-lead-s 40 --out var/reporting/n4b/observe_live_summary_gate2.json` | ~11.5 min after wait; 3 seals | `evaluations>0`, S=`C_hat`, sigma=Binance returns, OMS=0 | 507 obs, **230 evaluated**, strategy skips/`no_intent`; payload proves S/`sigma`; OMS/orders=0 | **PASS** | `var/reporting/n4b/observe_live_summary_gate2.json`, `var/reporting/n4b/n4b_20260721T204921Z/` |
| **3 N5B** | No live SHADOW runner | `run_n5_shadow_live.py` composing N4 decisions → ShadowOMS | Inject N4 into compose; ShadowOMS + depth-walk config | `python tools/n5_shadow/run_n5_shadow_live.py --mode live --wait-for-boundary --prep-lead-s 40 --duration-s 1100 --max-windows 3 --min-seals 3 --out var/reporting/n5b/shadow_live_summary_gate3.json` | ~12 min; 3 seals (2 MATCH) | Live decisions → ShadowOMS; `orders_live=0` | 329 evaluated WAIT/SKIP; `oms_is_shadow=ShadowOMS`; `orders_submitted=0`; no venue mutation | **PASS** (decision path; execution not naturally exercised) | `var/reporting/n5b/shadow_live_summary_gate3.json`, `var/reporting/n5b/n5b_20260721T210201Z/` |

---

## 3. N3B analysis (Gate 1)

### Prior match/mismatch investigation

Earlier capture (1 match / 2 mismatches) was **not** proof that Chainlink EXACT K was wrong. Dominant causes in prior artifacts:

- Stale / appended JSONL from earlier runs mixed with current windows
- Mid-window start → no EXACT tick for the active window
- Displayed PTB is an independent SSR `openPrice` that can lag host seal time

Relationship:

- **Candidate K** = Chainlink tick with `source_ts == event_start` (`EXACT_AT_START`)
- **Displayed PTB** = Polymarket SSR `openPrice` (comparison only; never substituted as K)
- **Accepted rule** = EXACT at boundary; seal immutably after selection
- Timing: host ingress ~1s after provider `source_ts`; SSR often publishes several seconds later

### Gate 1 sealed windows

| window_id | sealed_k | chainlink_source_ts | receive_wall | attestation (live) | post-hoc SSR |
|-----------|----------|---------------------|--------------|--------------------|--------------|
| `btc-updown-5m-1784665200` | 66358.63106351519 | 20:20:00Z | 20:20:01.388Z | INCOMPLETE | **MATCH exact** |
| `btc-updown-5m-1784665500` | 66336.11223781579 | 20:25:00Z | 20:25:01.332Z | INCOMPLETE | **MATCH exact** |
| `btc-updown-5m-1784665800` | 66356.41765129865 | 20:30:00Z | 20:30:01.373Z | INCOMPLETE | **MATCH exact** |

Missed: `1784664900` (`window_ended_without_exact_seal` — started mid-window).

Immutability: each seal re-seal returned identical K (`immutable_check_ok=true`). Window ownership isolated active vs prepared-next.

Evidence: `var/reporting/n3b/gate1_live/summary.json`, `posthoc_ssr_attestation.json`, `seal_*.json`.

Feeds: chainlink 630, binance 9261, clob 6834; `orders_live=0`, `auth_touched=false`.

---

## 4. N4B analysis (Gate 2)

### Root cause of first live attempt (`observations>0` but all skipped)

1. SSR `openPrice` parsed as float → `NumericError` → attestation INCOMPLETE → hard skip
2. Clock sync treated OS baseline vs Binance offset as “disagreement” → DEGRADED on every tick
3. Future Binance rejection used uncorrected host time while feeds used corrected ingress

### Fixes applied

- SSR: stringify JSON numbers; grace delay before seal; negative cache
- Clock: Binance cross-check is `estimated_offset`, not multi-source disagreement
- Shared `SnapshotTimeAuthority` + corrected `now` in evaluate path
- Clear stale `exact_candidate_absent` after EXACT selection

### Gate 2 live proof (re-run)

| Metric | Value |
|--------|-------|
| Seals | 3 EXACT (`1784667000` MATCH; `7300`/`7600` INCOMPLETE at 20s grace — post-hoc MATCH) |
| Observations | 507 |
| Evaluated | **230** |
| Decisions | `no_intent` 192; strategy skips (jump_guard, min_samples, …) |
| Readiness skips | 277 `attestation_unavailable` (incomplete seals after promote) |
| `model_spot_source` | `aligned_c_hat` |
| `sigma_source` | `binance_raw_returns` |
| `zgap_S_equals_c_hat` | true |
| OMS / orders | all zero; `auth_touched=false` |

Clock at end: `READY`, offset ~601 ms, uncertainty 187 ms.

Evidence: `var/reporting/n4b/observe_live_summary_gate2.json`, `n4b_20260721T204921Z/`.

---

## 5. N5B analysis (Gate 3)

| Metric | Value |
|--------|-------|
| Seals | 3 (2 MATCH with empty blockers; 1 INCOMPLETE at stop) |
| Shadow records | **329 evaluated** |
| Decisions | WAIT 72 / SKIP 257 across two MATCH windows |
| `runtime_mode` | SHADOW (329) |
| `fill_model_id` | `shadow_depth_walk_v1` |
| OMS | `ShadowOMS` only |
| `orders_submitted` | 0 |
| `orders_live` | 0 |
| `venue_mutation` | false |
| `live_oms` | false |
| Lifecycle | FLAT; `portfolio_flat=true` |
| Fees / P&L labels | `estimated` / `simulated_shadow` |

**Natural entry:** none under current thresholds — do **not** claim live fill/exit/P&L validation. Offline N5A + structural guards cover execution composition.

Evidence: `var/reporting/n5b/shadow_live_summary_gate3.json`, `n5b_20260721T210201Z/`.

---

## 6. Tests

Focused (examples):

```text
pytest tests/test_n3b_seal_contracts.py tests/test_n5b_live_guards.py tests/test_n2_real_data_adapters.py::test_os_monitor_binance_offset_is_ready_not_disagreement -q
```

Full suite:

```text
python -m pytest tests -q --tb=no
575 passed in 19.42s
```

Fixture vs live: fixture OBSERVE/SHADOW remain offline; live evidence under `var/reporting/n3b|n4b|n5b/` only.

---

## 7. Files created / changed

**Created**

- `src/tyrex_pm/runtime/live_zgap_compose.py`
- `src/tyrex_pm/adapters/polymarket/ssr_ptb_attestation.py`
- `tools/n3_ptb/run_n3_ptb_live.py`, `tools/n3_ptb/README.md`
- `tools/n4_observe/run_n4_observe_live_sealed.py`
- `tools/n5_shadow/run_n5_shadow_live.py`
- `config/observe_shadow_z_gap_n5b_live.json`
- `tests/test_n3b_seal_contracts.py`, `tests/test_n5b_live_guards.py`
- This report

**Modified**

- `src/tyrex_pm/adapters/clock_sync.py`
- `src/tyrex_pm/domain/polymarket/ptb_capture.py`
- `src/tyrex_pm/runtime/n4_observe_runtime.py`
- `src/tyrex_pm/runtime/n5_shadow_runtime.py`
- `tools/n4_observe/run_n4_observe.py`
- `tests/test_n2_real_data_adapters.py`

**Generated evidence (not committed)**

- `var/reporting/n3b/gate1_live/**`
- `var/reporting/n4b/n4b_20260721T204921Z/**`, `observe_live_summary_gate2.json`
- `var/reporting/n5b/n5b_20260721T210201Z/**`, `shadow_live_summary_gate3.json`

---

## 8. Reproduce (Windows Git Bash / PowerShell)

```bash
# Gate 1 — N3B seal
python tools/n3_ptb/run_n3_ptb_live.py --min-seals 3 --wait-for-boundary --prep-lead-s 40 --max-duration-s 1200 --out-dir var/reporting/n3b/gate1_live

# Gate 2 — N4B OBSERVE
python tools/n4_observe/run_n4_observe.py --mode live --duration-s 1100 --max-windows 3 --prep-lead-s 40 --out var/reporting/n4b/observe_live_summary_gate2.json

# Gate 3 — N5B SHADOW
python tools/n5_shadow/run_n5_shadow_live.py --mode live --wait-for-boundary --prep-lead-s 40 --duration-s 1100 --max-windows 3 --min-seals 3 --out var/reporting/n5b/shadow_live_summary_gate3.json

# Tests
python -m pytest tests -q --tb=no
```

---

## 9. Remaining before N6

- Natural SHADOW entry/fill/exit not seen live (strategy thresholds OPEN; do not tune for trades)
- Occasional SSR lag past seal grace → INCOMPLETE attestation at seal (post-hoc MATCH); consider async SSR
- `clob_ready=false` while book events flow — clarify readiness flag
- Host clock offset ~500–600 ms vs Binance — correction applied; OS sync still OPEN for production
- N6 live execution / reconciliation **not started**
- Production `basis_ewma_half_life_s` remains OPEN (live-validation uses 30s)

Evidence is sufficient to **begin N6 planning**, not to enable real money.

---

## 10. Explicit confirmations

- No order submitted or cancelled
- No venue state changed
- No authenticated trading path used
- `.env` / credentials not changed or printed
- N6/N7 and real-money execution not implemented
- Nothing pushed
- TLS verification remained on
- No Binance-as-PTB or displayed-PTB-as-K substitution
- No import from `old/`
