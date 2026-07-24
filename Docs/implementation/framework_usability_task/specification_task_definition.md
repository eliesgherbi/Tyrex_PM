# Configurable runtime execution (YAML + CLI)

**Purpose:** Specify a small configuration layer: YAML → resolve/validate → one resolved config → existing OBSERVE or SHADOW.  
**Status:** Frozen specification — **implemented** (YAML OBSERVE/SHADOW `tyrex-pm run`).  
**Path:** `Docs/implementation/framework_usability_task/specification_task_definition.md`  
**Companion plan:** [`implementation_plan.md`](implementation_plan.md)

**Priority:** simple, explicit, usable > abstract or comprehensive.

---

## 1. Objective

Make the existing Tyrex_PM OBSERVE and SHADOW paths configurable so strategy, risk, execution, and runtime parameters are selected from YAML and CLI — without editing Python.

```bash
tyrex-pm run \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/example_risk.yaml \
  --execution config/execution/example.yaml \
  --runtime config/runtime/observe_btc_5m.yaml \
  --scenario aggressive \
  --mode observe \
  --run-name z_gap_aggressive_test
```

Also:

```bash
tyrex-pm run ... --validate-config
tyrex-pm run ... --show-config
```

LIVE_TINY / N7 stay on existing commands. No `--live` on `tyrex-pm run`.

---

## 2. Simplified user workflow

| Step | Action |
|------|--------|
| 1 | Edit YAML under `config/{strategies,risk,execution,runtime}/` |
| 2 | Optionally add `--scenario <name>` (leaf overrides) |
| 3 | `--validate-config` or `--show-config` (same resolve path) |
| 4 | `tyrex-pm run … --mode observe\|shadow` |
| 5 | Existing host runs; facts/reporting unchanged |

| Experiment | How |
|------------|-----|
| Default Z-Gap OBSERVE | required YAML paths + `--mode observe` |
| Aggressive knobs | add `--scenario aggressive` |
| Different risk | change `--risk` only |
| Same knobs in SHADOW | same strategy/risk/runtime; shadow execution; `--mode shadow` |
| Named run | `--run-name …` |
| Inspect finals | `--show-config` |

---

## 3. Current repository findings

| Topic | Finding |
|-------|---------|
| Active CLI | `tyrex-pm` → `application/cli.py`; no `run` yet; no YAML; JSON configs |
| OBSERVE / SHADOW | Subcommands → `ObserveHost` / `ShadowHost` |
| LIVE_TINY | `n7-live` / tools; sealed JSON; **out of this task** |
| Strategy select | `strategy_kind` + `build_strategy_binding` |
| Pure Z-Gap | `strategies/z_gap/config.py` → `ZGapConfig` |
| Flat observe knobs | `ZGapObserveRuntimeConfig` (mixes strategy + wiring) |
| Incomplete bridge | `zgap_config_from_runtime` omits several `ZGapConfig` fields |
| CLI defect | `observe --btc-window` rebuilds `ObserveConfig` without `strategy_kind` / `z_gap` |
| Archived YAML | `old/config/**` + `old/Docs/CONFIG_MODEL.md` — layout inspiration only |
| PyYAML | Not in `pyproject.toml` dependencies today |

### 3.1 Spread checks (two different gates)

| Existing field | Consumer | Role |
|----------------|----------|------|
| `ObserveConfig.max_book_spread` | Host → binding `evaluate(..., max_book_spread=…)`; **ReferenceMomentum** directional signal | Pre-strategy signal spread gate |
| `RiskPlanConfig.max_spread` | `risk/policies.py` → `SPREAD_TOO_WIDE` | Risk authorization on intent |

`ZGapBinding.evaluate` currently ignores `max_book_spread`. They are **not** the same constraint. YAML ownership (§5.2): expose both with unmistakable names; do not merge into one knob.

### 3.2 `target_notional` duplication today

Present on both `RiskPlanConfig` and `ZGapObserveRuntimeConfig`; shadow host can prefer `z_gap.target_notional`. **Resolved ownership:** risk only (§5).

---

## 4. Configuration directory structure

```text
config/
├── strategies/   # strategy identity + Z-Gap mathematics / decisions
├── risk/         # sizing and permitted exposure
├── execution/    # planner and ShadowOMS behavior
├── runtime/      # data source, fixture, freshness, timing, market/window
└── scenarios/    # one bounded leaf-level overlay
```

