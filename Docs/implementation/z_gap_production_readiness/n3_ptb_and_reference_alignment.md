# N3 — PTB and reference alignment

**Status:** `PASS_WITH_ENVIRONMENT_BLOCKER` (N3A executed; N3B deferred)  
**Acceptance:** [n3_acceptance_report.md](n3_acceptance_report.md)  
**Document:** `Docs/implementation/z_gap_production_readiness/n3_ptb_and_reference_alignment.md`  
**Depends on:** N1 evidence + N2 settlement/trading reference feeds  
**Unblocks:** N4 offline real-input OBSERVE wiring; N4 live acceptance needs N3B  

N3A delivers deterministic boundary capture, attestation port, causal pairing,
log-basis alignment, and `SealedReferenceInput` without strategy/OMS binding.
N3B live Polymarket evidence remains blocked on the current host TLS interceptor
(see N2).

---

## 1. Objective

Evolve the existing provider-independent PTB and reference-basis contracts into
production-ready services that:

- Capture and classify \(K\) on **three orthogonal axes** (quality, lock, readiness);
- Preserve lock immutability (locked \(K\) never mutates; mismatch evidence allowed);
- Compute **causal** Chainlink/Binance basis and Chainlink-aligned Binance
  estimates without treating Binance as settlement truth;
- Emit facts sufficient for later calibration.

N3 still does **not** productize end-to-end real OBSERVE/SHADOW hosts (N4/N5).

---

## 2. Why the milestone exists

F2 shipped `PtbSnapshot`, `PtbLockStore`, and `compute_basis_bps`, but Z-Gap
binding currently uses Binance as a stand-in settlement reference and fixture K.
Real inputs without alignment logic would produce systematically wrong \(z\) and
false edges.

---

## 3. Scope

### PTB: three orthogonal axes (not one linear machine)

Do **not** model PTB as `candidate → confirmed → locked → mismatched`.

| Axis | Values | Notes |
|------|--------|-------|
| **A. Capture quality** | candidate/provisional · attested/confirmed · mismatched | Attestation from N1-chosen sources |
| **B. Lock state** | unlocked · locked | Locked value is immutable |
| **C. Entry readiness** | allowed · blocked | Derived from quality + lock + mode policy |

Rules:

- A locked \(K\) **never mutates**.
- A locked \(K\) can later receive mismatch evidence → quality may become
  mismatched / readiness **blocked** while preserving the locked numeric value.
- Real SHADOW and live entry require **attested and locked** \(K\) (readiness allowed).
- OBSERVE may evaluate a provisional (unlocked or locked) \(K\) only with explicit
  provisional and counterfactual labels.
- **Lock must occur before** any real SHADOW/live entry evaluation is accepted —
  not “at entry” after economics were already evaluated.
- Exact capture and attestation rule is determined by the N1 audit (provisional
  until proven; runtime attestation remains active).

### Exposure-aware consequences of PTB/basis degradation

| Exposure | Behavior |
|----------|----------|
| FLAT | Block new exposure (entry readiness blocked) |
| ACTIVE confirmed | Continue risk management; seek safe exit — do **not** invent flatness |
| UNKNOWN | Reconcile; never guess quantity |

Entry readiness and exit capability are separate.

### Synchronized basis and aligned estimate

Primary log-basis (recommended production form):

\[
b_t = \ln(C_t / B_t)
\]

Chainlink-aligned Binance estimate:

\[
\hat{C}_t = B_t \cdot e^{b_{\mathrm{latest}}}
\]

Window-relative equivalent (evaluate; do not assume equivalence without evidence):

\[
\hat{C}_t = K \cdot (B_t / B_0)
\]

where \(B_0\) is Binance at the PTB lock / boundary pairing under the **causal** policy.

**Reject:** comparing raw Binance price directly to Chainlink \(K\) as if they
were the same series.

### Causal pairing and gates (live/production)

**Policy ID:** `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`

| Concern | Plan |
|---------|------|
| Timestamp pairing | For each Chainlink tick \(C\) with `source_ts = t_C`, choose latest Binance tick with `source_ts ≤ t_C` |
| Look-ahead | **Forbidden** in live/replay of live policy |
| Max age/skew | \(t_C - t_B ≤\) max skew (from N1; open until measured) |
| Timestamps recorded | source_ts, corrected receive-wall, monotonic receive, clock uncertainty |
| Interpolation / symmetric nearest | **Offline analysis only**, explicitly labelled — creates look-ahead bias if a later Binance tick is used |
| Basis freshness | Age of \(b_{\mathrm{latest}}\) vs max age |
| Basis drift limits | Max \|Δb\| over window / short horizon |
| Divergence gates | \|b\| or bps form vs `basis_max` (Z-Gap config) |
| Precedence | Chainlink-direct for settlement path; Binance-proxy \(\hat{C}\) for model \(S\) when configured |
| After reconnect | Invalidate basis until both series fresh and re-paired causally |
| Deterministic replay | Same ordered ingress → same aligned snapshot; preserve arrival order |
| Lateness budget | Bounded event-time buffer; budget measured N1, frozen here |

