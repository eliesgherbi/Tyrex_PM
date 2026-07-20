# N3 — PTB and reference alignment

**Status:** planned (no implementation in this planning commit)  
**Document:** `Docs/implementation/z_gap_production_readiness/n3_ptb_and_reference_alignment.md`  
**Depends on:** N1 evidence + N2 settlement/trading reference feeds  
**Unblocks:** N4 real-input OBSERVE, N5 real-input SHADOW

---

## 1. Objective

Evolve the existing provider-independent PTB and reference-basis contracts into
production-ready services that:

- Capture and classify \(K\) (candidate → confirmed/attested → locked → mismatched);
- Preserve lock immutability;
- Compute synchronized Chainlink/Binance basis and Chainlink-aligned Binance
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

### PTB lifecycle (if supported by N1 evidence)

| State | Meaning | Trading implication |
|-------|---------|---------------------|
| Candidate / provisional | Best current capture; not attested | OBSERVE OK with labels; entry gated by mode policy |
| Independently confirmed / attested | Agrees with display and/or second source within tolerance | May upgrade quality toward `CONFIRMED_CANONICAL` |
| Locked | Frozen for the window (especially once used for entry) | Immutable |
| Mismatched / blocked | Sources disagree beyond tolerance | Fail closed for the window |

**Lock immutability:** never silently mutate a locked \(K\). If a later
authoritative value conflicts, keep the locked snapshot, mark mismatch evidence,
and fail closed for that window.

**Entry requirement (recommended provisional):** real SHADOW/live entry requires
confirmed/attested K (not merely provisional). OBSERVE may evaluate with
provisional K if explicitly labeled.

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

where \(B_0\) is Binance at the PTB lock / boundary pairing.

**Reject:** comparing raw Binance price directly to Chainlink \(K\) as if they
were the same series.

### Pairing and gates

| Concern | Plan |
|---------|------|
| Timestamp pairing | Pair \(C_t\) and \(B_t\) by nearest `source_ts` within max skew |
| Interpolation | Prefer nearest-tick; interpolation only if N1 shows necessity |
| Max pairing skew | Config; provisional default from N1 (open decision) |
| Basis freshness | Age of \(b_{\mathrm{latest}}\) vs max age |
| Basis drift limits | Max \|Δb\| over window / short horizon |
| Divergence gates | \|b\| or bps form vs `basis_max` policy (Z-Gap config owns threshold) |
| Precedence | Chainlink-direct for settlement path; Binance-proxy \(\hat{C}\) for model \(S\) when configured |
| After reconnect | Invalidate basis until both series fresh and re-paired |
| Deterministic replay | Pure functions of recorded ticks + explicit pairing policy id |

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
| `PtbQuality` | Document upgrade path provisional → confirmed |
| Capture rule id | Versioned string in `provenance_ref` / attestation map |
| `AlignedReferenceSnapshot` | \(B_t\), \(C_t\), \(b_t\), \(\hat{C}_t\), skew_ms, freshness, validity |
| Log-basis helper | Pure indicator alongside or evolving `compute_basis_bps` |
| Pairing policy | Explicit enum: `NEAREST_WITHIN_SKEW` (initial) |

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
RTDS Chainlink ticks + Binance ticks
  → pairing (source_ts within max_skew)
  → b_t = ln(C_t / B_t)
  → C_hat_t = B_t * exp(b_latest)

Boundary / capture rule (N1-frozen)
  → candidate PtbSnapshot (PROVISIONAL)
  → optional attestation vs display / second source
  → quality upgrade or MISMATCHED
  → lock on policy trigger (e.g. first usable confirmed, or at entry)

assemble_zgap_decision_snapshot
  → model uses K + S(=B or C_hat per config) + basis validity
  → readiness fail-closed if K mismatched / basis stale / skew exceeded
```

---

## 11. Failure and degraded-mode behavior

| Failure | Behavior |
|---------|----------|
| No post-boundary Chainlink tick within max lag | `LATE` / missing; block entry |
| Locked K vs new attestation disagree | Keep locked K; `MISMATCHED`; block window |
| Skew > max | Basis INVALID/STALE; block entry; cautious exits later |
| One feed reconnecting | Basis not ready until re-paired |
| \(B_0\) missing for window-relative form | Disable that estimator; fall back to log-basis form or block |

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

1. Real ticks can produce `PtbSnapshot` with provenance and quality classes.
2. Locked K is immutable under conflicting input.
3. Log-basis \(b_t\) and \(\hat{C}_t\) implemented as pure, tested functions.
4. Max skew / freshness / drift gates fail closed.
5. Z-Gap binding no longer uses Binance-as-Chainlink stand-in when real dual refs are configured.
6. Window-relative estimator evaluated and either adopted with evidence or documented as rejected/ deferred.
7. Facts cover candidate/confirm/lock/mismatch/basis.
8. Pytest green; no OMS.

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
| Boundary sampling | Legacy candidate: first Chainlink tick with `source_ts >= event_start` within max lag — **confirm in N1** |
| Confirmation | Display agreement and/or dual-source bps tolerance |
| Entry K class (real SHADOW) | Require confirmed/attested |
| Max skew | Open — measure in N1; do not invent |
| \(C\) vs \(\hat{C}\) as model \(S\) | Prefer \(\hat{C}\) when trading on Binance books latency; always gate on basis |

---

## 20. Expected commit boundary

```text
N3 commit theme:
  "Align PTB capture and Chainlink/Binance reference basis"

Include: domain PTB service, indicators, binding wiring, tests, N3 docs
Exclude: real OBSERVE host productization (N4), ShadowOMS changes, live OMS
```
