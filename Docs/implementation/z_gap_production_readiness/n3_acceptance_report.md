# N3 acceptance report — PTB capture and causal reference alignment

**Verdict:** `PASS_WITH_ENVIRONMENT_BLOCKER`  
**Date:** 2026-07-21  
**Branch:** `rest_project`  
**Scope:** N3A offline deterministic implementation only; N3B live Polymarket
acceptance deferred (TLS environment block from N2).

---

## 0. Git / baseline

| Item | Value |
|------|--------|
| Branch | `rest_project` |
| Starting HEAD | `0b2146253db255470fca35f8e593b2c37689df50` |
| Worktree before | Clean |
| Ending HEAD | Tip after N3A commit — verify with `git rev-parse HEAD` |
| Worktree after | Clean |
| Commit message | `Add deterministic PTB capture and causal reference alignment` |

Offline baseline at start: `python -m pytest tests -q --tb=no` → **501 passed**.

---

## 1. Verdict

**`PASS_WITH_ENVIRONMENT_BLOCKER`**

- **N3A:** Complete — boundary candidates, PTB lifecycle, attestation port,
  causal pairing, log-basis/alignment EWMA, sealed reference input, readiness
  reasons, offline tests (including F1–F5 regressions).
- **N3B:** `NOT_RUN_ENVIRONMENT_BLOCKED` — Polymarket TLS hostname mismatch on
  this host (N2 conclusive). Fixture/replay evidence is **not** live evidence.

N4 offline implementation may start against N3A sealed contracts. N4 must not
claim live/production PTB acceptance until N3B runs on a healthy TLS host.

---

## 2. Modules delivered (N3A)

| Concern | Module |
|---------|--------|
| Boundary candidates | `domain/polymarket/boundary_candidates.py` |
| Attestation port + compare | `domain/polymarket/ptb_attestation.py` |
| Capture lifecycle engine | `domain/polymarket/ptb_capture.py` |
| Blocker reason codes | `domain/polymarket/reference_blockers.py` |
| Sealed reference input | `domain/polymarket/sealed_reference.py` |
| Causal pairing | `indicators/causal_pairing.py` |
| Log-basis + EWMA alignment | `indicators/reference_alignment.py` |
| Legacy linear bps (unchanged gates) | `indicators/reference_basis.py` (doc note) |
| Existing lock store | `domain/polymarket/ptb.py` (`PtbLockStore`) |
| Fixtures | `tests/fixtures/n3/n1_three_windows.json` |
| Tests | `tests/test_n3_ptb_alignment.py` |

**Not changed (intentional):** Z-Gap strategy, assemble, OBSERVE/SHADOW hosts,
OMS, Portfolio, adapters redesign, auth/orders.

---

## 3. Boundary candidates

Rule IDs:

| Rule | Semantics |
|------|-----------|
| `EXACT_AT_START` | Chainlink `source_ts == event_start` |
| `FIRST_AT_OR_AFTER` | First tick with `source_ts >= event_start` |
| `LAST_AT_OR_BEFORE` | Last tick with `source_ts <= event_start` |

**Provisional preferred policy:** prefer `EXACT_AT_START` when present
(classification `PROVISIONAL` — N1 three-window exact match to displayed
`openPrice` at 0 bps). **Not promoted to PROVEN.**

If exact is absent, FIRST and LAST are retained and reported. Divergent FIRST
vs LAST yields `ambiguous_fallback_candidates` — **no silent selection**.

Each candidate records market/window, rule, value, Chainlink source_ts, raw and
corrected receive walls, monotonic receive, clock fields, ingress sequence,
connection generation, fingerprint, boundary source delta, receive delay,
late/OOO.

---

## 4. PTB lifecycle

Phases: `WAITING_BOUNDARY` → `CANDIDATE_CAPTURED` → `ATTESTED` → `SEALED`,
with `DEGRADED` / `FAILED` side paths.

- Lock via existing `PtbLockStore` (immutable locked K).
- Seal produces immutable `SealedReferenceInput`.
- Duplicate identical fingerprints: idempotent.
- Conflicting exact values: evidence retained, phase `FAILED` (before seal) or
  `DEGRADED` (after seal; sealed K unchanged).
- Late exact before seal: becomes candidate.
- Late after seal: append-only evidence + `late_event_after_seal`; no rewrite.

---

## 5. Attestation architecture

- Port: `PtbAttestationPort`
- N3A provider: `FixturePtbAttestationProvider` (replay/SSR openPrice fixtures)
- Compare: exact equality only → `MATCH | MISMATCH | INCOMPLETE`
- Classification: `PROVEN | PROVISIONAL | REFUTED | OPEN`
- `mismatch_tolerance_bps` forced unset (**OPEN** — not invented)
- No SSR/HTML hot-path adapter; no invented crypto PTB HTTP endpoint

---

## 6. Causal Binance pairing

**Policy ID (frozen):** `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`

- Latest Binance with `source_ts <= Chainlink source_ts`
- Future Binance rejected even if numerically closer (proven in tests)
- Direct Binance Spot vs RTDS Binance identities distinct; primary stays Spot
- `max_skew_ms` optional; when `None`, skew gate status is **OPEN** (not applied)
- Missing eligible history → `no_causal_binance_pair`

---

## 7. Basis / alignment formula and sign

**Accepted N3 production form** (plan + initiative README; no contradiction with
legacy linear gates once documented as separate):

\[
b_t = \ln(C_t / B_t)
\]

