# Shutdown cancel-and-drain — implementation plan

## 1. Objective

Make **live** shutdown **safe by default**: **cancel working orders and drain** to terminal states (bounded wait) **before** `TradingNode` stop/disconnect—because **framework stop does not imply clean venue cleanup**.

## 2. Scope

- **In:** `execution_mode: live`, Polymarket venue, Tyrex guru copy strategies using `NautilusGuruExecutionPort`.
- **Out:** Shadow runs (no drain required); manual kill -9 (document as unclean).

## 3. Clean ownership boundary

| Owner | Responsibility |
|-------|----------------|
| **Tyrex `ShutdownDrainCoordinator`** | Orchestrate **phase order** §8; mandatory default for live |
| **Nautilus** | Route `CancelOrder` / `CancelAllOrders` to execution client |
| **Polymarket adapter** | `_cancel_order`, `_cancel_all_orders`, `_batch_cancel_orders` (`adapters/polymarket/execution.py` ~835–982); `_disconnect` **only** closes websockets (~245–257) |
| **`NautilusKernel`** | `stop_async` → `_disconnect_clients` (`system/kernel.py` ~1064–1092)—**no mass cancel** in reviewed path |

## 4. Framework / adapter capabilities already available

- **Cancel primitives (adapter):** HTTP cancel by venue order id; cancel-all uses `self._cache.orders_open(..., strategy_id=...)` then `cancel_orders` (`execution.py` `_cancel_all_orders`).
- **Stop:** `TradingNode.stop` → `kernel.stop_async` → engine disconnect; adapter `_disconnect` closes WS.
- **Proof:** Venue orders **can** remain **live** after stop if not canceled—**Tyrex must cancel first**.

## 5. What Tyrex must add

1. **`ShutdownDrainCoordinator`** invoked from `scripts/run_guru.py` **always** on live exit path (normal return, `KeyboardInterrupt`, and `finally` before manifest) **before** `node.stop()`.
2. **Disable new entries** first: set `ExecutionLifecycleStatus.entries_allowed = false` (or equivalent) **synchronously** visible to strategy.
3. Issue **CancelAllOrders** (or equivalent) **per instrument** / **per strategy** per §7—through **Nautilus command API** (strategy `cancel_all_orders` or trader command—**spike** exact public API on `Strategy`/`Trader` in pinned version).
4. **Await** terminal order states: poll `cache.orders_open` until empty or timeout.
5. **Timeout** and residual reporting §13.
6. **Config:** `shutdown_drain_enabled` default **true** for live; `shutdown_drain_timeout_seconds` default **30**; `shutdown_drain_override` **only** when `TYREX_SHUTDOWN_DRAIN_OVERRIDE=1` or YAML `shutdown_drain_override: true` **with loud log**.

## 6. What Tyrex must not own

- Raw `py_clob` cancel from `run_guru` (must go through Nautilus).
- Assuming `node.stop()` clears venue.

## 7. Required interfaces / contracts

```text
ShutdownDrainResult
  canceled_order_count: int
  residual_open_orders: list[ClientOrderId]
  timed_out: bool
  drain_duration_ms: int

ShutdownDrainCoordinator
  def run(self, *, trader: Trader, strategy_id: StrategyId, timeout: float) -> ShutdownDrainResult
```

**Cancel scope (frozen default):** **All open orders** for the **Tyrex strategy** on **POLYMARKET** venue (same as `_cancel_all_orders` filter in adapter: `orders_open(..., strategy_id=...)`). **Rationale:** guru-tagged-only risks missing orphans if tags stripped; strategy-scoped cancel-all matches adapter implementation.

## 8. Lifecycle behavior — ordered phases

1. **Stop-requested:** Operator interrupt or normal shutdown.
2. **Entries off:** Flip lifecycle flag; strategy returns early on BUY/SELL before risk (or only BUY—**match startup**; **frozen:** block **both** during drain to avoid race).
3. **Cancel:** Emit cancel-all **per strategy** (or batch per open instrument if API requires).
4. **Drain wait:** Poll until no `orders_open` for strategy or timeout.
5. **Optional final open-check:** Trigger or wait one `LiveExecEngine` cycle if API allows (nice-to-have).
6. **`node.stop()`:** Kernel disconnect as today.
7. **Manifest finalize:** Record drain result.

## 9. Module responsibilities

| Module | Responsibility |
|--------|----------------|
| **run_guru** | Call coordinator in `finally` |
| **Coordinator** | Phases §8 |
| **Strategy** | Respect entries off |
| **Reporting** | Residual facts |

## 10. Dependencies on other plans

- **After** `ExecutionLifecycleStatus` exists (`startup_readiness.md` / health phase).
- **Independent** of capital unification except optional final snapshot.

## 11. Implementation steps

1. Spike: how to invoke **CancelAllOrders** from Tyrex runtime (strategy method vs `trader.cancel_all_orders`).
2. Implement coordinator with polling loop (use existing `NautilusExecutionStateReader`).
3. Wire `run_guru.py` `try/finally`.
4. Add override flag + logs.
5. Operator runbook update.

## 12. Tests / validation strategy

- Mock `Cache.orders_open` sequence → timeout vs success.
- Integration: optional live smoke with tiny order (manual).

## 13. Observability / reporting needs

- Fact: `shutdown_drain` with `timed_out`, `residual_count`, `canceled_count`.
- Manifest: `shutdown_drain_clean: bool`, `shutdown_residual_orders: [...]`.

## 14. Pre-coding decisions that must be frozen

1. Default **timeout** (proposal **30s**).
2. **Cancel scope:** strategy-wide vs guru-tag-only (**frozen:** strategy-wide default).
3. **Behavior on timeout:** proceed with `node.stop()` anyway **after** logging **ERROR** and facts—**do not** hang forever.

## 15. Phase readiness and cancel-all API spike

### 15.1 Phase readiness (this doc)

| Workstream | Codable now? | Spike question | Spike exit criterion |
|------------|----------------|----------------|---------------------|
| Coordinator phases, timeouts, facts, polling loop (mocked `Cache`) | **Yes** | — | — |
| **Live** mass cancel before `node.stop()` | **No** | Which **public** `Strategy` / `Trader` / kernel API in pinned Nautilus routes strategy-scoped cancel-all to the Polymarket adapter? | One documented call site in Tyrex + smoke check; adapter command path unchanged |

**Parallel work:** Phase 4 skeleton merges with Phases 1–3 **interfaces**; **live** shutdown safety **blocks** on §15.1 row 2. Program summary: [`README.md`](README.md) §9.1 Phase 4.

### 15.2 Note

Adapter already implements cancel primitives internally (`execution.py`); the spike is **only** Tyrex → Nautilus **public** orchestration — no raw venue HTTP from `run_guru`.
