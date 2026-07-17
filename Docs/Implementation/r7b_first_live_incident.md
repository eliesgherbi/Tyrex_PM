# R7B first live incident — analysis (R7C)

**Run ID:** `d632b631-166f-4e35-8db8-fe69a3f86795`  
**Commit at incident:** `4555dfdb93166d1ca86f835e8974c3e55c6babef`  
**R7B CLI checkpoint (pre-R7C):** `d506ea661a6478f7359fc623e69a0054b5dcdc65`  
**Mutations during R7C analysis:** none (read-only recon only)

## Invariants (post-incident)

```text
Order insert status != trade settlement
MATCHED != CONFIRMED
Planned quantity != acquired quantity
Confirmed fill != immediately sellable balance
```

## Timeline (UTC)

| t | Event | Evidence |
|---|--------|----------|
| 15:42:47.036 | Market bound / pre-submit | facts: `btc-updown-5m-1784303100`, planned qty **9.47**, limit 0.51, max collateral 4.99567 |
| 15:42:47.782 | BUY insert response | `ok=true`, `status=matched`, order `0x68efa63a…43e71db` |
| 15:42:47.787 | **Bug:** `entry_filled` qty=9.47 | ~5 ms after insert; **no** trade/balance confirmation |
| 15:42:48.015 | Auto FAK SELL | Rejected: `balance: 0, order amount: 9470000` |
| 15:42:48.016 | Terminal `MANUAL_INTERVENTION` | exit code 3 |
| Later (UI) | Manual flatten | User sold ~9.5 shares in Polymarket UI |

## Quantity distinctions

| Concept | Value | Notes |
|---------|-------|-------|
| Planned / max estimated shares | 9.47 | Sizing only — not inventory |
| Order insert status | `matched` | Non-terminal for settlement |
| Venue BUY trade (recon) | **9.470587** @ 0.51, status **CONFIRMED** | trade `4698dcfb-…`, tx `…506fe83b44` |
| USDC spent (size×price) | ~**4.82999937** | Under $5 fee-inclusive envelope |
| Reported residual (buggy) | 9.47 | Asserted planned qty as residual |
| Manually observed UI position | ~9.5 | After settlement visible |
| Manual SELL trade (recon) | **9.47** @ 0.50, **CONFIRMED** | order `0x4de12437…10ac`, trade `732f6210-…` |
| Final selected-market position | **flat** (Data API) | open orders **zero** |
| Conditional dust | ~0.000587 shares | BUY 9.470587 − SELL 9.47 |

## Root-cause ranking (evidence-based)

1. **MATCHED → settlement / balance visibility delay — high confidence**  
   Auto-SELL ~230 ms after insert `matched` saw `balance:0`; later CONFIRMED BUY + UI position prove inventory appeared after delay. Runtime treated insert matched as fill.

2. **CLOB conditional balance cache delay — medium**  
   Same evidence; sell path checks conditional balance/allowance.

3. **Signer ≠ funder/proxy — medium as contributing factor for address discipline, low as sole sell failure cause**  
   Recon: signer `0xbee8…b66c`, funder/proxy `0x6436…2bb0`, `signature_type=1`. BUY/SELL maker fingerprints match **funder**, not signer. Error text was `balance:0` (not wrong-account auth failure). Positions correctly queried via funder.

4. **Conditional allowance — low**  
   Error wording was balance, not allowance.

5. **Partial/failed settlement — low**  
   BUY trade later CONFIRMED near full planned size; no FAILED trade observed in recon.

**Uncertain:** Historical MATCHED→MINED→CONFIRMED intermediate statuses were not captured live (only terminal CONFIRMED visible in later REST). Exact milliseconds until balance became non-zero are unknown.

## Address roles (fingerprints only)

| Role | Fingerprint |
|------|-------------|
| Signer EOA | `0xbee8…b66c` |
| Funder / proxy | `0x6436…2bb0` |
| Positions query | funder |
| BUY maker | funder |
| Manual SELL maker | funder |

## Acknowledgment positions

Four acknowledged resolved positions were **not** targeted by the lifecycle BUY/SELL.  
Read-only recon’s ack validator may report set-change noise when Data API row shape is incomplete; treat as secondary. Manual confirmation: ack tokens were not the selected-market token.

## Corrected lifecycle (R7C)

```text
ENTRY_SUBMITTING
  → ENTRY_MATCHED          # insert status only
  → ENTRY_SETTLING         # wait trades + conditional balance
  → ENTRY_CONFIRMED        # MINED/CONFIRMED + sellable balance
  → ACTIVE
  → EXIT_SUBMITTING        # qty = min(confirmed, balance)
  → EXIT_MATCHED → EXIT_SETTLING
  → FLAT
  or MANUAL_INTERVENTION / FLAT_EXTERNAL_ACTION
```

## Artifacts

- Report: `var/reporting/r7b/report_d632b631-166f-4e35-8db8-fe69a3f86795.json`
- Facts: `var/reporting/r7b/facts_d632b631-166f-4e35-8db8-fe69a3f86795.jsonl`
- Budget: `var/reporting/r7b/budget_d632b631-166f-4e35-8db8-fe69a3f86795.json`
- Fixture: `tests/fixtures/r7b_incident_d632b631_facts.jsonl`
- Read-only recon: `var/reporting/r7c/incident_recon.json` (gitignored)