**Why not symmetric nearest:** choosing a Binance tick after \(t_C\) uses future
information relative to the Chainlink event and biases basis/\(\hat{C}\) in live
decisions.

---

## 4. Explicit non-goals

- No OMS / live orders
- No replacing F2 pure policy thresholds with adapter logic
- No HTML-primary PTB
- No Binance-as-K
- No full real OBSERVE acceptance (N4)
- No resolution redeem

---

## 5. Dependencies and entry criteria

| Criterion | Notes |
|-----------|-------|
| N1 boundary + confirmation recommendations | Required |
| N2 RTDS Chainlink + Binance feeds | Required |
| Existing `PtbSnapshot` / `PtbLockStore` | Extend, do not fork |
| Existing `compute_basis_bps` | Evolve or complement with log-basis helpers |
| Z-Gap readiness already gates on PTB quality / basis | Keep thresholds in Z-Gap config |

---

## 6. Decisions that must already be frozen

- Provider-independent PTB contract (F2)
- Lock immutability (F2)
- Strategy does not capture PTB
- Dual-reference design (trading vs settlement-associated)
- Sell-side / entry economic corrections A–F unchanged

From N1 (must be frozen or explicitly provisional with owner):

1. Boundary sampling semantics  
2. PTB confirmation source  
3. Max Chainlink/Binance timestamp skew  
4. Whether entry requires confirmed K in real OBSERVE vs SHADOW  

---

## 7. Responsibility / module ownership

| Concern | Owner | Must not |
|---------|-------|----------|
| PTB capture service | domain/runtime composition over adapter ticks | `strategies/z_gap` network |
| `PtbLockStore` | `domain/polymarket/ptb.py` | Silent mutation |
| Basis / \(\hat{C}\) math | `indicators/` (reusable) | Z-Gap thresholds inside indicator |
| Threshold gates | `strategies/z_gap` config/policy | Adapters |
| Binding assembly | `runtime/strategy_binding.py` | Embed capture heuristics beyond wiring |
| Attestation compare | domain/runtime | Strategy inventing K |

---

## 8. Contracts, ports, and data structures to add or evolve

| Item | Change |
|------|--------|
| `PtbSourceClass` | Ensure `SETTLEMENT_BOUNDARY` used for real capture; add attestation class if needed |
| Capture quality axis | provisional / attested / mismatched (orthogonal to lock) |
| Lock state | unlocked / locked; immutable value when locked |
| Entry readiness | allowed / blocked (mode-aware) |
| Capture rule id | Versioned string in `provenance_ref` / attestation map |
| `AlignedReferenceSnapshot` | \(B_t\), \(C_t\), \(b_t\), \(\hat{C}_t\), skew_ms, freshness, validity, **pairing_policy_id** |
| Log-basis helper | Pure indicator alongside or evolving `compute_basis_bps` |
| Pairing policy | Live: `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`; offline-only: labelled nearest/interp |

---

## 9. Expected files / modules affected

```text
src/tyrex_pm/domain/polymarket/ptb.py
src/tyrex_pm/indicators/reference_basis.py
src/tyrex_pm/indicators/*aligned* or extend reference_basis.py
src/tyrex_pm/runtime/strategy_binding.py     # wire dual refs + PTB service
src/tyrex_pm/strategies/z_gap/assemble.py    # consume aligned snapshots
src/tyrex_pm/strategies/z_gap/config.py      # skew/freshness/drift keys
tests/fixtures/z_gap/                        # pairing / mismatch goldens
```

Adapters: consume only; no new venue mutation.

---

## 10. End-to-end data or control flow

```text
RTDS Chainlink ticks + Binance ticks (ordered ingress)
  → causal pair: latest B with source_ts ≤ C.source_ts within max skew
  → b_t = ln(C_t / B_t)
  → C_hat_t = B_t * exp(b_latest)

Boundary / capture rule (N1 provisional or proven)
  → candidate quality (provisional)
  → optional attestation → attested/confirmed or mismatched
  → lock when attestation policy satisfied (before entry eval)
  → readiness allowed only if attested+locked (SHADOW/live)

assemble_zgap_decision_snapshot
  → model uses K + S(=B or C_hat per config) + basis validity
  → entry blocked if readiness blocked / basis stale / skew exceeded
  → ACTIVE exposure still eligible for exit policies
```

---

## 11. Failure and degraded-mode behavior

