# M2B.4 — Strategy-bound offline lab (updated charter)

**Status:** M2B.4 strict layer accepted; **M2B.4-B exploratory layer** implementation complete — awaiting acceptance  
**Depends on:** M2B.3 (normalizer), M2B.3-A (RTDS + price-to-beat + extended PM events)  
**Authority:** [`tyrex_pm_phase2b_plan.md`](../Phase%202%20completion/tyrex_pm_phase2b_plan.md)  
**Data reference:** [`DATA_LAKE.md`](../../DATA_LAKE.md), [`polymarket_data_coverage.md`](polymarket_data_coverage.md)

> **Implementation gate:** M2B.4 implementation complete per spec rev 2. Awaiting user/reviewer **acceptance** before M2B.5.

---

## 1. Objective

Create offline notebooks that consume the Parquet data lake and produce **decision outputs directly tied to the paired-binary BTC 5-minute strategy**.

This is **not** generic market exploration.

The goal is to replace anecdotal tuning with measured evidence for:

```text
pair stop distance
trail distance
survivor floor buffer
breakeven/recovery arming policy
FAK reprice ladder
liquidity-vacuum warning
event-driven vs fixed-cadence monitoring
BTC-led trigger go/no-go
```

### Milestone boundary (do not blur)

```text
M2B.4 = proxy analysis, decision memos, and parameter-grid preparation.
M2B.5 = authoritative replay through live strategy/survival code.
M2B.7 = formal survival-case labeler.
```

M2B.4 must **not** become replay or a labeler.

---

## 2. Core rule — every notebook must decide something

Every notebook must end with a markdown **DECISION OUTPUT** block:

```text
DECISION OUTPUT:
- numeric candidate or module go/no-go (or "insufficient" if bucket sample too small)
- confidence level (low / medium / high)
- sample size: total sample AND effective per-bucket sample (see Section 7)
- provisional vs stable (see Section 6)
- required follow-up before live enforcement
- insufficient_latency_prior: true/false (Notebook 02 only, when applicable)
```

If a notebook cannot produce a **parameter candidate**, **go/no-go decision**, or an explicit **`insufficient`** verdict for under-sampled buckets, it does not belong in M2B.4.

Plots and exploratory cells are allowed only as supporting evidence for the decision block.

Every **bucketed table** in a notebook must include:

```text
bucket_id
n_observations
n_markets
sufficient: true/false
```

If `sufficient: false`, output **`insufficient`** instead of a numeric candidate for that bucket.

---

## 3. Strategy reminder (what we are measuring)

Current paired-binary lifecycle:

```text
enter both BTC 5m UP/DOWN legs
→ one leg weakens / stop-loss triggers
→ loser is sold
→ survivor floor and recovery level are computed
→ trailing arms only after recovery
→ FAK retry / quality-reject retry handles execution
→ final exit through normal OMS/risk/planner path
```

M2B.4 must measure the **economics of this lifecycle**:

```text
loser stop overshoot
survivor reachability
gap-through-floor behavior
liquidity evaporation
FAK slippage
PM / BTC / Chainlink timing
```

Relevant live parameters (for mapping notebook outputs — **do not change in M2B.4**):

| Area | Example config keys / modules |
|------|-------------------------------|
| Pair entry / stop | `pair_stop_loss_pct`, `slippage_buffer`, `max_spread_*` |
| Survivor floor | `survival.survivor_floor.*` |
| Recovery / arming | `survival.recovery_level.*`, `recovery_buffer` |
| Trailing | `survival.trailing_stop.trail_distance`, `trail_distance_mode` |
| Execution | `entry_order_style` / `exit_order_style` (FAK), quality-reject retry |
| Cadence | `tick_interval_s` vs event-driven monitor (research comparison only) |

---

## 4. Timestamp and alignment discipline

Cross-cutting rules for all notebooks:

