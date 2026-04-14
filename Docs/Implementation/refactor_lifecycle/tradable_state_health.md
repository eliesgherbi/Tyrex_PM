# Tradable state health — implementation plan

## 1. Objective

Define **TradableStateHealth** so risk and reporting use a **declared, non-fuzzy** trust model: health is derived from **NautilusTrader / LiveExecEngine–owned signals** (or a **minimal typed bridge** to those signals), **never** from permanent log heuristics or guesswork.

## 2. Scope

- **In:** Classification of OMS/cache trust for **Polymarket live**; consumption in Tyrex risk and facts.
- **Out:** Implementing reconciliation algorithms (FW owns); collateral freshness (see `collateral_unification.md`); startup/shutdown orchestration (sibling docs).

## 3. Clean ownership boundary

| Layer | Owns |
|-------|------|
| **Nautilus `LiveExecEngine`** | Running reconciliation, emitting **machine-readable** status **(target)** or internal events **(today—verify in spike)** |
| **Polymarket adapter** | WS/HTTP → `generate_order_*` / reports |
| **Tyrex** | Subscribing to or polling **documented** FW health; composing `RiskStateView`; **no** OMS repair |

## 4. Framework / adapter capabilities already available

- Position discrepancy detection and fill-query repair: `nautilus_trader/live/execution_engine.py` (`_process_cached_position_discrepancies`, `_query_and_find_missing_fills`, warnings when discrepancy persists).
- Open-order consistency checks: `_check_orders_consistency` (`execution_engine.py`).
- Order/fill/position **state** in `Cache` / `Portfolio` (Nautilus core).
- Adapter does **not** export a dedicated “reconciliation OK” API in the reviewed `PolymarketExecutionClient` (`adapters/polymarket/execution.py`).

**Gap:** A **single public** “engine reconciliation health” type may be **missing** in the pinned Nautilus version—**must be verified in Phase 2 spike** (message bus topics, `LiveExecEngine` facade methods, config flags).

## 5. What Tyrex must add

1. **Spike (blocking):** Enumerate how Nautilus exposes post-reconciliation state: `MessageBus` subscriptions, callbacks, or periodic **typed** flags on engine/cache—not log strings.
2. **One of (frozen before coding):**
   - **Path A (preferred):** Contribute or consume **upstream** `LiveExecEngine` / kernel API: e.g. `reconciliation_status(instrument_id|venue)` or bus event `ReconciliationCompleted` with `{discrepancy_pending: bool, ...}`.
   - **Path B:** **Typed bridge** module in Tyrex that registers **only** against **documented** Nautilus extension points (e.g. custom `LiveExecEngine` subclass **not** desired—prefer upstream). If the only hook is internal, **Path A is mandatory**.
3. **`TradableStateHealth` enum** and immutable snapshot DTO updated **only** from Path A/B inputs.
4. Wire **`ConfiguredRiskPolicy`** to **`RiskStateView`** including health.

## 6. What Tyrex must not own

- Parsing stderr/log lines as the **final** health source.
- “Temporary” heuristics without an **open upstream ticket** and **removal date**.
- Duplicate `get_orders` / `get_trades` reconciliation.

## 7. Required interfaces / contracts

```text
TradableStateHealth (enum)
  HEALTHY
  UNKNOWN_BOOTSTRAP       # engine not yet observed / no signal received
  DEGRADED_OMS # non-fatal: e.g. open-check deferred, recoverable
  DIVERGENT_PERSISTENT  # framework logged persistent discrepancy w/o repair

TradableStateHealthSnapshot
  level: TradableStateHealth
  reason_code: str       # stable Tyrex reason taxonomy, mapped from FW codes when available
  observed_at_utc: datetime
  framework_detail: str | None  # opaque pass-through from FW if provided

RiskStateView
  health: TradableStateHealthSnapshot
  deployment: ... # from NautilusDeploymentBudget
  capital: CapitalState  # from collateral_unification
```

**Owner module:** `tyrex_pm/runtime/tradable_state/` (or equivalent).

## 8. Lifecycle behavior

- **Boot → connect:** `UNKNOWN_BOOTSTRAP` until first **valid** FW signal.
- **After first engine reconciliation signal:** transition to `HEALTHY` or non-healthy per FW mapping table (implementation fills mapping once spike done).
- **Live:** re-evaluate on **FW events** or **scheduled snapshot** aligned with engine (not faster than engine truth).

## 9. Module responsibilities

| Module | Responsibility |
|--------|----------------|
| **Tradable state** | Health snapshot + façade over cache readers |
| **Risk** | Deny/allow per §10 matrix |
| **Reporting** | Emit `tradable_state_health` fact |
| **Nautilus (upstream)** | Expose reconciliation outcome **as API or bus event** |

## 10. Risk behavior by health (default product policy — **frozen**; mirrored in [`startup_readiness.md`](startup_readiness.md) §8.1 / §8.4)

**Framework-first rationale:** SELL still needs **trusted inventory / OMS** (`ConfiguredRiskPolicy` sell gates use `NautilusDeploymentBudget` / cache). Where health is not `HEALTHY`, the default is **fail-closed** on SELL so we do not reduce or flatten on **untrusted** position state.

| Health | BUY (new risk) | SELL (reduce / exit) |
|--------|----------------|----------------------|
| `HEALTHY` | Allowed if other gates pass | Allowed if inventory gate passes |
| `UNKNOWN_BOOTSTRAP` | **Deny** | **Deny** |
| `DEGRADED_OMS` | **Deny** | **Deny** unless `risk.allow_exit_when_degraded_oms=true` (default **false**) |
| `DIVERGENT_PERSISTENT` | **Deny** | **Deny** |

