# Startup readiness — implementation plan (frozen contract)

## 1. Objective

Define **one** startup contract: exact **READY** / **NOT_READY** conditions, timeouts, modes, and **who** decides—so `run_guru` / compose can gate **all** order submission without ambiguity.

## 2. Scope

- **In:** Live and shadow Tyrex runs using `TradingNode` (`tyrex_pm/runtime/guru_compose.py`, `scripts/run_guru.py`).
- **Out:** Shadow-specific simplifications explicitly below.

## 3. Clean ownership boundary

| Owner | Responsibility |
|-------|----------------|
| **Nautilus** | Connect exec/data clients; run `LiveExecEngine` scheduled open/position checks (`LiveExecEngineConfig` from `guru_compose._live_exec_engine_config`) |
| **Tyrex `StartupReadinessGate`** | Evaluate preconditions; set `ExecutionLifecycleStatus`; **no** reconciliation implementation |
| **Operator / YAML** | Override only via **explicit** flags documented below |

## 4. Framework / adapter capabilities already available

- `LiveExecEngine` open/position check intervals (`nautilus_trader/live/config.py`, wired from `tyrex_pm/runtime/guru_compose.py`).
- Adapter `_connect`: WS + `_update_account_state` (`adapters/polymarket/execution.py`).
- **There is no single “ready” flag** in Tyrex today—**this doc adds the gate**.

## 5. What Tyrex must add

1. `StartupReadinessGate` module with **deterministic** evaluation order (see §8).
2. `ExecutionLifecycleStatus`: `phase`, `entries_allowed: bool`, `readiness: READY | NOT_READY | DEGRADED`.
3. `CopyStrategy` (and any live strategy) checks `entries_allowed` **before** `submit_intent` / risk path for **BUY**; **SELL** per §8.4.
4. Runtime YAML: `startup_readiness_timeout_seconds` (default **120**), `startup_mode` overrides.
5. Reporting: `startup_readiness` fact + manifest field.

## 6. What Tyrex must not own

- Polling `get_orders` directly to “fake” readiness.
- Treating `guru_cache_warmup` completion as READY by itself.

## 7. Required interfaces / contracts

```text
StartupReadinessResult
  status: READY | NOT_READY | DEGRADED
  reasons: list[str]   # stable codes
  evaluated_at_utc: datetime

StartupReadinessGate
  def evaluate(self) -> StartupReadinessResult

ExecutionLifecycleStatus
  entries_allowed: bool
  readiness: READY | NOT_READY | DEGRADED
  degraded_definition: Literal["NO_NEW_ENTRIES"] | None
```

## 8. Lifecycle behavior — **frozen definitions**

### 8.1 Modes

| Mode | Meaning |
|------|--------|
| **READY** | All mandatory preconditions (§8.2) true. **BUY and SELL** allowed subject to normal risk/Layer A. |
| **NOT_READY** | Timeout not elapsed: keep waiting (or exit—§8.5). After timeout: **terminal NOT_READY** → process **must not** enter LIVE; **exit with non-zero** or stay **NO_TRADE** per §8.5. |
| **DEGRADED** | **Operator-only** or **explicit YAML** `startup_allow_degraded_live: true`. **Definition (frozen):** `NO_NEW_ENTRIES` — **no BUY ever** in this mode. **SELL:** **identical** to [`tradable_state_health.md`](tradable_state_health.md) §10 — allowed only if health is `HEALTHY` (inventory gate passes), or if health is `DEGRADED_OMS` **and** `risk.allow_exit_when_degraded_oms=true` (default **false**) with inventory gate passes. **Deny** SELL for `UNKNOWN_BOOTSTRAP`, `DIVERGENT_PERSISTENT`, and for `DEGRADED_OMS` when the flag is false. |

### 8.2 READY conditions (live) — **all required**

1. **Exec client connected:** Nautilus exec engine reports clients connected (use **documented** `check_disconnected()` false / equivalent spike-verified predicate on kernel or engine).
2. **Capital fresh:** `CapitalStateProvider.freshness_ok` true ([`collateral_unification.md`](collateral_unification.md)).
3. **Tradable health evaluable:** At least one health signal received **or** explicit `UNKNOWN` handling: **default** requires health **≠** `UNKNOWN_BOOTSTRAP` for READY; if FW silent, remain NOT_READY until timeout unless shadow.
4. **Health level:** `TradableStateHealth == HEALTHY` for **strict READY**. If product enables DEGRADED path, READY strict can be relaxed **only** to transition to **DEGRADED** (not full READY).
5. **Instrument policy:** Dynamic-instrument prerequisites satisfied ([`execution_truth_alignment.md`](execution_truth_alignment.md) §8)—e.g. no “trade” token without cache instrument unless scenario waives.

### 8.3 NOT_READY conditions

- Any §8.2 clause false **before** timeout.
- **Shadow mode:** Preconditions (1) simplified: node built; if `execution_mode=="shadow"`, **READY** immediately after compose (no Polymarket gate) unless operator sets `startup_strict_shadow: true` (optional dev flag).

