# F2 — Z-Gap model and policy foundations

**Status:** implemented  
**Starting commit:** `8497c816b3e7f045d9b6176a306872d689c7356e`  
**Branch:** `rest_project`

## Objective

Build an offline-testable analytical chain:

fixture market/window + K/PTB + deterministic time + BTC reference + books + fees + optional position  
→ reusable indicators → immutable model snapshot → entry/position valuations → pure Z-Gap policy decision

F2 does **not** wire OBSERVE/SHADOW, OMS, RiskEngine, or venues.

## Architecture placement

| Layer | Ownership | Location |
|-------|-----------|----------|
| Resolution / PTB contracts | Framework / binary-market | `domain/polymarket/resolution.py`, `ptb.py` |
| TimeAuthority | Core (extends Clock) | `core/time_authority.py` |
| Pure φ(p) fee helper | Core (no execution import) | `core/fees_phi.py` |
| EWMA / fair value / basis | Reusable indicators (C) | `indicators/ewma_volatility.py`, `binary_fair_value.py`, `reference_basis.py` |
| Config, snapshots, valuations, policies, reasons, state | Z-Gap-specific (D) | `strategies/z_gap/*` |

**Import firewall:** `strategies/z_gap` must not import adapters, risk, planning, execution, portfolio, persistence, reporting writers, runtime, or `old/`.

## Modules created

```text
src/tyrex_pm/
  domain/polymarket/resolution.py
  domain/polymarket/ptb.py
  core/time_authority.py
  core/fees_phi.py
  indicators/ewma_volatility.py
  indicators/binary_fair_value.py
  indicators/reference_basis.py
  strategies/z_gap/
    __init__.py
    config.py
    reasons.py
    snapshots.py
    valuations.py
    policies.py
    state.py
```

No `strategies/z_gap/strategy.py` (F3 orchestration).

## Formulas and units

| Quantity | Formula | Units |
|----------|---------|-------|
| Log return | \(r=\ln(S_t/S_{t-1})\) | dimensionless |
| EWMA λ | \(e^{-\ln 2 / T_{1/2}}\) | — |
| Variance | \(\lambda\,\mathrm{var}+(1-\lambda)\,r^2\) | — |
| σ | \(\sqrt{\mathrm{var}/dt_s}\) | **per √second** |
| \(\tau_{\mathrm{eff}}\) | \(\max(\tau,\tau_{\mathrm{floor}})\) | seconds |
| \(z\) | \(\ln(S/K)/(\sigma\sqrt{\tau_{\mathrm{eff}}})\) | dimensionless |
| \(p_{\mathrm{UP}}\) | \(\Phi(z)\) via `math.erf` | probability |
| \(p_{\mathrm{DOWN}}\) | \(1-p_{\mathrm{UP}}\) | probability |
| Basis | \((S-S_{\mathrm{ref}})/S_{\mathrm{ref}}\times 10000\) | bps |
| φ(p) | \(r\cdot(p(1-p))^e\) | probability / share |
| \(C_{\mathrm{entry,unit}}\) | \(ask+fee_{\mathrm{buy}}+slip_{\mathrm{buy}}\) | probability |
| \(E_{\mathrm{settlement}}\) | \(p-C_{\mathrm{entry,unit}}\) | probability |
| \(E_{\mathrm{repricing}}\) | \(p-C_{\mathrm{entry,unit}}-F_{\mathrm{exit}}\) | probability |
| \(V_{\mathrm{sell,unit}}\) | \(bid-fee_{\mathrm{sell}}-slip_{\mathrm{sell}}\) (no double slip) | probability |
| Market richness | \(V_{\mathrm{sell,unit}}-p_{\mathrm{held}}\) | probability |

Decimal at economic boundaries; explicit float only for log/CDF/EWMA math.

## PTB/K contract and fixture boundary

- `PtbSnapshot`: K, window, source class, source/receive timestamps, lag, quality, locked, mismatch evidence, provenance, readiness reasons.
- Qualities: `CONFIRMED_CANONICAL` · `PROVISIONAL` · `INFERRED` · `LATE` · `MISMATCHED` · `MISSING`.
- `PtbLockStore`: lock immutability; conflicting candidates → `MISMATCHED` without mutating the locked snapshot.
- `make_fixture_ptb` for offline tests only.
- **Real Chainlink/RTDS/network provider not implemented** (open decision for non-fixture F3).

