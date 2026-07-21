# N4 acceptance report — Real-input OBSERVE composition

**Verdict:** `PASS_WITH_ENVIRONMENT_BLOCKER`  
**Date:** 2026-07-21  
**Branch:** `rest_project`  
**Scope:** N4A offline deterministic OBSERVE composition (including N4A
aligned-price correction); N4B live Polymarket OBSERVE deferred (TLS
environment block).

---

## 0. Git / baseline

| Item | Value |
|------|--------|
| Branch | `rest_project` |
| Starting HEAD (N4 wire) | `29d711db939b6fba089596ae88664d00e90cf925` |
| Worktree before N4A correction | Clean at `29d711d` |
| Ending HEAD | Tip after N4A correction — verify with `git rev-parse HEAD` / `git log -1` (exact SHA in agent final response) |
| N4 wire commit | `Wire sealed PTB references into offline Z-Gap observe runtime` |
| N4A correction commit | `Use aligned reference in Z-Gap observe evaluation` |

Baseline tests at N4 wire: **528 passed**.  
After N4A correction: **535 passed**.

---

## 1. Verdict

**`PASS_WITH_ENVIRONMENT_BLOCKER`**

- **N4A:** Complete — sealed vs dynamic split, causal alignment semantics,
  coherent Chainlink-space model price (\(S=\hat{C}_t\) vs \(K\)),
  active/prepared-next sessions, Z-Gap OBSERVE wiring, decisive numerical
  regressions, fixture CLI.
- **N4B:** `NOT_RUN_ENVIRONMENT_BLOCKED` — same Polymarket TLS hostname mismatch
  as N2/N3. Fixture/replay is **not** live evidence.

SHADOW must not start until N4A contracts are consumed and N4B runs on a healthy
TLS host for live acceptance.

---

## 1b. N4A price-space correction (Task A–F)

### Root-cause classification

**`CODE_USES_RAW_BINANCE`** (pre-correction)

Proven numerical path before correction:

1. `N4ObserveRuntime._evaluate_session` set
   `DecisionSnapshot.reference.price = bn.value` (raw Binance \(B_t\)).
2. `assemble_zgap_decision_snapshot` set `S = market_snapshot.reference.price`.
3. Fair value used \(z=\ln(S/K)\) with sealed Chainlink \(K\).
4. \(\hat{C}_t\) was recorded on the observe fact but **not** fed into the
   strategy as model spot.

That compared Binance-space \(B_t\) to Chainlink-space \(K\) unless an
undocumented \(K\) transform existed (it did not).

### Corrected mapping (required)

| Concept | Required source |
|---------|-----------------|
| Sealed anchor \(K\) | Chainlink PTB (`SealedWindowPtb.ptb_k`) |
| Raw fast reference | Binance \(B_t\) (observable; sigma returns) |
| Model price level \(S\) | Aligned \(\hat{C}_t\) |
| Volatility source | Accepted Binance-return EWMA (`volatility_price=B_t`) |
| Latest settlement observation | Chainlink \(C_t\) (basis / residual gate) |
| Basis estimate | Causally accepted estimate with as-of provenance |

### Formula used by the strategy (not only the alignment layer)

Alignment:

\[
b^{\mathrm{est}} = \widehat{\ln(C/B)}
\quad\text{(causal accepted estimate)}
\]

\[
\hat{C}_t = B_t \cdot \exp(b^{\mathrm{est}})
\]

Z-Gap fair value (OBSERVE path):

\[
S = \hat{C}_t,\quad K = K_{\mathrm{chainlink}}
\]

\[
z = \frac{\ln(S/K)}{\sigma\sqrt{\tau}}
\]

Sigma \(\sigma\) continues to be estimated from **raw Binance** returns
(F1–F5 design). Model level and volatility series are intentionally distinct.

### Unavailable alignment

- `UNAVAILABLE` or missing \(\hat{C}\): hard skip
  `basis_estimate_unavailable` — **no** silent raw-\(B_t\) fallback.
- Unconfigured basis EWMA half-life remains **OPEN**
  (`threshold_not_configured`); fixture CLI may pass an explicit
  fixture-only half-life (labelled; not a production default).
- Future ticks never affect earlier evaluations.

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
Binance price to form \(\hat{C}_t\), then evaluates Z-Gap on
\((\hat{C}_t, K)\). Future Binance/Chainlink ticks are rejected. EWMA half-life
unconfigured → `threshold_not_configured` (OPEN), not a silent default.

---

## 4. Pipeline and session lifecycle

```text
Discovered market
  → normalized Chainlink/Binance ingress
  → boundary candidate capture (N3)
  → attestation + seal SealedWindowPtb
  → AcceptedBasisEstimate + DynamicAlignedReference
  → DecisionSnapshot.reference = C_hat (model)
  → volatility_price = raw Binance (sigma)
  → assemble_zgap_decision_snapshot / ZGapStrategy  (S=C_hat, K=sealed)
  → OBSERVE facts only (no OMS)
```

**Sessions:** `active` vs `prepared_next` (`runtime/n4_observe_runtime.py`).

- Prepared-next collects books/boundary independently (`publish_as_active=False`).
- Promote atomically; prior window strategy binding dropped (no K/thesis leak).
- Global: Binance history + basis EWMA / AcceptedBasisEstimate may continue.
- Window-local: sealed K, strategy binding, books.

---

## 5. Z-Gap input mapping

