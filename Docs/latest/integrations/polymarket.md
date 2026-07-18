# Polymarket integration

**Purpose:** current supported capabilities and hard limits.  
**Semantics evidence:** [`../../implementation/r6_venue_semantics.md`](../../implementation/r6_venue_semantics.md).

Never document private keys, API secrets, complete account addresses, or real `.env` values.

## Capability matrix

| Capability | Status | Notes |
|------------|--------|-------|
| Gamma discovery (BTC 5m windows) | implemented / validated | `discover-btc-window`, adapters |
| Public market WebSocket books | implemented / validated | observe/shadow live |
| CLOB public reads | implemented / validated | books, time |
| Authenticated L2 reads | implemented / validated | positions, trades, balances |
| User stream | implemented / experimental ops | confirm on operator host |
| Order submission (FAK) | implemented / validated (tiny-live) | operator `--execute-live` only |
| FAK semantics | validated | partial / no-match possible |
| Insert vs trade settlement | validated | MATCHED ≠ CONFIRMED |
| Signer EOA vs funder/proxy | validated | address roles fail-closed |
| Balance / allowance | validated | funder conditional balance authoritative |
| Selected-market flatness | validated | excludes sealed ack set appropriately |
| Acknowledgment policy | validated | exactly four sealed identities |
| Residual dust handling | validated | three records; cleanup `NONE` |
| Fee amount certainty | experimental / uncertain | bounds ≠ realized fees |
| Heartbeat | unsupported in safe paths | can cancel all opens if misused |
| Redeem / merge / split / transfer | unsupported / forbidden | no auto cleanup |
| Generic continuous live | unsupported | not productized |

## Order insert versus settlement

```text
submit → insert status (e.g. matched)
trade statuses: MATCHED → MINED → CONFIRMED
```

Only **CONFIRMED** (+ sellable balance) creates sellable inventory.

## Safety gates (LIVE_TINY)

- Clean git worktree for `--execute-live`
- Durable acknowledgment present and matching sealed policy
- Residual registry evaluated; unexpected tradable exposure blocks
- One lifecycle per process; $5 fee-inclusive BUY cap
- Exit from fresh bids; entry BUY limit not reused

## Related

- Exit floors: [`../../implementation/r7_exit_floor_policy.md`](../../implementation/r7_exit_floor_policy.md)
- Success live: [`../../implementation/r7_successful_live_acceptance.md`](../../implementation/r7_successful_live_acceptance.md)
