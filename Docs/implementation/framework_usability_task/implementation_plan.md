# Implementation plan — YAML configurable OBSERVE / SHADOW

**Purpose:** Ordered, proportionate plan to implement the frozen specification.  
**Specification:** [`specification_task_definition.md`](specification_task_definition.md)  
**Status:** Implemented (WP1–WP10). Example YAMLs live under `config/{strategies,risk,execution,runtime,scenarios}/`.  
**Boundary:** OBSERVE + SHADOW only. No LIVE_TINY / N7 / reporting / analytics.

### Execution examples (resolved)

| File | `enable_oms` | Use with |
|------|--------------|----------|
| `config/execution/example.yaml` / `observe_noop.yaml` | `false` | `--mode observe` |
| `config/execution/shadow_example.yaml` | `true` | `--mode shadow` |

---

## 0. Frozen interface (from specification)

### Configuration tree

```text
config/
├── strategies/z_gap.yaml
├── risk/example_risk.yaml
├── execution/example.yaml
├── runtime/observe_btc_5m.yaml
└── scenarios/aggressive.yaml
```

### CLI

```bash
tyrex-pm run \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/example_risk.yaml \
  --execution config/execution/example.yaml \
  --runtime config/runtime/observe_btc_5m.yaml \
  --scenario aggressive \
  --mode observe \
  --run-name z_gap_aggressive_test

tyrex-pm run ... --validate-config
tyrex-pm run ... --show-config
```

### Precedence

1. Typed code defaults  
2. Strategy + risk + execution + runtime YAML  
3. One scenario leaf overlay  
4. Allowlisted CLI (`--mode`, `--run-name`, …)  
5. Hard validation / safety rejection  

### Explicit non-goals

Reporting redesign; analytics; run comparison; dashboards; databases; experiment tracking; sweeps; inheritance; plugins; new strategy; Z-Gap math changes; risk/OMS/portfolio/lifecycle redesign; LIVE_TINY; N7 changes.

---

## 1. Example artifact contents (created in-repo; comments in files are authoritative)

### `config/strategies/z_gap.yaml`

```yaml
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

### `config/risk/example_risk.yaml`

```yaml
target_notional: "5"
max_notional: "10"
min_price: "0.01"
max_price: "0.99"
max_spread: "0.50"
min_liquidity_notional: "1"
no_entry_before_close_s: 0
duplicate_lifetime_s: 3600
kill_switch_active: false
```

### `config/execution/example.yaml`

```yaml
kind: shadow
shadow:
  enable_oms: false   # true for SHADOW runs / shadow-specific file
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

### `config/runtime/observe_btc_5m.yaml`

```yaml
source: fixture
fixture_path: "tests/fixtures/n3/n1_three_windows.json"
binance_symbol: "BTCUSDT"
signal_max_book_spread: "0.50"
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

### `config/scenarios/aggressive.yaml`

```yaml
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

Demonstrates: single `target_notional` under risk; nested `friction.fee_curve`; runtime `source`; scenario leaf overrides only.

---

## 2. Implementation areas and work packages

Order is strict: WP1 → WP2 → … Complete each package’s acceptance before starting the next area’s dependent work.

```text
Area 1 (contracts/resolver)  WP1 → WP2 → WP3 → WP4
Area 2 (runtime adapt)       WP5 → WP6
Area 3 (CLI)                 WP7 → WP8
Area 4 (tests/docs/examples) WP9 → WP10
```

---

### WP1 — Add PyYAML and YAML safe-load helper

| Item | Detail |
|------|--------|
| **Objective** | Make YAML loading available with `yaml.safe_load` only |
| **Current components** | `pyproject.toml` (no PyYAML); no active YAML loader |
| **Add/modify** | `pyproject.toml` dependency; small helper e.g. `src/tyrex_pm/runtime/yaml_config/load.py` (`load_yaml_mapping(path)`) |
| **Behavior** | Load UTF-8 YAML → mapping; invalid YAML → clear error with path; reject non-mapping roots |
| **Dependencies** | None |
| **Tests** | Valid YAML; invalid YAML; missing file |
| **Acceptance** | Helper usable by resolver; PyYAML listed |
| **Backward compatibility** | Additive dependency only |