### 8.4 Exits when readiness not achieved

| Readiness | BUY | SELL |
|-----------|-----|------|
| NOT_READY (waiting) | **Deny** | **Deny** (default) |
| NOT_READY (terminal after timeout) | **Deny** | **Deny** |
| DEGRADED (`NO_NEW_ENTRIES`) | **Deny** | **Same as** [`tradable_state_health.md`](tradable_state_health.md) §10 (SELL only if `HEALTHY`, or `DEGRADED_OMS` + `risk.allow_exit_when_degraded_oms`, with inventory gate) |

**Rationale:** One risk matrix everywhere; startup mode only removes **BUY**, not OMS trust requirements for **SELL**.

### 8.5 Timeout and terminal behavior

#### 8.5.1 Start of timeout clock (**T0**) — **frozen**

- **`T0`** = `time.monotonic()` (or equivalent) captured **once per process run**, **immediately before** `TradingNode.run(...)` is invoked in `scripts/run_guru.py`, **after** `node.build()` has returned successfully.
- **Why:** That call is the **observable** moment the live trading subsystem begins its connect/start work in the current architecture; it is a single, reviewable line and does not depend on Nautilus internals.
- **Observable:** Log `startup_readiness_t0_mono` (and optionally wall time) at capture; **`deadline_mono = T0 + startup_readiness_timeout_seconds`** (default **120**).
- **Implementation obligation:** `node.run()` **blocks** today; readiness evaluation must run **concurrently** (e.g. timer in a Nautilus actor registered at compose time, or a **documented** background thread) so `StartupReadinessGate.evaluate()` can run **after** `T0` while the node is up. **Phase 3** delivers that mechanism; **T0 semantics do not change** if the mechanism does.

#### 8.5.2 Terminal behavior

- **On deadline passed with still NOT_READY:** **Exit process** with non-zero exit code and `run_ended_cleanly=false`, **or** remain in **NO_TRADE** if `startup_not_ready_behavior: no_trade` (default **`exit`** for live)—per §14.1 item 1.

### 8.6 Signal that marks readiness

**Frozen:** Transition to **READY** occurs on **first successful `StartupReadinessGate.evaluate()`** returning READY **after** all §8.2 true—**not** on wall-clock alone. Emit **`startup_readiness` fact** with `status=READY` and timestamp.

## 9. Module responsibilities

| Module | Responsibility |
|--------|----------------|
| **StartupReadinessGate** | Implement §8 |
| **run_guru** | Invoke gate loop; enforce timeout |
| **CopyStrategy** | Honor `ExecutionLifecycleStatus` |
| **Reporting** | Facts + manifest |

## 10. Dependencies on other plans

- **Requires:** [`collateral_unification.md`](collateral_unification.md), [`tradable_state_health.md`](tradable_state_health.md), [`execution_truth_alignment.md`](execution_truth_alignment.md) for full §8.2.5.
- **Parallel:** [`shutdown_drain.md`](shutdown_drain.md) consumes same lifecycle status family.

## 11. Implementation steps

1. Add `ExecutionLifecycleStatus` holder (inject into strategy + gate).
2. Implement gate evaluation per §8.
3. Wire `run_guru` loop: wait until READY or timeout.
4. Block `CopyStrategy` submit path when not allowed.
5. Facts + manifest + docs.

## 12. Tests / validation strategy

- Unit: gate with mocked capital/health/instrument providers.
- Integration (mocked node): timeout → exit.
- Shadow: immediate READY default.

## 13. Observability / reporting needs

- `startup_readiness` fact: `status`, `reasons`, `timeout_seconds`, `mode`, `t0_mono`, `deadline_mono`.
- Manifest: `startup_readiness_status`, `startup_duration_ms`.

## 14. Pre-coding decisions, phase readiness, and residual spikes

### 14.1 Pre-coding decisions (product)

1. `startup_not_ready_behavior`: **`exit`** vs **`no_trade`** (default **exit** live).
2. Whether **DEGRADED** is allowed at all in prod (default **false** unless YAML).
3. **SELL / health:** No separate startup exit flag — only `risk.allow_exit_when_degraded_oms` (see `tradable_state_health.md` §10).

### 14.2 Phase readiness (this doc)

| Item | Codable now? | Spike question | Spike exit criterion |
|------|----------------|------------------|---------------------|
| SELL / DEGRADED / health | **Yes** | — | — |
| **T0 / deadline** | **Yes** | — | — |
| Concurrent gate while `node.run` blocks | **No** | Minimal Tyrex/Nautilus integration (actor vs thread) | Documented insertion point + approach in Tyrex repo; no duplicate OMS |
| §8.2.1 exec connected predicate | **No** | Exact boolean on pinned Nautilus | `exec_clients_ready(node) -> bool` specified in code + tests |

**Parallel work:** Phase 1 (capital) may proceed; Phase 3 coding **blocks** on concurrency + predicate spikes **only** for wiring the gate loop, not for policy tables.

### 14.3 Open design questions

- **None** for contracts; remaining items are **spike deliverables** in §14.2.
