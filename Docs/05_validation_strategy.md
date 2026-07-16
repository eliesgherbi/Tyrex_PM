# 05 — Validation strategy

**Phase:** R5 — shadow execution lifecycle for `ReferenceMomentumStrategy`.

## Name

`ReferenceMomentumStrategy` (`framework_validation`)

## Exit policy (precedence)

```text
KILL_SWITCH
→ MARKET_CLOSE_BOUNDARY
→ MAX_HOLD
→ SIGNAL_REVERSAL
→ SIGNAL_FLAT
```

Max-loss exit deferred until mark-to-market accounting is correct.

## Shadow fill model (honest limits)

- Visible-depth marketable limits only
- No queue position, latency, or market-impact model
- Does not mutate authoritative external books
- Fee model: configurable (`shadow_zero_fee_v1` allowed for framework validation)
- Shadow P&L is **not** profitability evidence

## Out of scope

PTB · Chainlink · Z-Gap edge · live private trading · venue reconciliation (R6)
