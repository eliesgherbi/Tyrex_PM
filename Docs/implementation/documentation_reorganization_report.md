# Documentation reorganization report

**Date:** 2026-07-18  
**Base checkpoint:** `fb9d0d8` (R8 PASS)  
**Scope:** documentation-only (no runtime behavior change, no live trading, no `.env` edits, no Z-Gap)

## Inventory (before → after)

| Current path (before) | Purpose | Status | Target path | Links to update |
|-----------------------|---------|--------|-------------|-----------------|
| `Docs/00_objective.md` … `09_*.md` | Spec baselines | current / baseline | `Docs/specifications/` | README, cross-refs to Implementation |
| `Docs/` + capital-I implementation folder | Phase evidence | historical evidence (kept) | `Docs/implementation/` (case normalize) | All legacy capital-I path refs |
| *(none)* | Current guides | new | `Docs/latest/**` | Root README → Docs/README |
| `README.md` | Project entry | rewritten (concise) | `README.md` | Point to Docs layers |
| `old/Docs/**` | Pre-reset archive | historical (untouched) | unchanged | n/a |

Duplication policy: specifications retain formal text; `latest/` synthesizes current behavior and links out — no wholesale copy of reports.

## Before / after tree

**Before**

```text
Docs/
├── 00_objective.md … 09_z_gap_future_mapping.md
└── <capital-I implementation folder>/
    └── r6_*.md, r7_*.md, r8_*.md, …
```

**After**

```text
Docs/
├── README.md
├── specifications/
│   ├── README.md
│   └── 00_objective.md … 09_z_gap_future_mapping.md
├── implementation/
│   ├── README.md
│   ├── documentation_reorganization_report.md
│   └── (all prior phase evidence files via git mv)
└── latest/
    ├── README.md
    ├── getting_started/
    ├── concepts/
    ├── modules/
    ├── integrations/
    ├── how_to/
    └── developer_guide/
```

## Move manifest

- `git mv Docs/0*.md Docs/specifications/`
- Case-only rename via temp path: capital-I folder → `Docs/_implementation_tmp` → `Docs/implementation`

## New documents

- `Docs/README.md`
- `Docs/specifications/README.md`
- `Docs/implementation/README.md`
- `Docs/implementation/documentation_reorganization_report.md`
- Entire `Docs/latest/` tree (see [`../latest/README.md`](../latest/README.md))
- `tests/test_docs_links.py`

## Rewritten documents

- Root `README.md` — concise status + pointer to Docs layers (removed stale R6A/B “uncommitted” claim)

## Broken links repaired

- Spec → evidence links retargeted to `../implementation/…`
- Legacy capital-I folder string refs → `Docs/implementation/`
- Spec handoff link to `scripts/r6c_target_host_handoff.md` → `../../scripts/…`

## Contradictions found and resolved

| Contradiction | Resolution |
|---------------|------------|
| Root README claimed R6A/B uncommitted and “R7 requires authorization” as if unfinished | Latest/root now state R8 accepted at `fb9d0d8`; R7 live complete; NT non-dependency |
| Historical success CLI `terminal=FLAT` with dust | Latest docs teach `FLAT_WITH_DUST`; historical reports preserved under implementation |
| Capital-I implementation folder vs lowercase need | Normalized via git mv temp path; link test forbids legacy capital-I path string |

Code/tests remain authoritative; historical reports were not rewritten except path fixes and prior R8 terminal annotations already present.

## Decisions

- Proportional to maturity: no API docs, tutorials, deploy/backtest manuals
- No new live command in latest how-tos
- Thin layer READMEs added for navigation (needed for three-layer model)
- Link validation via repository-local pytest (no external markdown linter required)

## Commands verified

```text
tyrex-pm --help
tyrex-pm version          → 0.3.0
tyrex-pm observe --help
tyrex-pm shadow --help
tyrex-pm r7c-recon --help
pyproject requires-python ≥3.11; extras dev/live
```

## Validation results

- Full `pytest`: **376 passed** (includes `tests/test_docs_links.py`)
- Relative Markdown links in `Docs/**` + root README: all resolve
- No legacy capital-I implementation path strings in active Docs/README/tests
- Packaging still excludes `old*` / Docs from package
- No secrets or full wallet addresses introduced in new docs
- `.env` unchanged (`sha256` matches prior R7A env hash gate)
- `src/` runtime sources unmodified this phase

## Remaining documentation gaps

- Some specification phase banners still say older phase IDs in body text (historical — intentional)
- Optional external markdownlint not configured
- User-stream ops remain “confirm on operator host”
- Continuous live product docs intentionally absent

## Commit / worktree

See git after documentation-reset commit on `rest_project` (not pushed).
