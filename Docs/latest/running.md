# Running and recovery

## Configuration

Each run is one YAML file. Unknown keys and invalid bounds fail validation. The current live safety ceiling in the shipped configs is $5 total entry debit. Field-by-field roles: [config_guide.md](config_guide.md).

Registered kinds: `z_gap`, `ask70`, `feature_probe`. Unknown `strategy.kind` fails at load.

| Config | Strategy | Extra connectors | Protection |
|--------|----------|------------------|------------|
| `config/runs/z_gap_tiny_live.yaml` | `z_gap` | Binance spot trades, Chainlink TWAP, PTB seal | optional overlay (shipped YAML includes SL + resting GTD TP) |
| `config/runs/ask70_protection_tiny_live.yaml` | `ask70` | Polymarket books + clock + account only | **required** SL 0.55 + resting GTD TP 0.85 (trailing off for SL proof) |
| `config/runs/ask70_maker_tiny_live_danger.yaml` | `ask70` | same as ask70 | far-bid maker POST then cancel (optional extra run) |
| `config/runs/feature_probe_catalog.yaml` | `feature_probe` | Binance spot L2 + L1 imbalance | validate-only; `emit_entry: false`; no `protection:` |

CLI default `--config` is `config/runs/z_gap_tiny_live.yaml`.

```bash
python -m tyrex_pm.application.cli run \
  --config config/runs/z_gap_tiny_live.yaml \
  --validate-config

python -m tyrex_pm.application.cli run \
  --config config/runs/ask70_protection_tiny_live.yaml \
  --validate-config

python -m tyrex_pm.application.cli run \
  --config config/runs/feature_probe_catalog.yaml \
  --validate-config
```

## evaluate() lab (no live)

`eval` calls the same `driver.evaluate()` as production from a JSON fixture. It never starts adapters or POSTs. `--live` is rejected.

```bash
python -m tyrex_pm.application.cli eval \
  --config config/runs/ask70_protection_tiny_live.yaml \
  --fixture tests/fixtures/lab/ask70_books.json

python -m tyrex_pm.application.cli eval \
  --config config/runs/feature_probe_catalog.yaml \
  --fixture tests/fixtures/lab/feature_probe_l2.json

python -m tyrex_pm.application.cli eval \
  --config config/runs/z_gap_tiny_live.yaml \
  --fixture tests/fixtures/lab/z_gap_trades.json

python -m tyrex_pm.application.cli eval \
  --config config/runs/z_gap_tiny_live.yaml \
  --fixture tests/fixtures/lab/z_gap_entry_eligible.json
```

Fixture schema and packs: [`tests/fixtures/lab/`](../../tests/fixtures/lab/README.md). Helper: `tyrex_pm.lab.evaluate_replay`.

`--show-config` prints a typed subset (schema, run name, kind, market, risk, lifecycle, directories). It does not dump strategy parameters or the `protection:` block; read the YAML for those.

ask70 does not need Binance or Chainlink liveness. z_gap does: the tiny-live Wi-Fi profile accepts up to 750 ms clock uncertainty and a 30-second arrival delay only for an immutable `EXACT_AT_START` Chainlink PTB. Those bounds do not relax order-book freshness or final pre-dispatch validation. Actual measurements and configured maxima are written to the report.

## Before `--live`

Prove the door before any venue mutation. Lab success is **not** OMS proof (fills, SL print, dust).

