# Z0 — Z-Gap design and legacy audit

**Status:** P0 evidence baseline — legacy Phase A reconstruction (**not** the accepted full-strategy identity)  
**Date:** 2026-07-18 (P0 freeze 2026-07-20)  
**Branch:** `rest_project`  
**HEAD at authoring:** `0a48dfc713060da2ce23091ccacc7df4db91668e`  
**Framework baseline:** `fb9d0d8`  
**Accepted full strategy:** [`z0_z_gap_full_strategy_spectrum.md`](z0_z_gap_full_strategy_spectrum.md)  
**Implementation plan:** [`z_gap_full_strategy_implementation_plan.md`](z_gap_full_strategy_implementation_plan.md)

> **Document role:** Records what legacy Phase A **implemented**, with formula traces and debt.  
> It is **not** rewritten to pretend legacy code already contained corrected sell-side economics, non-sticky resolution hold, or the F1–F5 architecture.  
> Where this audit and the full-strategy spec disagree (especially sell-side richness sign), **spectrum §0 corrections A–F** are canonical for future work.

**Evidence tags used throughout:**

| Tag | Meaning |
|-----|---------|
| **ACTIVE VERIFIED** | Behavior verified in active R8 `src/` and/or `tests/` |
| **LEGACY VERIFIED** | Behavior verified in `old/` code and/or tests |
| **DOCUMENTED** | Claimed in documentation without executable lock |
| **INFERENCE** | Interpretation derived from available evidence |
| **PROPOSAL** | Recommended future behavior for the new framework |
| **USER DECISION** | Ambiguity that must not be silently resolved |

**Authority hierarchy for framework claims:** active code/tests → active implementation evidence → current docs → legacy → inference.  
**Authority hierarchy for Z-Gap hypothesis:** compare legacy code, tests, configs, reports, and docs; do not treat any single legacy file as automatically correct.

---

## A. Executive conclusion

### Recovered Z-Gap hypothesis (known)

Z-Gap is a **single-leg, fee-aware fair-value** strategy on Polymarket **BTC Up/Down 5-minute** markets.

- Settlement question: does reference BTC finish **at or above** a window **price-to-beat** \(K\) (UP) or not (DOWN)?
- Spot \(S\) comes from Binance BTC; \(K\) is captured from a Chainlink/RTDS boundary tick and locked for the window. **LEGACY VERIFIED**
- Short-horizon EWMA volatility \(\sigma\) (per \(\sqrt{\mathrm{second}}\)) and time-to-expiry \(\tau\) produce a digital fair probability via \(z = \ln(S/K)/(\sigma\sqrt{\tau_{\mathrm{eff}}})\), \(p_{\mathrm{UP}}=\Phi(z)\). **LEGACY VERIFIED**
- The strategy buys the **better** YES/NO leg when fee-aware edge exceeds a threshold and quality gates pass. **LEGACY VERIFIED**
- Phase A exits (code): **kill switch → thesis stop on adverse \(z\) → time flatten** near event end. No take-profit, no entry-price stop, no hold-to-resolution. **LEGACY VERIFIED**
- One position per market window; no re-entry after exit. **LEGACY VERIFIED**

### Coherence of legacy material

Legacy material is **sufficiently coherent for a Phase A v1** when the source of truth is treated as:

```text
old/src/tyrex_pm/quant/*  +  entry_eval.py  +  exit_eval.py  +  golden/tests  +  z_gap.yaml
```

Broader strategy docs and Phase B/C plans are useful context but **ahead of or divergent from code** (rich exits, hold-to-resolution, dual bootstrap paths). Those are not silently adopted.

### Most important ambiguities (**USER DECISION**)

1. Confirm **Phase A exits only** for Z-Gap v1 (recommended), versus adopting undocumented Phase B rich-exit/hold behavior.
2. Confirm **PTB source policy**: keep Chainlink/RTDS boundary capture as in legacy, including live/log precedence and 0.5 bps mismatch — versus an alternate PTB definition.
3. Confirm whether **enforce-grade clock uncertainty gates** (TimeAuthority ≤ 250 ms) apply to OBSERVE, SHADOW, or only a future live phase.
4. Confirm **config lag binding**: yaml `max_usable_boundary_lag_ms` vs hardcoded `MAX_USABLE_LAG_MS = 5000` in `ptb_policy.py`.
5. Confirm **Z1–Z4 sequencing** relative to the stub in `Docs/specifications/09_z_gap_future_mapping.md` (see §P).

### Can implementation begin after Z0?

**Yes for design freeze → Z1**, provided the blocking decisions in §Q are answered (or provisional defaults explicitly accepted).  
Critical reconstruction is **not** missing for Phase A math, entry gates, or Phase A exits. Missing items that **do not** block Z1: live fee↔fill attestation, Path A/B bootstrap parity, Phase B features, continuous live.

### Minimum architectural prerequisite (**PROPOSAL**)

Before Z-Gap strategy code can honestly sit on the R8 host:

1. Decouple the strategy protocol from validation-specific `ObserveDecision`.
2. Widen protocol intent returns to include `ExitIntent` / `FlattenIntent` (already consumed by `ShadowHost`).
3. Keep host-owned lifecycle + enriched `DecisionContext`; do **not** require raw `on_execution_event` for v1.
4. Add Z-Gap-owned data contracts (PTB, σ, fair value, fee-aware edge) without importing `old/` or `runtime/r7*`.

Promote only strategy-independent capabilities that Z-Gap’s runtime requirements actually need (settlement concepts, inventory classification, bid-side exit planning ideas, fee vocabulary). Keep R7 acknowledgment CLI, one-shot authorization, and residual operator flows as live-ops concerns.

---

## B. Source inventory

### Active framework (baseline)

| Source | Contents | Role | Recovered |
|--------|----------|------|-----------|
| `src/tyrex_pm/strategies/protocol.py` | `Strategy` callbacks | **ACTIVE VERIFIED** debt | `on_signal` returns `ObserveDecision` + `list[EnterIntent]` only |
| `src/tyrex_pm/strategies/context.py` | `DecisionContext` | **ACTIVE VERIFIED** | Lifecycle snapshot, flatten/hold timers, kill switch, retry gates |
| `src/tyrex_pm/core/intents.py` | Enter/Exit/Cancel/Flatten | **ACTIVE VERIFIED** | Exit/Flatten exist; Exit is `target_flat=True` only |
| `src/tyrex_pm/runtime/observe_host.py` | OBSERVE host | **ACTIVE VERIFIED** | Analytical path; exits deferred in dry path |
| `src/tyrex_pm/runtime/shadow_host.py` | SHADOW host | **ACTIVE VERIFIED** | Risk → ExitPlanner → ShadowOMS → lifecycle |
| `src/tyrex_pm/lifecycle/trade_lifecycle.py` | Host trade phases | **ACTIVE VERIFIED** | FLAT…EXIT_*…TERMINAL |
| `src/tyrex_pm/planning/exit_planner.py` | Shadow SELL plan | **ACTIVE VERIFIED** | Best-bid limit SELL |
| `src/tyrex_pm/execution/polymarket/lifecycle_exit_plan.py` | FAK bid-walk exit | **ACTIVE VERIFIED**, R7-wired | Reusable planning concept |
| `src/tyrex_pm/execution/polymarket/settlement.py` | Flatness / CONFIRMED inventory | **ACTIVE VERIFIED** | Inventory axis |
| `src/tyrex_pm/execution/polymarket/fees_fd.py` | Dynamic `fd` fee helpers | **ACTIVE VERIFIED** | Same φ form as legacy edge fee |
| `src/tyrex_pm/runtime/r7*.py`, `config/r7/` | One-shot live ops | **ACTIVE VERIFIED** phase-specific | Not for Z-Gap import |
| `src/tyrex_pm/indicators/momentum.py`, `signals/directional.py` | Validation strategy stack | **ACTIVE VERIFIED** | Not Z-Gap math |
| `src/tyrex_pm/core/clock.py` | `Clock` / `FakeClock` | **ACTIVE VERIFIED** | No TimeAuthority |
| `Docs/latest/**` | Current framework docs | **DOCUMENTED** + consistency-tested | Protocol debt, two-axis lifecycle |
| `Docs/specifications/09_z_gap_future_mapping.md` | Future mapping stub | **DOCUMENTED** | Sequencing differs from this Z0 handoff |
| `Docs/implementation/r8_framework_acceptance.md` | R8 acceptance | Evidence | Live lessons, exit-floor formulas |
| `Docs/implementation/documentation_consistency_review.md` | Docs↔code review | Evidence | Debt inventory |
| `tests/test_r8_*.py`, `test_r5*.py`, `test_docs_consistency.py` | Framework locks | **ACTIVE VERIFIED** | Protocol absences, flatness, shadow exits |

