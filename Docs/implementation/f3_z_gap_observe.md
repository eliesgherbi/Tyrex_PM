# F3 — Z-Gap fixture OBSERVE integration

**Status:** implemented  
**Starting commit:** `2149ca5f8173fd37017d7d1c763c18ac8d46a8ed`  
**Branch:** `rest_project`

## Objective

Wire the pure F2 Z-Gap decision engine into the existing OBSERVE composition path using deterministic fixtures, atomic snapshots, timer evaluation, and auditable facts — with **no OMS, fills, Portfolio mutation, network PTB, or live behavior**.

## Precondition audits

### A. Independent golden oracle

**Finding:** F2 stored `expected_z` / `expected_p_*` that had been recomputed with the same `math.erf` path as production `compute_fair_value` — circular.

**Correction (in this commit):**

- Fixture retains **legacy** `legacy_expected_*` vectors from `old/tests/fixtures/z_gap/fair_value_golden.json` (read-only provenance) with explicit tolerance checks.
- Production expectations come from the **independent dual** `tests/oracles/binary_fair_value_oracle.py` (does not import `tyrex_pm.indicators`).
- Analytical lock: `S=K ⇒ p=0.5`; complement and monotonicity invariants.

### B. Fee-helper ownership

**Finding:** `core/fees_phi.py` encoded the Polymarket `fd` curve (`φ(p)=r(p(1-p))^e` on probability prices), not domain-generic economics.

**Decision:** Relocate to `domain/polymarket/fees.py` as the single authoritative pure owner.

- `execution/polymarket/fees_fd.py` reuses `phi_taker_fee_per_share` for USDC sizing (R7 behavior unchanged; quantization remains in execution).
- Z-Gap imports domain fees only — never `execution.polymarket`.
- `core/fees_phi.py` removed.

## Signal / protocol integration

**Choice:** strategy-kind dispatch via `StrategyBinding` (`runtime/strategy_binding.py`), not a large signal hierarchy and not `isinstance(ZGapStrategy)`.

| Kind | Binding | Host path |
|------|---------|-----------|
| `reference_momentum` | `ReferenceMomentumBinding` | Original `_evaluate_momentum_once` (ShadowHost hooks preserved) |
| `z_gap` | `ZGapObserveBinding` | `_evaluate_zgap_once` → assemble + thin strategy |

Smallest coherent change: keep `Strategy.on_signal(DirectionalSignal, …)` for momentum; Z-Gap uses `ZGapStrategy.on_decision(ZGapDecisionSnapshot, …)` behind the binding. Host selects by `ObserveConfig.strategy_kind` string.

## Thin `ZGapStrategy`

`strategies/z_gap/strategy.py` orchestrates only:

immutable decision input → F2 valuation/policy → F1 `StrategyDecision` + economic intents

Private state: window binding, entry-lineage consumed, thesis-confirm state, decision epoch counter.

Must not: formulas, adapters, stores, RiskEngine, planners, OMS, venue qty/price, fact writers, `execution.polymarket`, `runtime/r7*`.

## Atomic snapshot assembly

`strategies/z_gap/assemble.py` builds one sealed `ZGapDecisionSnapshot` per evaluation from:

normalized `DecisionSnapshot` + EWMA vol snapshot + fixture PTB + TimeAuthority view + fee curve + trigger

Rejects mismatched epochs inside the decision-input type. Host does not embed Z-Gap formulas.

## Timer mechanism

Host-owned `_run_timer_evaluations` after fixture feed publish:

`FakeClock.advance` → `TimerElapsed` fact → `evaluate_once(trigger="timer")`

Same binding/assemble/policy path as feed triggers. No timer logic inside `ZGapStrategy`. No blocking sleeps.

## Fixture / configuration

| Artifact | Role |
|----------|------|
| `config/observe_z_gap_fixture_f3.json` | Fixture OBSERVE + `strategy_kind=z_gap` |
| `tests/fixtures/z_gap/observe_f3_window.json` | Market/books/BTC ticks for one window |

CLI (fixture only): `tyrex-pm observe --config config/observe_z_gap_fixture_f3.json`  
Do not use for live/network.

## OBSERVE semantics

ENTER = would-enter under these conditions (counterfactual).  
No Portfolio mutation, no fills, no realized P&L, no OMS submit.  
Intents recorded with `observe_only` / `oms_submit=false` and estimated/counterfactual labels.

## Expected fixture timeline (typical)

1. Early feed ticks → `WAIT` (`MODEL_NOT_READY` / warmup)
2. First ready edge → `ENTER` + one `EnterIntent` (lineage consumed)
3. Later feed + timer ticks → `SKIP` (no re-entry)
4. Facts include model snapshot, entry valuations, calibration rows, timer fires

## Fact taxonomy

| Fact type | Content |
|-----------|---------|
| `zgap_model_snapshot` | z, p, σ, τ, K, S, PTB quality, time readiness |
| `zgap_entry_valuation` | per-leg edges (counterfactual/estimated) |
| `zgap_calibration_row` | machine-readable calibration schema v1 |
| `observe_decision` | F1 action + reason |
| `intent_created` / `intent_observe_no_oms` | economic intent without OMS |
| `timer_elapsed` | host timer fire |

Reporting stores outputs; it does not import Z-Gap valuation modules.

## Tests / results

| File | Focus |
|------|-------|
| `tests/test_f3_z_gap_observe.py` | OBSERVE timeline, architecture, RM regression |
| Updated F2 golden/fee tests | Preconditions |

Command:

```text
python -m pytest tests -q --tb=no
```

Result: **434 passed** (baseline was 424; +F3 tests / precondition corrections).

## Explicit exclusions

No real PTB/Chainlink/RTDS · no network TimeAuthority · no ShadowOMS · no fills/Portfolio · no Z-Gap risk extensions · no execution plans · no live OMS · no resolution intent · no threshold optimization · no `.env` / `var/state/` / `config/r7/` / `runtime/r7*` edits.

## Unresolved real PTB/reference provider

Still open (P3/P4). F3 uses fixture K only.

## F4 prerequisites

- Same decision path with ShadowOMS on
- Simulated fills → Portfolio truth
- Strategy-driven EXIT against real shadow position
- Still no live venue
