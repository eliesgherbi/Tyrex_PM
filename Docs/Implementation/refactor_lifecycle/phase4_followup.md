# Phase 4 follow-up (deferred hardening)

## 1. Purpose

Track **accepted** Phase 4 shutdown-drain follow-ups that are **intentionally not redesigned** now so Phase 5 (execution truth alignment) can proceed without reopening the whole lifecycle program.

## 2. Follow-up items

### A. Tyrex-orchestrated per-instrument `cancel_all_orders`

- **State:** Live drain calls `Strategy.cancel_all_orders(InstrumentId, client_id=POLYMARKET_CLIENT_ID)` once per distinct instrument that had open orders at snapshot `open0` (sorted order for stability).
- **Why acceptable:** Matches the **public** Nautilus `Strategy` API on the pinned stack; adapter still owns venue routing. The “cancel all for this strategy” behavior is **composed** in Tyrex, not a single framework call.
- **Revisit:** If/when Nautilus exposes a **single** strategy-scoped cancel-all that is explicitly supported for Polymarket, consider switching call sites to reduce Tyrex orchestration surface.

### B. Startup terminal path: worker may call `node.stop()` before `finally` drain

- **State:** `StartupReadinessCoordinator` may call `node.stop()` from the **daemon worker** when startup is terminal `NOT_READY` and `startup_not_ready_behavior: exit` (see `coordinator.py`).
- **Risk:** That **first** stop can begin teardown/disconnect **before** `run_guru`’s `finally` runs `drain_before_node_stop()`. Phase 4 drain still runs afterward, but cancel commands may be **less reliable** if the execution client is already winding down (behavior depends on Nautilus/idempotency).
- **Does not block Phase 5:** Phase 5 scope is execution-truth alignment, not full lifecycle stop ownership. This remains an **operational ordering** concern for a narrow path (startup failure exit).

### C. Startup worker vs. `finally` drain: lifecycle mutations until `coord.stop()`

- **State:** `run_guru` calls `drain_before_node_stop()` **before** `coord.stop()`. Until `coord.stop()` joins the worker, `StartupReadinessCoordinator` may still call `apply_startup_resolution(...)` (e.g. interim `NOT_READY` / `READINESS_WAIT` polling).
- **Risk:** In principle, that can **overwrite** `ExecutionLifecycleStatus` fields after `begin_shutdown_drain()` set `SHUTDOWN_DRAIN` (e.g. `KeyboardInterrupt` while the gate is still polling). Strategy traffic is usually quiescent once `node.run()` has unwound, but **lifecycle phase visibility** is not strictly single-writer during the drain window.
- **Does not block Phase 5:** Same as (B) — document and harden in a **lifecycle consolidation** pass (e.g. join/stop coordinator before drain, or ignore startup updates once shutdown has started).

### D. Uncaught cancel-loop exception risk

- **State:** `ShutdownDrainCoordinator.run` calls `Strategy.cancel_all_orders(...)` in a loop **without** a per-call `try`/`except`. Any exception **propagates** out of the coordinator and can **abort** the `run_guru` `finally` block before orderly `shutdown_drain` fact emission, manifest field updates, or the subsequent `node.stop()` / `coord.stop()` sequence (depending on where the exception surfaces).
- **Why acceptable to defer:** Live cancel failures are **exceptional**; normal path completes. Operators still get logs from Nautilus/strategy on failure modes in many cases.
- **Why still worth hardening:** A single bad `cancel_all_orders` should not prevent **recording** drain outcome (timeout vs partial cancel vs error) and should not skip **best-effort** `node.stop()` if policy says to always attempt teardown.
- **Clean target direction:** Wrap each cancel in a bounded handler; **continue** or **abort** the loop under an explicit policy; always emit a **terminal** `shutdown_drain` fact with `error_detail` when applicable; consider **collecting residual** `orders_open` after partial failure.
- **Does not block Phase 5:** Phase 5 is execution-truth alignment (adapter/engine config + readiness policy), not shutdown error containment.
- **Revisit:** With **Phase 4.5 / lifecycle hardening** or the next drain-focused maintenance pass.

## 3. Why these do not block Phase 5

Phase 5 is scoped to **execution truth / adapter alignment**, not a full redesign of process shutdown or startup threading. Items **A–D** above are **lifecycle orchestration** and **drain error-containment** hardening, not prerequisites for truth-alignment toggles.

## 4. Recommended revisit point

- **Post–Phase 5 stabilization**, or a dedicated **Phase 4.5 / lifecycle hardening** milestone: unify **stop ownership** (single place for `node.stop()`), and define a **shutdown latch** so startup polling cannot clobber drain phase.