1. `--validate-config` — YAML and `strategy.kind` load; unknown keys fail.
2. `eval --config … --fixture tests/fixtures/lab/<name>_….json` — same `evaluate()` as production; `--live` is rejected. How to add a pack: [extending.md](extending.md#lab-fixture-before-live).
3. Only then `--live` with the $5 cap and a real `--out-dir`.

Shipped fixtures: `ask70_books.json`, `feature_probe_l2.json`, `z_gap_trades.json`, plus the R4 packs `z_gap_entry_eligible.json`, `z_gap_no_entry_eligible.json`, `z_gap_inputs_invalid.json`, and `z_gap_exit_held.json` (`MARKET_RICH_EXIT` at τ=180 s, before the 90 s host flatten).

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
- `NO_ENTRY_STRATEGY_INPUT_BLOCKED`: execution machinery became available, but no decision had a fully valid input set for **this** contract (clock/PTB/model for z_gap; books/clock eligibility for ask70 / feature_probe). This is not an economic no-signal result.
- `ENTRY_ABORTED_BEFORE_SESSION`: a strategy entry intent was emitted but a final pre-session prerequisite changed before the execution baseline could be opened.
- `COMPLETED_NO_DISPATCH`: prepare or the final gate failed before POST (`ACCOUNT_REFRESH_IN_FLIGHT`, `PREPARE_BUDGET_EXCEEDED`, price/age fuse). No venue mutation. `lifecycle.prepare_budget_ms` must stay below `market.maximum_candidate_age_ms`; `refresh_wait_budget_ms` is the max wait for an in-flight wallet refresh before sign is refused.
- `COMPLETED_NO_FILL`: an entry was authoritatively unfilled.
- `COMPLETED_FLAT`: exposure was confirmed and later reconciled to baseline flatness.
- `COMPLETED_FLAT_WITH_DUST`: the sell flattened all venue-legal size; a classified leftover below `min_order_size` / `dust_abs_shares` / `dust_max_notional` remains. This is not `COMPLETED_FLAT`. `ok` is true only when the residual is persisted and scoped so the next window does not treat it as unknown exposure.
- `OPEN_EXPOSURE`: the process ended while exposure remained.
- `MANUAL_INTERVENTION_REQUIRED`: evidence was ambiguous, leftover was still tradable or unclassified, or safe automated recovery was impossible.
- `RUNTIME_DEGRADED`: a recoverable component error occurred; review durable run evidence before another LIVE run.
- `REPORTING_DEGRADED`: explanatory evidence could not be fully persisted or projected.
- `RUNTIME_FAILURE`: the composition failed; `run_summary.json` still contains the fatal stage and mutation count.

`run_summary.json` schema version 5 includes `run_evidence_summary`, `no_entry_diagnosis`, `validation_scope`, `reproducibility`, `startup_reconciliation`, `recovery_sessions`, and `strategy_session`. For no-entry runs, inspect `furthest_stage`, `execution_infrastructure_ready_ever`, `entry_executable_ever`, `strategy_inputs_eligible_ever`, `strategy_input_blocker_counts`, and `last_readiness_blockers`. `NO_ENTRY_RUNTIME_BLOCKED` is reserved for infrastructure that never cleared; model/jump-guard/PTB lockouts with healthy account/stream/books are `NO_ENTRY_STRATEGY_INPUT_BLOCKED`. `COMPLETED_NO_ENTRY_SIGNAL` is reserved for a genuinely eligible economic evaluation. `validation_scope.execution_lifecycle_exercised=false` means the run provides no evidence about BUY, fills, or EXIT. The full ordered `run_evidence` list is persisted incrementally in SQLite and projected into the report.

`reproducibility` is captured **before** runtime/network creation so a composition failure still writes identity. It includes package version, full `sys.version`, listed dependency versions (`polymarket-client`, `PyYAML`, `websockets`), git revision and dirty flag, source-config SHA-256, canonical effective config and its separate SHA-256, config path, permitted CLI flags (`live`, `out_dir`), run instance id, and recovery identifiers. It does not include secrets or environment values. Emergency reports keep those ids/hashes and omit the large `effective_config` dump.

Fill language is not implied from an empty list:

- `COMPLETED_NO_DISPATCH` is a confirmed no-mutation outcome. Venue reconciliation is not required to call it certain.
- A process with `state is None` can still have `prior_session_risk=true` when an unsafe restored session blocked entry.
- Pending or ambiguous BUY **or SELL** attempts make `observed_fills_complete` false.
- `MANUAL_INTERVENTION` never presents empty fills as proven zero.
- Unknown fees and unclassified residuals stay `null`.

The CLI refuses to start if `--out-dir` (or the stamped default) already contains `run_summary.json` or `startup_report.json`. That check runs before `TradingRuntime.create`.

Startup outcomes are not economic no-signal results:

- `STARTUP_RECONCILIATION_BLOCKED`: inspection could not prove the account is clear.
- `STARTUP_ACTION_REQUIRED`: a bot-owned order or position can be closed only after an explicit `approve`.
- `ACCOUNT_CHECK_READY`: `--check-account` finished ready and submitted nothing.
- `OPERATOR_STOP_BEFORE_TRADING`: the operator declined, timed out, or had no terminal at the recovery prompt.

A new process always starts with `STARTUP_CHECK_PENDING`. A clearance stored by an earlier process does not authorize a BUY until this process inspects again.

Never delete the configured SQLite state to make a blocker disappear. Inspect `run_summary.json`, account open orders, balances, and the journal first. Deleting state destroys the evidence required for safe recovery.

## Startup reconciliation

Every `run --live` inspects the account before strategy feeds start. `run --check-account` performs that same inspection and writes `startup_report.json` without signing, posting, cancelling, or redeeming. `--check-account` together with `--live` is rejected.

Inspection can append read-derived evidence to the journal. It clears a blocker only when that evidence is complete: an exact terminal no-fill order, a uniquely allocated external sale, transfer, merge, or redeem, a supported payout proof, or classified dust. A zero balance, a missing open-order row, a passed end time, `closed=true`, or an indexer `redeemable` flag is not clearance. Pinned `polymarket-client` 0.5.0 market records do not contain a payout vector. When `TYREX_POLYGON_RPC_URL` is set to a real HTTPS Polygon endpoint, a standard binary market can be settled from `payoutDenominator` and `payoutNumerators` on Conditional Tokens `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` after `eth_chainId` returns 137. The placeholder value `https://your-polygon-rpc-endpoint` is rejected before a connection is opened. The call is read-only, uses the `latest` tag, and stays inside the inspection budget. A timeout, malformed response, wrong chain, zero denominator, or missing token index leaves that market unresolved and blocks startup.

Neg-risk positions also live on Conditional Tokens, and their exchange path is separate. They stay unresolved. The pinned SDK exposes `neg_risk`, `outcome_index`, and yes/no token ids, but not a proven `payoutNumerators` slot for the held token, and not whether that condition is a named outcome, a placeholder, or Other. Price, `closed`, end time, `redeemable`, and UMA text are not used as a payout.

When the only remaining work is a bot-owned order or position, the process prints a proposal and waits for the word `approve`. Decline, EOF, a non-interactive terminal, or the prompt timeout leaves the session untouched. An approved close uses the existing lifecycle, coordinator, and final gate on that historical market. The approved quantity, price floor, identity, and expiry are checked again immediately before POST. A recovery action never places a BUY and does not spend the new strategy's $5 entry budget.

Two processes cannot share one `state_directory`. The lock is released on shutdown and is not a license to delete the journal.

In-run emergency flatten and shutdown drain are unchanged. They are not a substitute for this startup decision.

`--continue` is not a first-release operator step. Discovery is current + next 5m only.
