# N4 acceptance report — Real-input OBSERVE composition

**Verdict:** `PASS_WITH_ENVIRONMENT_BLOCKER`  
**Date:** 2026-07-21  
**Branch:** `rest_project`  
**Scope:** N4A offline deterministic OBSERVE composition; N4B live Polymarket
OBSERVE deferred (TLS environment block).

---

## 0. Git / baseline

| Item | Value |
|------|--------|
| Branch | `rest_project` |
| Starting HEAD | `bd3500e445afa1aef599c11493f06f71d6fa2c97` |
| Worktree before | Clean |
| Ending HEAD | Tip after N4A commit — `git rev-parse HEAD` |
| Commit | `Wire sealed PTB references into offline Z-Gap observe runtime` |

Baseline tests at start: **517 passed**.

---

## 1. Verdict

**`PASS_WITH_ENVIRONMENT_BLOCKER`**

- **N4A:** Complete — sealed vs dynamic split, causal alignment semantics,
  active/prepared-next sessions, Z-Gap OBSERVE wiring, offline tests, fixture CLI.
- **N4B:** `NOT_RUN_ENVIRONMENT_BLOCKED` — same Polymarket TLS hostname mismatch
  as N2/N3. Fixture/replay is **not** live evidence.

SHADOW must not start until N4A contracts are consumed and N4B runs on a healthy
TLS host for live acceptance.

---

## 2. Sealed versus dynamic state (Task A)

**Conclusion:** N3 `SealedReferenceInput` previously embedded seal-time
`trading_reference` / `c_hat` / basis fields, which could be misread as freezing
\(B_t\) for the window. N4A splits ownership:

| Package | Module | Frozen? | Contents |
|---------|--------|---------|----------|
| `SealedWindowPtb` | `domain/polymarket/sealed_reference.py` | Yes (per window) | market/window, start/end, K, boundary rule/class, attestation, boundary Chainlink ts/value, clock at seal, evidence ids |
| `DynamicAlignedReference` | same + `evaluate_dynamic_alignment` | No — rebuilt every eval | \(B_t\), causal pair, instantaneous basis, **basis estimate used**, as-of ts, includes-current flag, \(\hat{C}_t\), alignment mode |

`SealedReferenceInput` is now an alias of `SealedWindowPtb`. Seal-time Binance
pairing remains in `provenance` only (`dynamic_refs_not_sealed: true`).

---

## 3. Alignment / basis timing semantics (Task B)

\[
b^{\mathrm{inst}}_t = \ln(C_t / B_t)
\]

\[
\hat{C}_t = B_t \cdot \exp(b^{\mathrm{est}})
\]

| Field | Meaning |
|-------|---------|
| `instantaneous_basis_ln` | Current causal pair basis (when CL present) |
| `basis_estimate_used_ln` | Estimate applied to current \(B_t\) for \(\hat{C}\) |
| `basis_estimate_as_of_ts` | Chainlink source_ts of last accepted estimate |
| `basis_estimate_includes_current_chainlink` | Whether estimate was updated from this CL event |
| `alignment_mode` | `RECONSTRUCTION` (same-pair → \(\hat{C}\equiv C\)) \| `BETWEEN_TICKS` \| `UNAVAILABLE` |

Between Chainlink ticks, N4 uses the latest accepted estimate with the current
Binance price. Future Binance/Chainlink ticks are rejected. EWMA half-life
unconfigured → `threshold_not_configured` (OPEN), not a silent default.

---

## 4. Pipeline and session lifecycle

```text
Discovered market
  → normalized Chainlink/Binance ingress
  → boundary candidate capture (N3)
  → attestation + seal SealedWindowPtb
  → AcceptedBasisEstimate + DynamicAlignedReference
  → immutable DecisionSnapshot + sealed PtbSnapshot
  → existing assemble_zgap_decision_snapshot / ZGapStrategy
  → OBSERVE facts only (no OMS)
```

