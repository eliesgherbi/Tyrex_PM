# 03 — Module contracts

**Phase:** R1 — only `application` is implemented. Other modules are planned contracts for later phases.

## Implemented (R1)

### `tyrex_pm.application`

- **Responsibility:** CLI entry and future composition root.
- **Public:** `cli.main`, `tyrex-pm` console script.
- **Owns:** nothing persistent.
- **Deps:** `tyrex_pm` version only.
- **Forbidden:** venue I/O, strategy decisions, imports from `old/`.

## Planned (not created until consumed)

| Module | Phase | Responsibility |
|--------|-------|----------------|
| `core` | R2 | Ids, clock, base Event, enums, fact envelope |
| `engine` | R2 | EventDispatcher, strategy host loop |
| `adapters.*` | R3 / R6 | Normalize I/O; no trading decisions |
| `market_data` | R3 | Book + reference stores, snapshots |
| `indicators` | R3 | Reusable transforms |
| `signals` | R3 | Typed signals |
| `strategies` | R3–R4 | Protocol + `ReferenceMomentumStrategy` |
| `risk` | R4 | Intent evaluation |
| `execution` | R4–R5 | Planner + OMS protocol + shadow |
| `portfolio` | R5 | Orders, fills, positions |
| `lifecycle` | R5 | Host phases |
| `operations` | R3–R4 | Timers, preflight, kill switch |
| `persistence` | R5 | State repository |
| `reporting` | R3+ | Facts sink |
| `domain.polymarket` | R3 | Instrument/window types |

Do not create empty packages ahead of consumers.