---

### WP2 — Typed section parsers and strict schemas

| Item | Detail |
|------|--------|
| **Objective** | Parse strategy / risk / execution / runtime YAML into typed structures with unknown-field rejection |
| **Current components** | `ZGapConfig` (+ nested), `RiskPlanConfig`, `ShadowConfig`, `FreshnessConfig`, `FeeCurveParams` |
| **Add/modify** | e.g. `src/tyrex_pm/runtime/yaml_config/{strategy,risk,execution,runtime}.py` — thin parsers that **build existing dataclasses**, not a second domain model |
| **Behavior** | Strategy → `strategy` id + complete `ZGapConfig` (optional leaves → code defaults); risk → fields for `RiskPlanConfig` (no `runtime_mode` in file); execution → `ShadowConfig`; runtime → source/freshness/window/etc. Strict unknown keys. Nested `friction.fee_curve.{fee_rate,exponent}` |
| **Dependencies** | WP1 |
| **Tests** | Unknown field fail; wrong type; unsupported strategy; nested fee_curve; omitted optional uses default |
| **Acceptance** | Round-trip sample mappings; every `ZGapConfig` field constructible from YAML |
| **Backward compatibility** | No change to existing JSON loaders yet |

---

### WP3 — Scenario leaf overlay + precedence resolve

| Item | Detail |
|------|--------|
| **Objective** | Merge base sections + one named scenario + CLI allowlist into one resolved object |
| **Current components** | None active (old deep-merge is archive-only — do not revive) |
| **Add/modify** | e.g. `src/tyrex_pm/runtime/yaml_config/resolve.py` — `ResolvedRunConfig` dataclass; `resolve_run_config(...)` |
| **Behavior** | Precedence: defaults → YAML bases → leaf scenario overlay → CLI (`mode`, `run_name`). Scenario name → `config/scenarios/<name>.yaml`. Reject unknown leaves / section replacement. Set `risk.runtime_mode` from `--mode`. No YAML reload later |
| **Dependencies** | WP2 |
| **Tests** | Precedence order; scenario leaf apply; unknown scenario target fail; missing scenario file |
| **Acceptance** | Single `ResolvedRunConfig` with strategy id, `ZGapConfig`, risk fields, shadow, runtime fields, mode, run_name |
| **Backward compatibility** | Additive |

---

### WP4 — Validate, show-config serialization, representative YAML files

| Item | Detail |
|------|--------|
| **Objective** | Final validation API + complete effective dump; add example YAML under `config/` |
| **Current components** | `validate_zgap_config`; dataclass `__post_init__` |
| **Add/modify** | validate function on `ResolvedRunConfig`; serialize-all-effective for `--show-config`; create example files listed in §1 (first time production YAML tree is added — planning authorizes this package only when implementation starts) |
| **Behavior** | Cross-checks: shadow mode ⇒ `enable_oms`; observe may have OMS off; `target_notional <= max_notional`; source/fixture consistency. Safety violations reject (no silent clamp). Show-config includes defaults |
| **Dependencies** | WP3 |
| **Tests** | Invalid combinations; show-config includes defaulted fields |
| **Acceptance** | Examples load via resolve; validate fails loudly on bad combos |
| **Backward compatibility** | New files; existing JSON untouched |

---

### WP5 — Complete Z-Gap / risk / execution / runtime adapters