**Sessions:** `active` vs `prepared_next` (`runtime/n4_observe_runtime.py`).

- Prepared-next collects books/boundary independently (`publish_as_active=False`).
- Promote atomically; prior window strategy binding dropped (no K/thesis leak).
- Global: Binance history + basis EWMA / AcceptedBasisEstimate may continue.
- Window-local: sealed K, strategy binding, books.

---

## 5. Z-Gap input mapping

| Z-Gap input | N4A source |
|-------------|------------|
| K | `SealedWindowPtb.ptb_k` via locked `PtbSnapshot` |
| S (trading) | Current Binance \(B_t\) on `DecisionSnapshot.reference` |
| settlement_ref (bps gate) | Latest accepted Chainlink value (distinct from S) |
| Books | Session up/down quotes (YES←Up / NO←Down slots) |
| Time / σ | Existing TimeAuthority + EWMA vol estimator |

No strategy formula redesign. Optional `settlement_ref=` on `ZGapBinding.evaluate`.

---

## 6. Attestation mismatch (Task F)

Exact-zero compare still reports `MISMATCH` with exact/bps diffs. Nonzero
production tolerance remains **OPEN**. N4 skips OBSERVE with
`attestation_mismatch` and does not invent a tolerance. Phase is `DEGRADED`
(seal for audit allowed; readiness false).

---

## 7. Offline test evidence

```text
python -m pytest tests -q --tb=no
528 passed
```

`tests/test_n4_observe_runtime.py` covers sealed K immutability with moving
\(\hat{C}\), estimate as-of, no future leakage, reconstruction/between-ticks,
missing exact, mismatch blockers, late-after-seal, prepared-next promote, EWMA
continuity, replay, fixture CLI, no `old/` imports. F1–F5 regressions via full suite.

---

## 8. Deferred live command (N4B)

```text
python tools/n4_observe/run_n4_observe.py --mode live --duration-s 180 --max-windows 2 --prep-lead-s 60 --out var/reporting/n4/observe_live_summary.json
```

Prerequisite on the host:

```text
python tools/n2_smoke/diagnose_connectivity.py
```

Must show Polymarket TLS OK (not hostname mismatch). Evidence pack includes
Gamma active+prepared-next, RTDS Chainlink, Binance, CLOB, clock, N3 seal path,
N4 observations, graceful shutdown. Do not auto-freeze OPEN thresholds.

Fixture mode (CI):

```text
python tools/n4_observe/run_n4_observe.py --mode fixture --fixture tests/fixtures/n3/n1_three_windows.json --windows 2 --out var/reporting/n4/observe_summary.json
```

---

## 9. Decisions

### Frozen

- Sealed K immutable; dynamic \(B_t\) / basis / \(\hat{C}\) not sealed into K
- Pairing `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`
- OBSERVE records only — no OMS/orders
- Up/Down label mapping; YES/NO slots compatibility only

### Provisional

- Preferred boundary `EXACT_AT_START`
- Fixture attestation path for N4A
- OBSERVE may evaluate provisional K with labels when attested MATCH

### OPEN

- Fallback inequality when exact absent
- Mismatch tolerance (nonzero)
- Skew / lateness / freshness / clock uncertainty / EWMA half-life
- Scope-A strategy timing numerics
- Live N4B acceptance

---

## 10. Exact readiness for SHADOW (N5)

| Allowed | Not yet |
|---------|---------|
| Offline SHADOW design against N4A observation contracts | Live SHADOW acceptance |
| Fixture dual-ref OBSERVE facts | Claim production OBSERVE on this host |
| | Freeze OPEN thresholds from blocked host |

N5 must not invent OMS mutations beyond existing ShadowOMS fixture paths without
N4B live evidence.

---

## 11. Exclusions confirmed

No new strategy formulas, no automatic FIRST/LAST PTB fallback, no threshold
selection, no SHADOW/LIVE enablement, no OMS/Portfolio/auth/orders, no TLS
bypass, no `.env`, nothing pushed, N5 not started.
