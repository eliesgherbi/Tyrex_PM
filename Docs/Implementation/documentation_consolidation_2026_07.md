# Documentation consolidation — Phase 2 + Phase 1 (2026-07)

Summary of the documentation pass aligning main docs with the current codebase.

## 1. Codebase/doc review summary

- **Phase 2** delivered WS-primary `MarketStateStore`, quality gates, executable book, event-driven paired-binary scheduler, readiness on reconnect gap, and M8/M9 validators.
- **Phase 1** delivered optional `survival/` layer (floor, recovery, trailing, enforce dispatch, FAK order policy, quality-reject retry latch) integrated into `PairedBinaryMonitor`.
- Main docs still described paired-binary as "design only", market WS as "scaffolded not consumed", and lacked survival config reference.
- Implementation detail lives under `Docs/Implementation/`; this pass surfaces it in operator-facing docs.

## 2. Updated documentation files

| File | Change |
|------|--------|
| `Docs/Architecture.md` | Phase 2/1 sections, updated package map and paired-binary status |
| `Docs/README.md` | Program status table, survival/WS links |
| `Docs/CONFIG_MODEL.md` | §4.1 survival, §4.2 strategy_lifecycle, §4.3 market_data extensions |
| `Docs/LIVE_ARCHITECTURE.md` | §1.2 market book truth (WS-primary) |
| `Docs/OPERATIONS.md` | Phase 1 trailing-enforce run + validator |
| `Docs/developer_guide.md` | §4.2c extend survival |
| `Docs/modules/README.md` | `market_data/`, `survival/` entries |
| `Docs/modules/survival/README.md` | **New** module reference |
| `Docs/modules/market_data/README.md` | **New** module reference |
| `Docs/modules/strategies/README.md` | paired_binary implemented + survival/WS |
| `Docs/modules/runtime/README.md` | paired_binary_run, lifecycle, recovery |
| `Docs/modules/state/README.md` | WS-primary market store |

## 3. Updated module docs/docstrings

- New module READMEs for `survival/` and `market_data/`.
- Package-level docstrings unchanged in code (READMEs are canonical per project convention).

## 4. Legacy/stale concepts — handling

| Stale concept | Handling |
|---------------|----------|
| paired_binary "design only" | Updated to **implemented · live-validated** |
| Market WS "not consumed" | Updated to WS-primary for paired-binary |
| P6 TP/SL deferred | Clarified: superseded by `protection/` overlay; separate from Phase 1 survival |
| Phase 1 target_policy/stall as primary flow | Documented as **legacy**; simplified flow is floor → trailing |
| Guru-only runtime spine | Note retained; paired-binary is first-class CLI path |

## 5. Current Phase 1 architecture summary

Opt-in survivor damage control after loser stop: hard floor (default advisory) → recovery level → trailing (enforce in experiment scenarios) → order policy (FAK retry). Pre-submit quality reject → pending intent latch → WS retry. Pre-close flatten preempts pending intents. All enforce paths use existing Intent → RiskEngine → OMS spine.

## 6. Current Phase 2 infrastructure summary

WS-authoritative books, context-aware quality gates, executable depth for planner and survival, event-driven monitor wake, market readiness pause/resume, decision snapshots with book_age/source/verdict, validators for WS-primary and lifecycle pass/fail classifications.

## 7. Remaining documentation or validator gaps

- `reporting_fact_model.md` not fully updated with all Phase 1 survival fact payloads (schema_v2 + validator are source of truth).
- Guru-follow live path less documented post paired-binary focus.
- `features.py` (M5) lightly documented only.

## 8. Tests run and results

Documentation-only change; no code tests required. Spot-check: existing test suite unchanged (no source modifications).

## 9–11. Statements

- **No trading behavior changed** — documentation edits only.
- **No live trading was run.**
- **Global enforcement defaults unchanged** — docs reflect `survival.enabled: false`, module enforce `advisory`, `retry_quality_rejects: false`.
