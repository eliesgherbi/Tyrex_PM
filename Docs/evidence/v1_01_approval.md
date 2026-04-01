# Milestone v1.01 — Approval record

| Field | Value |
|-------|-------|
| **Decision** | **Approved** |
| **Scope of approval** | **Technical validation of the resolution pipeline only** |
| **Date** | 2026-03-27 |
| **Approver** | Technical lead (per process) |

## Rationale (condensed)

- `config/v1_markets.yaml` is present and version-controlled.
- `Docs/validation/v1_01_resolution_notes.md` documents successful slug → instrument/token resolution, matching tick size, and book check for the reference market.

## Operational caveat (required)

The current allowlist still uses an **example / reference** market slug (`gta-vi-released-before-june-2026`), **not** the final operational v1 universe.

**Before live copy usage:**

1. Replace the allowlist with the real v1 universe in `config/v1_markets.yaml`.
2. Rerun resolution / note generation so `Docs/validation/v1_01_resolution_notes.md` reflects that universe.

This milestone approval validates the **resolution path**, **not** final production market selection.
