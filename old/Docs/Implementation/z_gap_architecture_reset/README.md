# Z-Gap architecture reset package

This package defines the objective-driven architecture for Tyrex_PM, validated first through Z-Gap.

## Documents

| File | Status | Purpose |
|------|--------|---------|
| [objective.md](objective.md) | **Active** | Project objective, capability classification, scope |
| [evidence_audit.md](evidence_audit.md) | **Active** | Reachability and ownership audit (code evidence) |
| [architecture.md](architecture.md) | **Active** | Invariants, ownership, dependency rules, target flow |
| [nautilus_decision.md](nautilus_decision.md) | **PoC pending** | Version-pinned NT assessment and PoC tracks; engine decision unresolved |
| [migration_map.md](migration_map.md) | **Draft** | Current → target disposition for Z-Gap-touched modules |
| [z_gap_functional_spec.md](z_gap_functional_spec.md) | **Draft** | Z-Gap inputs, decisions, intents, lifecycle, facts |
| [validation_report.md](validation_report.md) | Placeholder | Filled as phases complete |

## Current phase

**Phase 0 — Objective, evidence, invariants, PoC specification** (this delivery).

No production module moves yet. Engine decision is blocked on PoC tracks A–E in `nautilus_decision.md`.

## Working rules (summary)

- Z-Gap is the only active migration target.
- Permanent dual-engine architecture is forbidden.
- No tiny-live through a new path without explicit authorization after observe + shadow parity.
- Do not delete live paths before parity evidence exists.
