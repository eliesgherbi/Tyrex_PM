# N5 acceptance report — Z-Gap SHADOW (simulated execution)

**Verdict:** `PASS_WITH_ENVIRONMENT_BLOCKER`  
**Date:** 2026-07-21  
**Branch:** `rest_project`  
**Scope:** N5A offline deterministic SHADOW + `shadow_depth_walk_v1` end-to-end
lifecycle hardening; N5B live real-input SHADOW deferred (Polymarket TLS).

---

## 0. Git / baseline and HEAD discrepancy

| Item | Value |
|------|--------|
| Branch | `rest_project` |
| Starting HEAD (this audit) | `afa3011c195bbbd435b3d3c82c44777cb108c590` |
| Worktree before | Clean |
| Parent of N5 tip | `f73029bb1505dde67e5b37b3db3d3e7cc1f6b851` |
| Prior report ending HEAD | `3a35011d98257ac481bb27a5e360009f7af970d4` |
| Tip before this hardening | `afa3011c195bbbd435b3d3c82c44777cb108c590` |
| Ending HEAD (this commit) | `git rev-parse HEAD` after `Harden Z-Gap SHADOW lifecycle acceptance` |
| Hardening commit message | `Harden Z-Gap SHADOW lifecycle acceptance` |

### Why `3a35011…` vs `afa3011…`

Both hashes share the same commit message
(`Run Z-Gap SHADOW with deterministic depth-walk execution`) and the same
parent `f73029b…`. The earlier acceptance report recorded the **first** N5
commit hash (`3a35011…`). That commit was later **amended** (documentation /
HEAD line bookkeeping) into tip `afa3011…` without changing the intended N5
scope. Agent feedback correctly reported the amended tip. This is a
bookkeeping inconsistency only — not a second divergent N5 implementation.

Baseline at audit start: **548 passed**.

---

## 1. Verdict

**`PASS_WITH_ENVIRONMENT_BLOCKER`**

- **N5A:** Critical lifecycle, causality, residual, recovery, rollover, and
  labelling requirements are proven end-to-end through the composed ShadowHost
  pipeline with `shadow_depth_walk_v1` (see matrix below).
- **N5B:** `NOT_RUN_ENVIRONMENT_BLOCKED` — same TLS environment block as N2–N4.
  Fixture/replay is **not** live evidence.

N6 remains blocked until N5A keeps this verdict **and** N3B→N4B→N5B succeed on
a healthy host with reviewed live-input parameters.

---

## 2. Defect found and bounded fix (N5 scope)

### Root cause

Depth-walk fill causality used provider `book.ts_event` / event receive times as
`available_at`, while `FakeClock` often lagged those timestamps. Decisions could
see rich books in the host book store while OMS selected stale books or failed
the latency gate. Separately, the latency “now” proxy used
`max(book.available_at)`, which blocked fills after the clock advanced past the
last book ingest.

### Fix

1. `ShadowHost._on_book_updated`: `available_at = clock.now_utc()` (ingress /
   host clock).
2. `ShadowOMS`: accept `clock=…`; latency gate uses `clock.now_utc()`; select
   latest book with `available_at ≤ arrival` (no look-ahead).
3. `N5ShadowRuntime.publish_book`: default `available_at` to host clock.
4. Match-trace outcome/residual: compute remaining locally after the fill
   amount — nested `OrderFilled` events are queued by the dispatcher, so
   reading `OrderStore.remaining_quantity` mid-match incorrectly labeled full
   fills as `partial`.

Regression: `test_depth_walk_no_lookahead_waits_for_arrival_book` and F4 rich
timeline with `fills.model_id=shadow_depth_walk_v1` → BUY/SELL FILLED, lifecycle
FLAT.

---

## 3. Evidence classification

| Class | Status |
|-------|--------|
| Synthetic deterministic fixtures | N5A proofs (E2E depth-walk lifecycle, component causality, parity, promote) |
| Recorded live-market evidence | None claimed |
| Current-host live network | `NOT_RUN_ENVIRONMENT_BLOCKED` |

---

## 4. Composed runtime data flow