```text
- Use source_ts where present.
- Use recv_ts as fallback only.
- Always state which timestamp is used in each notebook (markdown preamble + DECISION OUTPUT).
- time_to_expiry must be computed from the selected event timestamp and markets.event_end_ts.
- Notebook 05 must quote clock_sync drift/latency bounds next to lead-lag estimates.
- If timestamp quality is insufficient, lag outputs must be marked low confidence or insufficient.
```

Expected lags may be on the order of hundreds of milliseconds; alignment choices materially affect lead-lag and protection-distance conclusions.

---

## 5. Required preliminary audit — Notebook 01 (precondition gate)

**Notebook 01 accepted output is a precondition for interpreting notebooks 02–06.**

Before using any strategy-conditioned features in notebooks 02–06, complete this audit in **Notebook 01**:

```text
1. Validate clean-market universe (coverage, PTB present, recording window).
2. Validate direction_vs_price_to_beat against settlement / UI sample where available.
3. Investigate ws_seq_gap semantics (diagnostic vs missing data).
4. Decide whether gap_rate is usable as a feature or only as a diagnostic artifact.
5. Confirm snapshot-dominant book handling (deltas sparse; use snapshots + best_bid_ask).
```

### Required artifact

```text
research/output/m2b4/clean_markets.csv
```

(JSON equivalent acceptable if documented.)

Columns (minimum):

```text
market_id
event_start_ts
event_end_ts
include_for_analysis
exclusion_reason
ptb_present
final_reference_present
coverage_status
dropped_events
corrupt_row_count
gap_rate
gap_rate_usable_as_feature
label_validity_status
```

Notebooks **02–06 must load and reuse** this artifact. Do not re-derive inclusion criteria independently.

### Notebook 01 decision outputs

```text
clean-market filter (artifact path)
label-validity verdict
gap-rate usability verdict
minimum dataset size for stable parameters
```

---

## 6. Data sufficiency rule

### Dataset-level

| Dataset | Use |
|---------|-----|
| Current validated day (`date=2026-07-05`, ~27 markets, RTDS PTB populated) | Build, debug, and run all notebooks; mark outputs **provisional** |
| ≥1 week of recordings **or** ≥500 clean markets across volatility regimes | Required before **stable** tail-risk parameter freeze |

**Stable parameter freeze also depends on accepted longer-horizon recorder artifacts.** Until the M2B.1-B 24h / longer-run recorder gate is accepted, all M2B.4 outputs remain **provisional** even if notebooks execute successfully. This does **not** block M2B.4 implementation; it only blocks stable-parameter claims.

Any DECISION OUTPUT from the current small sample must include:

```text
provisional: true
reason: sample size below stability threshold and/or M2B.1-B long-run gate pending
```

### Per-bucket minimums

Configurable constants in `research/lib/buckets.py` (or equivalent). Suggested defaults:

| Bucket type | Minimum |
|-------------|---------|
| Jump / protection-distance bucket | ≥200 observations |
| Survivor episode bucket | ≥50 synthetic episodes |
| Depth / slippage bucket | ≥100 depth observations |
| Lead-lag fast-move bucket | ≥30 fast-move events |

Under minimum → bucket verdict is **`insufficient`**, not a numeric candidate.

---

## 7. Required supporting artifact: latency prior (before Notebook 02)

**Rationale:** Notebook 02 computes jump distributions and “honest protection distances.” Honest distance must include bot latency, not market move alone.

Protection distance should be based on:

```text
market move window
+ event detection latency
+ decision latency
+ submit latency
+ ack/fill latency when available
```

### Implementation (deferred until M2B.4 authorized)

`research/lib/latency.py` will provide helpers to derive a latency prior from existing live facts. Use **M2B.0-C event/fact correlation fields** when available. Older live runs without full correlation fields may contribute partial priors; confidence must be marked **low** and `missing_fields` documented.

### Output artifact (not inside `research/lib/`)

```text
research/output/m2b4/latency_prior.json
```

Minimum schema:

