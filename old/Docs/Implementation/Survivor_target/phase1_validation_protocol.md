# Pre-live validation protocol

Non-live gates before the first tiny-live **advisory** run. No live trading is performed as part of this checklist automation.

## Post-Wave-D advisory hardening

- **Pin market metadata** before every advisory live run, or pass `--event-url` on preflight/run to resolve from Gamma.
- **Run preflight** before live:
  `python scripts/preflight_phase1_live_scenario.py --scenario live_paired_binary_phase1_tiny --event-url "https://polymarket.com/event/btc-updown-5m-<START_TS>"`
- **Resolution-based PnL** may require manual reconciliation unless trustworthy resolution cashflow is available; facts `paired_binary_resolution_exit_accounting` classify this explicitly.
- **Authoritative fill reconciliation:** `paired_binary_realized_pnl` means **final venue-reconciled** PnL only. OMS submit amounts emit `paired_binary_realized_pnl_tentative`. Enforcement is blocked until `pnl_status: final` with no unresolved `oms_fill_discrepancy_detected` facts. See `Docs/Implementation/Survivor_target/fill_reconciliation_root_cause.md`.
- **Stop app cleanly** after `paired_binary_loop_stopped` or use `runtime.paired_binary.stop_background_tasks_after_strategy_done: true` (enabled in Phase 1 tiny scenario).
- **Do not enable enforce** until 3–5 advisory runs pass validator/replay **and** realized PnL is venue-reconciled (`pnl_status: final`) or explicitly accepted after manual reconciliation.

## Non-live validation gate

1. Full unit test suite passes (Wave A–D survival subset at minimum).
2. Phase 2 regression passes with `survival.enabled: false`.
3. Replay script runs on available live1/live3/live5-style facts and reports `missing_fields` / `unsupported_replay_reason` explicitly when data is insufficient.
4. Validator recognizes `PHASE1_SURVIVAL_PASS` and `PHASE1_RUNTIME_PREMATURE_EXIT`.
5. `config/scenarios/live_paired_binary_phase1_tiny.yaml` exists, parses, and keeps all enforcement modes **advisory** (preflight rejects placeholders until operator pins metadata).

## Preflight command

```bash
python scripts/preflight_phase1_live_scenario.py \
  --scenario live_paired_binary_phase1_tiny \
  --event-url "https://polymarket.com/event/btc-updown-5m-<START_TS>"
```

## First live advisory protocol (operator — not automated)

1. Pick a BTC 5-minute market.
2. Pin `event_start_ts`, `event_end_ts`, `condition_id`, and token IDs in the scenario overlay.
3. Use `live_paired_binary_phase1_tiny.yaml`.
4. All survival modules remain **advisory**; kill switches enabled but no enforce modes.
5. Run 3–5 markets.
6. Validate facts and classifications (`PHASE1_SURVIVAL_PASS`, replay summary, analyze script).
7. Only after advisory validation passes, enable **enforce** on **one module at a time** (never all together in Phase 1).

## Wave D notes

- `survivor_executable_exit_evaluated` is emitted once per material advisory tick (deduped by `snapshot_id`). Legacy runs without the dedicated fact remain valid when executable evidence is embedded in reachability/stall/trailing/economics facts.
- Kill-switch force-flatten uses the existing Phase 2 reduce-only shutdown path (`handle_open_exposure_at_shutdown`). No cancel-all.
- Kill switches are inactive unless `survival.enabled: true` **and** `survival.kill_switches.enabled: true`.
