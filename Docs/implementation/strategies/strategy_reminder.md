# BTC 5-minute strategies: quick reminder

These summaries follow the existing implementation plans. Both strategies may
skip a window. The $3 objective is **expected net profit on a selected episode**,
subject to risk and liquidity limits; it is not a guaranteed realized profit.

## 1. `residual_band` — buy a discount, then resell

**Description:** Compare an outcome token's book price with its modeled fair value.
When it looks cheap, buy preferably through a resting, post-only order, then seek
to resell as the executable price improves before the exit deadline.

**Goal:** Capture a temporary pricing discount after acquisition, exit costs and
execution risk. A shrinking residual alone is insufficient: fair value could
fall without our token becoming more valuable.

**Example:** Buying at $0.65 and selling at $0.69 captures $0.04 per share before
costs. The resale and its price must remain uncertain in the forecast.

```text
DATA     -> Verified rules/reference, BTC prices/volatility, both outcome books,
            compatible calibrated models, account/orders/fills.
SIGNAL   -> Fair value versus book estimate; forecast executable resale value.
RULES    -> Fresh supported inputs; positive expected episode profit after costs;
            acceptable fill/toxicity/latency risk; inventory and capital caps.
DECISION -> Skip, or post a buy on the attractive side with a feasible size.
MANAGE   -> Reprice/cancel safely; sell only confirmed inventory; withdraw entries
            when risk rises; follow the mandatory exit policy by its deadline.
RECORD   -> Actual fills, costs and realized resale profit versus forecast.
```

One directional episode per window. A separately gated taker override permits at
most one dispatched taker entry attempt. Complete-set acquisition is an optional,
separate mode. Details: [residual-band plan](residual_band.md).

## 2. `settlement_value` — buy expected payout cheaply, then hold

**Description:** Estimate the probability of the official settlement outcome.
Buy Up or Down only when its conservative expected payout exceeds its executable
all-in acquisition cost by enough. The primary policy holds through resolution.

**Goal:** Capture value between the purchase cost and expected settlement payout,
without depending on a profitable pre-expiry resale.

**Example:** Under a hypothetical $1 payout, a conservative win probability of
0.76 and all-in cost of $0.73 give $0.03 expected profit per share. Buying 100
shares gives $3 expected profit, but a losing outcome can lose the $73 debit.

```text
DATA     -> Verified payout/rules/threshold, required reference history,
            BTC volatility, both books, fees, account and resolution capability.
SIGNAL   -> Conservative settlement probability versus size-dependent total cost.
RULES    -> Supported model and fresh inputs; sufficient edge; feasible target,
            depth and loss/capital limits; working hold/resolution lifecycle.
DECISION -> Skip, or buy the best feasible outcome/size; default route is taker.
MANAGE   -> Consume the episode on a confirmed fill; commit the supported hold;
            monitor official resolution and reconcile payout/claim evidence.
RECORD   -> Receivable remains separate from spendable cash until cash is proven.
```

One directional entry episode per window; a partial fill does not authorize
topping up to reach $3. No routine profit-price target or residual exit in the
primary policy. Details: [settlement-value plan](settlement_value.md).