### Legacy Z-Gap (read-only)

| Source | Contents | Role | Recovered |
|--------|----------|------|-----------|
| `old/src/tyrex_pm/quant/binary_fair_value.py` | \(z\), \(\Phi\), \(p\) | **Authoritative math** | Full FV formula + not-ready reasons |
| `old/src/tyrex_pm/quant/volatility.py` | EWMA σ | **Authoritative math** | Units, jump guard, readiness |
| `old/src/tyrex_pm/quant/fees.py` | φ from `fd` | **Authoritative math** | Rejects flat bps for edge |
| `old/src/tyrex_pm/quant/edge.py` | Fee-aware edge | **Authoritative math** | Leg selection |
| `old/src/tyrex_pm/quant/entry_cap.py` | Model-capped limit | **Authoritative math** | Fill-floor search |
| `old/src/tyrex_pm/quant/model_sanity.py` | Numeric anomaly | **Authoritative** | warn/block \|z\| |
| `old/src/tyrex_pm/strategies/z_gap/entry_eval.py` | Entry gates | **Authoritative decision** | Reason codes, would_enter/skip |
| `old/src/tyrex_pm/strategies/z_gap/exit_eval.py` | Exit precedence | **Authoritative Phase A** | kill > thesis > flatten |
| `old/src/tyrex_pm/exit_policy/thesis_stop.py` | Thesis stop | **Authoritative** | Confirm window |
| `old/src/tyrex_pm/strategies/z_gap/sizing.py` | Fixed USD shares | **Authoritative** | Floor shares |
| `old/src/tyrex_pm/strategies/z_gap/entry_plan.py` | Enforce entry plan | **Authoritative** | FAK + cap |
| `old/src/tyrex_pm/strategies/z_gap/exit_plan.py` | Exit work unit | Evidence | Qty ownership hooks |
| `old/src/tyrex_pm/strategies/z_gap/state.py` | `ZGapPhase` | **Authoritative phases** | IDLE…DONE/FAILED |
| `old/src/tyrex_pm/strategies/z_gap/lifecycle.py` | Fill reconcile | **Authoritative** | Transitions |
| `old/src/tyrex_pm/strategies/z_gap/ptb_policy.py` | PTB select/lock | **Authoritative** | 0.5 bps, 5s lag hardcoded |
| `old/src/tyrex_pm/ingestion/price_to_beat_tracker.py` | Boundary capture | **Authoritative** | Late/missing/observed |
| `old/src/tyrex_pm/state/signal_state_store.py` | \(S\), \(S_{CL}\), PTB, basis | **Authoritative** | Freshness axes |
| `old/src/tyrex_pm/runtime/time_authority.py` | Corrected clock | **Authoritative** | Enforce ≤250 ms uncertainty |
| `old/src/tyrex_pm/runtime/z_gap_run.py` | Observe tick wiring | **Authoritative wiring** | τ, FV, edge, eval |
| `old/src/tyrex_pm/runtime/z_gap_enforce.py` | Enforce loop | Evidence | Explicitly no hold-to-resolution |
| `old/config/strategies/z_gap.yaml` | Defaults | **Authoritative defaults** | Bands, sizing, exits, PTB |
| `old/tests/fixtures/z_gap/fair_value_golden.json` | Golden FV | **Authoritative numeric** | Exact expected z/p |
| `old/tests/test_z_gap_*.py`, `test_binary_fair_value.py` | Behavioral locks | **LEGACY VERIFIED** | Entry/exit/PTB/σ/sizing |
| `old/Docs/.../z_gap_functional_spec.md` | Target behavior | Design intent | Notes deviations |
| `old/Docs/.../architecture.md`, `objective.md`, `migration_map.md`, `evidence_audit.md` | Reset package | Architecture / evidence | Dual-path contradiction |
| `old/Docs/.../z_gap_strategy_v0*.md` | Broader plan | Partially stale | Phase B aspirational |
| `old/Docs/.../d2_ptb_precedence_and_mismatch.md` | PTB policy | Aligned with code | Live/log/lock |
| `old/Docs/.../fee_curve_spike_notes.md` | Live `fd` sample | Evidence; partly stale prose | Formula matches code |
| `old/Docs/.../nautilus_decision.md` | Engine choice | **Stale vs active project** | Active project rejects Nautilus |
| Dual Path A/B session runtimes | Bootstrap divergence | **Contradictory evidence** | Reject dual path for v1 |

---

## C. Plain-language strategy explanation

Polymarket’s BTC Up/Down 5-minute market asks a yes/no question for a short window: will Bitcoin finish **above a strike** (the price-to-beat \(K\)) or not?

- The **YES (UP)** token pays about $1 if BTC finishes ≥ \(K\), else ~$0.
- The **NO (DOWN)** token is the opposite side.
- Market prices for those tokens are like **implied probabilities** (0 to 1), plus spread and fees.

Z-Gap does **not** try to predict long-term BTC direction. It builds a short-horizon model:

1. Watch Binance BTC (\(S\)) as the trading reference.
2. Lock the window’s official strike \(K\) (legacy: first Chainlink tick at/after window start).
3. Estimate how “jumpy” BTC has been recently (\(\sigma\)).
4. Convert \((S, K, \sigma, \text{time left})\) into a fair probability that UP wins.
5. Compare that fair probability to the **executable ask**, after taker fee and a small slippage allowance.
6. If one side looks cheap enough **and** quality gates pass, buy that side with a small fixed dollar budget.
7. If the thesis breaks (model \(z\) crosses against the held side), or time runs out, or a kill switch fires — become flat.

Why a large gap can look like an opportunity: if the model says UP is ~83% likely but the market offers UP at ~70% after fees, the difference is the edge.

Why the signal can still be wrong: model \(\sigma\) can be wrong; \(K\) can be late or mismatched; Binance and Chainlink can diverge (basis); books can be stale or thin; fees can be mis-estimated; and binary outcomes are discrete — a small adverse move near expiry can erase the edge.

### Numerical example (illustrative; golden-locked inputs)

From `old/tests/fixtures/z_gap/fair_value_golden.json` case `s_gt_k` (**LEGACY VERIFIED** vectors; market prices below are **examples** only):

