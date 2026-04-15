# Scenario: `wallet_sync_live`

Live validation of the **Venue Sync Truth** implementation (`WalletSyncActor`, readiness gate wallet sync clause, health source wallet sync awareness).

## What this tests

1. **WalletSyncActor startup** — first sync cycle discovers all wallet instruments, `first_sync_complete` flips, readiness gate unblocks.
2. **Continuous discovery** — instruments placed by a human (or another bot) on the wallet appear in Cache within `poll_interval + reconciliation_interval` (~60s worst case).
3. **Health reporting** — `wallet_sync` and `wallet_sync_startup_timeout` facts emitted; health source reports `DEGRADED_OMS` on stale/unresolvable, `HEALTHY` otherwise.
4. **Deployment budget accuracy** — `portfolio_deployment_usd` and per-token caps reflect wallet-side positions discovered by the actor.
5. **Startup timeout** — if `wallet_sync_startup_deadline_seconds` elapses without a successful first sync, gate reports `startup_wallet_sync_timeout`.

## Config differences vs `layer_a_follow`

| Key | layer_a_follow | wallet_sync_live | Rationale |
|-----|---------------|-----------------|-----------|
| `wallet_sync_enabled` | (default: true) | `true` (explicit) | Clarity for operator |
| `wallet_sync_poll_interval_seconds` | (default: 15.0) | `15.0` (explicit) | |
| `wallet_sync_startup_deadline_seconds` | (default: 120.0) | `120.0` (explicit) | |
| `wallet_sync_per_instrument_max_retries` | (default: 3) | `3` (explicit) | |
| `polymarket_use_data_api_for_positions` | (default: true w/ ws) | `true` (explicit) | Data API bulk for reconciliation |
| `polymarket_wallet_position_warmup_max` | (default: 128) | `128` (explicit) | Compose-time seed before actor |
| `allow_exit_when_degraded_oms` | `false` | `true` | Allow SELL if wallet sync degrades |
| `reporting_capital_snapshot_period_seconds` | `300.0` | `60.0` | More frequent snapshots for review |
| `startup_readiness_timeout_seconds` | (default: 120.0) | `180.0` | Extra margin for wallet sync |

## Run

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/wallet_sync_live/guru_follow.yaml \
  --risk-conf config/scenarios/wallet_sync_live/guru_follow_risk.yaml \
  --live-conf config/scenarios/wallet_sync_live/live_polymarket.yaml
```

## What to look for in logs

### Startup sequence (INFO level)

```
event=wallet_sync_instrument_added ... condition_id=... token_id=... instrument_id=...
```
One line per instrument discovered by the actor. Should see these for every wallet position not already seeded by compose-time warmup.

### Per-cycle facts (reporting)

Look in `var/reporting/runs/<run_id>/` for `wallet_sync` facts:
```json
{
  "cycle": 1,
  "positions_fetched": 5,
  "orders_fetched": 2,
  "condition_ids_wallet": 4,
  "condition_ids_cache": 4,
  "newly_added": 3,
  "resolution_failures": 0,
  "unresolvable_retrying": 0,
  "unresolvable_terminal": 0,
  "http_positions_ok": true,
  "http_orders_ok": true,
  "first_sync_complete": true,
  "elapsed_ms": 1250.0,
  "failure_details": {}
}
```

### Health degradation (if any)

```
event=wallet_sync_unresolvable ... condition_id=... retry_count=3
```
Followed by health source reporting `DEGRADED_OMS` with reason `wallet_sync_unresolvable_instruments`.

### Readiness gate

Look for the startup readiness coordinator log lines:
- `startup_wallet_sync_pending` — first cycle not done yet
- `startup_wallet_sync_timeout` — deadline exceeded (investigate Data API / CLOB connectivity)
- Gate transitions to READY after first sync + engine reconciliation + capital check all pass

### Deployment budget

After startup, check `risk_decision` facts — `portfolio_deploy_usd` should include positions discovered by wallet sync that were not in the static instrument list.

## Before live use

1. Set `guru_wallet_address` in `guru_follow.yaml` to your guru wallet.
2. Ensure `POLYMARKET_PK`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_PASSPHRASE` env vars are set.
3. Optional: set `POLYMARKET_FUNDER` if using a proxy/funder address different from the PK-derived address.
4. Tune risk caps (`max_notional_usd_per_order`, `max_portfolio_notional_usd_open`) for your desired exposure.
