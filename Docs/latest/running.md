# Running and recovery

## Configuration

Each run is one YAML file. Unknown keys and invalid bounds fail validation. The current live safety ceiling in the shipped configs is $5 total entry debit.

Registered kinds: `z_gap`, `ask70`. Unknown `strategy.kind` fails at load.

| Config | Strategy | Extra connectors | Protection |
|--------|----------|------------------|------------|
| `config/runs/z_gap_tiny_live.yaml` | `z_gap` | Binance spot trades, Chainlink TWAP, PTB seal | none (strategy exits) |
| `config/runs/ask70_protection_tiny_live.yaml` | `ask70` | Polymarket books + clock + account only | **required** SL/TP/trailing |

CLI default `--config` is `config/runs/z_gap_tiny_live.yaml`.

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --validate-config

python -m tyrex_pm.application.cli run \
  --config config/runs/ask70_protection_tiny_live.yaml \
  --validate-config
```

`--show-config` prints a typed subset (schema, run name, kind, market, risk, lifecycle, directories). It does not dump strategy parameters or the `protection:` block; read the YAML for those.

ask70 does not need Binance or Chainlink liveness. z_gap does: the tiny-live Wi-Fi profile accepts up to 750 ms clock uncertainty and a 30-second arrival delay only for an immutable `EXACT_AT_START` Chainlink PTB. Those bounds do not relax order-book freshness or final pre-dispatch validation. Actual measurements and configured maxima are written to the report.

## Live command

`--live` is the explicit process-level mutation authorization. Without it, the CLI refuses to start (exit 2). `--validate-config` / `--show-config` do not need `--live` and do not touch the network.

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --dotenv .env \
  --out-dir var/runs/z_gap/manual_validation \
  --live
```

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/ask70_protection_tiny_live.yaml \
  --dotenv .env \
  --out-dir var/runs/ask70/manual_validation \
  --live
```

Before running, ensure credentials and collateral allowance are correct. Allowance changes are deliberately out of band and never occur in the latency-sensitive order path. Copy `.env.example` to `.env`; never commit `.env`.

Every live run writes `run_summary.json` under `--out-dir` (or `report_directory/<run_name>_<utc stamp>`). Execution authority is persisted in SQLite under `state_directory`.

## Outcomes

- `COMPLETED_NO_ENTRY_SIGNAL`: execution was available, but no strategy entry was selected.
- `NO_ENTRY_RUNTIME_BLOCKED`: no execution session opened because required runtime capability never became available.
- `NO_ENTRY_STRATEGY_INPUT_BLOCKED`: execution machinery became available, but no decision had a fully valid input set for **this** contract (clock/PTB/model for z_gap; books/clock eligibility for ask70). This is not an economic no-signal result.
- `ENTRY_ABORTED_BEFORE_SESSION`: a strategy entry intent was emitted but a final pre-session prerequisite changed before the execution baseline could be opened.
- `COMPLETED_NO_FILL`: an entry was authoritatively unfilled.
- `COMPLETED_FLAT`: exposure was confirmed and later reconciled to baseline flatness.
- `OPEN_EXPOSURE`: the process ended while exposure remained.
- `MANUAL_INTERVENTION_REQUIRED`: evidence was ambiguous or safe automated recovery was impossible.
- `RUNTIME_DEGRADED`: a recoverable component error occurred; review durable run evidence before another LIVE run.
- `REPORTING_DEGRADED`: explanatory evidence could not be fully persisted or projected.
- `RUNTIME_FAILURE`: the composition failed; `run_summary.json` still contains the fatal stage and mutation count.

`run_summary.json` schema version 4 includes `run_evidence_summary`, `no_entry_diagnosis`, and `validation_scope`. For no-entry runs, inspect `furthest_stage`, `execution_infrastructure_ready_ever`, `entry_executable_ever`, `strategy_inputs_eligible_ever`, `strategy_input_blocker_counts`, and `last_readiness_blockers`. `NO_ENTRY_RUNTIME_BLOCKED` is reserved for infrastructure that never cleared; model/jump-guard/PTB lockouts with healthy account/stream/books are `NO_ENTRY_STRATEGY_INPUT_BLOCKED`. `COMPLETED_NO_ENTRY_SIGNAL` is reserved for a genuinely eligible economic evaluation. `validation_scope.execution_lifecycle_exercised=false` means the run provides no evidence about BUY, fills, or EXIT. The full ordered `run_evidence` list is persisted incrementally in SQLite and projected into the report.

Never delete the configured SQLite state to make a blocker disappear. Inspect `run_summary.json`, account open orders, balances, and the journal first. Deleting state destroys the evidence required for safe recovery.