## TimeAuthority scope

- `FakeTimeAuthority` / `ClockTimeAuthority` over existing `Clock` / `FakeClock`.
- Corrected UTC, monotonic ns, sync status, uncertainty_ms, readiness.
- No SNTP/HTTP/network sync in F2.
- Wall duration and monotonic confirmation duration are independent.

## Atomic epoch

- `DecisionEpoch` binds epoch_id, market/window, evaluated_at, correlation/causation.
- Valuations reject mismatched epochs (`EPOCH_MISMATCH` / `WINDOW_MISMATCH`).
- Pure functions take immutable inputs only (no store queries).

## Reusable vs Z-Gap-specific

| Reusable (C / domain / core) | Z-Gap-specific (D) |
|------------------------------|--------------------|
| EWMA, Φ(z) fair value, basis | Entry/position valuation |
| φ(p) fee curve helper | Leg selection, thesis, realization |
| PTB snapshot + lock store | Action precedence |
| TimeAuthority contract | Z-Gap config / reason codes |

Thresholds (`θ_take`, `θ_rich`, `p_stop`, bands) live only in Z-Gap config/policy.

## Golden provenance

Active fixture: `tests/fixtures/z_gap/fair_value_golden.json`.

- Inputs copied from trusted legacy `old/tests/fixtures/z_gap/fair_value_golden.json` (read-only).
- Expected `z`/`p` recomputed with F2 `math.erf` CDF (legacy fixture used slightly different rounded Φ values).
- Tests never import `old/` or depend on legacy paths.

## Policy functions and precedence

1. UNKNOWN inventory → `BLOCKED`
2. Emergency / kill switch → `FLATTEN`
3. Model invalid → `WAIT` (flat) or `FLATTEN` (active)
4. Thesis invalid → `EXIT`
5. Market-rich realization → `EXIT`
6. Time / resolution semantic preference → `EXIT` or `HOLD` (no `HoldToResolutionIntent` in F2)
7. Hold / skip / entry candidate

Sell remains eligible when resolution holding is available (Correction B). Resolution preference is Z-Gap-local only.

## Configuration defaults (provisional)

See `strategies/z_gap/config.py`. Notable provisional values:

| Field | Default | Label |
|-------|---------|-------|
| EWMA half-life / min samples / jump | 30s / 20s / 4σ | provisional legacy |
| `theta_take` | 0.05 | provisional |
| `theta_rich` | 0.02 | provisional P5 |
| `p_stop` | 0.4013 (≈ Φ(−0.25)) | provisional P5 |
| Fee sample r=0.07, e=1 | until live `fd` | provisional |
| Flatten deadline | 20s before end | provisional operational |

Invalid ranges rejected by `validate_zgap_config`.

## Tests and results

| File | Covers |
|------|--------|
| `tests/test_f2_ptb_time_epoch.py` | PTB quality/lock, time authority, epoch |
| `tests/test_f2_ewma_fair_value.py` | EWMA, golden FV, basis |
| `tests/test_f2_valuations_policies.py` | Fees, valuations, selection, policies |
| `tests/test_f2_architecture.py` | Import firewalls, no host formulas, F1 actions |

Command:

```text
python -m pytest tests -q --tb=no
```

Result: **424 passed** (baseline was 394; +F2 focused tests).

## Explicit exclusions

No real PTB network provider · no network time sync · no `ZGapStrategy` host orchestration · no OBSERVE/SHADOW wiring · no RiskEngine Z-Gap policies · no plans/OMS/portfolio/persistence/facts · no resolution intent/lifecycle · no live/CLI · no `.env` / `var/state/` / `config/r7/` / `runtime/r7*` changes.

## Unresolved real-provider decision

Non-fixture K/settlement-reference provider selection remains open (P3/P4). Fixtures are sufficient for F2 and for F3 offline parity.

## F3 prerequisites

- Thin `ZGapStrategy` orchestration calling the same pure entrypoints
- Host snapshot assembler binding one `DecisionEpoch`
- Fact emission from valuation/policy evidence (no recalculation)
- Optional timer/heartbeat wiring
- Still no Z-Gap formulas inside hosts
