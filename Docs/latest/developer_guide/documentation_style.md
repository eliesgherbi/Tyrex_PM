# Documentation style

**Purpose:** keep docs navigable and honest as the framework evolves.

## Document header

Start current docs with:

- **Purpose** (one line)
- Checkpoint / status when relevant
- Links to specifications or implementation evidence instead of pasting them

## Current vs historical language

| Layer | Language |
|-------|----------|
| `Docs/latest/` | Present tense; current accepted behavior |
| `Docs/specifications/` | Baseline / requirements; mark obsolete nav metadata only |
| `Docs/implementation/` | Chronological evidence; preserve incidents |

Mark superseded auth/workflows explicitly (`HISTORICAL` / `SUPERSEDED`).

## Paths and links

- Prefer relative Markdown links
- After moves, update every affected relative link
- Do not leave the legacy capitalized Implementation folder path in active docs (use `Docs/implementation/`)

## Mermaid

Use compact diagrams for architecture/flows. Tyrex_PM terminology only — do not relabel NT components as ours.

## Commands

- Verify against `tyrex-pm --help` / subcommand help and tests before documenting
- Do not invent Docker/deploy/live runbooks
- Do not provide new real-money live commands in latest docs

## Secrets

Never include private keys, API secrets, complete wallet addresses, or real `.env` values. Use suffixes or placeholders.

## Experimental / unsupported

Use explicit labels: `implemented`, `validated`, `experimental`, `unsupported`, `forbidden`.

## Update rules

- Behavior change → update `Docs/latest/`
- Phase completion / incident → add under `Docs/implementation/`
- Objective/architecture baseline change → update `Docs/specifications/` and reflect in latest
