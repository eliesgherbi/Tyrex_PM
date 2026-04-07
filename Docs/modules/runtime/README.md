# Module: `tyrex_pm.runtime`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · **[Current state](../../Implementation/current_state.md)**

## A. Role

**Wire** configuration and Tyrex components into a runnable **Nautilus `TradingNode`**: guru actor + copy strategy + risk + execution ports. Provide **canonical Nautilus/py-clob read boundaries** for risk (`state_readers.py`). Build **`ClobClient`** from environment when needed for **dynamic instrument resolution**, **allowance/balance** snapshots, and **optional C3** REST — **not** for parallel guru order submit (live guru orders go through **`NautilusGuruExecutionPort`** only).

## B. Boundaries

**Belongs here:** Composition roots, `build_guru_trading_node`, **`GuruTradingAssembly`**, instrument dynamic controller wiring, optional guru cache warmup, L2 env helper for Nautilus factories.

**Does not belong here:** Signal/risk **policy** formulas, Data API row parsing, strategy orchestration.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `guru_compose.py` | **`build_guru_trading_node`** — `TradingNode`, readers → risk, execution port branch, **`GuruMonitorActor`** + optional **`GuruStreamActor`** (when `guru_ingest_mode` is `rtds_shadow` or `rtds_primary`) + strategy; **`GuruTradingAssembly.deployment_budget`** when **`execution_mode == live`**; **B5** INFO log (`tyrex_pm phase_b:`); **C1** `guru_rtds_wallet_identity` INFO when RTDS modes enabled. |
| `phase_b_startup.py` | **B5:** `phase_b_startup_summary_line` — formatted active Phase B settings (informational). |
| `deployment_budget.py` | **`NautilusDeploymentBudget`** — pending + filled **USD deployed** per token / portfolio (no mark-based exposure). |
| `state_readers.py` | **`NautilusExecutionStateReader`** (+ **B3** ``count_guru_resting_orders_open``, ``is_guru_resting_order``), **`NautilusAccountSnapshotProvider`**, **`ClobAllowanceStateProvider`**, **`NautilusPositionStateReader`**, `instrument_id_for_outcome_token`. |
| `guru_instrument_dynamic.py` | **`GuruInstrumentDynamicController`** — Gamma + CLOB + `Cache` activation. |
| `guru_cache_warmup.py` | Optional **`warm_polymarket_cache_from_guru_activity`**. |
| `polymarket_nautilus_env.py` | L2 env for Nautilus factories. |
| `clob_factory.py` | **`build_clob_client_from_env`**. |
| `live_stub.py` | Legacy smoke placeholder. |

## D. Main interactions

- **config:** three settings types.
- **data:** `GuruMonitorActor`, `GuruStreamActor` (RTDS), shared dedup/watermark wiring.
- **strategy / risk / execution:** compose + inject.

## E. Status

**Operational:** **`execution_mode: live`** = Polymarket Nautilus data/exec + **`NautilusGuruExecutionPort`** + **zero-bootstrap** per runtime YAML; **C1** guru ingest modes on runtime YAML (`guru_ingest_mode`). See **`Implementation/step_5_runtime_integration.md`**, **`phase_a_closure.md`**, **`OPERATIONS.md`** § Phase B & § Guru ingestion (C1), **`Implementation/c1_shadow_run_guide.md`**, and **`Implementation/phase_b_operational_validation.md`**.

**Soak reports:** `scripts/guru_shadow_report.py`, `scripts/guru_primary_report.py` (Nautilus log file from `run_guru.py`).

## F. Extension guidance

- New runners: new compose functions; keep `TradingNodeConfig` differences explicit.
- Do not move **`Cache`/`Portfolio` reads** into `CopyStrategy`; use **readers** + risk injection.