```text
N4 normalized / sealed inputs
  → N4ObserveRuntime.prepare_aligned_eval()
      (S=C_hat, K=Chainlink PTB, sigma from raw Binance returns)
  → shared ZGapBinding.evaluate (no formula duplication in OMS)
  → intent
  → RiskEngine
  → ExecutionPlanner / ExitPlanner
  → ShadowOMS + shadow_depth_walk_v1
  → OrderStore / FillLedger
  → Portfolio / TradeLifecycle
  → next DecisionContext
  → exit decision → exit planner → depth-walk
  → terminal FLAT or explicit residual
  → SHADOW facts (simulated_shadow / estimated)
```

Numerical contracts proven in E2E tests:

- Decision uses \(C\_hat\), not raw Binance (`test_observe_shadow_parity_follows_c_hat_not_raw_b`).
- Sigma source labeled `binance_raw_returns`.
- Inventory comes only from simulated fills; exit qty ≤ confirmed inventory.
- No LiveOMS / auth / venue mutation on the path
  (`test_no_liveoms_on_n5_decision_path`).

---

## 5. Evidence-to-requirement matrix

Classification legend:

- `PROVEN_END_TO_END_DEPTH_WALK` — composed ShadowHost (or N5ShadowRuntime) path
  with `shadow_depth_walk_v1`
- `PROVEN_COMPONENT_ONLY` — OMS/unit only
- `PROVEN_LEGACY_FILL_ONLY` — F4/F5 with immediate-fill default
- `NOT_PROVEN` / `DEFECT_FOUND`

| # | Requirement | Entrypoint | Decisive test | Depth-walk | Pipeline | Classification |
|---|-------------|------------|---------------|------------|----------|----------------|
| 1 | Full entry fill | ShadowHost + OMS | `test_e2e_rich_entry_exit_flat_depth_walk` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 2 | Partial entry fill | ShadowHost + latency thin book | `test_e2e_partial_entry_confirmed_qty_only` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 3 | Entry no-fill | ShadowHost zero ask both legs | `test_e2e_entry_nofill_no_false_active` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 4 | Full exit | ShadowHost rich | `test_e2e_rich_entry_exit_flat_depth_walk` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 5 | Partial exit | ShadowHost thin bid | `test_e2e_restart_with_exit_residual_no_duplicate` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 6 | Exit no-fill | ShadowHost zero bid | `test_e2e_exit_nofill_keeps_explicit_exposure` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 7 | Market-rich exit | ShadowHost | `test_e2e_rich_entry_exit_flat_depth_walk` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 8 | Thesis-invalidation exit | ShadowHost | `test_e2e_thesis_exit_depth_walk` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 9 | Time exit | ShadowHost | `test_e2e_time_exit_depth_walk` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 10 | Kill-switch / risk flatten | ShadowHost | `test_e2e_kill_flatten_depth_walk` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 11 | Missing inputs while FLAT | N5ShadowRuntime | `test_flat_vs_active_degradation` | n/a (skip) | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 12 | Degraded inputs while ACTIVE | ShadowHost | `test_e2e_active_degradation_does_not_abandon_exposure` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 13 | UNKNOWN inventory | ShadowHost | `test_e2e_unknown_blocks_sell_while_active` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 14 | Restart while ACTIVE / after run | ShadowHost persist | `test_e2e_restart_after_active_no_duplicate_entry` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 15–16 | Restart exit residual | ShadowHost | `test_e2e_restart_with_exit_residual_no_duplicate` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 17 | Replay / idempotency | ShadowHost | `test_e2e_replay_same_evidence_idempotent` | yes | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 18 | Prepared-next only when FLAT | N5ShadowRuntime | `test_promote_requires_flat` | labels | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 19 | Non-flat rollover block | N5ShadowRuntime | `test_promote_requires_flat` | labels | composed | `PROVEN_END_TO_END_DEPTH_WALK` |
| 20 | Cross-window isolation | N5 promote | `test_promote_requires_flat` | n/a | composed | `PROVEN_END_TO_END_DEPTH_WALK` |

Supporting component proofs (not counted as full lifecycle alone):

