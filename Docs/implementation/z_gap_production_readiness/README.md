# Z-Gap production readiness (N1–N7)

**Status:** planned (documentation only — no N1+ implementation started)  
**Initiative:** one major track taking Z-Gap from fixture OBSERVE/SHADOW to
trustworthy real inputs, real-input OBSERVE/SHADOW, and reconciled
operator-controlled tiny live.  
**Baseline:** F1–F5 accepted · branch `rest_project`  
**Parent index:** [`../README.md`](../README.md)

N1–N7 are **milestones within this initiative**, not separate top-level
programs.

---

## Purpose

Move Z-Gap from deterministic fixture-based OBSERVE/SHADOW to:

1. trustworthy real read-only inputs;
2. real-input OBSERVE;
3. real-input SHADOW;
4. reconciled operator-controlled live execution (tiny, Scope A first).

Preserve frozen architecture: thin strategy, ports/adapters for providers,
same sealed decision path for OBSERVE/SHADOW, fail-closed live enablement,
no `old/` / `runtime/r7*` imports into Z-Gap.

---

## Milestone documents

| Milestone | Document | Role |
|-----------|----------|------|
| **N1** | [n1_source_and_legacy_audit.md](n1_source_and_legacy_audit.md) | Evidence audit: PTB/Chainlink, latency, discovery, `old/` keep-adapt-reject |
| **N2** | [n2_real_data_adapters.md](n2_real_data_adapters.md) | Real read-only adapters (RTDS, Binance, CLOB, time sync) |
| **N3** | [n3_ptb_and_reference_alignment.md](n3_ptb_and_reference_alignment.md) | PTB lifecycle + Chainlink/Binance basis alignment |
| **N4** | [n4_real_observe.md](n4_real_observe.md) | Real-input OBSERVE (no OMS); one-run + continuous |
| **N5** | [n5_real_data_shadow.md](n5_real_data_shadow.md) | Real-input SHADOW (ShadowOMS); labeled simulation |
| **N6** | [n6_live_execution_and_reconciliation.md](n6_live_execution_and_reconciliation.md) | Generic live OMS + recon design (Scope A/B); not Z-Gap money |
| **N7** | [n7_tiny_operator_live.md](n7_tiny_operator_live.md) | Operator-gated tiny live one-shot (Scope A) |

---

## Mode progression (do not collapse)

| Mode | Inputs | Execution | Economics labels | Money |
|------|--------|-----------|------------------|-------|
| Fixture OBSERVE (F3) | Fixtures | None | counterfactual / estimated | No |
| Fixture SHADOW (F4/F5) | Fixtures | ShadowOMS | `simulated_shadow` / estimated | No |
| Real-input OBSERVE (N4) | Live public feeds | None | counterfactual / estimated | No |
| Real-input SHADOW (N5) | Live public feeds | ShadowOMS | `simulated_shadow` / estimated | No |
| Tiny live (N7, Scope A) | Live feeds + auth | LiveOMS | confirmed fills when known | Yes (tiny) |
| Later production live | TBD | LiveOMS (+ Scope B if built) | confirmed + redeem if any | Yes (separate acceptance) |

---

## Roadmap

```text
N1  Source & legacy audit (evidence only)
 └► N2  Real read-only adapters
      └► N3  PTB capture + reference alignment
           └► N4  Real-input OBSERVE (engineering sample)
                └► N5  Real-input SHADOW (simulated execution)
                     └► N6  Generic live execution + reconciliation (no Z-Gap enable)
                          └► N7  Tiny operator-controlled live (Scope A, one-shot)
```

---

## Critical path and dependencies

```mermaid
flowchart TD
  N1[N1 audit] --> N2[N2 adapters]
  N2 --> N3[N3 PTB and basis]
  N3 --> N4[N4 real OBSERVE]
  N4 --> N5[N5 real SHADOW]
  N5 --> N6[N6 live capability]
  N6 --> N7[N7 tiny live Scope A]
  F15[F1-F5 accepted] --> N1
  R7[R7 LiveOMS concepts] -.->|adapt not import| N6
```

| Blocks | Dependency |
|--------|------------|
| N2 coding | N1 frozen source recommendations |
| N3 lock/entry policy | N1 boundary + confirmation semantics |
| N4 product OBSERVE | N2 + N3 |
| N5 | N4 engineering acceptance |
| N6 implementation | N5 + generic reuse of execution stack |
| N7 money | N1–N6 evidence + operator go/no-go |

### Safe parallel work

| Parallelizable | Constraint |
|----------------|------------|
| N1 browser/PTB proof vs latency capture scripts | Both read-only; freeze jointly before N2 |
| Adapter normalize fixtures vs TimeAuthority design | After N1 provisional sources known |
| N6 design detailing vs N4/N5 implementation | N6 **code** should not enable Z-Gap live before N5 |
| Calibration notebook sketches vs N4 facts | Non-blocking research |
| Docs/runbook drafts for N7 | No credentials, no mutate flags on |

---

## Decision gates

| Gate | Must freeze before |
|------|--------------------|
| Boundary sampling + PTB confirmation source | N3 implementation complete |
| Direct Binance vs RTDS Binance for \(S\) | N2 primary wiring |
| Max skew / basis freshness | N3 gates |
| Real OBSERVE sample size | N4 acceptance |
| SHADOW fill assumptions | N5 acceptance |
| Live Scope A vs B | N6/N7 (recommend A) |
| Order type + tiny caps + one-shot workflow | N7 |
| Deployment / clock sync | N4 continuous + N7 |
| Resolution finality / redeem ownership | Scope B only (after N7 A) |

---

