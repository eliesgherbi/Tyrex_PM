# Lifecycle phase contracts (cross-reference)

**Objective:** Provide a **single index** mapping lifecycle phases to **owning modules** and **child plan** sections so implementers do not hunt across documents.

**Scope:** Phases from general plan README §6; does not replace normative detail in `startup_readiness.md`, `shutdown_drain.md`, or `tradable_state_health.md`.

---

## Phase → owner → child document

| Phase | Primary owner | Normative detail |
|-------|---------------|------------------|
| Boot / compose | Tyrex `guru_compose` | README; [`startup_readiness.md`](startup_readiness.md) §8 |
| Connect (clients) | Nautilus kernel + AD `_connect` | [`startup_readiness.md`](startup_readiness.md) §4–5 |
| Reconcile / readiness wait | FW engine schedules; Tyrex **StartupReadinessGate** | [`startup_readiness.md`](startup_readiness.md); **readiness deadline** clock **`T0`** frozen §8.5.1 |
| Live trading | Tyrex strategy + FW OMS | README §4.1 |
| Degraded / no new entries | Tyrex risk + lifecycle | [`startup_readiness.md`](startup_readiness.md); [`tradable_state_health.md`](tradable_state_health.md) |
| Stop requested | Tyrex lifecycle | [`shutdown_drain.md`](shutdown_drain.md) |
| Cancel-and-drain | Tyrex **ShutdownDrainCoordinator** + AD cancel | [`shutdown_drain.md`](shutdown_drain.md) |
| Final reconcile (optional) | FW | [`shutdown_drain.md`](shutdown_drain.md) |
| Disconnect | `NautilusKernel.stop_async` / AD `_disconnect` | [`shutdown_drain.md`](shutdown_drain.md) §4–5 |
| Terminated / manifest | Tyrex reporting | [`shutdown_drain.md`](shutdown_drain.md) §13 |

---

## Dependencies between phases

- **Readiness** may not complete until **CapitalState** is fresh ([`collateral_unification.md`](collateral_unification.md)) and **TradableStateHealth** is evaluable ([`tradable_state_health.md`](tradable_state_health.md)).
- **Readiness timeout** uses **`deadline_mono = T0 + startup_readiness_timeout_seconds`** where **`T0`** is captured **immediately before** `TradingNode.run(...)` **after** successful `node.build()` — contract [`startup_readiness.md`](startup_readiness.md) §8.5.1 (not “process start” or “compose finished”).
- **Drain** requires **entries disabled** via `ExecutionLifecycleStatus` (same flag family as startup “not live”).

---

## Planning-level open questions

**None** for lifecycle semantics; product knobs are in child **pre-coding** sections. **Spike-gated** engineering items (Nautilus API surfaces, factory kwargs, concurrent gate wiring) are listed per phase in **README §9.1** and each child’s **phase readiness** subsection (e.g. [`startup_readiness.md`](startup_readiness.md) §14.2, [`tradable_state_health.md`](tradable_state_health.md) §15.2).