```text
p50_event_to_decision_ms
p90_event_to_decision_ms
p95_event_to_decision_ms
p50_decision_to_submit_ms
p90_decision_to_submit_ms
p95_decision_to_submit_ms
p50_submit_to_ack_ms
p90_submit_to_ack_ms
p95_submit_to_ack_ms
p50_total_trigger_to_ack_ms
p90_total_trigger_to_ack_ms
p95_total_trigger_to_ack_ms
source_runs
available_fields
missing_fields
confidence
```

### Notebook 02 dependency

- Notebook 02 **must consume** `latency_prior.json` and quote **p50 / p90 / p95** latency in its DECISION OUTPUT when emitting final candidate protection distances.
- If latency prior is **missing**, Notebook 02 may run in **debug mode** but must set `insufficient_latency_prior: true` and **must not** emit final candidate protection distances.

---

## 8. Notebook set (six notebooks — no Notebook 07)

| # | Notebook | Primary tables | Decision outputs |
|---|----------|----------------|------------------|
| **01** | Coverage, quality, label validity | `markets`, `ws_quality`, `price_to_beat`, `lifecycle_events` | `clean_markets.csv`; label-validity; gap-rate usability; min stable sample |
| **02** | Jump distribution + honest protection distances + resolution tail | `trade_prices`, `best_bid_ask`, `reference_prices`, `book_snapshots`, `tick_size_changes`, `price_to_beat`, `markets` | stop/trail/floor candidates; event-driven vs fixed-cadence; near-close recommendations (see §8.1–8.2) |
| **03** | Survivor reachability | `markets`, `price_to_beat`, `trade_prices`, `best_bid_ask`, `reference_prices` | arming go/no-go; ratchet go/no-go; entry-cost filter; **never_armed_and_lost proxy rate** |
| **04** | Depth, slippage, liquidity vacuum | `book_snapshots`, `best_bid_ask`, `trade_prices` | FAK reprice ladder; `min_depth_fraction`; liquidity-vacuum warning go/no-go |
| **05** | PM / BTC / Chainlink lead-lag + divergence + strike liquidity | `reference_prices`, `btc_ticks`, `trade_prices`, `best_bid_ask`, `clock_sync`, `price_to_beat`, `markets` | BTC-led trigger go/no-go; feed role separation; PM reaction lag; divergence census; liquidity-at-strike warning |
| **06** | Pre-replay counterfactual scoring | outputs from 01–05 + `markets` | parameter grid for M2B.5; coarse proxy scoring only — **not replay** |

### 8.1 Notebook 02 — buckets (02a)

Segment analysis by:

```text
time_to_expiry
short-horizon BTC volatility (Chainlink or Binance)
distance_to_price_to_beat
market direction (direction_vs_price_to_beat)
spread regime
```

Requires `latency_prior.json` for final protection-distance candidates (Section 7).

### 8.2 Notebook 02 — Resolution-tail protection behavior (02b)

Required subsection within Notebook 02. Analyze:

```text
T-60s to T+60s behavior
pin speed toward 0.99 / 0.01
spread blowout
liquidity vanish point
tick_size_change events
post-close tradability
```

Inputs: `best_bid_ask`, `trade_prices`, `book_snapshots`, `tick_size_changes`, `price_to_beat`, `reference_prices`, `markets`.

Additional decision outputs:

```text
candidate disable_near_close_s
candidate flatten_before_event_end_s
near-close survivor floor recommendation
post-close tradability verdict
```

All outputs remain **provisional** unless dataset and per-bucket sample thresholds are satisfied.

### 8.3 Notebook 03 — core metric and invisible-loss proxy

Core metric:

```text
P(survivor touches recovery level before expiry | distance_to_beat, time_to_expiry, volatility)
```

**Required explicit report:**

```text
proxy_never_armed_and_lost frequency
```

Definition:

```text
Counterfactual survivor episodes where the survivor never reaches the recovery/trailing-arm
level before expiry and the episode ends unfavorably under the research proxy formula.
```

**Wording:** This is an **M2B.4 proxy diagnostic**, not the formal **M2B.7** survival-case label.

DECISION OUTPUT must include:

```text
never_armed_and_lost_proxy_rate
confidence
sample_size (total + per-bucket)
provisional flag
```

