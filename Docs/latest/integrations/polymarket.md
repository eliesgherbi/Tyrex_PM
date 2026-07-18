# Polymarket integration

**Purpose:** current supported capabilities and hard limits.  
**Semantics evidence:** [`../../implementation/r6_venue_semantics.md`](../../implementation/r6_venue_semantics.md).

Never document private keys, API secrets, complete account addresses, or real `.env` values.

## Capability matrix

| Capability | Status | Notes |
|------------|--------|-------|
| Gamma discovery (BTC 5m windows) | implemented / validated | `discover-btc-window --which` |
| Public market WebSocket books | implemented / validated | observe/shadow live |
| CLOB public reads | implemented / validated | books, time |
| Authenticated L2 reads | implemented / validated | positions, trades, balances |
| User stream | implemented / experimental ops | confirm on operator host |
| Order submission (FAK) | implemented / validated (tiny-live) | operator `--execute-live` only |
| FAK semantics | validated | partial / no-match possible |
| Insert vs trade settlement | validated | MATCHED ≠ CONFIRMED |
| Signer EOA vs funder/proxy | validated | address roles fail-closed |
| Balance / allowance | validated | funder conditional balance authoritative for sellability |
| Selected-market flatness | validated | ack/residual gates (R7) |
| Acknowledgment policy | validated | sealed identity set in `config/r7/` |
| Residual dust handling | validated | cleanup `NONE` |
| Fee amount certainty | experimental / uncertain | bounds ≠ realized fees |
| Heartbeat | unsupported in safe paths | can cancel all opens if misused |
| Redeem / merge / split / transfer | unsupported / forbidden | no auto cleanup |
| Generic continuous live | unsupported | not productized |

## Operation effects

| Path | Typical effects |
|------|-----------------|
| Public books / discovery | Network read |
| Authenticated inventory | Network read (+ credentials) |
| Ack regenerate | Network read + local state write |
| Recon scripts | Network read + report write |
| `r7b-live-once --execute-live` | Venue mutation (+ reports; may residual local-state write) |

## Order insert versus settlement

```text
submit → insert status (e.g. matched)
trade statuses: MATCHED → MINED → CONFIRMED
```

Only **CONFIRMED** (+ sellable balance) creates sellable inventory.

## Safety gates (LIVE_TINY / R7)

Phase-specific but active:

- Clean git worktree for `--execute-live`
- Local ack artifact matching sealed policy
- Residual registry evaluated
- One lifecycle per process; $5 fee-inclusive BUY cap
- Exit from fresh bids; entry BUY limit not reused

## R8 snapshot (not evergreen)

Account counts (acknowledged positions, dust records) at acceptance are recorded in [`../../implementation/r8_framework_acceptance.md`](../../implementation/r8_framework_acceptance.md) at commit `fb9d0d8`. Mechanisms are generic; counts are snapshot evidence.

## Related

- Exit floors: [`../../implementation/r7_exit_floor_policy.md`](../../implementation/r7_exit_floor_policy.md)
- Success live: [`../../implementation/r7_successful_live_acceptance.md`](../../implementation/r7_successful_live_acceptance.md)
