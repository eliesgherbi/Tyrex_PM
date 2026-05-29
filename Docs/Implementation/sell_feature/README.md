# SELL / exit feature

Architecture review and implementation plan for **native SELL-side and exit support** on the Polymarket **CLOB V2** stack.

| Document | Purpose |
|----------|---------|
| [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) | Master plan: phases, validation criteria, file list |
| [guru_sell_ledger_parity_plan.md](./guru_sell_ledger_parity_plan.md) | P5: guru allocation-aware SELL (implemented; live validation pending) |
| [allocation_test_strategy_plan.md](./allocation_test_strategy_plan.md) | P4 validation toy (live-validated) |

## Phase status (2026-05-21)

| Phase | Status |
|-------|--------|
| P3.5 live validation hardening | Complete |
| P4 allocation ledger | Complete — `allocation_test_live_auto_1` |
| P4.1 resting SELL lifecycle | Complete |
| P5 guru allocation-aware SELL | **Code + shadow/unit complete** — live guru validation pending |
| P6 TP/SL overlays | Deferred |

**Architecture truth:** allocation ledger is required. All strategy SELLs clamp to per-owner allocation. Guru SELL: `final_size = min(planned, allocated_available, available_to_sell)`.

Start with **IMPLEMENTATION_PLAN.md** for details.