| Symbol | Value | Meaning |
|--------|-------|---------|
| \(S\) | 110 | Binance BTC |
| \(K\) | 100 | Price-to-beat |
| \(\sigma\) | 0.01 | per \(\sqrt{\mathrm{s}}\) |
| \(\tau\) | 100 s | time left |
| \(z\) | ≈ 0.953 | standardized distance |
| \(p_{\mathrm{UP}}\) | ≈ 0.830 | fair UP probability |
| \(p_{\mathrm{DOWN}}\) | ≈ 0.170 | fair DOWN probability |

Suppose UP ask = 0.70, fee φ(0.70) ≈ 0.0147 with \(r=0.07,e=1\) (**example** using legacy fee form), slippage = 1 tick = 0.01:

\[
\mathrm{edge}_{UP} \approx 0.830 - 0.70 - 0.0147 - 0.01 \approx 0.105
\]

With default `theta_take = 0.05`, this would clear the edge threshold **if** all other gates pass. That does **not** prove profitability; it only shows how the decision arithmetic works.

---

## D. Canonical strategy hypothesis

### Canonical v1 hypothesis (**PROPOSAL** grounded in **LEGACY VERIFIED** Phase A)

```text
Observed condition:
  For a BTC Up/Down 5m market, after K is locked and σ is ready,
  fee-aware model edge on one leg ≥ theta_take,
  with z, τ, basis, books, fees, and lifecycle gates passing.

Inferred opportunity:
  The selected YES/NO token is underpriced relative to a short-horizon
  digital fair value after friction.

Desired economic position:
  Long that single outcome token, sized by fixed USD notional (default $5),
  one entry per window.

Invalidation:
  Kill switch; or thesis stop (z crosses against held leg for stop_confirm_s);
  or time flatten when τ ≤ flatten_before_event_end_s;
  or operational/recovery conditions that require flatten.

Expected exit behavior:
  Express ExitIntent/FlattenIntent → framework resolves sellable quantity,
  plans fresh bid-side exit, reconciles settlement → flat / dust / residual / unknown.
  No simultaneous opposite exposure. No re-entry in the same window.
```

| Aspect | Statement | Evidence |
|--------|-----------|----------|
| What is predicted | Short-horizon digital mispricing of UP/DOWN vs model \(p\) | `quant/*`, `entry_eval` |
| Holding horizon | Intra-window only; flatten by default ≥20s before end | `exit.flatten_before_event_end_s` |
| Why edge might exist | Temporary dislocation vs model after fees/slip | Functional spec + edge math |
| Invalidation evidence | Adverse \(z\), kill flags, τ deadline, data failure | `exit_eval`, `thesis_stop` |
| Strategy vs shared risk | Strategy owns thesis/edge/τ flatten reasons; framework owns allowance, sizing caps, OMS, fill truth, inventory truth | Active architecture + legacy intent boundary |

### Alternate hypotheses (not canonical for v1)

| Hypothesis | Source | Status |
|------------|--------|--------|
| Phase B “rich exit” / hold-to-resolution / `z_neutral` timeout | `z_gap_strategy_v0_pahseB.md`, some v0 prose | **DOCUMENTED only** — **reject for v1** unless user chooses otherwise |
| Continuous multi-entry / scaling | Not in Phase A defaults | **Reject for v1** (`one_position_per_window`) |
| Nautilus-hosted Z-Gap | `nautilus_decision.md` | **Reject** — active project forbids Nautilus |

---

## E. Canonical input contracts

| Input | Source | Type/unit | Event time | Receive time | Freshness | Authority | Failure behavior |
|-------|--------|-----------|------------|--------------|-----------|-----------|------------------|
| Market identity | Gamma/discovery | `market_id`, condition, slug | Window metadata | Local receive | Must resolve before trading | Instrument registry | Block / WAIT |
| YES/NO tokens | Market metadata | token ids | Metadata | Local | Required | Instrument registry | Block |
| Window open/close | Market metadata | UTC epoch | Venue schedule | Local | Required for τ | Market metadata | Block if missing τ |
| Public order book | Polymarket WS/book store | price/size, tick | Venue book event time | Adapter receive | Stale → reject | Market state store | `z_gap_book_stale` / quality reject |
| Binance BTC \(S\) | Binance trade/mid | USD | Trade event time | Adapter receive | Stale → reject | Reference store | `z_gap_feed_stale` |
| Chainlink/RTDS \(S_{CL}\) | Chainlink feed | USD | Source tick time | Adapter receive | Fresh required for trusted basis | Reference / signal store | Basis untrusted; enforce may block |
| PTB \(K\) | Boundary derivation from Chainlink at/after `event_start` | USD | Boundary source time | Capture receive | Lag ≤ 5000 ms usable (**hardcoded** in policy) | PTB service + lock store | missing/late/mismatch → no entry |
| Volatility history | Derived from \(S\) samples | σ per √s | Sample bucket times | Local compute | Warmup + jump guard | Strategy indicator state | not ready / jump → no entry |
| Fee `fd` | `/clob-markets` raw | \(r,e,to\) | Market info fetch | Local | Must resolve for edge | Fee resolver | `z_gap_fee_model_unknown` |
| Tick / liquidity | Book + market rules | tick size, depth | Book | Local | Required for plan | Book + planner | quality / depth reject |
| Time authority | SNTP + cross-check (legacy) | corrected now, uncertainty_ms | Sync epoch | Local | Enforce: synced & ≤250 ms | TimeAuthority | Enforce clock fail; observe warns |

### PTB and time semantics (explicit)

| Question | Recovered answer | Tag |
|----------|------------------|-----|
| How is PTB obtained? | First Chainlink tick with `source_ts ≥ event_start_ts`; lag = \((source-start)×1000\) ms | **LEGACY VERIFIED** (`price_to_beat_tracker.py`, D2 doc) |
| When does PTB freeze? | Once a usable \(K\) is selected → **locked** for the window; later conflicting \(K\) rejected | **LEGACY VERIFIED** (`ptb_policy.py`) |
| Is late PTB valid? | Lag > 5000 ms → `late`; not usable for enforce entry | **LEGACY VERIFIED** |
| Live vs log | Prefer usable live; else usable log; both usable and \(|\Delta| > 0.5\) bps → mismatch block | **LEGACY VERIFIED** |
| Boundary membership timestamp | Chainlink **source** time vs `event_start_ts` | **LEGACY VERIFIED** |
| Clock uncertainty | Enforce requires `TimeAuthority` synced and `uncertainty_ms ≤ 250` | **LEGACY VERIFIED** |
| Reconnect / missing data | Missing/stale feeds → skip/not_ready; thesis tracker resets if feeds stale / model not ready | **LEGACY VERIFIED** |
| Authoritative vs informational | Locked \(K\), books, fills/settlement authoritative; basis is a gate (informational feed with hard reject) | **INFERENCE** from gates |

**USER DECISION:** Whether new-framework Z-Gap v1 **must** retain Chainlink/RTDS boundary capture exactly, including log fallback and attestation reference \(K\). Do not invent an alternate PTB silently. Spec stub `09_z_gap_future_mapping.md` lists Chainlink/RTDS under Z1, which aligns with legacy.

**Disagreement:** yaml `ptb.max_usable_boundary_lag_ms: 5000` vs `ptb_policy.MAX_USABLE_LAG_MS = 5000` hardcoded — config may not bind selection. **USER DECISION** to bind config in reimplementation.

