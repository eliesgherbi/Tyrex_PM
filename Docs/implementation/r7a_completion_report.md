# R7A — Mutation-path implementation and dry validation

**Base:** R6D `f77c5c1`  
**Mutations attempted:** **false**  
**R7B:** not authorized; `live-once` refuses execution even with `--i-authorize-r7b`

## Official sources (V2)

| Topic | Source |
|-------|--------|
| Order types GTC/GTD/FOK/FAK | https://docs.polymarket.com/trading/orders/create |
| FAK BUY = dollar `amount` + worst-price limit | same |
| Heartbeat cancels all opens if session armed then missed (~10s) | https://docs.polymarket.com/api-reference/trade/send-heartbeat |
| SDK | `py-clob-client-v2` (`create_and_post_market_order`, `cancel_order`) |

No V1 assumptions from `old/`. Historical code inspected for auth lessons only (R6D).

## Implementation

| Area | Modules |
|------|---------|
| Read/mutation protocols | `execution/polymarket/protocols.py` |
| Spy + gated SDK mutation transport | `mutation_transport.py` (`SpyMutationTransport`, `SdkMutationTransport`) |
| Live budget authority | `live_budget.py` (`LiveBudgetGuard`) |
| Approval artifact | `approval.py` |
| Decimal sizing under $5 | `order_sizing.py` (FAK dollar `amount`) |
| Mutation state machine | `mutation_lifecycle.py` |
| Dry lifecycle harness | `runtime/r7_dry_lifecycle.py` |
| Read-only proposal builder | `runtime/r7a_proposal.py` |
| CLI | `tyrex-pm r7a-prepare`, `tyrex-pm live-once --approval … --i-authorize-r7b` (R7B body not enabled) |

### SDK methods wrapped (mutation transport)

- `create_and_post_market_order` (FAK/FOK)
- `create_and_post_order` (GTC/GTD)
- `cancel_order` (single owned id only)
- **Forbidden:** `cancel_all`, `cancel_market_orders`, credential create/derive, heartbeat

Network calls require `MutationArmToken(allow_network=True)`. R7A never issues that token. Preflight composition never imports mutation transport.

### Order policy (first $5 lifecycle)

**FAK** marketable BUY: dollar amount ≤ $5, worst price = best ask (tick-quantized down).  
Rationale: immediate fill-or-cancel residual → no resting working BUY → no cancel-all → no heartbeat session.

### Heartbeat decision

**Do not call** for the FAK one-shot. Official heartbeat is optional but, once used, missing heartbeats cancel **all** open orders. Avoid activating it unless a later resting-order design requires it and is separately approved.

### Exit / flatten authorization

Modeled separately from entry in `MutationLifecycle` (`entry_mutations_enabled` vs `exit_mutations_enabled`). After any real fill, entry auth is consumed; exit remains available until flat.

## Tests

| Suite | Count (this phase) |
|-------|--------------------|
| `test_r7a_*.py` | 32 passed |
| Full repo | **230 passed** |

Covered: budget invariant/restart/second entry/new-market rebind; approval accept/mismatch/expire/replay; FAK sizing under $5 + min-size block; dry full fill/exit, partial+cancel, reject, unknown submit; spy cancel-all forbid; SDK arm gate + FAK market path with fake client; preflight mutation-impossible; LIVE_TINY still denied; `.env` hash; dry signing redaction.

**Zero real mutations** in tests and in `r7a-prepare --run-preflight`.

## Live read-only preflight (this run)

Artifact: `var/reporting/r7/live_preflight.json` (gitignored)

| Check | Result |
|-------|--------|
| Public / auth REST | ok (`mutations_attempted=false`) |
| User stream | connected + authenticated |
| Reconciliation | **not clean** — `nonzero_position_count=4`, open orders=0 |
| Balance evidence | present (`balance_ok` per preflight) |
| Geo / Cloudflare | not CF-blocked for this run |
| Readiness | `MUTATIONS_DISABLED` only |
| Credential create/derive | not called |
| Heartbeat | not called |

## Exact proposed R7B trade (blocked — no approval artifact issued)

Market probe succeeded and sized under $5, but **existing venue positions block** issuance of an R7B approval artifact.

| Field | Proposed value |
|-------|----------------|
| Approval artifact ID/hash | **None** — blocked |
| Expiration | n/a |
| Market title | Bitcoin Up or Down - July 17, 2:10AM-2:15AM ET |
| Market start/end | start `2026-07-16T06:17:34Z` / end `2026-07-17T06:15:00Z` |
| Condition ID | `0x97d12f4d071eb505…` |
| Outcome/token | YES (Up token index 0); token present |
| Side | BUY |
| Quantity (max shares) | 9.41 |
| FAK dollar amount | **4.99** |
| Limit / worst price | 0.53 |
| Maximum BUY notional | ≤ $5.00 (sized 4.99) |
| Order type | **FAK** |
| Tick size | 0.01 |
| Minimum order size | 5 |
| Visible executable depth | ask_size 26 @ 0.53 |
| Expected fee bound | protocol fees unknown bps; small vs $5 |
| Maximum holding duration | 180s |
| Entry deadline | proposal window +10m (recomputed at R7B) |
| Normal exit policy | FAK SELL acquired shares only, bounded price |
| Emergency flatten policy | risk-reducing SELL of venue-confirmed qty only |
| Flatten-before-close | 30s |
| Cancel policy | only owned venue order id; no cancel-all; FAK usually no residual |
| User stream | Ready |
| Reconciliation | **Not clean** (4 nonzero positions) |
| Existing relevant position | **Must be zero — currently nonzero** |
| Existing relevant order | none |
| Balance/allowance | Ready (preflight) |
| Mutations | **Still disabled** |

### Worst-case controlled loss (if unblocked later)

```text
Maximum capital at risk: 4.99 USD BUY notional
Possible spread cost: bounded by worst price = best ask
Possible fees: protocol match fees (unknown bps; small vs $5)
Possible slippage within price caps: none beyond limit price
Residual/manual-intervention risk: low for FAK; nonzero if exit book one-sided
```

## R7B gate (not executed)

Requires **both**:

1. Valid approval artifact bound to this report/commit/market/token/caps  
2. Explicit user authorization for that exact artifact  

Plus: venue flat for the selected instrument (and preferably empty relevant account exposure), clean reconciliation, user stream ready.

CLI (future): `tyrex-pm live-once --approval <artifact> --i-authorize-r7b`  
Current build: refuses real submission even when the flag is present.

## Stopping rule

- Mutations disabled  
- No order submit/cancel  
- No reusable approval (none issued)  
- Awaiting flatten of existing positions before any R7B authorization request