**Single opt-in:** `risk.allow_exit_when_degraded_oms` (YAML/runtime) is the **only** switch that may allow SELL under `DEGRADED_OMS`. There is **no** separate “startup-only” exit permission.

**Startup `DEGRADED` mode** (`NO_NEW_ENTRIES`): **BUY always deny.** **SELL** follows **this same table** (i.e. SELL allowed only when health is `HEALTHY`, or when health is `DEGRADED_OMS` **and** the opt-in flag is **true**, in both cases subject to inventory gate). **Never** SELL on `UNKNOWN_BOOTSTRAP` or `DIVERGENT_PERSISTENT`.

### 10.1 WP2 producer — what `HEALTHY` does **not** mean (operators / validation)

When snapshots come from **`NautilusLiveExecutionHealthSource`** (`tyrex_pm/runtime/tradable_state/nautilus_live_health.py`), **`HEALTHY` indicates that Nautilus `LiveExecutionEngine._startup_reconciliation_event` is set** — i.e. the engine’s **startup reconciliation pass has completed** in the sense of that latch (`reconcile_execution_state` always signals the event in a `finally` block). It does **not** mean:

- mass-status or venue reconciliation **succeeded** end-to-end (no separate public success flag on the pinned stack tied to this latch), or
- there are **no** position/order discrepancies, or
- **`DEGRADED_OMS`** / **`DIVERGENT_PERSISTENT`** have been ruled out (the current producer does **not** emit those levels from live framework signals).

**Do not over-interpret `HEALTHY` as full machine-readable OMS health truth** until additional framework-exposed signals exist (§15.2). Use **`framework_detail`** and **`reason_code`** on facts for the explicit mapping (`nautilus_exec_startup_reconciliation_complete` vs pending).

## 11. Dependencies on other plans

- **Before:** [`collateral_unification.md`](collateral_unification.md) for combined `RiskStateView`.
- **Parallel:** [`startup_readiness.md`](startup_readiness.md) (readiness uses health).
- **After:** [`execution_truth_alignment.md`](execution_truth_alignment.md) may add instrument-coverage dimension to health.

## 12. Implementation steps

1. Spike Nautilus message bus + `LiveExecEngine` for **any** reconciliation/completion signal (document findings in repo `Docs/...` spike note).
2. If signal missing: open **Nautilus issue/PR**; **do not** ship fuzzy production health—keep `UNKNOWN_BOOTSTRAP` → deny.
3. Implement `TradableStateHealthSnapshot` producer wired to Path A or B only.
4. Integrate `ConfiguredRiskPolicy.evaluate` with health matrix.
5. Facts + unit tests with **mocked FW signals**, not log fixtures.

## 13. Tests / validation strategy

- Unit: mapping table from FW DTO → enum.
- Integration (mocked engine): transitions on synthetic events.
- Regression: **no** test that asserts on log substrings for health.

## 14. Observability / reporting needs

- Fact: `tradable_state_health` with `level`, `reason_code`, `framework_detail`.
- **WP4:** When the gate is on but **no** `TradableStateHealthSource` is wired at evaluate, risk still fail-closes as §10; reporting emits the same fact type with `reason_code=health_source_missing`, `level=unknown_bootstrap`, and `reporting_only_synthetic: true` (not framework-derived health).
- Summary: time spent per health level per run.

## 15. Pre-coding decisions, phase readiness, and spike exits

### 15.1 Pre-coding decisions (product / release)

1. **Path A vs B** after spike (upstream vs bridge) — see §5.
2. **Default risk matrix** (§10) vs product overrides — matrix text is **frozen** unless product explicitly documents a change.
3. **Pinned Nautilus version** for API availability.

### 15.2 Phase readiness (this doc)

| Workstream | Codable now? | Spike question | Spike exit criterion |
|------------|----------------|----------------|---------------------|
| §10 risk matrix + `ConfiguredRiskPolicy` + facts | **Yes** (with **mocked** FW inputs) | — | — |
| Live `TradableStateHealthSnapshot` producer | **Yes** (WP2) | What observable FW signal proves **bootstrap** readiness? | **Path B:** `NautilusLiveExecutionHealthSource` reads `LiveExecutionEngine._startup_reconciliation_event` (see `tyrex_pm/runtime/tradable_state/nautilus_live_health.py`). `UNKNOWN_BOOTSTRAP` until set; then `HEALTHY` per mapping there. **Gap:** mass-status success/fail is not a separate public signal; `DIVERGENT_PERSISTENT` / `DEGRADED_OMS` are not derived from this latch alone. |
| Production transitions (`UNKNOWN` → `HEALTHY` / degraded) | **Partial** | Same as row above | Bootstrap transition **yes**; degraded/divergent still require future FW API or bus events. |

**Parallel work:** Phases 1 and 3 may implement **consumers** of `TradableStateHealthSnapshot` against **stubs**; **live** readiness and risk behavior that depends on real health **blocks** on producer spike exit. Align with program table [`README.md`](README.md) §9.1 Phase 2.

### 15.3 Behavior until spike completes

**WP2 update:** With **`NautilusLiveExecutionHealthSource`**, there **is** a bootstrap signal (`§10.1`, §15.2). Until that latch fires, snapshots stay **`UNKNOWN_BOOTSTRAP`** → deny per §10. **`HEALTHY` after the latch still does not imply full OMS truth** (§10.1). If the gate is off or the producer is not wired, behavior follows compose/risk defaults.
