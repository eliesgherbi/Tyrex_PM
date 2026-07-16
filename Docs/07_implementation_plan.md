# 07 — Implementation plan (accepted)

**Branch:** `rest_project`  
**Pre-reset commit:** `35f83fd986dfe91aa952787644d895bdeb1f75e4`  
**Status:** R1 executed; R2 not started  
**Supersedes:** root `RESET_PHASE_R0.md` (removed after incorporation)

## Engine decision

Minimal Tyrex event-driven engine. **NautilusTrader is not a dependency, PoC target, or future foundation.**

## Safe rollback (binding)

Allowed only:

- Explicit reversal of archive `git mv` operations (named paths).
- Targeted `git restore` on named paths.
- `git revert` of the R1 commit if already committed.

**Do not use** repository-wide `git checkout HEAD -- .` or `git clean -fd`.  
Do not delete unrelated untracked files. Never modify/delete `.env`.

## Archive (R1)

Moved to `old/`: `src`, `tests`, `scripts`, `config`, `Docs`, `research`, `ops`, `pyproject.toml`, `README.md`, `commandes.md`, `.env.example`.  
Record: `old/HISTORICAL_ENVIRONMENT.md`.  
Retained at root: `.git/`, `.env`, `var/` (untouched), infrastructure.

## Tooling isolation

`old/` excluded from package discovery, editable install, pytest collection, ruff, coverage. Never on `sys.path` / `PYTHONPATH`.

## Roadmap

| Phase | Scope | Status |
|-------|--------|--------|
| **R1** | Archive + minimal CLI skeleton + docs + firewall tests | **Done** |
| **R2** | Event-driven core contracts + dispatcher | Next |
| **R3** | Read-only MD + indicators + signal + observe strategy | Planned |
| **R4** | Intents + risk + planning | Planned |
| **R5** | Shadow OMS + portfolio + persistence | Planned |
| **R6** | Live Polymarket execution adapter | Planned |
| **R7** | Tiny-live validation (explicit auth) | Planned |
| **R8** | Framework acceptance | Planned |
| **Z1–Z4** | Z-Gap foundation → observe → shadow → tiny-live | After R8 |

### R2 acceptance (proposed scope — do not start until approved)

Implement: identifiers, UTC clock interface, base event metadata, market/timer events, instrument model, immutable snapshot types, indicator result, signal, intent stubs, fact envelope, in-process event dispatcher with deterministic ordering, correlation ids, unit tests.  
No adapters, no OMS, no `old/` imports, no NautilusTrader.

## Validation strategy

`ReferenceMomentumStrategy` — see `05_validation_strategy.md`.

## Prohibited

NT install/import/PoC · `old/` imports · venue calls from strategies · trading decisions in adapters/CLI · Z-Gap in generic modules · empty package forests · live trading without authorization · deleting `old/`.