| Item | Detail |
|------|--------|
| **Objective** | Map `ResolvedRunConfig` → existing runtime objects with **complete** typed mapping |
| **Current components** | `zgap_config_from_runtime` (incomplete); `ObserveConfig`; `build_strategy_binding`; shadow host notional preference for `z_gap.target_notional` |
| **Add/modify** | Adapter module e.g. `yaml_config/adapt.py`; prefer new `zgap_config_from_mapping` / build `ZGapConfig` directly; **stop relying on incomplete flat bridge for `run`**. Build `ObserveConfig` with `mode←source`, `max_book_spread←signal_max_book_spread`, `strategy_kind=z_gap`, risk from risk YAML (`target_notional` only from risk), shadow from execution. Binding: `zgap_config=` complete object; `target_notional` from risk |
| **Behavior** | All fields survive: `theta_fill_floor`, `block_abs_z`, `require_repricing_edge`, `tie_epsilon`, `max_ptb_lag_ms`, `max_clock_uncertainty_ms`, `exit_friction_reserve`, `min_exit_depth`, `reset_on_stale_model`, `sell_vs_resolve_margin`, `resolution_capability_default`, `provisional_resolve_penalty_per_share`, nested fee curve, etc. No silent strategy downgrade |
| **Dependencies** | WP4 |
| **Tests** | Golden: YAML → `ZGapConfig` equality for full field set; risk `target_notional` wins; signal vs risk spread fields distinct |
| **Acceptance** | Adapter unit tests green; incomplete bridge not used on `run` path |
| **Backward compatibility** | May leave old `zgap_config_from_runtime` for legacy JSON until WP8; mark or extend carefully |

---

### WP6 — Fix legacy observe `--btc-window` strategy drop

| Item | Detail |
|------|--------|
| **Objective** | Prevent silent loss of `strategy_kind` / `z_gap` on existing observe CLI |
| **Current components** | `application/cli.py` `_build_observe_config` reconstructs `ObserveConfig(...)` omitting strategy fields |
| **Add/modify** | `cli.py` — apply `event_slug` via replace/rebuild that copies **all** fields including `strategy_kind`, `z_gap`, `risk`, `shadow` |
| **Behavior** | `--btc-window` only changes market identity fields |
| **Dependencies** | Can parallelize after WP1; must land before claiming CLI regression done (before WP10) |
| **Tests** | Regression: load z_gap JSON + btc-window keeps `strategy_kind==z_gap` and z_gap block |
| **Acceptance** | Test fails on current code; passes after fix |
| **Backward compatibility** | Behavior fix (correctness); no API break |

---

### WP7 — `tyrex-pm run` command (validate / show / execute OBSERVE|SHADOW)

| Item | Detail |
|------|--------|
| **Objective** | Unified CLI matching frozen UX |
| **Current components** | `application/cli.py` `build_parser` / `main` |
| **Add/modify** | Add `run` subparser with required strategy/risk/execution/runtime, optional scenario/run-name, mode observe\|shadow, validate-config, show-config. Wire to resolve → adapt → ObserveHost or ShadowHost |
| **Behavior** | `--validate-config` / `--show-config` exit without host. Run starts host with adapted config. Output path derived from `--run-name` under `var/reporting/` when provided. **No `--live`**. Reject if mode not observe/shadow |
| **Dependencies** | WP5 |
| **Tests** | CLI parse; validate-config exit 0/1; show-config prints; run observe fixture smoke |
| **Acceptance** | Documented command works for Z-Gap fixture OBSERVE |
| **Backward compatibility** | Additive subcommand; observe/shadow remain |

---

### WP8 — Route legacy observe/shadow through shared adapt where feasible

| Item | Detail |
|------|--------|
| **Objective** | Reduce dual-path drift without forcing JSON users to YAML immediately |
| **Current components** | `load_observe_config`, observe/shadow CLI handlers |
| **Add/modify** | Optional: JSON→internal builder shared with adapt layer; or document JSON as legacy and only share Z-Gap complete mapping helper |
| **Behavior** | Minimum: shared complete `ZGapConfig` construction helper used by both YAML adapt and any remaining flat JSON path. Prefer not forking two Z-Gap mappers |
| **Dependencies** | WP5, WP7 |
| **Tests** | JSON fixture z_gap still works; same Z-Gap fields as YAML path when equivalent |
| **Acceptance** | No silent field loss on either path for supported fields |
| **Backward compatibility** | Existing JSON commands kept |

---

### WP9 — Focused automated tests (Area 4 core)