| Area | Test | Classification |
|------|------|----------------|
| Full/partial/no-fill + look-ahead | `tests/test_n5_depth_walk.py` | `PROVEN_COMPONENT_ONLY` |
| Sell ≤ confirmed inventory | `test_sell_cannot_exceed_confirmed_inventory` | `PROVEN_COMPONENT_ONLY` |
| F4/F5 families on legacy fill | `tests/test_f4_z_gap_shadow.py`, `test_f5_*` | `PROVEN_LEGACY_FILL_ONLY` |

Pre-hardening audit found **0/20** as end-to-end depth-walk; this hardening
closes that gap without redesigning Z-Gap or advancing to N6.

---

## 6. Entry readiness vs exit safety

| State | Behavior | Proof |
|-------|----------|-------|
| FLAT | Missing seal/alignment/sigma blocks new exposure with skip reasons | `test_flat_vs_active_degradation`, N4 unavailable-alignment skips |
| ACTIVE | Confirmed inventory retained if books/inputs degrade; not reported FLAT | `test_e2e_active_degradation_does_not_abandon_exposure` |
| ACTIVE | Missing exit depth → explicit residual / non-FLAT | `test_e2e_exit_nofill_keeps_explicit_exposure` |
| UNKNOWN | No guessed sell even under kill-switch | `test_e2e_unknown_blocks_sell_while_active` |

---

## 7. Persistence / restart / idempotency

Persisted SHADOW state includes runtime mode, sessions/windows, fill-model id,
orders, fills, portfolio, lifecycle, and retry/dedup fields (existing
`StateSnapshotStore` + N5 session labels).

| Proof | Test |
|-------|------|
| Snapshot labels | `test_persistence_roundtrip` |
| Residual qty restore | `test_e2e_restart_with_exit_residual_no_duplicate` |
| Replay economics | `test_e2e_replay_same_evidence_idempotent` |
| Promote require-flat | `test_promote_requires_flat` |

---

## 8. Fixture CLI

```text
python tools/n5_shadow/run_n5_shadow.py \
  --mode fixture \
  --config config/observe_shadow_z_gap_n5a.json \
  --out var/reporting/n5/shadow_summary.json
```

Summary fields:

- N5 composition records (seal + evaluate labels)
- `depth_walk_lifecycle`: complete synthetic F4 entry→EXIT→FLAT through
  `shadow_depth_walk_v1`, with match traces, orders, lifecycle terminal,
  `orders_live=0`, `not_live_evidence=true`
- `n5b_status=NOT_RUN_ENVIRONMENT_BLOCKED`
- Deferred healthy-host command documented (not validated here)

Smoke: `test_fixture_cli_smoke`.

---

## 9. Offline tests

```text
python -m pytest tests -q --tb=no
```

N5A-focused modules:

- `tests/test_n5_depth_walk.py`
- `tests/test_n5_shadow_runtime.py`
- `tests/test_n5a_depth_walk_lifecycle.py` *(new E2E depth-walk lifecycle)*

---

## 10. Deferred N5B (healthy TLS host only)

```text
python tools/n5_shadow/run_n5_shadow_live.py --mode live \
  --duration-s 180 --max-windows 2 \
  --out var/reporting/n5/shadow_live_summary.json
```

**Status:** `NOT_RUN_ENVIRONMENT_BLOCKED`. Not validated on this host.

---

## 11. OPEN parameters (unchanged)

- Production EWMA half-life
- Freshness / skew / lateness / clock thresholds
- Nonzero PTB mismatch tolerance
- Production strategy timing / decision thresholds
- Live retry / residual-exposure limits
- N3B / N4B / N5B live acceptance

Fixture-only values (latency_ms, theta overrides, etc.) are labeled in tests and
are **not** production defaults.

---

## 12. Exact N6 readiness

N6 remains blocked until:

1. N5A justified `PASS_WITH_ENVIRONMENT_BLOCKER` (this report)
2. N3B → N4B → N5B succeed on a healthy host
3. Required live-input parameters and operational policies are reviewed

| Allowed | Not yet |
|---------|---------|
| Offline design notes for generic live OMS | LiveOMS enablement |
| | Authenticated mutation |
| | Claiming N5B live SHADOW on this host |

---

## 13. Exclusions confirmed

No LiveOMS, auth trading channels, real orders/cancels, redeem, new Z-Gap
formulas, production threshold invention, TLS bypass, `.env` changes, N6/N7.
Nothing pushed.