**Sanity check (not acceptance gate):** Synthetic stop frequency should be compared to live-run intuition where available.

Proxy survivor path uses PM quote/trade evidence via `research/lib/episodes.py`; label limitations must be stated in DECISION OUTPUT.

### 8.4 Notebook 04 — book data rule

Use **`book_snapshots` and `best_bid_ask`**, not deltas alone (sessions are snapshot-dominant).

### 8.5 Notebook 05 — feed roles and required analyses

**Feed roles:**

```text
Binance (btc_ticks)          → may explain trader reaction speed
Chainlink (reference_prices) → settlement / price-to-beat reference
Both feeds matter for different reasons — do not collapse into one series
```

**5.1 Binance / Chainlink divergence census**

Analyze windows where Binance-implied direction relative to `price_to_beat` differs from Chainlink-implied direction relative to `price_to_beat`.

Report:

```text
frequency
duration
time_to_expiry distribution
PM UP/DOWN pricing during divergence
whether spreads/depth deteriorate
```

Decision output:

```text
Binance-vs-Chainlink divergence is useful / not useful as a future quality-gate signal
```

**5.2 Liquidity at the strike**

Analyze whether PM liquidity deteriorates when:

```text
abs(Chainlink_or_Binance_price - price_to_beat)
```

is small.

Report:

```text
spread near price_to_beat
depth near price_to_beat
trade intensity near price_to_beat
quote instability near price_to_beat
```

Decision output:

```text
liquidity-at-strike warning go/no-go
```

Quote `clock_sync` drift/latency bounds alongside lead-lag estimates (Section 4).

### 8.6 Notebook 06 — boundary with M2B.5

```text
Notebook 06 is NOT the M2B.5 replay engine.
It assembles candidate parameter grids and coarse proxy scoring only.
Do not import live monitor / survival / execution code.
Do not change live YAML or scenario configs.
Output: parameter grids and provisional enforce on/off recommendations for M2B.5 to validate.
```

---

## 9. `research/lib/` contracts (implementation deferred)

### `loaders.py` — multi-day from day one

Must support (names illustrative):

```text
load_day(path_or_date)
load_days([...])
load_partitions([...])
```

Notebook parameters must accept either:

```text
one parquet day partition
```

or:

```text
a list of parquet day partitions
```

Notebooks may be **demonstrated** on one validated day, but loaders must support multiple days from the start.

### `markets.py`

Load `clean_markets.csv` and apply inclusion filter.

### `episodes.py` — market and counterfactual survivor episode utilities

Purpose:

```text
Market and counterfactual survivor episode utilities.
```

Spec-level capabilities:

```text
- market window iteration
- token quote/trade path extraction
- synthetic stop candidate generation
- survivor path extraction after synthetic stop
- research-only entry/stop/recovery arithmetic
- MFE / MAE / touch / giveback calculations
```

**Boundary:**

```text
Do not import live survival, strategy, execution, risk, OMS, or runtime pipeline code.
```

M2B.4 formulas are **research proxies**. Authoritative formula fidelity is validated later in **M2B.5** replay.

### `latency.py`

Derive latency prior from live facts (Section 7). Output to `research/output/m2b4/latency_prior.json`.

### `buckets.py` (or equivalent)

Per-bucket sufficiency thresholds (Section 6).

### `plots.py`

Shared plotting helpers.

---

## 10. Boundary

### In scope (after sign-off)

- `research/lib/` as specified above
- `research/notebooks/01_coverage_quality.ipynb` … `06_prereplay_counterfactual.ipynb`
- Artifacts under `research/output/m2b4/` (`clean_markets.csv`, `latency_prior.json`, memos)
- Light tests (Section 12)
- Updates to `Docs/DATA_LAKE.md` only if schema join patterns need documenting

### Out of scope

```text
no live config changes
no strategy implementation
no replay engine (M2B.5)
no FeatureBuilder (M2B.6)
no survival labeler (M2B.7)
no advisors (M2B.8)
no Phase 3 adaptive enforcement
no imports from src/tyrex_pm/strategies, survival, execution, risk, runtime/pipeline
```

