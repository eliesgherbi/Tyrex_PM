# R7 successful live acceptance — run `55fd9a76`

**Status:** R7 live validation **complete**. This is the successful **third** operator-run lifecycle.  
**No further R7 live run is required.** Any future live test needs a new explicitly scoped phase.

Agent never executed `--execute-live`. Operator ran the command; artifacts preserved under `var/reporting/r7d2_second_live/` (local, gitignored).

## Identity

| Field | Value |
|-------|-------|
| Run ID | `55fd9a76-743b-4fb8-835d-adcdbf0f517a` |
| Branch | `rest_project` |
| Commit at execution | `3df3e210841c9cb0e468faac85324c70ffae1357` (`3df3e21`) |
| Worktree | clean |
| Strategy | `ReferenceMomentumStrategy` (`reference-momentum`) |
| Market family | `btc_updown_5m` |
| Market | `btc-updown-5m-1784318400` (Bitcoin Up/Down Jul 17, 4:00–4:05PM ET) |
| Token suffix | `75720912` (Up / YES) |
| Condition suffix | `6d170cd7` |
| Mode | `EXECUTE_LIVE` |
| Mutations attempted | **2** (BUY submit, SELL submit) — no redeem/merge/transfer/approval |

## Terminal classification (R8-corrected)

| Concept | Value |
|---------|-------|
| Lifecycle completed | **yes** (`lifecycle_completed=true`) |
| Inventory terminal | **`FLAT_WITH_DUST`** |
| Residual quantity | `0.000587` (< min tradable `0.01`) |
| Open orders (selected token) | **0** |

Historical report/facts from the run printed `terminal=FLAT` while `final.realized_result=FLAT_WITH_DUST`. That is inconsistent with the state model. R8 runtime now emits `TerminalOutcome.FLAT_WITH_DUST` whenever residual dust is positive; exact zero remains `FLAT`.

## Entry (BUY)

| Field | Value |
|-------|-------|
| Order ID | `0xde990e41f647c7f2041b595e04b2d34b7c116e1109f2cd53f5dccdde51d46a99` |
| Trade ID | `ac2c4015-044e-4fbd-9617-67975be5c183` |
| Limit | `0.51` |
| Order type | FAK |
| Planned max shares (not inventory) | `9.47` |
| Buy notional | `4.83` |
| Estimated max entry fee | `0.16567` |
| Max collateral (fee-inclusive) | `4.99567` (≤ $5.00) |
| Venue size (CONFIRMED) | `9.470587` |
| Venue price | `0.51` |
| Venue `fee_rate_bps` | `0` (amount not separately returned) |
| Transaction hash | `0x4795a335786b36be76837a1eea0295d7c05ec3e477565621bdf20f4c5950c097` |
| Match time (unix) | `1784318154` |
| Settlement ladder | `MATCHED` → `MINED` → **`CONFIRMED`** (poll 8; inventory only after CONFIRMED) |

## Exit planning

| Field | Value |
|-------|-------|
| Urgency | `NORMAL` |
| Status | `PLANNED` |
| Best bid | `0.50` |
| Worst accepted / limit | `0.50` |
| Expected VWAP | `0.50` |
| Quantity | `9.47` (qty-step quantized) |
| Book fingerprint | `0c830ec2fbf3f0a6` |
| Book age | `168` ms (≤ 2000 ms) |
| Floor applied | `0.01` |
| Entry BUY limit reused | **false** (`entry_buy_limit_not_used=true`) |

## Exit (SELL)

| Field | Value |
|-------|-------|
| Order ID | `0x336afebee21bcdbfb3eba22842a5acccc99d97b22e5eb279d446fb7bb5a7cc70` |
| Trade ID | `249274ae-e345-4791-985e-2a14b66c27f9` |
| Limit | `0.50` |
| Size | `9.47` |
| Status | **CONFIRMED** |
| Transaction hash | `0xadf1a9e064f1bd593efc6676d8949b50dc4d7936fa20a10a562a2f7dd951f541` |
| Match time (unix) | `1784318167` |
| Manual exit required | **no** |

## Economics (safety evidence — not profitability proof)

| Component | Amount |
|-----------|--------|
| BUY notional | `4.83` |
| SELL proceeds (`9.47 × 0.50`) | `4.735` |
| Estimated entry fee (pre-submit bound) | `0.16567` |
| Gross price P&L (ex-fee) | `4.735 − 4.83 = −0.095` |
| Approx. fee-inclusive P&L | `≈ −0.26` |
| Final conditional balance | `0.000587` (non-tradable dust) |

This run proves the guarded lifecycle path works end-to-end. It does **not** prove strategy edge or Z-Gap readiness.

## Acknowledgment / residual registry

| Gate | Result |
|------|--------|
| Sealed ack set | exactly **4** resolved positions; gate ok; untouched |
| Ack identities targeted | **none** |
| Residual registry after run | **3** distinct `FLAT_WITH_DUST` records (`36466979`, `28780279`, `75720912`) |
| Cleanup policy | `NONE` (no auto redeem / on-chain cleanup) |
| Success residual provenance | `r7e_live_buy_0xde990e41_sell_0x336afebe_bid_0.50` |

## Artifact paths (local)

- `var/reporting/r7d2_second_live/report_55fd9a76-743b-4fb8-835d-adcdbf0f517a.json`
- `var/reporting/r7d2_second_live/facts_55fd9a76-743b-4fb8-835d-adcdbf0f517a.jsonl`
- `var/state/r7/lifecycle_residuals.json`
- `var/state/r7/position_acknowledgment.json`
- Read-only recon: `python scripts/r8_readonly_recon.py` → `var/reporting/r8/account_recon.json`

## Three-live series (closed)

1. **First live** (`d632b631-…`): settlement race — MATCHED treated as inventory; fixed in R7C.
2. **Second live** (`76e8470a-…`): wrong SELL price (BUY limit reused); fixed in R7E.
3. **Third live** (`55fd9a76-…`): automatic CONFIRMED BUY + bid-side SELL succeeded — **this document**.
