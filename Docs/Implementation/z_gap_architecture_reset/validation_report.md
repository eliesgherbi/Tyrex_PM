# Validation report

**Status:** Placeholder — fill as phases complete  
**Date opened:** 2026-07-16

## Phase 0 — Objective / evidence / invariants / PoC spec

| Item | Result |
|------|--------|
| `objective.md` | Written |
| `evidence_audit.md` | Written (code-backed) |
| `architecture.md` | Written (invariants + ownership) |
| `nautilus_decision.md` | PoC pinned to 1.230.0; decision PENDING |
| `migration_map.md` | Draft |
| `z_gap_functional_spec.md` | Draft |
| Production code moved | **None** (intentional) |
| Tests added | **None** yet |

### Critical finding carried forward

Path B (`z_gap_session_runtime.start_production_feeds`) does not start CLOB market ingest or attach allocation ledger; Path A (`execute_run`) does. Control-path unification is a prerequisite for Phase 3.

## Later sections (templates)

### Architecture tests

_TBD_

### Data / strategy / execution / e2e tests

_TBD_

### Observe parity (old vs new)

_TBD_

### Shadow parity

_TBD_

### NT PoC A–E results

_TBD — see nautilus_decision.md_

### Known deviations

_TBD_

### Live-readiness

**Not ready** for a new tiny-live path. Existing live paths remain untouched.
