# N5 acceptance report — Z-Gap SHADOW (simulated execution)

**Verdict:** `PASS_WITH_ENVIRONMENT_BLOCKER`  
**Date:** 2026-07-21  
**Branch:** `rest_project`  
**Scope:** N5A offline deterministic SHADOW + depth-walk fill model; N5B live
real-input SHADOW deferred (Polymarket TLS hostname mismatch).

---

## 0. Git / baseline

| Item | Value |
|------|--------|
| Branch | `rest_project` |
| Starting HEAD | `f73029bb1505dde67e5b37b3db3d3e7cc1f6b851` |
| Worktree before | Clean |
| Ending HEAD | `3a35011d98257ac481bb27a5e360009f7af970d4` |
| N5A commit | `Run Z-Gap SHADOW with deterministic depth-walk execution` |

Baseline tests at start: **535 passed**.

---

## 1. Verdict

**`PASS_WITH_ENVIRONMENT_BLOCKER`**

- **N5A:** Complete — N4-aligned evaluation input (`S=\hat{C}_t`, `K` sealed),
  shared `ZGapBinding.evaluate`, ShadowOMS `shadow_depth_walk_v1`, prepared-next
  require-flat promote, persistence labels, offline tests + fixture CLI.
- **N5B:** `NOT_RUN_ENVIRONMENT_BLOCKED` — same TLS environment block as N2–N4.
  Fixture/replay is **not** live evidence.

N6 remains blocked under this verdict for live implementation (offline design
review may proceed separately only if explicitly planned later).

---

## 2. Evidence classification

| Class | Status |
|-------|--------|
| Synthetic deterministic fixtures | Used for N5A proofs (depth-walk, parity, promote, labels) |
| Recorded live-market evidence | None claimed in this acceptance |
| Current-host live network | `NOT_RUN_ENVIRONMENT_BLOCKED` |

Do not call the N3/N4 JSON fixtures “real data” without provenance.

---

## 3. OBSERVE / SHADOW decision parity

Shared path:

```text
N4ObserveRuntime.prepare_aligned_eval()
  → DecisionSnapshot.reference = C_hat
  → ZGapBinding.evaluate(..., volatility_price=B_t, settlement_ref=C_t)
  → OBSERVE: record facts only
  → SHADOW (N5ShadowRuntime): RiskEngine → planners → ShadowOMS
```

Decisive test: `test_observe_shadow_parity_follows_c_hat_not_raw_b` — when
\(B_t < K < \hat{C}_t\), both modes use \(\hat{C}_t\) (positive gap), not raw
Binance.

No Z-Gap formulas in ShadowHost, RiskEngine, planners, OMS, Portfolio, or
reporting.

---

## 4. Price / sigma mapping (frozen from N4)

| Concept | Source |
|---------|--------|
| Sealed anchor \(K\) | Chainlink PTB |
| Model price \(S\) | Aligned \(\hat{C}_t\) |
| Volatility | Raw Binance returns |
| Settlement observation | Latest accepted Chainlink |
| Basis estimate | Causal accepted estimate + as-of |

Unavailable alignment → skip (`basis_estimate_unavailable`); no raw-Binance
fallback.

---

## 5. Fill model `shadow_depth_walk_v1`

Algorithm:

1. Record decision/plan time (`order.created_at`).
2. Apply configured `latency_ms` → simulated arrival.
3. Wait until simulated time has reached arrival (via recorded book ingress).
4. Select book for matching: latest book with `available_at ≤ arrival` when
   present; otherwise first book at/after arrival once latency elapsed.
5. Walk executable depth for requested quantity.
6. Apply `extra_slip_ticks` only when configured (never double-count with depth).
7. Emit full / partial / no-fill; record model id + assumptions.

Invariants: no queue claim; no look-ahead books (`available_at > arrival` when a
pre-arrival book exists); deterministic replay; fees `estimated`; P&L
`simulated_shadow`. Fixture latency/slip values are **not** production defaults.

---

## 6. Lifecycle / residual / promote

| Area | Evidence |
|------|----------|
| Entry/exit families | F4/F5 regression suite (legacy immediate fill default) + N5 depth-walk unit tests |
| Partials / no-fill / inventory cap | `tests/test_n5_depth_walk.py` |
| UNKNOWN blocks guessed sell | `test_unknown_inventory_blocks_sell` |
| FLAT degradation | Missing seal/alignment skips; no exposure |
| Prepared-next require-flat | `test_promote_requires_flat` |
| Persistence | Snapshot includes `n5_sessions` + `fill_model_id` |
| Resolution | Off by default (Scope A); F5 remains available when enabled |

---

## 7. Offline test evidence

```text
python -m pytest tests -q --tb=no
```

N5-focused modules:

- `tests/test_n5_depth_walk.py`
- `tests/test_n5_shadow_runtime.py`
- Existing F1–F5 and N4 regressions

---

## 8. Fixture CLI (N5A)

```text
python tools/n5_shadow/run_n5_shadow.py --mode fixture \
  --config config/observe_shadow_z_gap_n5a.json \
  --out var/reporting/n5/shadow_summary.json
```

Labels: `not_live_evidence=true`, `live_oms=false`, `fill_model_id=shadow_depth_walk_v1`,
`economics_label=simulated_shadow`, `fees_label=estimated`.

---

## 9. Deferred N5B live command (healthy TLS host only)

Documented intent (not validated on this host):

```text
python tools/n5_shadow/run_n5_shadow_live.py --mode live \
  --duration-s 180 --max-windows 2 \
  --out var/reporting/n5/shadow_live_summary.json
```

Must use ShadowOMS only, attested+locked PTB, prepared-next with require-flat,
and never LiveOMS / auth mutation. Prerequisite: Polymarket TLS OK via
`python tools/n2_smoke/diagnose_connectivity.py`.

---

## 10. OPEN parameters (unchanged)

- Production EWMA half-life
- Freshness / skew / lateness / clock thresholds
- Nonzero PTB mismatch tolerance
- Production strategy timing/decision parameters
- Live N4B / N5B acceptance

---

## 11. Exact N6 readiness

| Allowed | Not yet |
|---------|---------|
| Offline design notes for generic live OMS | LiveOMS enablement |
| | Authenticated mutation |
| | Claiming N5B live SHADOW on this host |

Under `PASS_WITH_ENVIRONMENT_BLOCKER`, N6 live implementation remains blocked
unless a later plan explicitly separates non-mutating design review.

---

## 12. Exclusions confirmed

No LiveOMS, auth trading channels, real orders/cancels, redeem, new Z-Gap
formulas, production threshold invention, TLS bypass, `.env` changes, N6/N7.
Nothing pushed.
