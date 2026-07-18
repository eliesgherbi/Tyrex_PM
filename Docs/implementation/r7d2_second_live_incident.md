# R7D.2 second live incident — side-incorrect FAK SELL (R7E)

**Run:** `76e8470a-72dc-4d6d-b653-760e87bdaf29`  
**Commit at run:** `2b1e50c`  
**Market:** `btc-updown-5m-1784315400` (Bitcoin Up/Down Jul 17 3:10–3:15PM ET)  
**Token suffix:** `28780279` (Up)  
**Preserved artifacts:** `var/reporting/r7d2_second_live/report_76e8470a-….json`, `facts_76e8470a-….jsonl`

## Venue-proven timeline

| Step | Evidence | Result |
|------|----------|--------|
| BUY `0x1eb62e98…` | FAK @ 0.51, `size_matched=9.470587` | MATCHED |
| BUY trade | `186f4107-…` CONFIRMED, tx `0x0dd61ddd…` | Inventory acquired |
| Settlement wait (R7C) | MATCHED → MINED → CONFIRMED; sellable `9.470587` | Correct |
| SELL attempt `0x29cd29a8…` | FAK @ **0.51** (reused BUY limit) | `no orders found to match` — **no fill** (`get_order` → null) |
| Manual SELL `0xd123c058…` | FAK @ **0.50**, size `9.47`, CONFIRMED, tx `0x46296676…` | External flatten |
| Residual | Conditional balance `0.000587` | `FLAT_WITH_DUST` |
| Market | Closed; Down winner; Up price 0 | Resolved / non-tradable dust |

Open orders on selected token: **zero**.

Classification after R7E recon: **`FLAT_WITH_DUST`** (non-tradable), with provenance retained in the residual registry. Manual flatten is proven by a CONFIRMED SELL at 0.50 that is not the runtime attempt id.

## Root cause

Exit submit reused entry `sized.limit_price` (BUY ceiling = 0.51) as the SELL limit:

```text
BUY limit  = maximum acceptable purchase price
SELL limit = minimum acceptable sale price (must be marketable vs bids)
```

The later manual fill at **0.50** proves executable bid-side liquidity existed at 0.50. A FAK SELL at 0.51 therefore found no resting bid at ≥ 0.51.

Historical book at the exact millisecond of the failed SELL is not reconstructed from a local snapshot store; the venue FAK rejection plus the subsequent 0.50 fill are sufficient proof.

## Follow-up live proof (operator) — R7 third live **complete**

Run `55fd9a76-743b-4fb8-835d-adcdbf0f517a` on commit `3df3e21` — full acceptance: [`r7_successful_live_acceptance.md`](r7_successful_live_acceptance.md).

- BUY `0xde990e41…` @ 0.51 → CONFIRMED `9.470587`
- Exit plan best_bid **0.50**, SELL `0x336afebe…` @ **0.50** (`entry_buy_limit_not_used=true`)
- Inventory terminal **`FLAT_WITH_DUST`** (residual `0.000587`); lifecycle completed successfully
- Historical CLI printed `terminal=FLAT` (pre-R8 inconsistency); R8 corrects inventory class to `FLAT_WITH_DUST`

## Side-correct fix (R7E)

Module: `src/tyrex_pm/execution/polymarket/lifecycle_exit_plan.py`  
Wired in: `src/tyrex_pm/runtime/r7b_live_once.py`

- Fresh bid-side book before every SELL
- Marketable FAK limit from worst consumed bid / VWAP depth
- Never reuse BUY limit when best bid is below it
- Bounded FAK no-match retries with cooldown + flatten deadline
- Confirmed sold qty ownership; residual registry upsert on incomplete exit

## Residual registry

Both lifecycle residuals remain (R7B dust not overwritten):

1. `…ab526401|…36466979|d632b631-…` — first live, `FLAT_WITH_DUST`
2. `…eea9b7be|…28780279|76e8470a-…` — second live, `FLAT_WITH_DUST`

Cleanup policy: **`NONE`**. Neither is in the four-position acknowledgment set.

## Legacy comparison (`old/` — read-only)

| Concept | Source | Verdict |
|---------|--------|---------|
| Fresh book capture at plan time | `old/.../pipeline.py`, `executable_book.py` | **REUSE_CONCEPT** |
| Urgent FAK worst/VWAP on bids | `old/.../execution/planner.py` | **REUSE_CONCEPT** |
| SELL tick floor | `old/.../order_builder.py` | **REUSE_CONCEPT** |
| Remaining-qty FAK retry helpers | `plan_fak_retry_for_remaining` (tests/docs; unwired) | **ADAPT** (now wired in R7E) |
| Strategy touch-bid seed as final price | paired-binary / z_gap exit | **ADAPT** — strategy intent only |
| Emergency unwind `bid` / `0.01` fallback without depth | `paired_binary_run._unwind_leg` | **REJECT** |
| OMS ack as final exit PnL | fill recon docs | **REJECT** |
| Importing or restoring `old/` architecture | — | **REJECT** |

No `old/` imports, `sys.path` entries, or legacy script execution were used.