| Z-Gap / observe concept | N4A source |
|-------------------------|------------|
| Sealed anchor \(K\) | `SealedWindowPtb.ptb_k` via locked `PtbSnapshot` |
| Model price \(S\) | \(\hat{C}_t\) on `DecisionSnapshot.reference` (`model_spot_source=aligned_c_hat`) |
| Raw fast reference | Binance \(B_t\) (`binance_raw_price`; not model \(S\)) |
| Volatility / \(\sigma\) | Binance raw returns via `volatility_price` / `volatility_ts` |
| settlement_ref (bps gate) | Latest accepted Chainlink \(C_t\) (Chainlink space; distinct from raw \(B\)) |
| Books | Session up/down quotes (YES←Up / NO←Down slots) |
| Time | Existing TimeAuthority |

Observation payload preserves, separately:
`binance_raw_price`, `chainlink_raw` / settlement fields, `instantaneous_basis_ln`,
`basis_estimate_used_ln`, `basis_estimate_as_of_ts`, `aligned_model_price` /
`model_spot`, `sealed_ptb_k`, and `zgap_S_equals_c_hat=true` on evaluated facts.

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
535 passed
```

`tests/test_n4_observe_runtime.py` — sealed K immutability with moving
\(\hat{C}\), estimate as-of, no future leakage, reconstruction/between-ticks,
missing exact, mismatch blockers, late-after-seal, prepared-next promote, EWMA
continuity, replay, fixture CLI, no `old/` imports.

`tests/test_n4_aligned_model_price.py` — decisive regressions that fail if raw
Binance is used as model \(S\):

1. Nonzero venue basis → model gap ≈ 0 at \(\hat{C}=K\), not \(\ln(B/K)\).
2. Direction-changing case → OBSERVE follows \(\hat{C}\) vs \(K\), not \(B\) vs \(K\).
3. Between-ticks: \(\hat{C}\) moves with \(B\); \(K\) fixed.
4. Newer causal basis affects later evaluations only.
5. Unavailable alignment → skip; no hypothetical from raw \(B\).
6. Facts identify raw \(B\) vs model \(\hat{C}\).
7. Replay identity of model spots.

F1–F5 regressions via full suite.

---

## 8. Fixture observation evidence (bounded CLI)

```text
python tools/n4_observe/run_n4_observe.py --mode fixture \
  --fixture tests/fixtures/n3/n1_three_windows.json --windows 2 \
  --out var/reporting/n4/observe_summary.json
```

| Metric | Value |
|--------|--------|
| Windows exercised | 2 (`btc-updown-5m-1784582100`, `btc-updown-5m-1784582400`) |
| PTBs sealed | 2 immutable \(K\) values |
| Fixture Binance ticks ingested | 3 (+ 3 dynamic updates × 2 windows) |
| Fixture Chainlink ticks ingested | 4 |
| Evaluation attempts / completed | 6 / 6 (`kind=evaluated`) |
| Alignment modes | `BETWEEN_TICKS` × 6 |
| Hypothetical decisions by type | `skip:missing_sigma` × 2; `skip:min_samples_not_met` × 4 |
| Hard skip reasons on records | none (`skip_reasons=[]`; strategy soft-skips after model build) |
| `orders_submitted` | 0 |
| OMS / Portfolio mutation | none |

Example (window 1): \(B_t=65275.0\),
\(b^{\mathrm{est}}\approx 2.237\times 10^{-5}\),
\(\hat{C}_t\approx 65276.46\), \(K\approx 65276.79\) with
`model_spot_source=aligned_c_hat` and `zgap_S_equals_c_hat=true`.

Fixture CLI supplies `basis_ewma_half_life_s=30.0` as **fixture-only** estimator
config so alignment is exercisable offline; production half-life remains OPEN.

---

## 9. Deferred live command (N4B)

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

---

## 10. Decisions

### Frozen

- Sealed \(K\) immutable; dynamic \(B_t\) / basis / \(\hat{C}\) not sealed into \(K\)
- Model fair-value pair: \(S=\hat{C}_t\), \(K=K_{\mathrm{chainlink}}\)
- No silent raw-Binance fallback when alignment unavailable
- Pairing `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`
- OBSERVE records only — no OMS/orders
- Up/Down label mapping; YES/NO slots compatibility only

### Provisional

- Preferred boundary `EXACT_AT_START`
- Fixture attestation path for N4A
- OBSERVE may evaluate provisional \(K\) with labels when attested MATCH
- Fixture-only basis EWMA half-life for offline CLI/tests

### OPEN

- Fallback inequality when exact absent
- Mismatch tolerance (nonzero)
- Skew / lateness / freshness / clock uncertainty / **production** EWMA half-life
- Scope-A strategy timing numerics
- Live N4B acceptance

---

## 11. Exact readiness for SHADOW (N5)

| Allowed | Not yet |
|---------|---------|
| Offline SHADOW design against N4A observation contracts (\(S=\hat{C}\)) | Live SHADOW acceptance |
| Fixture dual-ref OBSERVE facts | Claim production OBSERVE on this host |
| | Freeze OPEN thresholds from blocked host |

N5 must not invent OMS mutations beyond existing ShadowOMS fixture paths without
N4B live evidence. **N5 not started** in this correction.

---

## 12. Exclusions confirmed

No SHADOW/LIVE enablement, no OMS/Portfolio/auth/orders, no TLS bypass, no
`.env`, nothing imported from `old/`, nothing pushed, N5 not started.
Production basis EWMA half-life not invented.
