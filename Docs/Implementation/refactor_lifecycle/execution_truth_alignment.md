# Execution truth alignment — implementation plan

## 1. Objective

Align Tyrex **configuration and gates** with **Polymarket adapter + LiveExecEngine** behavior so `TradableStateHealth` reaches **`HEALTHY`** when the framework is genuinely converged, and **instrument coverage** never silently drops WS/OMS events.

## 2. Scope

- **In:** `PolymarketExecClientConfig` flags (`use_data_api`, etc.), `LiveExecEngineConfig` open-check flags, dynamic instrument activation (`tyrex_pm/runtime/guru_instrument_dynamic.py`, `guru_compose.py`), adapter behavior when instrument missing from cache (`execution.py` WS handlers).
- **Out:** Tyrex reconciliation loops; capital (sibling doc).

## 3. Clean ownership boundary

| Owner | Owns |
|-------|------|
| **Adapter** | WS message handling; skip unknown instrument with warning (`execution.py` ~1283–1288, ~1422–1427) |
| **LiveExecEngine** | `open_check_open_only`, lookback, position repair (`live/execution_engine.py`, `live/config.py`) |
| **Tyrex** | **When** to activate instruments; **YAML** for `use_data_api` vs CLOB; **readiness** instrument policy; **no** venue HTTP for fills |

## 4. Framework / adapter capabilities already available

- **Position reports:** `generate_position_status_reports` — Data API bulk vs CLOB per instrument (`use_data_api: bool = False` in `adapters/polymarket/config.py`).
- **Open-order reports:** `get_orders` in `generate_order_status_reports` (`execution.py`).
- **Open check:** `LiveExecEngineConfig.open_check_open_only` default **True** (`live/config.py`) — early return may limit missing-order resolution (`execution_engine.py` ~1284–1295).
- **Dynamic instruments:** Tyrex `GuruInstrumentDynamicController` + `NautilusGuruExecutionPort` resolve before submit (`tyrex_pm/execution/nautilus_guru_exec.py`).

## 5. What Tyrex must add

1. **Documented runbook** for `use_data_api` vs CLOB tied to **observed** discrepancy rates (measure in staging).
2. **Runtime YAML** exposing `polymarket_use_data_api_for_positions` (maps to adapter config factory in `guru_compose` / factory layer—**spike** where `PolymarketExecClientConfig` is constructed; today Tyrex passes `PolymarketExecClientConfig` from Nautilus factory—may need **extra kwargs** in `build_guru_trading_node`).
3. **Optional** `live_exec_open_check_open_only: false` for prod **only** after ops sign-off (understand trade-offs from Nautilus docs).
4. **Instrument readiness rule (frozen):** No `submit_order` for token until `cache.instrument` exists **and** user WS subscription for market completed per adapter `_maintain_active_market` contract—**verify** in code that submit path always calls `maintain` (adapter does on submit/cancel).
5. **Health linkage:** When instrument missing caused skipped WS events, surface **`DEGRADED_OMS`** or **`UNKNOWN`** via health module—**only** if FW signal exists; else **readiness** fails (strict).

## 6. What Tyrex must not own

- Patching adapter WS skip in Tyrex (fix belongs in **adapter upstream** if behavior wrong).
- Custom position polling HTTP for caps.

## 7. Required interfaces / contracts

```text
InstrumentReadinessPolicy
  def allow_submit(self, token_id: str, cache: Cache) -> bool

ExecutionAlignmentConfig (runtime YAML slice)
  polymarket_use_data_api_for_positions: bool
  live_exec_open_check_open_only: bool | None  # null = framework default
```

## 8. Lifecycle behavior

- **Startup:** Readiness includes instrument policy (see `startup_readiness.md` §8.2.5).
- **Live:** On dynamic resolve failure, **deny** intent (existing exec path) + health unchanged unless FW reports issue.

## 9. Module responsibilities

| Module | Responsibility |
|--------|----------------|
| **Runtime config** | New YAML keys + validation |
| **guru_compose** | Pass adapter/engine config |
| **Execution port** | Already resolves instrument; ensure ordering vs risk |
| **Health / readiness** | Consume outcomes |

## 10. Dependencies on other plans

- **After** [`tradable_state_health.md`](tradable_state_health.md) and [`startup_readiness.md`](startup_readiness.md) baseline.
- **Informs** health **mapping** when discrepancy persists (alignment reduces false `DIVERGENT_PERSISTENT`).

## 11. Implementation steps

1. Trace `PolymarketExecClientConfig` construction in `PolymarketLiveExecClientFactory.create` and Tyrex `build_guru_trading_node` to add `use_data_api` passthrough if missing.
2. Add `LiveExecEngineConfig` overrides from YAML beyond intervals (if not already).
3. Staging A/B on position source; document outcome.
4. Add instrument readiness assertion to `StartupReadinessGate` optional clause.

## 12. Tests / validation strategy

- Config loader tests for new keys.
- Mock adapter config snapshot in compose tests.

## 13. Observability / reporting needs

- Facts: `execution_alignment_profile` (non-secret): `use_data_api`, `open_check_open_only`.
- Compare discrepancy fact rate before/after (operational metric).

## 14. Pre-coding decisions that must be frozen

1. **Default `use_data_api`** for prod (proposal: **false** until staging proves parity).
2. Whether to ever set `open_check_open_only=false` in prod.
3. Owner for **upstream** adapter issue if WS skip is unacceptable (Nautilus repo).

## 15. Phase readiness and factory passthrough spike

### 15.1 Phase readiness (this doc)

| Workstream | Codable now? | Spike question | Spike exit criterion |
|------------|----------------|----------------|---------------------|
| YAML keys, validation, `InstrumentReadinessPolicy`, compose **unit** tests | **Yes** | — | — |
| **Live** `use_data_api` / `LiveExecEngineConfig` overrides reaching the adapter | **No** | What is the exact path from `build_guru_trading_node` / factory to `PolymarketExecClientConfig` kwargs today? | Trace documented in code comments + kwargs wired or minimal factory patch merged |

**Parallel work:** Phase 5 schema and policy helpers land anytime; **prod toggles** that change adapter/engine behavior **block** on §15.1 row 2. Program summary: [`README.md`](README.md) §9.1 Phase 5.

### 15.2 Residual alignment

Instrument §8 readiness rules depend on adapter `_maintain_active_market` — confirm on submit path in the same spike trace if any gap appears.
