# Polymarket integration

**Purpose:** current supported capabilities and hard limits.
**Semantics evidence:** [`../../implementation/r6_venue_semantics.md`](../../implementation/r6_venue_semantics.md).
**Transport:** official stable `polymarket-client==0.2.0` → thin Tyrex adapters → domain contracts → `MarketStateStore` / OMS. No `py-clob-client-v2` and no general raw CLOB client. Narrow exception: public `GET /time` via `adapters/polymarket/clob_server_time.py` because the unified SDK does not expose server time.

Never document private keys, API secrets, complete account addresses, or real `.env` values.

## Capability matrix

| Capability | Status | Notes |
|------------|--------|-------|
| Gamma discovery (BTC 5m windows) | implemented / validated | `PublicClient.get_event` via Tyrex discovery adapter |
| Public market WebSocket books | implemented / validated | `AsyncPublicClient.subscribe(MarketSpec)` |
| CLOB public reads | implemented / validated | `PublicClient.get_order_book` (+ metadata) |
| CLOB public server time | implemented / validated | Narrow Tyrex adapter `GET /time` (not in `polymarket-client==0.2.0`) |
| Authenticated L2 reads | implemented / validated | `SecureClient` balances/orders/trades/positions |
| User stream | implemented / experimental ops | `AsyncSecureClient.subscribe(UserSpec)`; N7 preflight probes RO |
| Chainlink RTDS boundary capture | implemented | N3/N4/N7 PTB seal (`EXACT_AT_START`) |
| SSR displayed `openPrice` attestation | implemented / **optional** | `require_ssr_price_match`; N7 default `false` |
| Order submission (FAK / marketable limit) | implemented / validated (tiny-live) | `SecureClient.place_market_order` / `place_limit_order`; R7/N7 `--live` only |
| FAK semantics | validated | partial / no-match possible |
| Insert vs trade settlement | validated | MATCHED ≠ CONFIRMED |
| Signer EOA vs funder/proxy | validated | address roles fail-closed |
| Balance / allowance | validated | funder conditional balance authoritative for sellability |
| Selected-market flatness | validated | ack/residual (R7); N7 classifies historical external |
| Acknowledgment policy | validated | sealed identity set in `config/r7/` |
| Residual dust handling | validated | cleanup `NONE` |
| Fee amount certainty | experimental / uncertain | bounds ≠ realized fees |
| Heartbeat | unsupported in safe paths | can cancel all opens if misused |
| Redeem / merge / split / transfer | unsupported / forbidden | no auto cleanup; N7 Scope A |
| Generic continuous live | unsupported | not productized |

## Operation effects

| Path | Typical effects |
|------|-----------------|
| Public books / discovery | Network read |
| Authenticated inventory | Network read (+ credentials) |
| Ack regenerate | Network read + local state write |
| Recon scripts | Network read + report write |
| `n7-preflight` / N7 oneshot without `--live` | Network read + report; **no** venue mutation |
| `n7-live --live` | Venue mutation (+ reports) for one BTC 5m window |
| `r7b-live-once --execute-live` | Venue mutation (+ reports; may residual local-state write) |

## Order insert versus settlement

```text
submit → insert status (e.g. matched)
trade statuses: MATCHED → MINED → CONFIRMED
```

Only **CONFIRMED** (+ sellable balance) creates sellable inventory.

## PTB / K vs SSR display

| Source | Role |
|--------|------|
| Chainlink boundary tick → `sealed_k` | Authoritative PTB/K for Z-Gap when sealed |
| Polymarket SSR page `openPrice` | Independent **comparison** only — never substituted as K |

When `require_ssr_price_match` is `false` (N7 default): no SSR HTTP scrape; incomplete/mismatch SSR must not block evaluation.
When `true`: require MATCH; block on INCOMPLETE/MISMATCH.

## Safety gates

### LIVE_TINY / R7

- Clean git worktree for `--execute-live`
- Local ack artifact matching sealed policy
- Residual registry evaluated
- One lifecycle per process; $5 fee-inclusive BUY cap
- Exit from fresh bids; entry BUY limit not reused

### N7 Z-Gap Scope A

- Operator `--live` is authorization (ceremony removed)
- Preflight `GO` before mutations
- Fee-inclusive debit ≤ $5.00; one entry lineage; no re-entry/reversal
- Historical `RESOLVED_REDEEMABLE` positions acknowledged and untouched
- No redeem / cancel-all / Scope B
- Mutations forced OFF at session end

## R8 snapshot (not evergreen)

Account counts (acknowledged positions, dust records) at acceptance are recorded in [`../../implementation/r8_framework_acceptance.md`](../../implementation/r8_framework_acceptance.md) at commit `fb9d0d8`. Mechanisms are generic; counts are snapshot evidence.

## Related

- Exit floors: [`../../implementation/r7_exit_floor_policy.md`](../../implementation/r7_exit_floor_policy.md)
- R7 success live: [`../../implementation/r7_successful_live_acceptance.md`](../../implementation/r7_successful_live_acceptance.md)
- N7 operator: [`../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md`](../../implementation/z_gap_production_readiness/n7_simplified_operator_live.md)