| Failure | Behavior |
|---------|----------|
| No post-boundary Chainlink tick within max lag | Late/missing quality; entry blocked |
| Locked K vs new attestation disagree | Keep locked K; mismatch evidence; entry blocked; ACTIVE may still exit |
| Skew > max (causal pair unavailable) | Basis invalid/stale; entry blocked |
| One feed reconnecting | Basis not ready until re-paired |
| \(B_0\) missing for window-relative form | Disable that estimator; fall back to log-basis form or block entry |
| Late boundary tick in buffer | Retain for audit/attestation; apply lateness policy |

---

## 12. Persistence and restart behavior

- Persist last locked `PtbSnapshot` per window in host snapshot (SHADOW/live later)
- On restart mid-window: restore locked K if fingerprint matches; do not recapture a different K silently
- If restored K conflicts with live attestation → mismatch / block
- Basis state is recomputed from feeds (optionally warm from short replay buffer)

---

## 13. Facts, metrics, and reporting

| Fact | Purpose |
|------|---------|
| `ptb_candidate` | Capture rule, K, lag_ms |
| `ptb_attestation` | Compare sources, bps diff |
| `ptb_lock` | Locked K + quality |
| `ptb_mismatch` | Conflict evidence |
| `basis_sample` | \(b_t\), skew_ms, ages |
| `aligned_ref` | \(B\), \(C\), \(\hat{C}\), method id |

These feed later calibration of basis gates and PTB lag — **non-blocking** for
tiny live once engineering gates pass.

---

## 14. Configuration ownership and units

| Key | Owner | Units |
|-----|-------|-------|
| `ptb.max_boundary_lag_ms` | z_gap / ptb_time | ms |
| `ptb.mismatch_tolerance_bps` | z_gap / ptb_time | bps |
| `ptb.require_confirmed_for_entry` | mode capability | bool |
| `basis.max_skew_ms` | z_gap / ptb_time | ms |
| `basis.max_age_ms` | z_gap / ptb_time | ms |
| `basis.max_bps` / log equivalent | z_gap entry gates | bps or dimensionless |
| `model.settlement_ref_mode` | z_gap model | `chainlink_direct` \| `binance_aligned` |
| `capture_rule_id` | ptb service | string |

---

## 15. Test strategy

| Test | Assert |
|------|--------|
| Lock immutability | Conflicting candidate does not change locked K |
| Mismatch | Quality/MISMATCHED + evidence |
| Pairing within skew | Deterministic \(\hat{C}\) |
| Pairing beyond skew | INVALID / not ready |
| Reconnect | Basis readiness clears then recovers |
| Replay | Same ticks → same snapshots |
| No raw B vs K gate | Architecture/unit guard on rejected comparison |
| Fixture F2/F3/F4/F5 | Still pass with fixture PTB path |

---

## 16. Deterministic acceptance criteria

1. Real ticks can produce PTB with quality / lock / readiness axes and provenance.
2. Locked K is immutable under conflicting input; mismatch evidence does not rewrite K.
3. Log-basis \(b_t\) and \(\hat{C}_t\) implemented as pure, tested functions under
   `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK`.
4. Live path never uses future Binance ticks relative to Chainlink `source_ts`.
5. Max skew / freshness / drift / lateness gates block **entry** when violated.
6. Z-Gap binding no longer uses Binance-as-Chainlink stand-in when real dual refs are configured.
7. Window-relative estimator evaluated and either adopted with evidence or documented as rejected/deferred.
8. Facts cover candidate/attest/lock/mismatch/basis/pairing_policy_id.
9. Pytest green; no OMS.

---

## 17. Expected deliverables

- PTB capture + attestation + lock service behind ports
- Aligned reference indicator + wiring
- Config keys + unit/golden tests
- N3 acceptance note freezing pairing policy and entry K class rules

---

## 18. Stop conditions

- N1 cannot define a capture rule even provisionally
- Attestation source unavailable and live entry would rely on unproven K
- Pressure to mutate locked K “to fix” a window
- Model would require strategy-side feed access

---

## 19. Remaining risks and decisions

| Decision | Provisional default |
|----------|---------------------|
| Boundary sampling | Legacy candidate: first Chainlink tick with `source_ts >= event_start` within max lag — **confirm/measure in N1**; label provisional if unproven |
| Confirmation | Display agreement and/or dual-source bps; runtime attestation stays on |
| Lock trigger | After attested capture, **before** entry evaluation |
| Entry readiness (real SHADOW/live) | Attested **and** locked |
| Live pairing | `LATEST_BINANCE_AT_OR_BEFORE_CHAINLINK` |
| Max skew / lateness | **OPEN** — measure in N1 |
| \(C\) vs \(\hat{C}\) as model \(S\) | Prefer \(\hat{C}\) when trading on Binance latency; always gate on basis |

---

## 20. Expected commit boundary

```text
N3 commit theme:
  "Align PTB capture and Chainlink/Binance reference basis"

Include: domain PTB service, indicators, binding wiring, tests, N3 docs
Exclude: real OBSERVE host productization (N4), ShadowOMS changes, live OMS
```
