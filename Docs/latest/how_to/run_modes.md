# How to run modes

**Purpose:** goal-oriented recipes confirmed by active CLI help.  
Working directory: repository root.  
**No new real-money live command is provided.**

## Effects legend

| Tag | Meaning |
|-----|---------|
| Offline | No network |
| Net-read | Public or authenticated reads |
| Report | Writes `var/reporting/` |
| Local-state | Writes `var/state/` |
| Venue | Submit/cancel/heartbeat |

## Observe (fixture) — Offline + Report

```bash
tyrex-pm observe --config config/observe_fixture_r3.json
```

### Z-Gap fixture OBSERVE (F3; offline only)

```bash
tyrex-pm observe --config config/observe_z_gap_fixture_f3.json
```

Records counterfactual Z-Gap decisions/facts. **No OMS, fills, or public live support.**

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

Simulated entry→exit via ShadowOMS using the same Z-Gap decision path as F3 OBSERVE. **No public live Z-Gap, real PTB provider, or venue mutation.**

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

## Dry one-shot — Net-read + Report (no Venue)

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

## Venue-mutation boundary (not a runbook)

`r7b-live-once --execute-live` can submit real orders (Venue). Requires clean worktree and gates.  
**R7 live validation is complete** (`55fd9a76`). Closed runbook: [`../../implementation/r7f_operator_runbook.md`](../../implementation/r7f_operator_runbook.md).  
Any future live test needs a **new explicitly scoped phase**.
