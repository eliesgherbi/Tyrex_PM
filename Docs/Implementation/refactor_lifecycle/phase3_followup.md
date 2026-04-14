# Phase 3 follow-up (deferred hardening)

## 1. Purpose

This file records **accepted Phase 3 follow-up / hardening items** that we **intentionally defer** so the main lifecycle refactor program stays on track (especially **Phase 4 — shutdown drain**) without silent scope creep. Items here are **not** ignored; they are **tracked risks** to revisit after Phase 4 unless implementation proves a **direct dependency**.

## 2. Follow-up items

### A. `node.stop()` from the worker thread

- **Current state:** The startup readiness **timeout** path may call `node.stop()` from the **daemon** background thread (see `StartupReadinessCoordinator`).
- **Why acceptable for Phase 3:** Unblocks the codable startup gate on the pinned stack without redesigning Nautilus kernel threading; operators get deterministic exit on terminal `NOT_READY` where configured.
- **Why still a lifecycle/concurrency risk:** `TradingNode.stop()` is not documented as thread-safe for arbitrary call sites; ordering vs main-thread shutdown and engine teardown may still produce races or double-stop edge cases.
- **Clean target direction:** Verify or encapsulate **idempotent** stop behavior; or move stop to a **framework-native** lifecycle hook when available.
- **Phase 4:** **Not** the main objective unless shutdown drain integration **requires** reconciling this path (e.g. shared stop ownership).

### B. Double-stop race

- **Current state:** The startup worker may invoke `node.stop()` on timeout while the **main** thread may also call `node.stop()` on `KeyboardInterrupt` or another exit path.
- **Why acceptable for now:** Idempotent stop is often safe in practice; duplicate calls may no-op harmlessly on many versions.
- **Why still worth hardening:** Without an explicit contract, ordering between drain, coordinator teardown, and stop can still produce intermittent failures or noisy errors.
- **Clean target direction:** **Centralize stop ownership** or add explicit **idempotent coordination** (single “stop requested” gate).
- **Phase 4 blocker?** **No**, unless implementation reveals a **direct conflict** with drain-before-stop ordering.

### C. Reporting sink thread-safety

- **Current state:** The startup readiness worker emits **`startup_readiness`** facts and may drive **manifest** updates from a **non-main** thread while the reporting sink runs asynchronously.
- **Why acceptable for Phase 3:** Keeps the gate observable without blocking on a full reporting redesign; volume is bounded.
- **Why still worth validating/hardening:** If the sink or manifest merge is not thread-safe, facts or manifest fields could corrupt or reorder under load.
- **Clean target direction:** Confirm sink **thread-safety** under documented assumptions, or introduce **one serialized emission path** for lifecycle facts if needed.
- **Phase 4:** **Not** a reason to redesign Phase 4 by default; drain facts are emitted from the main shutdown path, but the same sink is shared—monitor for overlap.

### D. Compose discipline dependency

- **Current state:** Strategy startup blocking assumes **compose** injects **both** `set_risk_settings()` and `set_execution_lifecycle()` before trading; missing either changes §8.4 / readiness behavior silently.
- **Why acceptable now:** Single compose path in `build_guru_trading_node` keeps the contract centralized.
- **Why still a fragility:** Future strategies or tests could register a strategy without mirroring that injection pattern.
- **Clean target direction:** **Assert** or **enforce** the injection contract (e.g. fail fast if lifecycle is set without risk settings), or document a single factory that always wires both.
- **Phase 4 blocker?** **No**, unless shutdown wiring **reuses** the same weakness (e.g. drain needs lifecycle but compose omits it).

## 3. Dependency check

These follow-up items **do not block Phase 4** by default. They should be revisited if **Phase 4 implementation** shows a **direct dependency** (e.g. stop ownership must be unified for drain-before-stop).

## 4. Recommended revisit point

- **Phase 4.5 / lifecycle hardening** pass, or  
- **Post–Phase 4 stabilization** once live shutdown drain has exercised the stack in practice.
