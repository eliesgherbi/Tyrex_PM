# How to run modes

**Purpose:** goal-oriented recipes confirmed by active CLI help.  
Working directory: repository root.  
**No new real-money live command is provided.**

## Observe (fixture / offline)

```bash
tyrex-pm observe --config config/observe_fixture_r3.json
```

## Observe (public live, read-only)

```bash
tyrex-pm observe --config config/observe_live_r3.json --btc-window next --duration-s 30
```

## Shadow (paper OMS, public data)

```bash
tyrex-pm shadow --config config/observe_shadow_r5.json --btc-window next
```

## Discover BTC window (ops helper)

```bash
tyrex-pm discover-btc-window
```

## Mutation-impossible preflight

```bash
tyrex-pm live-preflight --help
```

## Dry one-shot validation (read-only default)

```bash
tyrex-pm r7b-live-once \
  --strategy reference-momentum \
  --market-family btc_updown_5m \
  --max-windows 3 \
  --max-buy-collateral 5.00 \
  --dry-run \
  --output-dir var/reporting/docs_dry
```

## Read-only reconciliation

```bash
tyrex-pm r7c-recon --help
tyrex-pm r7-ack-regenerate --help
python scripts/r8_readonly_recon.py
python scripts/r7f_exit_rehearsal.py
```

## Mutation-capable boundary (do not treat as a runbook)

`r7b-live-once --execute-live` can submit real orders. It requires a clean worktree and durable gates.  
**R7 live validation is complete** (`55fd9a76`). The closed runbook is [`../../implementation/r7f_operator_runbook.md`](../../implementation/r7f_operator_runbook.md).  
Any future live test needs a **new explicitly scoped phase** — not this page.
