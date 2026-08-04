# N7 simplified operator live

**Status:** implemented (ceremony removed; fee-inclusive $5 cap; SSR match optional / disabled)

Checkpoint: `5b85771`.

## Operator authorization

Running this command **is** the authorization:

```bash
python tools/n7_live/run_n7_live_oneshot.py --live
# or: tyrex-pm n7-live --live
```

No phrase, envelope, nonce, or chat confirmation.

Invalidated historical envelopes (never reuse):

- `b3a95919-73a7-43e6-a1d8-3876dd09c2b6`
- `69cff32a-5f84-4b07-902e-dc56b7b93c80`

## Fee-inclusive cap

`worst_price × qty + conservative_entry_fee ≤ $5.00`

Size downward; SKIP if venue minimum cannot fit; never raise the cap.
Exit fees never block inventory-reducing exits.

## PTB readiness (SSR optional)

Config field (boolean): `require_ssr_price_match` in `config/n7_tiny_live.json`.

| Value | Behavior |
|-------|----------|
| `false` (current) | Sealed Chainlink `sealed_k` is PTB-ready; **no SSR scrape/gate** |
| `true` | Restore strict SSR `openPrice` MATCH before aligned eval |

Reports must include: `ptb_authority`, `sealed_k`, `ssr_match_required`,
`ssr_check_status` (`DISABLED`|`REQUIRED`), `ptb_ready`, `evals`.

## Safeguards retained

One BTC 5m market; one entry lineage; no re-entry/reversal/rollover; Scope A;
bounded exit ladder; recon; historical positions untouched; mutations OFF at end.

## Rehearsals (no venue mutation)

```bash
python tools/n7_live/run_n7_live_oneshot.py --fake-rehearsal
tyrex-pm n7-preflight
```

## Current docs

Evergreen operator/config surface: [`../../latest/how_to/run_modes.md`](../../latest/how_to/run_modes.md),
[`../../latest/how_to/configuration.md`](../../latest/how_to/configuration.md),
[`../../latest/concepts/operating_modes.md`](../../latest/concepts/operating_modes.md).