Every configurable value has **one** authoritative owner (§5).

---

## 5. Configuration ownership model

### 5.1 Strategy YAML — purely strategic

**May contain:**

- strategy identity (`strategy: z_gap`);
- all supported `ZGapConfig` mathematical and decision parameters;
- strategy-owned entry, hold, and normal-exit parameters.

**Must not own:**

| Value | Owner instead |
|-------|----------------|
| `window_id`, `ptb_k` | runtime |
| data `source`, fixture, freshness, duration | runtime |
| evaluate interval / timer counts | runtime |
| resolution evidence path / composition `resolution_capability` | runtime |
| `target_notional` | risk |
| planner / ShadowOMS mechanics | execution |

### 5.2 Risk YAML

Owns sizing and exposure authorization, including the single `target_notional`, `max_notional`, price/liquidity bounds, `max_spread` (risk engine), duplicate lifetime, kill switch, entry-close window.

### 5.3 Execution YAML

Owns planner/ShadowOMS fields (`ShadowConfig` and related). No strategy thresholds. No mutation authorization.

### 5.4 Runtime YAML

Owns:

- `source: fixture | live` (data source; **not** CLI mode);
- fixture path, market/window helpers, `window_id`, `ptb_k` when fixture;
- freshness, `binance_symbol`, duration, evaluate timing;
- `signal_max_book_spread` → maps to existing `ObserveConfig.max_book_spread` (signal gate);
- operational capability flags such as composition `resolution_capability` and `resolution_evidence_path`.

Adapter maps external `source` → existing internal `ObserveConfig.mode` (`SourceMode`).

### 5.5 Single-owner table (normative)

| Value | Owner | Maps to |
|-------|-------|---------|
| All `ZGapConfig.*` | strategy | `ZGapConfig` |
| `target_notional` | risk | `RiskPlanConfig.target_notional` (+ binding/context from risk) |
| `max_notional`, prices, liquidity, kill, duplicate, `no_entry_before_close_s` | risk | `RiskPlanConfig` |
| `max_spread` | risk | `RiskPlanConfig.max_spread` (intent risk) |
| `signal_max_book_spread` | runtime | `ObserveConfig.max_book_spread` (signal gate) |
| Shadow OMS / fill / retry | execution | `ShadowConfig` |
| `source`, fixture, freshness, duration, window/PTB fixture ids, eval timing | runtime | observe runtime fields |
| `resolution_capability`, evidence path | runtime | host/composition wiring |
| CLI `--mode` | CLI | OBSERVE vs SHADOW host + `RiskPlanConfig.runtime_mode` |
| `--run-name` | CLI | output identity only |

---

## 6. Strategy configuration schema

Preserve real nesting (`friction.fee_curve`, not flat fee fields).

```yaml
# config/strategies/z_gap.yaml
strategy: z_gap

parameters:
  volatility:
    half_life_s: 30.0
    min_samples_s: 20.0
    jump_threshold_sigma: 4.0
    sample_interval_s: 1.0
    tau_floor_s: 1.0
  entry:
    theta_take: "0.05"
    theta_fill_floor: "0.03"
    z_min: "0.8"
    z_max: "2.2"
    block_abs_z: "20"
    tau_min_s: 60.0
    tau_max_s: 210.0
    expected_slippage_buy: "0.01"
    exit_friction_reserve: "0.0"
    require_repricing_edge: true
    tie_epsilon: "0.001"
    reject_both_legs_edge: true
  realization:
    theta_rich: "0.02"
    min_exit_depth: "0"
    expected_slippage_sell: "0.01"
    slippage_included_in_executable_bid: true
  thesis:
    p_stop: "0.4013"
    stop_confirm_s: 1.0
    reset_on_stale_model: true
  time_resolution:
    flatten_before_event_end_s: 20.0
    sell_vs_resolve_margin: "0.0"
    resolution_capability_default: false
    ponr_before_event_end_s: 5.0
  ptb_time_quality:
    basis_max_bps: "3"
    max_ptb_lag_ms: 5000
    max_clock_uncertainty_ms: 250
  friction:
    fee_curve:
      fee_rate: "0.07"
      exponent: "1"
    provisional_resolve_penalty_per_share: "0.0"
```