## Technical readiness vs non-blocking calibration

### Technical live-readiness gates (blocking)

- Correct market identity and token map  
- PTB/display (or attestation) agreement; lock immutability  
- Dual-reference basis fail-closed; Binance ≠ Chainlink truth  
- Feed freshness + TimeAuthority READY  
- Sealed epochs; no mixed windows  
- UNKNOWN inventory → block + recon  
- Execution truth from fills/balances, not submitted price  
- Restart/recon recovery  
- Kill switch + ack timeout + ambiguous-order stop  
- Operator opt-in; mutations default OFF  
- Post-trade recon clean  

### Profitability / calibration (intentionally non-blocking)

Useful from N4+ facts; **not** required to start N7 Scope A:

- Fair-value calibration / Brier  
- Entry threshold / vol half-life tuning  
- Basis behavior studies  
- Entry timing, rich/thesis/time exit performance  
- Hold-vs-sell comparisons (needs Scope B or sim)  
- Actual vs estimated slip/fees (needs live fills)  
- Performance by exit family / regime  
- Missed / counterfactual opportunities  

---

## Recommended commit boundary per milestone

| Milestone | Commit theme |
|-----------|--------------|
| N1 | `Audit Z-Gap PTB sources, latency, discovery, and legacy reuse` |
| N2 | `Add read-only RTDS, dual-reference, and time-sync adapters` |
| N3 | `Align PTB capture and Chainlink/Binance reference basis` |
| N4 | `Wire real-input Z-Gap OBSERVE with window rollover` |
| N5 | `Run Z-Gap SHADOW on real inputs with labeled simulated execution` |
| N6 | `Add generic live OMS composition and reconciliation for strategies` |
| N7 | `Add operator-gated tiny Z-Gap live one-shot (Scope A)` |

---

## Frozen architectural principles

1. Strategy owns economic decisions and intents.  
2. Framework owns data/state, risk, plans, OMS, fills, Portfolio, lifecycle, persistence, reconciliation, reporting, operations.  
3. Keep `ZGapStrategy` thin.  
4. Real providers enter through reusable ports/adapters.  
5. No provider-specific network access inside strategy or valuation code.  
6. No Z-Gap-specific branching in generic runtime, RiskEngine, planner, OMS, Portfolio, or lifecycle.  
7. OBSERVE and SHADOW use the same sealed decision path.  
8. One-run and continuous-run modes use the same adapters and contracts; only orchestration differs.  
9. Atomic epochs and window identities remain mandatory.  
10. UNKNOWN inventory or conflicting state must block action and trigger reconciliation.  
11. Do not use submitted order price as execution truth.  
12. Do not use Binance as official Chainlink or resolution truth.  
13. No imports from `old/`, `runtime/r7*`, or `config/r7/`.  
14. R7 operational concepts may be selectively reimplemented; R7 must not become a Z-Gap dependency.  
15. Live capability must remain explicitly operator enabled and fail-closed.  

---

## Recommended architecture defaults (provisional until N1/N3 freeze)

| Topic | Recommendation |
|-------|----------------|
| Settlement reference feed | Polymarket RTDS `crypto_prices_chainlink` / `btc/usd` |
| Trading reference \(S\) | Direct Binance Spot WebSocket |
| RTDS Binance | Comparison / fallback only |
| PTB hot path | Structured RTDS (+ attestation); HTML secondary only |
| Basis | \(b_t=\ln(C_t/B_t)\); \(\hat{C}_t=B_t e^{b}\) — never raw \(B\) vs \(K\) |
| First live | **Scope A** (no resolution hold, no redeem) |
| Caps | ~$5 fee-inclusive BUY; one position; one-shot |

---

## Consolidated open decisions

| # | Decision | Why it matters | Decide by | Provisional default |
|---|----------|----------------|-----------|---------------------|
| 1 | Exact Chainlink boundary sampling semantics | Wrong \(K\) invalidates model | End N1 / before N3 done | First RTDS tick with `source_ts ≥ event_start` within max lag — **unproven until N1** |
| 2 | PTB confirmation source | Live entry gate | End N1 | Display agreement and/or dual-source bps; structured preferred |
| 3 | Direct Binance vs Polymarket-proxied Binance | Latency / basis quality | End N1 | Direct Binance primary |
| 4 | Max Chainlink/Binance timestamp skew | Pairing validity | N1 measure → N3 | Open — do not invent |
| 5 | Basis drift/freshness thresholds | Entry/validity gates | N3 (tune later) | Wire config; start from legacy-order magnitudes, label provisional |
| 6 | Required real OBSERVE engineering sample | N4 acceptance bound | N4 start | **5** consecutive windows |
| 7 | SHADOW fill assumptions | Honesty of N5 PnL | N5 start | Latency + slip ticks; no queue model |
| 8 | Initial live Scope A vs B | Redeem/finality burden | Before N6 impl / N7 | **Scope A** |
| 9 | Initial order type / marketable limit | Fill semantics | N6 | FAK-style marketable limit (R7-validated family) |
| 10 | Tiny per-order / per-window / daily limits | Risk | Before N7 run | $5 order; 1 pos/window; daily loss TBD numeric freeze |
| 11 | Live one-shot operator workflow | Safety ceremony | N7 | Explicit opt-in; preflight; post recon; disable after |
| 12 | Deployment location and clock sync | τ / boundary lag | N4 continuous + N7 | Stable host; SNTP+cross-check READY |
| 13 | Resolution finality and redemption ownership | Scope B only | Before any Scope B live | Framework settlement/redeem ports; strategy never redeems |

Unresolved items above are **not** accepted decisions. Assumptions are labeled provisional.
