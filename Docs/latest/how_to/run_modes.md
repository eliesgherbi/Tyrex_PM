# How to run modes

**Purpose:** goal-oriented recipes confirmed by active CLI help / N7 tools.  
Working directory: repository root.

## Effects legend

| Tag | Meaning |
|-----|---------|
| Offline | No network |
| Net-read | Public or authenticated reads |
| Report | Writes `var/reporting/` |
| Local-state | Writes `var/state/` |
| Venue | Submit/cancel/heartbeat |

## YAML-configurable Z-Gap (recommended) — Offline + Report

```bash
python -m tyrex_pm.application.cli run \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/example_risk.yaml \
  --execution config/execution/example.yaml \
  --runtime config/runtime/observe_btc_5m.yaml \
  --scenario aggressive \
  --mode observe \
  --run-name z_gap_aggressive_test
```

SHADOW (simulated OMS only; no venue mutation) — use `shadow_example.yaml`:

```bash
python -m tyrex_pm.application.cli run \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/example_risk.yaml \
  --execution config/execution/shadow_example.yaml \
  --runtime config/runtime/observe_btc_5m.yaml \
  --scenario aggressive \
  --mode shadow \
  --run-name z_gap_aggressive_shadow
```

`aggressive` overrides `theta_take`, `z_min`, risk `max_spread`, shadow `max_hold_s`, and `runtime_duration_s` only.

### LIVE one-shot (real Polymarket via N7) — Net-read + Report + Venue

`source: live` in runtime YAML selects market data. CLI `--mode live` selects the
N7 real-execution host. Operator `--live` arms mutations (no envelope/phrase).

Profiles:

- `config/strategies/z_gap.yaml`
- `config/risk/tiny_live_5usd.yaml`
- `config/execution/polymarket_live.yaml`
- `config/runtime/live_btc_5m.yaml`

Fake rehearsal (zero venue mutations):

```bash
python -m tyrex_pm.application.cli run --mode live --runtime config/runtime/live_btc_5m.yaml --fake-rehearsal
```

Authenticated read-only preflight (no mutations):

```bash
python -m tyrex_pm.application.cli run --mode live --runtime config/runtime/live_btc_5m.yaml
```

Operator real one-shot (you run this; agents must not):

```bash
python -m tyrex_pm.application.cli run --mode live --runtime config/runtime/live_btc_5m.yaml --live
```

Reports under `var/reporting/yaml_run/<run_name>/` (or `--out-dir`). The legacy
`tyrex-pm n7-live` / `tools/n7_live/run_n7_live_oneshot.py` path remains as a
compatibility entry point to the same N7 lifecycle.

## Observe (fixture) — Offline + Report

```bash
tyrex-pm observe --config config/observe_fixture_r3.json
```

### Z-Gap fixture OBSERVE (F3; offline only)

```bash
tyrex-pm observe --config config/observe_z_gap_fixture_f3.json
```

Records counterfactual Z-Gap decisions/facts. **No OMS, fills, or venue mutation.**

## Observe (public live) — Net-read + Report

```bash
tyrex-pm observe --config config/observe_live_r3.json --btc-window next --duration-s 30
```

## Shadow (paper OMS) — Net-read + Report (+ optional Local-state snapshot)

```bash
tyrex-pm shadow --config config/observe_shadow_r5.json --btc-window next
```

### Z-Gap fixture SHADOW (F4; offline only)

```bash
tyrex-pm shadow --config config/observe_shadow_z_gap_f4.json
```

Simulated entry→exit via ShadowOMS using the same Z-Gap decision path as F3 OBSERVE. **No venue mutation.**

### Z-Gap fixture SHADOW + resolution (F5; offline only)

```bash
tyrex-pm shadow --config config/observe_shadow_z_gap_f5.json
```

Optional simulated hold-to-resolution (capability + fixture evidence → simulated payout). **No redeem, network settlement, or live venue.**

