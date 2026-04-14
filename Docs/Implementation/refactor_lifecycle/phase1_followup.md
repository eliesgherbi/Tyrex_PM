# Phase 1 follow-up (deferred cleanup)

## 1. Purpose

This file records **accepted Phase 1 follow-up items** that are **intentionally deferred** so the main lifecycle refactor program (Phases 2–5) stays on track. Nothing here is a blocker unless a later phase discovers a **hard dependency**.

## 2. Follow-up items

### A. Narrowing the `CapitalStateProvider` dependency

**Status (WP3 — implemented):** `CapitalStateProvider.snapshot(..., policy: CapitalSnapshotPolicy)` and `freshness_ok(..., policy=...)`; policy is built via `CapitalSnapshotPolicy.from_risk_settings(risk)` at call sites. The provider no longer accepts full **`RiskSettings`**.

### B. Clarifying mixed-source capital observability

**Status (WP3 — implemented):** `risk_decision` / `account_snapshot` capital payloads add **`capital_attrib_free_collateral_usd`** and **`capital_attrib_allowance_usd`** (which field family supplied each scalar) alongside existing **`capital_state_merged_clob`** and per-leg USD fields.

## 3. Dependency check

**Do these items block Phase 2?**

**No** — unless Phase 2 implementation reveals a **direct** coupling requirement (none anticipated). Tradable state health is orthogonal to capital `source` semantics; capital provider shape can be narrowed independently.

## 4. Recommended revisit point

- **After Phase 2** (health producer spike + production wiring) or in a **small “Phase 1b”** hardening sprint: narrow capital provider inputs and clarify capital fact fields if operator confusion appears.
