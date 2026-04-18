# `runtime/`

Wires everything together. The only module allowed to import freely across layers.

## Files

| File | Role |
|------|------|
| `app.py` | CLI entrypoint (`tyrex-pm`). `cmd_run`, `cmd_live_attest`, `cmd_summarize`. Loads config, builds stores, starts supervisors, drives the guru loop |
| `config.py` | YAML loader + `AppConfig` parsing (see [CONFIG_MODEL.md](../../CONFIG_MODEL.md)) |
| `coordinator.py` | `RuntimeCoordinator` — holds `WalletStore`, `OrderStore`, `HealthRuntime`, runtime knobs (`submit_grace_s`, `adoption_grace_s`, `provisional_unknown_terminal_timeout_s`), dedup signatures (`last_reconcile_signature`, `last_wallet_sync_signature`). Builds `RiskContext` per call (including derived in-flight reservations) |
| `pipeline.py` | The synchronous business pipeline: guru signal → strategy → risk → OMS → reconcile + fact emission for each step. Owns `reconcile_coordinator` and `emit_wallet_sync` (with dedup signatures) |
| `live_supervisor.py` | Background async loops for live mode: `supervised_heartbeat_loop`, `venue_refresh_loop`, `provisional_repair_probe_loop`, `user_ws_staleness_loop` |
| `live_attest.py` | Standalone `tyrex-pm live-attest` command — minimal post + cancel against real CLOB |
| `health_runtime.py` | `HealthRuntime` — heartbeat, user-WS staleness, reconcile-drift, venue-restart-suspected flags. Read-only into `RiskContext` |
| `healthchecks.py` / `risk_contexts.py` | Helpers used by `coordinator.py` and the live supervisor |
| `dependency_graph.py` / `modes.py` / `supervisors.py` | Small helper modules (mode enums, dependency graph utilities) |

## How a single guru tick flows

```
cmd_run(args)
 ├─ load_app_config(...)              # CONFIG_MODEL.md
 ├─ WalletStore() / OrderStore()      # state/
 ├─ HealthRuntime() + RuntimeCoordinator(...)
 ├─ JsonlSink(<runs_dir>/<rid>/facts.jsonl)
 ├─ ShadowOMS or LiveOMS              # execution/
 ├─ SingleWriterOMS(backend)
 ├─ Optional: live supervisors started (live_supervisor.*)
 ├─ Loop:
 │   ├─ poll_guru_incremental         # ingestion/
 │   ├─ ingest_guru_signals           # state/strategy_store dedup + watermark
 │   └─ process_new_guru_signals      # pipeline.py
 │        ├─ guru_signal fact
 │        ├─ strategy.on_guru_signal  # strategies/
 │        ├─ risk.evaluate_intent     # risk/
 │        ├─ register_submit / oms.submit / ack_submit  # execution/
 │        ├─ apply_shadow_fill  (shadow only)
 │        └─ reconcile_coordinator    # state/reconcile
 └─ summarize_run -> run_summary.json # reporting/
```

## Live supervisors

| Loop | Cadence | Updates |
|------|---------|---------|
| `supervised_heartbeat_loop` | `interval_s` (≥5 s clamp) | `health.heartbeat_ok` + `health` fact on transition |
| `venue_refresh_loop` | `reconcile_interval_s` | REST wallet refresh → optional positions refresh → local OMS sync → `wallet_sync` fact (deduped) → `reconcile_coordinator` |
| `provisional_repair_probe_loop` | tighter probe | Refresh + reconcile when there are provisional rows; emits a `health` fact on first error |
| `user_ws_staleness_loop` | per-second | Marks `user_ws_stale` after no message for `stale_threshold_s` |

All loops share an `asyncio.Event stop` and use `asyncio.wait_for(stop.wait(), timeout=...)` instead of `asyncio.sleep` so shutdown is prompt.

## Run artifacts

`var/reporting/runs/<run_id_or_name>/`:

- `manifest.json` — parsed `AppConfig.raw`, git SHA, scenario name, args.
- `facts.jsonl` — every operator-relevant decision (one per line). See [reporting_fact_model.md](../../reporting_fact_model.md).
- `run_summary.json` — per-fact counts, top reason codes, last reconcile severity, runtime seconds.
