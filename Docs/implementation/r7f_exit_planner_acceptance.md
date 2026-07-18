# R7F — Exit-planner integration acceptance

**Prerequisite:** R7E on `3df3e21`.  
**Successful live proof (operator-run, not agent):** run `55fd9a76-…` → inventory **`FLAT_WITH_DUST`** (`0.000587`) with SELL limit **0.50** (bid), BUY limit **0.51** unused.  
**R7 live validation complete** — see [`r7_successful_live_acceptance.md`](r7_successful_live_acceptance.md). No further R7 live run.

## Policy values (exact)

Source: `tyrex_pm.runtime.r7_lifecycle_policy`

| Parameter | Value |
|-----------|-------|
| Book freshness max age | **2000 ms** |
| Normal exit price floor | **0.01** |
| Emergency exit price floor | **0.01** |
| Max exit slippage from touch | **0.05** |
| Max exit book spread | **0.20** |
| Settlement max wait | **45 s** |
| Settlement initial / max backoff | **0.25 s / 4.0 s** |
| FAK retry cooldown / max | **0.5 s / 4.0 s** (×1.5 backoff) |
| Max exit attempts | **3** |
| Flatten before close | **30 s** |
| Entry safety buffer | **45 s** (entry deadline = flatten − 45s) |
| Min time remaining for entry | **90 s** to entry deadline |
| Max hold | until flatten deadline (≥ 30 s floor) |
| Qty step / min tradable | **0.01** |
| Max BUY collateral | **$5.00** fee-inclusive |

## Submit-time race

A marketable FAK SELL is protected by a **minimum** acceptable price (bid-side worst/VWAP, floored). It **cannot guarantee a fill** if the book moves between the final snapshot and venue evaluation. Outcomes: full fill, partial fill, or `no orders found to match` → bounded fresh-book replan (new fingerprint), then MANUAL_INTERVENTION with exact residual if attempts/deadline exhaust.

## Leftover search dispositions

| Match | Location | Disposition |
|-------|----------|-------------|
| `sized.limit_price` on BUY submit | `r7b_live_once.py` | **KEEP** (entry only) |
| `sized.limit_price` as `entry_buy_limit` guard | exit planner call | **KEEP** (anti-reuse check) |
| `artifact.limit_price` on dry SELL | `r7_dry_lifecycle.py` | **FIXED** → bid-side planner |
| Ask-side sizing | entry window / BUY planner | **KEEP** (BUY only) |
| Fixed `0.01` floors | policy module | **KEEP** (venue min tick / floors — not emergency unwind fallback) |
| Planned shares as inventory | settlement path | **REJECTED** — CONFIRMED + balance only |
| `old/` imports | active `src/` | **NONE** |

## Rehearsal

```bash
python scripts/r7f_exit_rehearsal.py
```

Report: `var/reporting/r7f/exit_rehearsal.json` (zero mutations).