Canonical file should **show** important tunables explicitly. Omitted **optional** fields may take typed code defaults. Loader must support **every** current `ZGapConfig` field. Unknown keys fail. Required fields without defaults fail when absent. New optional typed fields must not invalidate existing YAML.

Money/price values: quoted strings → `Decimal`.

---

## 7. Risk configuration schema

```yaml
# config/risk/example_risk.yaml
target_notional: "5"          # sole owner of target notional
max_notional: "10"
min_price: "0.01"
max_price: "0.99"
max_spread: "0.50"            # RiskEngine SPREAD_TOO_WIDE
min_liquidity_notional: "1"
no_entry_before_close_s: 0
duplicate_lifetime_s: 3600
kill_switch_active: false
# runtime_mode filled from CLI --mode
```

---

## 8. Execution configuration schema

```yaml
# config/execution/example.yaml
kind: shadow

shadow:
  enable_oms: true
  max_position_notional: "20"
  max_total_exposure: "50"
  max_hold_s: 600
  flatten_before_close_s: 20
  exit_on_flat: true
  persistence_path: "var/state/z_gap_shadow_snapshot.json"
  cancel_unfilled_residual: false
  fee_rate: "0"
  fee_model_id: "shadow_zero_fee_v1"
  entry_retry_cooldown_s: 5.0
  entry_max_attempts: 3
  exit_retry_cooldown_s: 3.0
  exit_max_normal_retries: 3
  exit_escalate_after: 2
  require_flat_for_promote: true
  fills:
    model_id: "shadow_depth_walk_v1"
    latency_ms: 250
    extra_slip_ticks: "0"
    tick_size: "0.01"
```

For OBSERVE, `enable_oms: false` (or an observe-oriented execution file) is valid. `--mode shadow` requires `enable_oms: true`.

---

## 9. Runtime configuration schema

```yaml
# config/runtime/observe_btc_5m.yaml
source: fixture                 # fixture | live  (NOT CLI --mode)
fixture_path: "tests/fixtures/n3/n1_three_windows.json"
binance_symbol: "BTCUSDT"
signal_max_book_spread: "0.50"  # → ObserveConfig.max_book_spread (signal gate)
freshness:
  book_threshold_ms: 120000
  reference_threshold_ms: 120000
  future_tolerance_ms: 500
  timestamp_basis: "EVENT_TIME"
runtime_duration_s: null
evaluate_on_reference: false
evaluate_interval_s: 1.0
timer_eval_count: 2
window_id: "zgap-btc-5m"
ptb_k: "100000"
resolution_capability: false
resolution_evidence_path: null
```

Adapter: `source` → `ObserveConfig.mode` (`SourceMode.FIXTURE` / `LIVE`).

---

## 10. Scenario override schema

One schema-aware **leaf-level** overlay by name: `--scenario aggressive` → `config/scenarios/aggressive.yaml`.

```yaml
# config/scenarios/aggressive.yaml
strategy:
  parameters:
    entry:
      theta_take: "0.02"
      z_min: "0.5"
risk:
  max_spread: "0.80"
execution:
  shadow:
    max_hold_s: 300
runtime:
  runtime_duration_s: 120
```

**Allowed:** override existing leaves under strategy / risk / execution / runtime.  
**Forbidden:** inheritance, chaining, recursive includes, expressions, unknown fields, replacing an entire section when only one leaf changes.

---

## 11. CLI contract

Active entrypoint: `tyrex-pm` (`application/cli.py`).

| Flag | Role |
|------|------|
| `--strategy PATH` | required |
| `--risk PATH` | required |
| `--execution PATH` | required |
| `--runtime PATH` | **required** (U1) |
| `--scenario NAME` | optional; `config/scenarios/<name>.yaml` (U4) |
| `--mode observe\|shadow` | required; host selection (U2 — no live) |
| `--run-name NAME` | optional operational identity |
| `--validate-config` | resolve+validate; exit; no run |
| `--show-config` | print **complete** effective config (incl. defaults); exit; no run |

No arbitrary `--set key=value` in v1. No `--live` on this command.

Allowlisted operational CLI values for precedence step 4: `--mode`, `--run-name`, and (if retained for convenience) market helpers that **must not** drop strategy fields when applied during adapt — prefer applying slug on the resolved object, not partial reconstruct.

---

## 12. Configuration precedence

