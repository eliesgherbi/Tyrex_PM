# Operating modes

**Purpose:** what OBSERVE, SHADOW, LIVE_TINY (R7), and N7 Z-Gap tiny live mean for users and operators.

Mode selects **OMS dispatch** (or none). The observe/signal pipeline is shared where applicable.

## OBSERVE

| Aspect | Behavior |
|--------|----------|
| Market data | Fixtures (offline) or public live feeds (network read) |
| Strategy | Runs; emits decisions/intents in-process |
| OMS | None |
| Portfolio | No real venue fills |
| Venue mutation | **None** |
| Other effects | Report write under `var/runs/` |
| Purpose | Signal/path validation without orders |
| Proves | Adapters → signals → strategy wiring |
| Does not prove | Fill quality, live settlement, profitability |

CLI: `tyrex-pm observe --config …`

## SHADOW

| Aspect | Behavior |
|--------|----------|
| Market data | Public books/reference (network read) or fixtures |
| Strategy | Full intent path |
| Risk | Active (`runtime_mode: SHADOW`) |
| OMS | `ShadowOMS` (deterministic local fills) |
| Portfolio / lifecycle | Updated from shadow fills (`TradeLifecycle`) |
| Venue mutation | **None** |
| Other effects | Report write; may local-state write snapshot if configured |
| Purpose | Paper execution against books |
| Proves | Intent → risk → plan → OMS → portfolio loop |
| Does not prove | Venue ack races, fees, or live settlement |

CLI: `tyrex-pm shadow --config config/observe_shadow_r5.json …`  
Z-Gap fixtures: `observe_shadow_z_gap_f4.json` / `observe_shadow_z_gap_f5.json`.

## LIVE_TINY (R7 — framework validation, closed)

| Aspect | Behavior |
|--------|----------|
| Market data | Live Polymarket + reference |
| Strategy | Validation strategy under strict envelope |
| Risk / gates | Ack, residuals, clean worktree, $5 fee-inclusive BUY cap, one lifecycle |
| OMS | Live Polymarket OMS via mutation transport |
| Settlement | `MATCHED` → `MINED` → `CONFIRMED` before inventory |
| Venue mutation | Only with explicit `r7b-live-once --execute-live` |
| Dry default | Network read + report write; **no** venue mutation |
| Purpose | Guarded tiny-live **framework** validation (complete) |
| Proves | Mutation path + settlement + exit planner safety |
| Does not prove | Strategy edge, Z-Gap alpha, continuous live ops |

R7 live validation is **complete**. Phase-specific packages must not become the generic Z-Gap import surface (`runtime/r7*` stays out of Z-Gap).

## N7 Z-Gap tiny live (operator Scope A)

Current **Z-Gap** one-shot live path (not R7). Config: `config/n7_tiny_live.json`.

| Aspect | Behavior |
|--------|----------|
| Market data | Live BTC 5m + Chainlink + Binance (+ CLOB books) |
| PTB / K | Immutable sealed Chainlink `sealed_k` (`EXACT_AT_START`) |
| SSR `openPrice` | **Optional**; `require_ssr_price_match: false` → not fetched, not gated |
| Strategy | Z-Gap evaluate on sealed K; at most one `EnterIntent` lineage |
| Risk / sizing | Fee-inclusive debit ≤ $5.00; Scope A; frozen N7 timing |
| Venue mutation | Only when operator runs with `--live` after preflight `GO` |
| Authorization | Invoking `--live` **is** authorization (no phrase/envelope/nonce) |
| Exit | Bounded Scope A exit ladder; mutations forced OFF at end |
| Purpose | Guarded Z-Gap tiny live testing |
| Proves | Seal → eval → (optional) one entry → exit → recon under $5 |
| Does not prove | Continuous multi-window trading, Scope B, redeem, or SSR display parity |

Safe rehearsals (no venue mutation):

- Fake: `python tools/n7_live/run_n7_live_oneshot.py --fake-rehearsal`
- Read-only: `tyrex-pm n7-preflight` or oneshot **without** `--live`

Operator venue mutation (explicit):

```bash
python tools/n7_live/run_n7_live_oneshot.py --live
# or: tyrex-pm n7-live --live
```

Evidence package: [`../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md`](../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md).

## ReferenceMomentumStrategy

Used across R3–R7 modes to exercise the stack.  
**Proves:** framework integration.  
**Does not prove:** profitability or readiness of Z-Gap as production alpha.