| Item | Detail |
|------|--------|
| **Objective** | Prove resolver, adapt, CLI, and safety boundary |
| **Current components** | `tests/` patterns (`test_n5_*`, `test_f3_*`, etc.) |
| **Add/modify** | e.g. `tests/test_yaml_run_config.py`, `tests/test_yaml_run_cli.py`, fixture acceptance script or pytest e2e |
| **Behavior covered** | Valid load; invalid YAML; missing files; unknown fields; wrong types; unsupported strategy; defaults; scenario leaves; unknown scenario target; precedence; complete Z-Gap mapping; strategy_kind preservation; btc-window regression; OBSERVE from YAML; SHADOW from same structure; validate-config; show-config; assert `run` cannot arm LIVE_TINY (no live flag / mode) |
| **Dependencies** | WP7–WP8 |
| **Tests** | (this package *is* the tests) |
| **Acceptance** | Listed cases green in CI/local pytest |
| **Backward compatibility** | Additive tests |

---

### WP10 — Docs + end-to-end fixture acceptance command

| Item | Detail |
|------|--------|
| **Objective** | Operator-facing how-to + one copy-paste acceptance command |
| **Current components** | `Docs/latest/how_to/configuration.md`, `run_modes.md` |
| **Add/modify** | Short section in latest how-to for `tyrex-pm run`; keep N7/live docs unchanged; optional `tools/` or documented pytest node id for e2e |
| **Behavior** | Document ownership, precedence, example command; e2e: fixture OBSERVE via YAML exits successfully |
| **Dependencies** | WP9 |
| **Tests** | E2E command/doc command matches tests |
| **Acceptance** | Spec acceptance criteria 1–12 demonstrable |
| **Backward compatibility** | Docs additive; no reporting redesign |

---

## 3. First work package to implement

**Start with WP1** (PyYAML + `safe_load` helper). It is the smallest dependency for all later packages and has no runtime-host risk.

---

## 4. Mapping risks to close in WP5 (checklist)

Incomplete today via `zgap_config_from_runtime` / flat observe — must be fully mapped from strategy YAML:

| Field | Status today |
|-------|----------------|
| `entry.theta_fill_floor` | dropped |
| `entry.block_abs_z` | dropped |
| `entry.require_repricing_edge` | dropped |
| `entry.tie_epsilon` | dropped |
| `entry.exit_friction_reserve` | forced `0` |
| `realization.min_exit_depth` | dropped |
| `realization.slippage_included_in_executable_bid` | forced True |
| `thesis.reset_on_stale_model` | dropped |
| `time_resolution.sell_vs_resolve_margin` | dropped |
| `time_resolution.resolution_capability_default` | forced False |
| `ptb_time_quality.max_ptb_lag_ms` | dropped |
| `ptb_time_quality.max_clock_uncertainty_ms` | dropped |
| `friction.provisional_resolve_penalty_per_share` | dropped |
| nested `friction.fee_curve` | partial via flat fee_rate/exponent |

Also: `strategy_kind` / `z_gap` drop on `observe --btc-window` → WP6.

Ownership fixes in adapt:

- `target_notional` from **risk only** (do not prefer strategy/z_gap copy).  
- `signal_max_book_spread` vs risk `max_spread` kept distinct.

---

## 5. Implementation-plan acceptance criteria

Planned work is done when:

1. Z-Gap OBSERVE runs without editing Python.  
2. Same YAML structure runs SHADOW.  
3. Strategy, risk, execution, runtime from explicit YAML.  
4. One named scenario overrides leaves.  
5. Precedence deterministic.  
6. Invalid/unknown config fails before runtime.  
7. `--show-config` shows complete effective config.  
8. `--validate-config` uses real resolve path without running.  
9. All supported Z-Gap fields survive resolve + adapt.  
10. Strategy cannot silently downgrade.  
11. LIVE_TINY / N7 unchanged.  
12. Reporting unchanged.

---

## 6. Genuine blockers from inspection

None that block planning. Known engineering risks (incomplete bridge, btc-window drop, dual spread fields, dual `target_notional`) are scheduled in WP5–WP6 and ownership rules — not open product questions.

---

## 7. Confirmation

This plan and the companion specification update are documentation only. No runtime implementation, no production config file creation, no strategy math change, no reporting change, and no N7/LIVE_TINY modification was performed in this planning task.