---

## 11. Current repo reality

| Path | State |
|------|-------|
| `research/normalize/` | **Exists** — M2B.3 complete |
| `var/parquet/date=2026-07-05/` | **Validated** — rich recording; 26/27 PTB observed |
| `Docs/DATA_LAKE.md` | Schema + folder structure documented |
| `research/lib/` | **Does not exist** |
| `research/notebooks/` | **Does not exist** |
| `research/output/m2b4/` | **Does not exist** |

---

## 12. Files to create (after sign-off)

| File | Why |
|------|-----|
| `research/lib/__init__.py` | Package root |
| `research/lib/loaders.py` | Multi-day Parquet loaders |
| `research/lib/markets.py` | Clean-market filter |
| `research/lib/episodes.py` | Counterfactual survivor episodes |
| `research/lib/latency.py` | Latency prior from live facts |
| `research/lib/buckets.py` | Per-bucket sufficiency constants |
| `research/lib/plots.py` | Shared plotting |
| `research/notebooks/01_coverage_quality.ipynb` | Audit + `clean_markets.csv` |
| `research/notebooks/02_jump_protection_distances.ipynb` | 02a + 02b + latency prior |
| `research/notebooks/03_survivor_reachability.ipynb` | Reachability + never_armed proxy |
| `research/notebooks/04_depth_slippage_vacuum.ipynb` | FAK ladder + vacuum |
| `research/notebooks/05_pm_btc_chainlink_leadlag.ipynb` | Lead-lag + divergence + strike liquidity |
| `research/notebooks/06_prereplay_counterfactual.ipynb` | Parameter grid for M2B.5 |
| `research/output/m2b4/.gitkeep` | Artifact root |
| `tests/test_research_lib_loaders.py` | Single-day + multi-day concatenation |
| `tests/test_research_episode_formulas.py` | Pin synthetic episode arithmetic |

### Golden test — episode formulas

`tests/test_research_episode_formulas.py` (or section in loaders test if simpler) must pin at least one synthetic example:

```text
entry cost
loser stop proceeds
computed recovery/breakeven target
survivor touch yes/no
never_armed_and_lost proxy yes/no
```

---

## 13. Files allowed to modify

| File | Modification |
|------|--------------|
| `Docs/README.md` | Link to M2B.4 memos (after implementation) |
| `Docs/Implementation/phase2b_data_backbone/milestones_index.md` | Status updates |
| `pyproject.toml` | Optional `[research]` notebook deps if needed |

---

## 14. Forbidden files / modules

```text
src/tyrex_pm/runtime/paired_binary_run.py
src/tyrex_pm/runtime/pipeline.py
src/tyrex_pm/strategies/*
src/tyrex_pm/survival/*
src/tyrex_pm/execution/*
src/tyrex_pm/risk/*
config/scenarios/live_*
config/strategies/paired_binary.yaml
```

---

## 15. Tests required

```bash
pytest tests/test_research_lib_loaders.py \
  tests/test_research_episode_formulas.py \
  tests/test_research_import_isolation.py -q
```

Requirements:

- `test_research_lib_loaders.py` includes **multi-day / golden-partition concatenation** test
- `test_research_episode_formulas.py` pins synthetic episode arithmetic (Section 12)

Notebook execution validated manually or via nbconvert smoke (optional CI later).

---

## 16. Acceptance criteria

M2B.4 is **accepted** when:

- [ ] All six notebooks execute on at least one normalized day
- [ ] Notebook 01 produces `research/output/m2b4/clean_markets.csv`; notebooks 02–06 consume it
- [ ] `latency_prior.json` produced before Notebook 02 final candidates (or Notebook 02 marks `insufficient_latency_prior: true`)
- [ ] Each notebook contains **DECISION OUTPUT** with confidence, total + per-bucket sample size, provisional flag
- [ ] Bucket tables include `sufficient: true/false`; under-minimum buckets output `insufficient`
- [ ] Written memos committed under `research/output/m2b4/`
- [ ] Results marked **provisional** where sample &lt; stability threshold and/or M2B.1-B long-run gate pending
- [ ] No live `src/tyrex_pm/**` or live scenario YAML modified
- [ ] Import isolation and formula golden tests pass
- [ ] Loaders demonstrated multi-day capable (test-backed)

