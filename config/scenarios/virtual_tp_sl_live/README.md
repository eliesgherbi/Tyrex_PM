# Scenario: `virtual_tp_sl_live`

**Purpose:** Small **live** validation of Tyrex **virtual take-profit / stop-loss** (long-only v1). Not a production profile.

**Isolated state:** use `var/scenarios/virtual_tp_sl_live/` for watermark, dedup, and virtual-exit JSON so you do not clobber other runs.

## Prerequisites

- Same as any live guru run: `.env` with `POLYMARKET_*` secrets, funder/signature type as needed.
- Edit **`guru_follow.yaml`**: set **`guru_wallet_address`**; set **`token_filter`** (this pack ships **`enabled: false`** = all guru tokens — **documented unfiltered** choice; switch to **`enabled: true`** + allowlist for a tighter drill).
- Conservative **risk** caps in `guru_follow_risk.yaml`; adjust before scaling.

## Run (Windows example)

From repo root:

```text
python scripts/run_guru.py ^
  --strategy-conf config/scenarios/virtual_tp_sl_live/guru_follow.yaml ^
  --risk-conf config/scenarios/virtual_tp_sl_live/guru_follow_risk.yaml ^
  --live-conf config/scenarios/virtual_tp_sl_live/live_polymarket.yaml
```

Create `var/scenarios/virtual_tp_sl_live/` if it does not exist (or let the process create files under it).

## What to watch

- **Facts / reporting** (when `reporting_enabled: true`): `virtual_exit_arm`, `virtual_exit_trigger`, `virtual_exit_submit`, `virtual_exit_hold`, `virtual_exit_retry`, `virtual_exit_reconcile`, `virtual_exit_disarm`, `virtual_exit_recovery`.
- **Logs:** `event=virtual_exit_*` from manager and `virtual_exit_submit_market` / `virtual_exit_submit_limit` from execution.
- **Tier A:** `venue_state` staleness drives **`virtual_exit_hold`** when `max_venue_staleness_seconds` exceeded.
- **Restart:** virtual exit state file (`virtual_exit_state.json`) should reconcile open exit orders against Nautilus cache — avoid double-submit on recovery.

## Notes

- **`bot_sell_validate`** scenarios **do not** wire `VirtualExitManager` (same as compose: validation harness only).
- TP default: **aggressive limit**; SL default: **market** FOK with optional **limit** fallback (`market_sl_fallback_to_limit` in runtime `virtual_exit`).
