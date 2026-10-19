# Automatic recognition of UI redemptions

Status: implemented and applied to the live ledger on 2026-10-19.

## How to use it

1. Redeem winning positions in the Polymarket UI. Nothing else is needed.
2. While a continuous hold run is active, its settlement worker runs discovery at startup and then every `settlement.redemption_discovery.interval_s` (default 300 s). The run journal records:
   - `REDEMPTION_DISCOVERED` when a credit is applied;
   - `REDEMPTION_DISCOVERY_UNRESOLVED` for items that couldn't be attributed;
   - `REDEMPTION_DISCOVERY_ERROR` on failures.
3. With no run active, run discovery from the CLI:

   ```
   tyrex-pm settlement --config <hold run yaml> discover-redemptions --connect-chain          # dry run
   tyrex-pm settlement --config <hold run yaml> discover-redemptions --connect-chain --apply  # record
   ```

   The command refuses with exit code 3 while a runner holds the state lock. That runner discovers redemptions itself.
4. `confirm-redemption --episode … --tx-hash …` still works. It uses the same proof rules and the same guard against crediting twice.

To turn discovery off, set `settlement.redemption_discovery.enabled: false`. The other settings are `interval_s`, `max_blocks_per_pass` and `max_log_calls_per_pass`.

## What counts as proof

Proof comes only from the receipt of a successful transaction at or below the finalized block. A balance increase, a missing token or a `redeemable` flag is never evidence. Two routes are recognised, and each needs every one of its facts.

### `ctf_direct`

A Conditional Tokens `PayoutRedemption` log with:

- redeemer = our wallet;
- collateral = USDC.e;
- our condition;
- an index set containing our outcome.

### `polymarket_adapter_pusd`

This is what the Polymarket UI does now. It was established from our own redemption: tx `0x85983a7f…`, block 95654969, an ERC-4337 bundle that redeemed 22 users of the same market. Within our wallet's operation in that bundle:

1. CTF `TransferSingle`/`TransferBatch` moves our position tokens from our wallet to the adapter `0xa1200000…00af` (operator = adapter).
2. CTF `PayoutRedemption` has redeemer = adapter, collateral = USDC.e, our condition, and payout X.
3. Polymarket USD (pUSD, `0xc011a7e1…`, 6 decimals) transfers X to our wallet.
4. The adapter's per-user event names our wallet, our condition, and X.

The credit is X in **pUSD**, not USDC.e. Its asset is recorded in the evidence. The adapter's event layout was inferred from this receipt (address and topic are pinned). If Polymarket changes the route, candidates are rejected with a reason and stay visible; they are not guessed.

## Attribution and accounting

A credit is applied only through `ResolutionManager.apply_redemption`, using the existing `CASH_CONFIRMED` and `EPISODE_COMPLETED` transitions, and only when all of these hold:

- every unsettled ledger episode holding the redeemed position is `REDEMPTION_PENDING_OPERATOR`;
- on the adapter route, the surrendered shares equal the ledger's shares;
- the episodes' receivables add up to the credit (2e-6 tolerance per episode).

When one transaction redeems several episodes of the same position, each episode is credited its own receivable.

Anything else stays an explicit unresolved item with a reason, and is retried on the next pass. The reasons are:

- `HOLDER_NOT_YET_AWAITING_REDEMPTION` (payout not yet proven);
- `SURRENDERED_SHARES_DIFFER_FROM_LEDGER`;
- `CREDIT_AMOUNT_MISMATCH`;
- `MULTIPLE_REDEMPTIONS_COVER_SAME_EPISODES`;
- `NO_UNSETTLED_LEDGER_EPISODE`.

### Crediting at most once

A redemption log is credited at most once, whether it comes from polling, a restart or manual confirmation. Ledger evidence stores the tx hash and log index. A legacy manual record without a log index blocks the whole transaction.

Loss allowances are never reset: realized losses are unchanged by a credit.

## Discovery mechanics

- **Candidate source.** Candidates are collateral transfers (USDC.e or pUSD) to the wallet, found with Alchemy's transfer index (`alchemy_getAssetTransfers`). The configured endpoint is Alchemy's free tier, which caps `eth_getLogs` at 10 blocks. When the transfer index is unavailable, `eth_getLogs` is used with adaptive chunks and at most `max_log_calls_per_pass` calls per pass. A candidate is only a pointer; the proof is the receipt.
- **Scan range.** Each pass scans from a persisted cursor up to the finalized block, at most `max_blocks_per_pass` blocks. The first start block is found by bisecting block timestamps around the earliest unsettled episode's fill, minus a 300-block margin.
- **Stored matches.** Proven matches are stored, so a redemption made before the payout was proven is applied once it is. This case is tested.
- **Errors.** Back-off doubles from 60 s up to 1 h. `--force` ignores it.
- **State and audit trail.** State is in `<ledger dir>/redemption_discovery.json`. Every decision (cursor, proven match, rejection, application, unresolved item, error) is appended to `redemption_discovery_audit.jsonl`.
- **Ownership.** Inside a run, discovery runs in the settlement worker, which owns the ledger and holds the state lock. It never runs inside strategy evaluation or order submission. The CLI takes the same lock.

## Applied to the live ledger (2026-10-19)

The ledger is `var/runtime_state/settlement/settlement_value_hold_candidates.jsonl`. A backup was taken before applying: `backups/settlement_value_hold_candidates.before_discovery_20261019.jsonl`.

The dry run scanned blocks 95,654,507 to 95,808,342. It found one candidate and one proven match, with nothing unresolved:

| Item | Value |
|---|---|
| Window | `btc-updown-5m-1792187400` |
| Route | `polymarket_adapter_pusd` |
| Transaction | tx `0x85983a7f…f45a`, block 95,654,969, log 1714 |
| Shares surrendered | 10.25532 of token `…4001` |
| Credited | 10.25532 pUSD |
| Allocation | the single pending episode |

`--apply` appended two records: `CASH_CONFIRMED` (seq 52, full evidence) and `EPISODE_COMPLETED` (seq 53). A second apply pass appended nothing.

| Total | Before | After |
|---|---|---|
| Redeemed cash | 0 | 10.25532 |
| Winning receivable | 10.25532 (1 item) | 0 |
| Realized P&L | −23.77000046 | −18.33468086 |
| Realized losses (loss allowance) | 23.77000046 | 23.77000046 (unchanged) |
| Worklist | 1 item | empty |

## Tests

`tests/unit/execution/test_redemption_discovery.py` runs against the real receipt, trimmed to 41 logs, as a fixture. It covers:

- proof for our wallet only, while other users in the same bundle keep their own matches;
- each required fact, by tampering with the mint amount, the token sender, and a failed status;
- the verifier used by `confirm-redemption`;
- dry run, apply, idempotent re-polling, restart, and the manual-confirm guard;
- a redemption seen before payout proof;
- allocation across two episodes;
- the shares-mismatch case;
- RPC back-off and the `eth_getLogs` fallback;
- the worker running discovery at startup;
- config validation.

## Limitations

- The adapter route is pinned to the observed adapter, event topic and pUSD addresses. A different Polymarket redemption contract is rejected, not guessed, so the run journal will show unresolved or rejected candidates and the code will need a new route.
- Collateral reads elsewhere (account cash) are not part of this change. The credit is recorded as pUSD.
- The existing `reconcile` and `confirm-redemption` CLI commands still don't take the state lock (unchanged). `discover-redemptions` does.