---

## 17. Divergence risks

| Risk | Control |
|------|---------|
| M2B.4 conclusions applied directly to live YAML | M2B.5 replay validates grids first |
| M2B.4 proxy labels treated as M2B.7 survival cases | Explicit wording in Notebook 03 |
| Protection distances ignore bot latency | Latency prior gate on Notebook 02 |
| Treating derived PTB as official settlement | Notebook 01 label audit |
| Using `gap_rate` as alpha without audit | Notebook 01 verdict required |
| Importing survival code for “accuracy” | Forbidden |
| Overfitting small sample | Provisional flag + per-bucket minimums |
| Stable claims before M2B.1-B 24h acceptance | Section 6 recorder-artifact dependency |

---

## 18. Review checklist (before implementation starts)

Reviewer confirms:

- [ ] Milestone boundary M2B.4 / M2B.5 / M2B.7 is clear
- [ ] Notebook 01 `clean_markets.csv` gate is acceptable
- [ ] Latency prior artifact and Notebook 02 dependency are acceptable
- [ ] Notebook 02b resolution-tail scope is acceptable
- [ ] `episodes.py` proxy scope and golden test are acceptable
- [ ] Per-bucket sample minimums are acceptable
- [ ] Multi-day loaders requirement is acceptable
- [ ] Notebook 05 divergence + strike-liquidity analyses are acceptable
- [ ] Notebook 03 never_armed_and_lost proxy wording is acceptable
- [ ] Timestamp discipline rules are acceptable
- [ ] M2B.1-B dependency for stable (non-provisional) claims is acceptable
- [ ] Authorized to proceed with implementation (**user sign-off required**)

---

## 19. Next milestone dependency

| Milestone | Relationship |
|-----------|--------------|
| **M2B.5** replay | Authoritative validation of parameter grids from Notebook 06 |
| **M2B.7** survival labeler | Formal labels; not M2B.4 proxy diagnostics |
| **M2B.8** advisors | Informed by memos; not implemented in M2B.4 |
| **M2B.1-B** 24h recorder | Blocks **stable** parameter claims, not M2B.4 implementation |
| **Phase 3** enforcement | Requires stable parameters + M2B.5 validation |

---

## 20. Done / not done examples

**Done:** Notebook 02 memo states `pair_stop_loss buffer candidate: 0.10 (provisional, n=26 markets, bucket n=240, p90_latency=85ms, confidence: low)`.

**Done:** Notebook 03 reports `never_armed_and_lost_proxy_rate: 0.18 (provisional, n_episodes=42, M2B.7 label pending)`.

**Done:** Notebook 02 bucket with n=15 outputs `insufficient` for that bucket.

**Not done:** Implementing `SurvivorTrailingStop` in `src/tyrex_pm/survival/`.

**Not done:** Running M2B.5 replay or editing `live_paired_binary_phase1_*.yaml`.

**Not done:** Emitting final Notebook 02 protection distances without `latency_prior.json`.

---

## M2B.4-B — Exploratory & tutorial layer (2026-07-06)

Adds **Layer 2** exploratory traces separate from strict `*_decision.json`:

```text
research/output/m2b4/<nn>_exploratory_trace.json
research/output/m2b4/<nn>_exploratory_trace.md
```

Every exploratory artifact carries `exploratory_only`, `do_not_use_in_live_yaml`, `overfit_warning`.

Also adds EX1–EX3 notebooks, `plot_market_story`, assumed-latency sensitivity tables in Notebook 02 exploratory layer, and partial `submit_to_ack` latency prior extraction from older `oms_submit`/`oms_result` facts.

Strict decision JSON schema and gating logic are unchanged.
