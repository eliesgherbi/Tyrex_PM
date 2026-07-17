# R7A.2 — Position acknowledgment and R7B session authorization workflow

**R7A/R7A.1 checkpoint:** `f3aeaee` — `add guarded tiny-live preparation and corrected R7 readiness`  
**R7A.2 checkpoint:** `35cc52d` — `add R7 position acknowledgment and session authorization workflow`  
**Not pushed.**

## Position acknowledgment policy

User text (hashed into the record):

> I acknowledge the four resolved positions and authorize them to remain untouched during R7. I do not authorize redemption or any other action on them. They may be excluded from selected-market flatness, but must remain visible in account-wide reconciliation.

| Permission / prohibition | Value |
|--------------------------|-------|
| Selected-market flatness exclusion | Allowed |
| Account-wide visibility | Required |
| Sell / redeem / merge-split / transfer / approve / on-chain | **Forbidden** |

Validity: exact four fingerprints must remain `RESOLVED_REDEEMABLE`; no selected-market match; no new unknown/active positions; no unknown open orders. Inventory changes require a **new** user decision (ack is never silently updated).

Module: `src/tyrex_pm/runtime/r7_position_ack.py`  
CLI: `tyrex-pm r7a2-prepare`

## Two-level authorization

```text
User-approved R7B session envelope
        ↓
Runtime selects one eligible BTC 5m market
        ↓
Runtime creates exact execution artifact
        ↓
All constraints revalidated
        ↓
One lifecycle or no trade
```

| Level | Contains exact token? | Lifetime |
|-------|----------------------|----------|
| `R7BSessionAuthorization` | No | ≤ 30 minutes |
| `R7BExecutionArtifact` | Yes | Short (~20s) at bind |

### Session limits (initial)

| Parameter | Value |
|-----------|------:|
| Session lifetime | 1800s |
| Windows observed | ≤ 3 |
| Markets bound | 1 |
| Max BUY collateral incl. fee | $5.00 |
| Entry / exit order | FAK |
| Entry attempts | prefer 1; max 2 if first definitively unused |
| Max hold | 180s (≤ flatten) |
| Flatten-before-close | 30s |
| Exit attempts / interval | 3 / 2s |
| Heartbeat / next market / pyramid | Disabled |

### Consumption

```text
Session created → not consumed
Market bound → selection locked
Entry submit begins → authorization consumed
```

Any fill permanently consumes entry authority. Exit remains only for the acquired position. Acknowledged resolved positions are never execution targets.

## Future operator flow

```text
tyrex-pm r7a2-prepare --run-preflight
# …user reviews draft session + authorization statement…

tyrex-pm live-once --session <authorized-envelope> --i-authorize-r7b
```

R7A.2 does **not** set `user_authorization_present=True` and does not arm mutations. `live-once` still refuses real submission.

### Pre-arm display (required before future R7B)

```text
Mode: REAL LIVE
Strategy: ReferenceMomentumStrategy
Market family: btc_updown_5m
Max collateral: $5.00 including entry fee
Max entries: 1
Max positions: 1
Entry order type: FAK
Exit authorization: FAK SELL acquired qty only + emergency flatten
Session lifetime: ≤ 30 minutes
Window limit: 3
Acknowledged old positions: 4 RESOLVED_REDEEMABLE (untouched)
Prohibited: redeem/sell ack set, heartbeat, cancel-all, on-chain, next market
```

## Exact future authorization wording

Generated at `var/reporting/r7/r7b_future_authorization_statement.txt` (gitignored). Template:

```text
I authorize one-shot R7B session <session_id> (hash-bound; nonce <nonce>)
on branch rest_project at commit <commit> for strategy ReferenceMomentumStrategy
on market family btc_updown_5m only. …
Maximum cumulative BUY collateral including entry fee is $5.00.
Allowed: one FAK BUY, cancel owned order id if required, FAK SELL of
venue-confirmed acquired quantity, emergency risk-reducing FAK SELL.
Forbidden: redemption or any action on acknowledged position set <ack_id>,
heartbeat, cancel-all, on-chain ops, allowance approval, credential
create/rotate, pyramiding, automatic next market, post-lifecycle continuation.
```

The position acknowledgment is **not** R7B authorization.

## Modules

| Area | Path |
|------|------|
| Ack | `runtime/r7_position_ack.py` |
| Session / artifact | `runtime/r7b_session.py` |
| Dry session | `runtime/r7b_session_dry.py` |
| Prepare | `runtime/r7a2_prepare.py` |
| Readiness policy | `acknowledged_resolved_redeemable` in `r7_readiness.py` |
