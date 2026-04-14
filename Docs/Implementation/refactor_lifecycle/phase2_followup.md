# Phase 2 follow-up (deferred)

## 1. Purpose

This file records **accepted Phase 2 follow-up items** that are **intentionally deferred** so the lifecycle refactor program stays on track. Items here are **not** reopened as part of Phase 3 unless code work surfaces a **direct dependency**.

## 2. Follow-up item

### A. Reporting symmetry when health gate is enabled but no source is injected

**Status (WP4 — implemented):**

- Risk outcome unchanged: still `RISK_HEALTH_UNKNOWN_BOOTSTRAP` when the gate is on and `tradable_state_health_source` is `None`.
- Reporting: a minimal synthetic snapshot is attached (`UNKNOWN_BOOTSTRAP` + `reason_code=health_source_missing`) so **`tradable_state_health`** facts and **`risk_decision`** `tradable_state_health_*` fields remain joinable; facts may set **`reporting_only_synthetic: true`**.
- Startup readiness: when the gate is on and the health source is missing, **`StartupReadinessResult.health_snapshot`** carries the same synthetic snapshot for lifecycle joinability.

## 3. Dependency check

This follow-up **does not block Phase 3**.

## 4. Recommended revisit point

**Post–Phase 3 reporting polish** (e.g. Phase 3.5) or a small **health/reporting** cleanup pass after startup readiness is stable.
