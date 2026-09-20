# Grounding fixes — implementation phase

**Status:** G1–G8 implemented. G3 is operator evidence on the next ask70 tiny-live (trailing off, resting GTD TP).

**Date:** 2026-09-07  
**Branch:** `rest_project`  
**Venue docs reviewed:** Polymarket CLOB order lifecycle, place-order, cancel APIs, book `min_order_size` / `tick_size` (docs.polymarket.com, 2026-09-07)  
**Live evidence:** ask70 tiny-live runs through 2026-09-07 (`…190959Z` accepted G1+G2)

This phase takes the eight next-work items we already understand in plain language and turns each into a **code-and-venue-grounded** build. Reactive take-profit and trailing already worked live. Cancel, resting TP, maker POST, spot L2, and optional z_gap protection are now wired on the same host.

## Locked rules for every task

1. Implement in the order below unless a later task’s plan lists an explicit exception.
2. Reuse `TradingRuntime` / `ExecutionLifecycle` / `PolymarketAsyncGateway`. Do not add a second live host.
3. Prefer official `polymarket-client==0.5.0` methods. Do not invent HTTP, a custom metadata cache, or auto-approve allowances.
4. Never report `COMPLETED_FLAT` while selected-token inventory remains above the dust policy.
5. Real venue mutations stay operator `--live` only. Unit/contract tests use fixtures.
6. Do not change Z-Gap `theta` / `z` / `tau` economics while grounding execution.

## Task order

| ID | File | Status |
|----|------|--------|
| G1 | [leftover_dust_flatten.md](leftover_dust_flatten.md) | Implemented + live-accepted |
| G2 | [submit_latency.md](submit_latency.md) | Implemented + live-accepted |
| G3 | [stop_loss_live_validation.md](stop_loss_live_validation.md) | Code ready; one live SL/resting-TP proof remaining |
| G4 | [oco_cancel.md](oco_cancel.md) | Implemented |
| G5 | [resting_take_profit.md](resting_take_profit.md) | Implemented |
| G6 | [maker_orders.md](maker_orders.md) | Implemented |
| G7 | [extra_market_data.md](extra_market_data.md) | Spot L2 + mid implemented; perp/funding fail-closed |
| G8 | [real_second_strategy.md](real_second_strategy.md) | Option A: optional z_gap `protection:` |

## What is already true (do not rebuild)

- One YAML + `--live` + $5 cap.
- Plugin strategies (`z_gap`, `ask70`) + fact contracts.
- Reactive SL / TP / trailing overlay; **TP and trailing fired live**.
- Final gate: `entry_price_moved`, `candidate_stale_after_signing` (5 s), post-only would-cross.
- Discovery/book already carry `tick_size` and `min_order_size`.
- Soft OCO = cancel session-owned ids, then G1 FAK. No `cancel_all` on tiny-live.

Current operator docs remain [`Docs/latest/`](../../latest/README.md). This folder is the build sequence, not the API.
