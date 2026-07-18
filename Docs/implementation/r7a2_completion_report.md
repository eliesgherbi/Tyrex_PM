# R7A.2 completion report

## Checkpoint

| Item | Value |
|------|-------|
| R7A/R7A.1 commit | `f3aeaee` — *add guarded tiny-live preparation and corrected R7 readiness* |
| R7A.2 commit | `35cc52d` — *add R7 position acknowledgment and session authorization workflow* |
| Pushed | **No** |
| `.env` | Unchanged `27210C97…F772` |

## Acknowledgment

| Item | Result |
|------|--------|
| Policy | `ACK_RESOLVED_REDEEMABLE_UNTOUCHED_R7A1` |
| Count | Exactly 4 |
| Category | All `RESOLVED_REDEEMABLE_POSITION` |
| Validation | `ack_ok=True` (live re-fetch matched fingerprints) |
| Sell/redeem/on-chain | Forbidden |
| Cleanup performed | **None** |
| Artifact | `var/reporting/r7/r7a2_position_acknowledgment.json` (gitignored) |

## Readiness

| Stage | Blockers |
|-------|----------|
| Before ack policy | `MUTATIONS_DISABLED`, `R7B_AUTHORIZATION_ABSENT` (legacy), `ACCOUNT_EXPOSURE_PRESENT` |
| After valid ack | `MUTATIONS_DISABLED`, `R7B_AUTHORIZATION_ABSENT` (legacy session model; **superseded** by operator `--execute-live`) |

No other non-authorization blockers in the R7A.2 prepare run (user stream ready, recon reachable, fees known at policy layer, selected market flat).

## Session design (draft only — not armed)

| Rule | Value |
|------|-------|
| Lifetime | 30 minutes |
| Max windows | 3 |
| Binding | Exactly one eligible BTC 5m market |
| Consumption | At entry submit begin |
| Budget | amount + max entry fee ≤ $5 |
| Exit | FAK SELL acquired qty only; emergency flatten; empty book → MANUAL_INTERVENTION |
| Prohibited | Heartbeat, cancel-all, redeem ack set, on-chain, pyramid, next market |

`user_authorization_present=false`. No mutation arm token. No submit/cancel.

## Dry validation

| Scenario | Result |
|----------|--------|
| Full bind → fill → exit → flat | Pass |
| Skip ineligible window | Pass |
| Max windows | Pass |
| Uncertain submit (budget reserved, no resubmit) | Pass |
| No switch after bind / after consume | Pass |
| Ack tokens never in spy submits | Pass |

**Full suite: 270 passed.** Zero real mutations.

## Future authorization

See `Docs/implementation/r7a2_authorization_workflow.md` and generated  
`var/reporting/r7/r7b_future_authorization_statement.txt`.

Do **not** treat the position acknowledgment as R7B authorization.

## Stop

R7A.2 complete. Awaiting explicit user authorization of the **session envelope** before any R7B.