\[
\hat{C}_t = B_t \cdot e^{b_{\mathrm{latest}}}
\]

**Sign:** positive \(b\) when Chainlink is above Binance (\(C > B\)).

**Legacy F2** `compute_basis_bps = (S − S_CL)/S_CL × 10000` remains for existing
Z-Gap gates; **not identical** to \(\ln\cdot 10^4\). N3A does not silently mix them.

**EWMA:** `BasisEwmaState` is continuous across five-minute windows (no rollover
reset). Half-life `None` → `EWMA_NOT_CONFIGURED` / `threshold_not_configured`
(OPEN). Instantaneous basis and \(\hat{C}\) still computable.

Window-relative \(\hat{C}=K\cdot(B_t/B_0)\) remains **deferred/unevaluated**
(documented OPEN), not implemented as live policy.

---

## 8. Sealed reference input

`SealedReferenceInput` includes market/window, start/end, PTB K, boundary rule +
classification, attestation status, trading reference + identity, aligned
raw/smoothed basis / \(\hat{C}\), pairing provenance, clock fields, readiness,
blockers, seal time, evidence ids.

Downstream must not receive mutable stores/adapters/provider payloads.
N3A **exposes** the contract; does **not** bind Z-Gap strategy decisions.

---

## 9. Readiness / blocker reasons (precise)

Includes: `no_boundary_candidate`, `exact_candidate_absent`,
`ambiguous_fallback_candidates`, `no_causal_binance_pair`, `stale_reference`,
`unsynchronized_clock`, `degraded_clock`, `attestation_unavailable`,
`attestation_mismatch`, `late_event_after_seal`, `source_reconnect_gap`,
`threshold_not_configured`, `conflicting_duplicate`.

Never collapsed to a generic `not_ready`.

---

## 10. Offline test evidence (N3A)

```text
python -m pytest tests -q --tb=no
517 passed
```

Coverage in `tests/test_n3_ptb_alignment.py` maps to required cases 1–20
(plus existing OBSERVE/SHADOW regressions in the full suite).

Fixture `tests/fixtures/n3/n1_three_windows.json` is **synthetic N1-shaped
replay**, explicitly labelled not live.

---

## 11. Current-host validation

| Check | Result |
|-------|--------|
| Polymarket Gamma / RTDS / CLOB live | `NOT_RUN_ENVIRONMENT_BLOCKED` |
| TLS bypass | Not used |
| Optional Binance/clock smoke | Not required for N3A; prior N2 evidence stands |
| Fixture presented as live? | **No** |

---

## 12. Deferred N3B live acceptance

On a **healthy Polymarket TLS** host, run a bounded public-data session and
record evidence under `var/reporting/n3/` (gitignored):

```text
# 1) Confirm TLS healthy (must succeed without verify bypass)
python tools/n2_smoke/diagnose_connectivity.py

# 2) Bounded dual-feed capture overlapping ≥3 consecutive BTC 5m boundaries
#    (reuse/adapt N1 capture tooling + N2 adapters; mutation-free)
python tools/n1_audit/capture_sources.py   # or successor N3B script when added
python tools/n1_audit/poll_displayed_ptb.py

# 3) Offline analyze against N3A engine (fixture→live evidence swap)
python -m pytest tests/test_n3_ptb_alignment.py -q
```

**Required N3B evidence pack:**

- current + prepared-next Gamma discovery
- RTDS Chainlink around ≥3 consecutive boundaries
- simultaneous direct Binance
- RTDS Binance comparison when available
- ClockSyncSnapshot stream
- per-window candidate rules (EXACT / FIRST / LAST)
- displayed/structured PTB attestation when available
- exact/bps match results (tolerance still OPEN)
- causal pairing + basis samples
- readiness/blockers + graceful shutdown

N3B must **not** auto-freeze numeric production thresholds.

---

## 13. Decisions

### Frozen

- RTDS Chainlink = settlement reference stream
- Direct Binance Spot = primary trading reference
- RTDS Binance = comparison/fallback only
- Pairing: `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`
- Up/Down label mapping
- Raw vs corrected receive walls distinct
- Append-only raw ingress evidence
- Locked/sealed K immutable
- Log-basis production formula + sign convention (above)

### Provisional

- Preferred boundary rule `EXACT_AT_START` (N1 sample)
- SSR `openPrice` as attestation evidence class (not hot path)
- Fixture attestation path for N3A

### OPEN (not invented in N3A)

- Canonical inequality when exact tick absent
- Crypto PTB structured HTTP
- Production attestation source beyond fixtures/SSR audit
- Max source skew
- Lateness budget
- PTB mismatch tolerance (nonzero)
- Basis freshness/drift limits
- Production clock-uncertainty threshold
- EWMA half-life production value
- Window-relative \(\hat{C}\) adoption

---

## 14. Exact N4 readiness

| Allowed after this verdict | Not allowed yet |
|----------------------------|-----------------|
| N4 offline OBSERVE wiring against `SealedReferenceInput` / capture engine | Claim live PTB/production acceptance |
| Consume sealed K + aligned basis when readiness_ready | Freeze OPEN thresholds from this host |
| Fixture dual-ref paths | Skip N3B on healthy TLS before live OBSERVE acceptance |

---

## 15. Explicit exclusions confirmed

No adapter redesign, no HTML production adapter, no N4 promotion runtime,
no Z-Gap signal/edge, no strategy entry/exit binding, no OMS/Portfolio/lifecycle
mutation, no auth/user channel/orders, no `.env`, no TLS bypass, nothing pushed.