---

## F. Exact mathematical specification

Symbols (consistent):

| Symbol | Meaning | Units |
|--------|---------|-------|
| \(S\) | Binance BTC reference | USD |
| \(S_{CL}\) | Chainlink BTC | USD |
| \(K\) | Price-to-beat | USD |
| \(\sigma\) | EWMA volatility | per \(\sqrt{\mathrm{second}}\) |
| \(\tau\) | Time to `event_end` | seconds |
| \(\tau_{\mathrm{floor}}\) | Denominator floor | seconds (default 1) |
| \(z\) | Standardized distance | dimensionless |
| \(p_{\mathrm{UP}}, p_{\mathrm{DOWN}}\) | Fair probabilities | probability in [0,1] |
| \(\phi(p)\) | Taker fee per share | probability units (USDC per share at $1 face) |
| \(\mathrm{ask}\) | Executable ask | probability |
| \(\mathrm{slip}\) | Expected slippage | probability |
| \(\theta_{\mathrm{take}}\) | Entry edge threshold | probability |
| \(\theta_{\mathrm{fill\_floor}}\) | Cap search floor | probability |

### F.1 Returns and EWMA σ — `EwmaVolatilityEstimator` (**LEGACY VERIFIED**)

Trace: `old/src/tyrex_pm/quant/volatility.py`

- Resample to `sample_interval_s` (default **1 s**); last price in bucket.
- \(r_t = \ln(S_t / S_{t-1})\) over actual \(dt_s\).
- \(\lambda = e^{-\ln 2 / T_{1/2}}\) with `half_life_s` default **30**.
- \(\mathrm{var}_t = \lambda\,\mathrm{var}_{t-1} + (1-\lambda)\,r_t^2\)
- \(\sigma_t = \sqrt{\mathrm{var}_t / dt_s}\)
- Jump guard: if \(|r_t| > 4\cdot\sigma_{t-1}\) (default), skip variance update, trip guard, keep prior σ.
- Ready when σ set, `effective_samples_s ≥ min_samples_s` (default **20**), and not jumped.
- Seeding: historical observations accepted with duplicate/future/out-of-order rejects (`SeedResult`).

Zero / missing returns: no update without two prices; invalid prices rejected via readiness. **LEGACY VERIFIED** in estimator policy comments + tests (`test_z_gap_sigma_warmup.py`).

### F.2 Time-to-expiry — `compute_tau_s` (**LEGACY VERIFIED**)

\[
\tau = \max(0,\; t_{\mathrm{end}} - t_{\mathrm{now}})
\]

\(t_{\mathrm{now}}\) from `TimeAuthority.corrected_epoch()` when synced (enforce path).

### F.3 Fair value — `compute_fair_value` (**LEGACY VERIFIED**)

Trace: `old/src/tyrex_pm/quant/binary_fair_value.py`  
Golden: `old/tests/fixtures/z_gap/fair_value_golden.json`

\[
\tau_{\mathrm{eff}} = \max(\tau, \tau_{\mathrm{floor}}),\quad
z = \frac{\ln(S/K)}{\sigma\sqrt{\tau_{\mathrm{eff}}}},\quad
p_{\mathrm{UP}} = \Phi(z),\quad
p_{\mathrm{DOWN}} = 1 - p_{\mathrm{UP}}
\]

\(\Phi\) via `erf`. Not-ready on missing/invalid \(S,K,\sigma,\tau\), unreadiness, unsupported units.

### F.4 Basis (**LEGACY VERIFIED**)

\[
\mathrm{basis\_bps} = \frac{S - S_{CL}}{S_{CL}} \times 10000
\]

