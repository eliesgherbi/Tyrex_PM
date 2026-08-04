# Strategy and execution flow

**Purpose:** end-to-end life of data, entries, and exits in the accepted framework.

## Runtime pipeline

```text
Venue payload
→ Adapter → Normalized event → State → Indicator → Signal
→ Strategy → Intent → Risk → Plan → OMS → Execution event
→ Orders / Fills / Portfolio → Facts
```

## What strategies may and may not own

| May own | Must not own |
|---------|----------------|
| Hypothesis, parameters, private flags | Venue API clients |
| Interpreting signals into intents | Book reconstruction |
| Evidence on intents | Risk authorization |
| Strategy-private state consistent with portfolio | Execution pricing / OMS state / portfolio truth / persistence |

`ReferenceMomentumStrategy` validates integration only — not profitability or Z-Gap.  
Z-Gap must not import `runtime/r7*`.

## Active callbacks

Protocol: `on_start` / `on_signal` / `on_stop` only.  
No `on_timer` or `on_execution_event`.  
See [events_signals_intents](events_signals_intents.md) for the protocol vs validation-strategy return-type debt.

## Life of a market-data update

1. Adapter receives WS/REST/fixture payload.  
2. Normalize → dispatch.  
3. Stores update; freshness assessed.  
4. Host builds `DecisionSnapshot`.  
5. Indicators + signals update.  
6. `on_signal` → optional intents.  
7. OBSERVE may record facts without OMS submit.

## Life of an entry (SHADOW)

1. Strategy emits `EnterIntent`.  
2. Dedup / retry may gate.  
3. Risk approves or denies.  
4. Planner sizes BUY.  
5. `ShadowOMS.submit` → local `OrderId` + shadow fills → portfolio / `TradeLifecycle`.

## Life of an entry/exit (LIVE_TINY R7 one-shot)

Phase-specific orchestration (`r7b-live-once`), not the generic host loop:

1. Gates: clean worktree, ack, residuals, $5 fee-inclusive BUY cap, one lifecycle.  
2. BUY submit → wait settlement `MATCHED` → `MINED` → `CONFIRMED`.  
3. Sellable qty = `min(confirmed_acquired, funder_conditional_balance)`.  
4. Exit planner: fresh bid-side book → FAK SELL (never reuse BUY limit).  
5. Inventory state from balance; lifecycle outcome from process terminal.

## Life of an N7 Z-Gap one-shot

Operator tool / `tyrex-pm n7-live` (Scope A). Config: `config/n7_tiny_live.json`.

```text
discover BTC 5m window
→ capture Chainlink boundary price
→ seal K immutably (EXACT_AT_START)
→ PTB ready when sealed_k valid
   (SSR openPrice optional; default require_ssr_price_match=false → no SSR fetch/gate)
→ evaluate Z-Gap using sealed_k as model_anchor / K
→ maybe EnterIntent (strategy thresholds)
→ if --live and intent: fee-inclusive size ≤ $5 → submit → bounded exit → recon
→ mutations OFF; report
```

Important distinctions:

| Outcome reason | Meaning |
|----------------|---------|
| `no_ptb_seal` | Chainlink seal failed / no window seal |
| `evaluated_no_enter_signal` | Seal OK; strategy evaluated; no enter intent |
| Entry + flat | One lineage entered and flattened under Scope A |
| SSR-related blockers | Only when `require_ssr_price_match: true` |

Report trust fields: `ptb_authority`, `sealed_k`, `ssr_match_required`, `ssr_check_status`, `ptb_ready`, `evals`, `model_anchor_k`.

## Settlement progression

```text
Order insert status  ≠  trade settlement status
MATCHED → MINED → CONFIRMED
```

Only **CONFIRMED** (+ sellable balance) creates sellable inventory. Planned shares are never inventory.

## Formal detail

- Spec flow: [`../../specifications/04_event_and_runtime_flow.md`](../../specifications/04_event_and_runtime_flow.md)
- Exit floors: [`../../implementation/r7_exit_floor_policy.md`](../../implementation/r7_exit_floor_policy.md)
- R7 live success evidence: [`../../implementation/r7_successful_live_acceptance.md`](../../implementation/r7_successful_live_acceptance.md)
- N7 operator: [`../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md`](../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md)
