# Operating modes

**Purpose:** what OBSERVE, SHADOW, and LIVE_TINY mean for users and operators.

Mode selects **OMS dispatch**. The observe/signal pipeline is shared.

## OBSERVE

| Aspect | Behavior |
|--------|----------|
| Market data | Fixtures (offline) or public live feeds (network read) |
| Strategy | Runs; emits decisions/intents in-process |
| OMS | None |
| Portfolio | No real venue fills |
| Venue mutation | **None** |
| Other effects | Report write under `var/reporting/` |
| Purpose | Signal/path validation without orders |
| Proves | Adapters → signals → strategy wiring |
| Does not prove | Fill quality, live settlement, profitability |

CLI: `tyrex-pm observe --config …`

## SHADOW

| Aspect | Behavior |
|--------|----------|
| Market data | Public books/reference (network read) |
| Strategy | Full intent path |
| Risk | Active (`runtime_mode: SHADOW`) |
| OMS | `ShadowOMS` (deterministic local fills) |
| Portfolio / lifecycle | Updated from shadow fills (`TradeLifecycle`) |
| Venue mutation | **None** |
| Other effects | Report write; may local-state write snapshot if configured |
| Purpose | Paper execution against live public books |
| Proves | Intent → risk → plan → OMS → portfolio loop |
| Does not prove | Venue ack races, fees, or live settlement |

CLI: `tyrex-pm shadow --config config/observe_shadow_r5.json …`

## LIVE_TINY

| Aspect | Behavior |
|--------|----------|
| Market data | Live Polymarket + reference |
| Strategy | Validation strategy under strict envelope |
| Risk / gates | Ack, residuals, clean worktree, $5 fee-inclusive BUY cap, one lifecycle |
| OMS | Live Polymarket OMS via mutation transport |
| Settlement | `MATCHED` → `MINED` → `CONFIRMED` before inventory |
| Venue mutation | Only with explicit `r7b-live-once --execute-live` |
| Dry default | Network read + report write; **no** venue mutation |
| Purpose | Guarded tiny-live framework validation |
| Proves | Mutation path + settlement + exit planner safety |
| Does not prove | Strategy edge, Z-Gap, continuous live ops |

R7 live validation is **complete**. Do not treat mode docs as a live command. Phase-specific packages must not become the generic Z-Gap import surface.

## ReferenceMomentumStrategy

Used across modes to exercise the stack.  
**Proves:** framework integration.  
**Does not prove:** profitability or readiness of any production alpha (including future Z-Gap).