Trusted only if Chainlink freshness is fresh (`signal_state_store`). Entry rejects if \(|\mathrm{basis}| > basis_max_bps` (default **3**).

### F.5 Fee φ — legacy `phi_taker_fee` and active `fees_fd` (**LEGACY VERIFIED** + **ACTIVE VERIFIED**)

\[
\phi(p) = r \cdot \bigl(p(1-p)\bigr)^{e},\quad p\in[0,1]
\]

- Legacy edge math: `old/.../quant/fees.py` — **does not** use `fee_rate_bps` for edge.
- Active R7 sizing: `src/.../fees_fd.py` — same φ; also share/USDC helpers and collateral caps.
- Sample parameters from fee notes: \(r=0.07\), \(e=1\) → \(\phi(0.5)=0.0175\) (**DOCUMENTED** sample; treat as provisional until market `fd` resolved at runtime).

### F.6 Edge — `compute_edge` (**LEGACY VERIFIED**)

\[
\begin{align}
\mathrm{edge}_{UP} &= p_{UP} - ask_{UP} - \phi(ask_{UP}) - slip_{UP}\\
\mathrm{edge}_{DOWN} &= p_{DOWN} - ask_{DOWN} - \phi(ask_{DOWN}) - slip_{DOWN}
\end{align}
\]

Select \(\max\). Slippage in observe/plan: `expected_slippage_ticks * tick_size` (default 1 tick).

### F.7 Entry threshold and cap

- Enter candidate if `selected_edge ≥ theta_take` (default **0.05**) after gates. **LEGACY VERIFIED**
- Model-capped limit: highest tick-quantized \(L\) with \(p_L - L - \phi(L) - slip ≥ \theta_{\mathrm{fill\_floor}}\) (default **0.03**) — `entry_cap.py`. **LEGACY VERIFIED**
- Plan limit: `min(ask_seen, max_fill)`; style FAK in enforce plan. **LEGACY VERIFIED**

### F.8 Sizing — `compute_fixed_usd_shares` (**LEGACY VERIFIED**)

\[
\mathrm{shares} = \lfloor \mathrm{max\_usd} / L \rfloor,\quad
\mathrm{notional} = \mathrm{shares}\cdot L \le \mathrm{max\_usd}
\]

Defaults: `max_usd=5`, `min_shares=5`.

### F.9 Thesis stop (**LEGACY VERIFIED**)

- Hold UP: exit if \(z ≤ -z_{\mathrm{stop}}\) for ≥ `stop_confirm_s`
- Hold DOWN: exit if \(z ≥ +z_{\mathrm{stop}}\) for ≥ `stop_confirm_s`
- Defaults: `z_stop=0.25`, `stop_confirm_s=1`
- Reset confirmation if feeds stale / model not ready

### F.10 Parameter table (defaults)

| Parameter | Default | Validated? | Trace |
|-----------|---------|------------|-------|
| `half_life_s` | 30 | Provisional (locked by tests, not walk-forward proven) | yaml + `SigmaConfig` |
| `min_samples_s` | 20 | Provisional | yaml |
| `jump_threshold_sigma` | 4.0 | Provisional | yaml |
| `sample_interval_s` | 1 | Provisional | yaml |
| `tau_floor_s` | 1 | Provisional | yaml |
| `theta_take` | 0.05 | Provisional | yaml |
| `theta_fill_floor` | 0.03 | Provisional | yaml |
| `z_band` | [0.8, 2.2] | Provisional | yaml |
| `tau_band_s` | [60, 210] | Provisional | yaml |
| `basis_max_bps` | 3 | Provisional | yaml |
| `z_stop` | 0.25 | Provisional | yaml |
| `flatten_before_event_end_s` | 20 | Provisional | yaml |
| PTB lag max | 5000 ms | Locked in policy constant | `ptb_policy` |
| PTB mismatch | 0.5 bps | Locked | `ptb_policy` |
| Enforce clock uncertainty | 250 ms | Locked | `time_authority` |
| `block_abs_z` | 20 | Provisional | yaml `model_sanity` |

**USER DECISION:** Accept provisional thresholds for OBSERVE/SHADOW evidence gathering, or require calibration before Z3 acceptance.

### F.11 Formula disagreements

| Topic | A | B | Resolution for Z0 |
|-------|---|---|-------------------|
| Math SoT | Functional spec → `quant/*` | v0.md same core formulas | **Aligned** — use `quant/*` |
| PTB lag config | yaml 5000 | hardcoded 5000 in policy | **Potential dead config** — escalate |
| Exits | Code Phase A trio | Phase B rich/hold docs | **Code wins for v1** |
| Modes | Code observe/enforce | Spec wants explicit shadow | **Map enforce→SHADOW/LIVE later; OBSERVE=observe_only** |
| `clock_drift_threshold_ms` | Config default 500 | `entry_eval` marks clock_drift gate N/A | **Stale config key** — do not revive without decision |
| Fee spike notes “fd not parsed” | Old notes | `quant/fees.py` parses `fd` | **Code wins** |
| Nautilus | Unresolved legacy note | Active project: no Nautilus | **Reject Nautilus** |
| Bootstrap Path A vs B | Spec: one path | Code: unequal wiring | **Reject dual path**; single composition root |

---

## G. Decision model

Economic meanings (names may map to legacy strings or new enums):

| Decision | Economic meaning |
|----------|------------------|
| WAIT | Market/PTB/σ not yet eligible; keep sampling |
| NO_ACTION / SKIP | Eligible enough to evaluate, but gates or edge reject |
| ENTER | Request long exposure on selected leg |
| HOLD | Position active; thesis still valid; no new entry |
| EXIT | Request flatten for strategy/time reason |
| FLATTEN | Urgent flatten (kill / failure / emergency) |
| BLOCKED | Hard fail-closed (clock, mismatch, unknown inventory, kill latched) |

Legacy entry statuses: `would_enter` | `skip` | `not_ready` (`entry_eval.py`). **LEGACY VERIFIED**

### Entry (**LEGACY VERIFIED** gates)

Eligible lifecycle: legacy `IDLE` and `can_submit_entry` (not attempted, not `exited_this_window`).

Required validity (ordered checks in `evaluate_z_gap_entry`):

1. Clock (enforce): TimeAuthority synced / uncertainty  
2. Binance fresh  
3. Chainlink fresh / basis trusted  
4. \(|basis| ≤ basis_max_bps\)  
5. PTB not missing/late/unverified/mismatch  
6. Jump guard not tripped  
7. σ ready  
8. Model sanity (\(|z| ≤ block_abs_z` on enforce)  
9. Books fresh / quality  
10. Fee model resolved  
11. Fair/edge ready  
12. \(\tau \in [60,210]\) s (default)  
13. \(|z| \in [0.8, 2.2]\) (default)  
14. No open position / re-entry blocked  
15. `selected_edge ≥ theta_take`

Dedup / epoch: one entry attempt per window; semantic dedup belongs to framework `RiskEngine` + host retry (**ACTIVE VERIFIED** pattern).

No position adjustment in v1 (no scale-in). **LEGACY VERIFIED** by one-position flags.

### Holding

While ACTIVE: continue evaluating fair value / \(z\) / τ / kill flags for exits. Strategy does not adjust size. **LEGACY VERIFIED**

---

## H. Exit model

### Separation of ownership (**PROPOSAL**, aligned with active architecture)

```text
Strategy: why become flat (thesis / time / kill request as intent reason)
Framework: confirmed/sellable qty, fresh bid-side plan, OMS, settlement,
           inventory class, whether exit completed
```

### Exit catalog

| Exit | Trigger | Owner | Intent | Required state | Failure behavior | Re-entry |
|------|---------|-------|--------|----------------|------------------|----------|
| Kill switch | manual intervention, critical MD, lifecycle failure, max loss | Shared risk / ops flags → strategy emits Flatten | `FlattenIntent` | ACTIVE | MANUAL_INTERVENTION / residual handling | Blocked this window |
| Thesis stop | Adverse \(z\) for `stop_confirm_s` | Strategy | `ExitIntent` | ACTIVE + model ready + feeds fresh | Retry via host; escalate per policy | Blocked this window after exit |
| Time flatten | \(\tau ≤ flatten_before_event_end_s\) | Strategy (time rule) | `ExitIntent` or Flatten if urgent | ACTIVE | Same | Blocked |
| Max loss / exposure | Risk breach | Risk / kill path | Flatten | Any open | Block | Blocked |
| Stale/disconnect | Critical MD / untrusted model | Ops → kill/flatten | Flatten | ACTIVE | Fail closed | Blocked |
| Unknown submission / recovery | Host recovery | Framework | Flatten / block new entries | Non-terminal | Fail closed until resolved | Blocked until flat/dust |
| External/manual flat | Venue evidence | Framework settlement | — | — | `FLAT_EXTERNAL_ACTION` provenance | New epoch only after inventory clear |

**Not in Phase A code (reject for v1 unless USER DECISION):** take-profit, edge-normalization exit, signal-reversal entry flip without flatten-first, hold-to-resolution.

### Default reversal behavior (**PROPOSAL** = active framework lesson + legacy `no_reentry_after_exit`)

```text
Exit current exposure
→ confirm flat or dust-only
→ start a new decision epoch (new market window)
→ reassess opposite entry only in a new eligible window
```

Do **not** hold simultaneous opposite exposure. Legacy blocks same-window re-entry. **LEGACY VERIFIED**

### Shadow vs future live

| Mode | Quantity authority for exit | Planner |
|------|-----------------------------|---------|
| SHADOW | Internal `Portfolio` / fills (**ACTIVE VERIFIED**) | `ExitPlanner` |
| Future live | CONFIRMED trades + funder sellable balance (**ACTIVE VERIFIED** in R7 settlement) | Bid-side FAK planner concepts — **not** via importing `r7*` |

---

## I. Lifecycle specification

### Reconcile legacy phases with active framework

| Legacy `ZGapPhase` | Active `TradeLifecycle` (approx) | Notes |
|--------------------|----------------------------------|-------|
| IDLE | FLAT | Ready to consider entry |
| ENTRY_PENDING | ENTRY_PENDING | Order in flight |
| ACTIVE | ACTIVE | Inventory held |
| EXIT_PENDING | EXIT_REQUESTED / EXIT_PENDING / EXIT_RETRY_WAIT | Exit in flight / retry |
| DONE | FLAT (+ terminal flags) | Window complete |
| FAILED | MANUAL_INTERVENTION / TERMINAL | Fail closed |

Pre-entry waiting concepts (**PROPOSAL** as strategy/host readiness, not necessarily new enum members): WAITING_FOR_MARKET, WAITING_FOR_PTB, WARMING_UP, READY — these are **eligibility**, not portfolio phases.

### Two axes (must remain separate) — **ACTIVE VERIFIED** docs/code

**Inventory:** `FLAT` | `FLAT_WITH_DUST` | `RESIDUAL_EXPOSURE` | `UNKNOWN` (`FlatClassification`)  
**Outcome/provenance:** automatic complete | external/manual flatten | manual intervention | blocked/dry-ok (`TerminalOutcome` / phases — still mixed in R7 types; **debt**)

### Phase rules (smallest complete set for Z-Gap v1)

| Phase | Owner | Strategy actions | Transition triggers | Authoritative evidence | Restart | Dup prevention |
|-------|-------|------------------|---------------------|------------------------|---------|----------------|
| READY/FLAT | Host + strategy eligibility | ENTER or WAIT/SKIP | Entry submit | Snapshot + PTB lock | OK if terminal prior | semantic_key + one_position |
| ENTRY_PENDING | Host lifecycle | No second ENTER | Fill / unfilled / fail | OMS + fills | Fail closed if non-terminal persist | entry_attempted |
| ACTIVE | Host | EXIT/FLATTEN/HOLD | Exit triggers | Portfolio (+ venue in live) | Fail closed if non-terminal | exit outstanding gate |
| EXIT_PENDING | Host | Suppress duplicate exits unless escalate | Fill / partial / fail | OMS + settlement | Resume carefully | RetryController |
| FLAT/DUST | Inventory axis | WAIT next window | Classification | Balance/fills | OK | `exited_this_window` |
| RESIDUAL/UNKNOWN | Framework | No new risk | Ops | Settlement | Manual | Block |

Partial fills: legacy allows EXIT_PENDING → ACTIVE if incomplete (**LEGACY VERIFIED** transitions). Framework must not oversell (**ACTIVE VERIFIED** R7 qty caps / shadow portfolio).

---

## J. Time and callback requirements

### Evaluation

| Option | Needed for Z-Gap v1? | Rationale |
|--------|----------------------|-----------|
| Decouple `ObserveDecision` from validation strategy | **Yes** | Protocol must not import `framework_validation` (**ACTIVE VERIFIED** debt) |
| Widen to `IntentLike` (Enter\|Exit\|Flatten) | **Yes** | Shadow already consumes; Z-Gap Phase A exits require it |
| Shared `StrategyDecision` envelope | **Useful, optional** | Neutral rename of decision object; kinds stay strategy-local |
| `on_timer` | **Not required for v1 if** host evaluates on reference/book ticks **and** injects `now` / `flatten_before_close` into `DecisionContext` | Thesis confirm and τ flatten are time-based but can run on signal cadence; add timer only if quiet periods miss deadlines |
| Raw `on_execution_event` | **Not required for v1** | No demonstrated need beyond updated lifecycle/position in next `on_signal` |
| Lifecycle/position notifications | **Prefer enriched `DecisionContext`** | Matches R5.1 pattern (**ACTIVE VERIFIED**) |

### Smallest recommended protocol shape (**PROPOSAL**)

```text
on_start(context) -> None
on_signal(signal, context) -> tuple[StrategyDecision, list[IntentLike]]
on_stop(reason) -> None

IntentLike = EnterIntent | ExitIntent | FlattenIntent
# CancelIntent later, when host cancel path exists
```

Signal type for Z-Gap should be a **Z-Gap signal** (edge/z/τ/PTB status), not forced through `DirectionalSignal` forever — either widen protocol signal typing in Z1/Z2 or use a shared `Signal` protocol. **USER DECISION** only if it changes host fan-out design; provisional: strategy-specific signal module consumed by a Z-Gap-aware host wiring (composition root), without inventing an abstraction forest.

Raw venue/OMS events stay inside framework:

```text
OMS/execution event → OrderStore/FillLedger/Portfolio/TradeLifecycle
→ next DecisionContext → strategy on_signal
```

---

## K. Active-framework capability mapping

| Requirement | Existing component | Status | Gap | Proposed owner |
|-------------|-------------------|--------|-----|----------------|
| Polymarket books + discovery | adapters + `market_data/*` | already generic | Window/PTB wiring | adapters / market_data |
| Binance reference | `adapters/binance` + `ReferenceDataStore` | already generic | — | market_data |
| Chainlink / PTB | — | **missing** | Full PTB capture/lock | New PTB service (Z2); not core pollution |
| TimeAuthority | `Clock`/`FakeClock` only | **missing** | Corrected clock + uncertainty | `core` or `runtime` clock service (Z1/Z2) |
| EWMA σ / FV / edge | — | **missing** (strategy-specific) | Port math | `indicators` / `signals` / `strategies/z_gap` (Z2) |
| Strategy protocol | `Strategy` | partially available | ObserveDecision leak; Enter-only typing | strategies (Z1) |
| Intents | core intents | already generic | Protocol widen; Cancel host path missing | core + runtime (Z1) |
| Risk | `RiskEngine` + policies | already generic | Possibly Z-Gap allowlists/bands as config policies | risk (extend, don’t fork) |
| Entry planning | `ExecutionPlanner` | partially available | Model cap / FAK specifics | planning (+ strategy evidence) |
| Exit planning (shadow) | `ExitPlanner` | already generic | — | planning |
| Exit planning (live-grade) | `lifecycle_exit_plan` | R7-wired / reusable concept | Promote without `r7*` imports | planning (Z1 selective) |
| OMS shadow | `ShadowOMS` | already generic | — | execution |
| Orders/fills/portfolio | OrderStore, FillLedger, Portfolio | already generic | — | execution / portfolio |
| Lifecycle | `TradeLifecycle` | already generic (shadow) | Map Z-Gap eligibility separately | lifecycle |
| Settlement / inventory class | `settlement.py` | already generic venue evidence | Host use without R7 residual registry | execution/polymarket |
| Persistence | `StateSnapshotStore` | partially available (shadow) | PTB lock persistence | persistence + PTB store |
| Facts | `FactEnvelope` / JSONL | already generic | Z-Gap fact taxonomy | reporting + strategy |
| Fee φ for sizing | `fees_fd.py` | already generic venue helper | Unify vocabulary with edge φ | execution/polymarket |
| Continuous live host | R7 one-shot only | R7-specific / missing generic | Out of Z0–Z4 | future ops |
| Ack / residual CLI | `r7_ack*`, residuals | R7-specific operator | Do not promote | operations (stay) |

---

## L. R7 promotion analysis

| Capability | Current location | Z-Gap requirement | Generic or operator-specific? | Recommendation |
|------------|------------------|-------------------|-------------------------------|----------------|
| CONFIRMED-only inventory | `settlement.py` | Needed before any future live exit truth | Generic venue | **Keep/reuse as-is**; do not bury in strategy |
| Sellable qty = min(confirmed, balance, remaining) | settlement + R7 wiring | Future live | Generic concept | **Reuse concept** in host; not Z1–Z4 live |
| Fresh bid-side FAK exit plan | `lifecycle_exit_plan.py` + `r7_lifecycle_policy` | SHADOW can use simpler `ExitPlanner`; live needs depth walk | Split | **Promote planner helpers** if needed; **leave R7 constants/policy** |
| Retry / partial / no-match | `RetryController`, lifecycle | Yes for SHADOW exits | Generic | **Reuse** |
| Dust/residual/unknown class | `FlatClassification` | Yes for honest terminals | Generic | **Reuse** |
| Fee-inclusive BUY sizing | `fees_fd` + `order_sizing` | Edge uses φ; sizing uses caps | Generic venue | **Reuse helpers**; separate bound vs confirmed |
| MutationLifecycle | `mutation_lifecycle.py` | Not required for OBSERVE/SHADOW | Operator / live one-shot | **Do not promote** into strategy path |
| Acknowledgment gates | `r7_ack*`, `config/r7` | Not required for Z1–Z4 | Operator-specific | **Keep R7-only** |
| Residual registry | `r7_lifecycle_residuals` | Not for Z-Gap v1 shadow | Operator-specific | **Keep R7-only** |
| One-shot live policy / $5 cap / `r7b-live-once` | `r7b_live_once.py`, policies | Out of Z0–Z4 | Operator-specific | **Keep R7-only**; future live redesign |
| LIVE_TINY deny in RiskEngine | `RuntimeModePolicy` | Correct for generic path | Generic safety | **Keep deny** until a dedicated live host exists |

**Verification of user expectation:** settlement, position resolution, exit-planning concepts, and inventory classification **do** contain reusable ideas. Ack policy, one-shot authorization, and operator CLI **should remain** live-operations concerns. Continuous generic live is **out of Z0–Z4**.

---

## M. Fee and P&L contracts

| Concept | Definition | May be claimed when |
|---------|------------|---------------------|
| BUY notional | shares × buy price (or capped USDC spend) | Order/plan known |
| SELL proceeds | shares × sell price | Exit fill known |
| Max fee-bound reservation | Collateral reserved using φ upper bound / sizing cap | Planning/sizing |
| Estimated fee | φ from resolved `fd` at assumed price | `fd` resolved |
| Confirmed venue-reported fee | Fee from fill/trade records | Venue reports it |
| Gross price P&L | SELL proceeds − BUY notional (prices only) | Both fills known |
| Realized net P&L | Gross − confirmed fees (both sides as available) | Confirmed fees known |
| Unknown/incomplete net P&L | Explicitly labeled when fees missing | Otherwise |

**Invariant:** a fee bound or estimated φ **must never** be stored as a confirmed expense. **ACTIVE VERIFIED** docs lesson + R8 notes (`fee_rate_bps=0` vs estimated bound).

| Mode | Fee gap blocking? |
|------|-------------------|
| OBSERVE | Unresolved `fd` blocks **edge-ready entry decision** (legacy `z_gap_fee_model_unknown`) — correct for comparable evidence |
| SHADOW | Same for dispatch; shadow fee model must be labeled estimated |
| Future live | Unknown `fd` or unknown fill fee → no claimed realized net; may still require bound for collateral |

---

## N. Legacy classification

| Component | Classification | Reason | Replacement / destination |
|-----------|----------------|--------|---------------------------|
| `quant/binary_fair_value.py` | **reuse concept** | Sound, golden-locked | Reimplement under `indicators`/`quant` in active tree (no `old` import) |
| `quant/volatility.py` | **reuse concept** | Sound EWMA + jump guard | Same |
| `quant/fees.py` φ edge | **adapt** | Align with active `fees_fd` types | Shared fee math module or thin adapter over `FeeDescriptor` |
| `quant/edge.py`, `entry_cap.py`, `model_sanity.py` | **reuse concept** | Core decision math | `signals` / strategy pure functions |
| `entry_eval.py` | **adapt** | Preserve gate semantics; new types | `strategies/z_gap` pure eval |
| `exit_eval.py` + `thesis_stop.py` | **adapt** | Phase A precedence | strategy exit eval |
| `sizing.py` | **adapt** | Fixed USD → EnterIntent notional | planning/strategy |
| `entry_plan.py` / `exit_plan.py` | **reimplement** | Tied to old OMS/intent shapes | Active planners + intents |
| `state.py` / `lifecycle.py` | **adapt** | Map to `TradeLifecycle` + strategy flags | lifecycle host + strategy private state |
| `ptb_policy.py` + `price_to_beat_tracker.py` | **adapt** | Keep policy; new adapters | PTB service (Z2) |
| `time_authority.py` | **adapt** | Needed concept; clean port | clock/time service |
| `signal_state_store.py` | **adapt** | \(S\), PTB, basis view | market_data / signal store |
| `z_gap_run.py` / `z_gap_enforce.py` | **reimplement** | Decision sequence valuable; runtime shape old | observe/shadow host wiring |
| Path A/B dual bootstrap | **reject** | Contradicts single-path invariant | Single composition root |
| `strategy.py` duck-typed OMS | **reject** | Violates strategy/OMS separation | Strategy protocol |
| Phase B rich exit / hold-to-res | **reject** (v1) | Docs-only; expands scope | Future version |
| Nautilus integration | **reject** | Conflicts with accepted architecture | — |
| Paired-binary / unrelated survival code | **reject** | Out of scope | — |
| Calibration sample machinery | **adapt** (later) | Useful for OBSERVE evidence | reporting Z3 |
| Shadow/scenario harnesses | **adapt** | Test ideas | `tests/` fixtures |
| Live preflight / interactive approval | **reject** for Z1–Z4 | Operator live concern | Future live phase |
| `old/` import bridge | **reject** | Project hard rule | — |

---

## O. Proposed canonical Z-Gap v1

**PROPOSAL** (Phase A parity on R8 hosts):

| Item | Choice |
|------|--------|
| Market family | Polymarket BTC Up/Down 5m only |
| Directions | Single-leg UP or DOWN (model-selected) |
| Positions | At most one position per window; no re-entry after exit |
| Inputs | Books, Binance \(S\), Chainlink/RTDS for PTB+basis, window times, `fd` fees, corrected clock |
| Indicators | EWMA σ; fair value (\(z,p\)) |
| Signal | Fee-aware selected edge + gate summary |
| Entry | Legacy gate stack + `theta_take`; fixed USD sizing |
| Exit | kill → thesis stop → τ flatten only |
| Time | τ bands on entry; flatten_before_end; thesis confirm seconds |
| Risk | Shared RiskEngine + kill switch; no LIVE_TINY via generic path |
| Evidence | Facts: PTB, σ readiness, FV, edge, entry/skip, exit trigger, lifecycle terminal |
| OBSERVE | Same model decision; no OMS; emit facts/calibration rows |
| SHADOW | Intents through risk → plan → ShadowOMS → portfolio/lifecycle → flat |
| Explicit exclusions | Phase B rich exits; hold-to-resolution; continuous live; R7 ack/residual CLI; Nautilus; importing `old/`; multi-position; silent PTB redesign |

Thresholds remain **provisional** until calibration evidence says otherwise (**USER DECISION** for Z3 bar).

---

## P. Z1–Z4 implementation roadmap

Default structure (handoff), with note vs spec stub:

| Source | Sequence |
|--------|----------|
| This Z0 (accepted handoff) | Z1 framework+economics → Z2 data/PTB/indicators → Z3 OBSERVE → Z4 SHADOW |
| `Docs/specifications/09_z_gap_future_mapping.md` | Z1 PTB/time → Z2 observe model → Z3 shadow → Z4 tiny-live |

**PROPOSAL:** follow the handoff sequence. Tiny-live remains **Future**, not Z4. Update spec stub when Z0 is accepted.

### Z1 — Minimum generic strategy/lifecycle promotion + economics

| Field | Content |
|-------|---------|
| Objective | Make the host honest for multi-strategy exits and fee vocabulary without implementing Z-Gap math |
| Scope | Protocol decision decoupling; `IntentLike` widen; DecisionContext sufficiency review; fee/P&L term types or reporting helpers; optional extraction of bid-walk exit helpers **without** R7 policy constants; docs debt labels |
| Modules | `strategies/protocol.py`, new `strategies/decisions.py` (or equivalent), `ReferenceMomentum` import fix-ups, `reporting`/`core` economics terms, possibly `planning/` promotion of exit helpers |
| Contracts | `StrategyDecision` (neutral); `IntentLike`; explicit fee bound vs estimated vs confirmed |
| Tests | Architecture/firewall; protocol typing; ReferenceMomentum still passes; docs consistency |
| Acceptance | Protocol no longer imports `framework_validation`; Shadow exit path typed; fee terms cannot mislabel bounds as confirmed; 384+ tests green |
| Evidence | Unit/architecture facts only |
| Config | None Z-Gap-specific |
| Exclusions | No PTB, no σ/FV, no Z-Gap strategy module, no live |
| Stop/review | User review before Z2 |
| Depends on | Z0 acceptance + blocking §Q decisions for protocol shape |

### Z2 — Z-Gap data/time/PTB + indicators/signals

| Field | Content |
|-------|---------|
| Objective | Reconstruct Phase A model inputs and pure math on active contracts |
| Scope | PTB capture/lock service; Chainlink/reference adapter as required; TimeAuthority (or equivalent); EWMA; fair value; edge; entry/exit pure eval; strategy module **without** OMS |
| Modules | `adapters/*`, `market_data/*`, new PTB store, `indicators/*`, `signals/*`, `strategies/z_gap/*` (pure) |
| Contracts | PTB status machine; σ snapshot; FV/edge snapshots; entry/exit evaluation results |
| Tests | Golden FV parity vs `fair_value_golden.json`; PTB policy tests ported; entry_eval reason codes; thesis stop |
| Acceptance | Bitwise/tolerance parity on golden vectors; PTB lock/mismatch semantics explicit; no `old/` imports |
| Evidence | Offline facts from fixtures |
| Config | `config/strategies/z_gap.yaml` (new active path) |
| Exclusions | No host OBSERVE productization beyond unit harness; no SHADOW; no live |
| Stop/review | User review of math parity + PTB decisions |
| Depends on | Z1 |

### Z3 — Complete OBSERVE validation

| Field | Content |
|-------|---------|
| Objective | End-to-end observe path: feeds → model → decisions → facts (no OMS) |
| Scope | Observe host wiring for Z-Gap; fact taxonomy; fixture + optional network-read observe (no mutations) |
| Modules | `runtime/observe_host` wiring or Z-Gap observe runner; reporting; CLI observe mode selection |
| Contracts | Observe facts schema; mode = analytical only |
| Tests | Fixture e2e would_enter/skip; stale/PTB failure paths; no order facts |
| Acceptance | Same decision function online as unit eval; facts sufficient to audit gates |
| Evidence | JSONL observe reports under `var/reporting/` (disposable) |
| Config | observe config referencing Z-Gap |
| Exclusions | No intents to OMS; no shadow fills; no live |
| Stop/review | Compare to legacy observe evidence if available; threshold provisional OK |
| Depends on | Z2 |

### Z4 — Complete SHADOW entry-to-exit lifecycle

| Field | Content |
|-------|---------|
| Objective | Full shadow lifecycle: entry intent → risk → plan → ShadowOMS → exit → flat/dust |
| Scope | Shadow host wiring; lifecycle ownership; dedup; persistence/recovery; exit precedence |
| Modules | `shadow_host` composition; risk allowlists; persistence |
| Contracts | Intent semantic keys; lifecycle mapping; terminal inventory axis |
| Tests | e2e shadow entry+thesis/time exit; kill flatten; no re-entry; recovery fail-closed |
| Acceptance | One window one position; exits not duplicated; terminals honest |
| Evidence | Shadow JSONL + snapshots |
| Config | shadow config for Z-Gap |
| Exclusions | **No tiny-live**; no ack registry; no venue mutations |
| Stop/review | User review before any future live phase |
| Depends on | Z3 |

### Future (not Z1–Z4)

Operator-controlled live: settlement-confirmed inventory, live exit planner, authorization, residual handling — redesign against R7 lessons without requiring Z-Gap to import `runtime/r7*`.

---

## Q. User decisions

### Blocking before Z1

| ID | Context | Options | Recommendation | Consequences | Provisional default? |
|----|---------|---------|----------------|--------------|----------------------|
| D1 | Protocol decision type | (a) Neutral `StrategyDecision` in strategies/core (b) Keep ObserveDecision but move module | **(a)** | Clears validation coupling | Yes: (a) |
| D2 | Intent return typing | (a) Widen to IntentLike (b) Separate exit callback | **(a)** | Matches ShadowHost today | Yes: (a) |
| D3 | Z1–Z4 vs spec 09 sequencing | (a) Handoff order (b) Spec 09 (PTB first, live as Z4) | **(a)**; update spec stub | (b) risks live in Z4 and delays protocol fix | Yes: (a) |

### Blocking before Z2

| ID | Context | Options | Recommendation | Consequences | Provisional default? |
|----|---------|---------|----------------|--------------|----------------------|
| D4 | PTB source | (a) Legacy Chainlink/RTDS boundary + live/log/0.5bps (b) Alternate source | **(a)** unless product forbids Chainlink | (b) requires new definition + tests | Prefer explicit accept of (a) |
| D5 | Phase A exits only? | (a) kill/thesis/flatten only (b) include Phase B rich/hold | **(a)** | (b) large scope, weak evidence | Yes: (a) |
| D6 | Config lag binding | (a) Bind yaml to policy (b) Keep hardcoded 5000 | **(a)** with default 5000 | Honesty of config | Yes: (a) |
| D7 | Clock gates by mode | (a) Enforce-strict only on future live; SHADOW warn+soft; OBSERVE warn (b) Strict on SHADOW too | **(a)** | Avoid false shadow blocks from lab clocks | Yes: (a) |

### Can wait until Z3/Z4

| ID | Topic | Note |
|----|-------|------|
| D8 | Threshold calibration | Keep yaml defaults provisional until observe evidence |
| D9 | Calibration sample schema | Port/adapt in Z3 |
| D10 | Fact name finalization | Stabilize during Z3 |
| D11 | Whether `on_timer` is needed after observe noise tests | Revisit if τ flatten missed |

### Future live-only

| ID | Topic |
|----|-------|
| D12 | Mutation authorization model (not R7 copy-paste) |
| D13 | Ack / residual operator tooling |
| D14 | Capital and fee-inclusive live caps |
| D15 | Whether live uses promoted `lifecycle_exit_plan` directly |

Low-level Python naming that does not change economics is **not** escalated here.

---

## Z0 acceptance checklist

| Criterion | Status |
|-----------|--------|
| Hypothesis explicit | Yes — §D Phase A |
| Formulas/units traceable | Yes — §F + golden |
| PTB/time semantics explicit | Yes — §E; lag-binding escalated |
| Entry and exit equally specified | Yes — §G/H |
| Contradictions visible | Yes — §F.11, dual bootstrap, Phase B docs |
| Lifecycle ownership clear | Yes — §I/J |
| No duplicate of active capabilities | Yes — §K |
| Framework changes map to requirements | Yes — §J/L/P |
| R7 ops not over-generalized | Yes — §L |
| Fee bound ≠ confirmed fee | Yes — §M |
| Z1–Z4 testable acceptance | Yes — §P |
| Blocking decisions isolated | Yes — §Q |

**Critical evidence missing?** None that block Phase A design freeze. Missing for *live profitability claims* and *parameter optimality* — explicitly out of Z0–Z4 scope.

---

## Verification (Z0 document authoring)

Commands to run after this file is written:

```text
python -m pytest tests -q --tb=no
git status
git diff -- Docs/implementation/z0_z_gap_design_audit.md
```

No commit, no push, no network, no live, no `old/` import, no `.env` / `var/state` modification.