### Z-Gap N5A SHADOW (offline depth-walk; not live evidence)

```bash
python tools/n5_shadow/run_n5_shadow.py --mode fixture \
  --config config/observe_shadow_z_gap_n5a.json \
  --out var/reporting/n5/shadow_summary.json
```

Uses N4-aligned model price \(\hat{C}_t\) vs sealed Chainlink \(K\), and
`shadow_depth_walk_v1` simulated fills. Labels: `simulated_shadow` /
`estimated`.

## Discover BTC window — Net-read

```bash
tyrex-pm discover-btc-window --which next
```

## Mutation-impossible preflight — Net-read + Report

```bash
tyrex-pm live-preflight --help
# Public only:
tyrex-pm live-preflight --skip-auth --output var/reporting/preflight_public.json
```

Authenticated preflight needs credentials in `.env` (values never printed). Optional `--user-stream-s` on a target host.

## N7 Z-Gap tiny live (Scope A)

Config: `config/n7_tiny_live.json`.  
Authorization for real mutations = operator `--live` (no phrase/envelope).  
Agents must not run `--live`.

### Fake rehearsal — Offline + Report (no Venue)

```bash
python tools/n7_live/run_n7_live_oneshot.py --fake-rehearsal
```

Deterministic FakeTransport entry→exit→FLAT. `real_venue_mutations: 0`.

### Read-only preflight — Net-read + Report (no Venue)

```bash
tyrex-pm n7-preflight
# or oneshot without --live:
python tools/n7_live/run_n7_live_oneshot.py
# or:
tyrex-pm n7-live
```

Expect `go_no_go: GO` (or abort codes). Mutations remain OFF.

### Operator live one-shot — Net-read + Report + Venue

```bash
python tools/n7_live/run_n7_live_oneshot.py --live
# or:
tyrex-pm n7-live --live
```

Flow: preflight → discover BTC 5m → seal Chainlink `sealed_k` → evaluate Z-Gap
(with `require_ssr_price_match: false` by default) → at most one entry → bounded
exit → recon → mutations OFF. Cap: fee-inclusive debit ≤ $5.00.

Reports under `var/reporting/n7/oneshot_<stamp>/` include:

- `ptb_authority`, `sealed_k`, `ssr_match_required`, `ssr_check_status`, `ptb_ready`
- `evals`, `reason` (e.g. `evaluated_no_enter_signal` when no edge)
- `real_venue_mutations`

Status helper: `tyrex-pm n7-status`.

Evidence: [`../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md`](../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md).

## Dry one-shot (R7 path) — Net-read + Report (no Venue)

```bash
tyrex-pm r7b-live-once \
  --strategy reference-momentum \
  --market-family btc_updown_5m \
  --max-windows 3 \
  --max-buy-collateral 5.00 \
  --dry-run \
  --output-dir var/reporting/docs_dry
```

Default without `--execute-live` is dry. Dry is **not** “offline”: it may perform network reads and write reports.

## Reconciliation tools

| Command | Effects |
|---------|---------|
| `tyrex-pm r7c-recon …` | Net-read + Report |
| `tyrex-pm r7-ack-regenerate` | Net-read + **Local-state** (ack artifact) |
| `python scripts/r8_readonly_recon.py` | Net-read + Report |
| `python scripts/r7f_exit_rehearsal.py` | Net-read + Report |

These perform **no venue order mutation** and **no on-chain mutation**.  
“No mutations” here means no venue/on-chain mutation — not “zero disk writes.”

## Venue-mutation boundary

| Path | Status |
|------|--------|
| `r7b-live-once --execute-live` | R7 validation **complete** (closed runbook) |
| `n7-live --live` / `run_n7_live_oneshot.py --live` | Current **Z-Gap** Scope A operator one-shot |

Closed R7 runbook: [`../../implementation/r7f_operator_runbook.md`](../../implementation/r7f_operator_runbook.md).  
Do not invent unscoped continuous-live commands beyond these operator gates.