1. Typed code defaults  
2. Selected strategy, risk, execution, runtime YAML  
3. One scenario leaf-level overlay  
4. Small allowlist of operational CLI values (`--mode`, `--run-name`, …)  
5. Hard validation and safety **rejection** (never silent clamp of safety limits)

Result: one validated resolved object. Hosts must not reload YAML.

---

## 13. Validation and error behavior

| Case | Behavior |
|------|----------|
| Missing file / bad YAML | Fail; name file |
| Unknown field | Fail; file + field |
| Wrong type / invalid combo | Fail; file + field |
| Unsupported strategy | Fail |
| Scenario leaf with no base target | Fail |
| Safety-limit violation | **Reject clearly**; do not silently clamp |

`--validate-config` and `--show-config` use the **same** resolve path as `run`.  
`--show-config` prints every final effective value, including defaults applied for omitted optionals.

---

## 14. Mapping to existing runtime components

| Resolved piece | Existing consumer |
|----------------|-------------------|
| `ZGapConfig` (complete) | `build_strategy_binding(..., zgap_config=…)` — **not** incomplete `zgap_config_from_runtime` |
| risk | `RiskPlanConfig` → RiskEngine; `target_notional` for intents/context |
| execution.shadow | `ShadowConfig` → ShadowHost |
| runtime + `--mode observe` | Build `ObserveConfig` (map `source`→`mode`, `signal_max_book_spread`→`max_book_spread`) → ObserveHost |
| runtime + `--mode shadow` | Same observe fields + shadow → ShadowHost |
| strategy id | Always preserved; never silent downgrade |

Fix legacy `observe --btc-window` partial reconstruct as part of migration hygiene (preserve `strategy_kind` / strategy config).

---

## 15. Backward compatibility

- Keep `tyrex-pm observe` / `shadow` temporarily.  
- Prefer routing them through shared builders where cheap.  
- Fix `--btc-window` strategy drop.  
- Do not change N7 / LIVE_TINY commands or behavior.  
- Do not change reporting/facts semantics.  
- Do not import `old/` loaders.

---

## 16. Safety boundaries

- `tyrex-pm run` supports **OBSERVE and SHADOW only**.  
- No LIVE_TINY arming via this command.  
- YAML cannot authorize venue mutations.  
- N7 caps, timing, mutation auth, collateral guards: **untouched**.  
- Safety violations → hard reject.

---

## 17. Defaults and completeness (frozen)

| Rule | Decision |
|------|----------|
| Loader/adapter support | Every current `ZGapConfig` field |
| Canonical `z_gap.yaml` | Explicitly shows important tunables |
| Omitted optional fields | Typed code defaults allowed |
| `--show-config` | Shows all effective values including defaults |
| Unknown fields | Fail |
| Required without default | Fail if absent |
| New optional typed field | Must not break existing YAML |

---

## 18. Accepted decisions (U1–U4 frozen)

| ID | Decision |
|----|----------|
| U1 | `--runtime` is required |
| U2 | Unified command: OBSERVE and SHADOW only |
| U3 | PyYAML + `safe_load` |
| U4 | Scenario by name only → `config/scenarios/<name>.yaml` |

---

## 19. Explicit non-goals

Reporting redesign; analytics; run comparison; dashboards; databases; experiment tracking; parameter sweeps; config inheritance; plugin systems; another strategy; Z-Gap math changes; risk/OMS/portfolio/lifecycle redesign; LIVE_TINY integration; N7 changes.

Existing reporting and facts continue unchanged.

---

## 20. Functional acceptance criteria

1. Z-Gap runs in OBSERVE without editing Python.  
2. Same structure works in SHADOW.  
3. Strategy, risk, execution, runtime come from explicit YAML.  
4. One named scenario overrides existing leaves.  
5. Precedence deterministic.  
6. Invalid/unknown config fails before runtime.  
7. `--show-config` shows complete effective config.  
8. `--validate-config` uses real resolve path without running.  
9. All supported Z-Gap fields survive resolve + adapt.  
10. Strategy cannot silently downgrade.  
11. LIVE_TINY / N7 unchanged.  
12. Reporting unchanged.

---

## 21. Remaining questions

None blocking. U1–U4 frozen. Spread dual ownership and `target_notional` ownership decided in §5.

---

**Confirmation:** Specification-only corrections in this document; no runtime implementation here.
