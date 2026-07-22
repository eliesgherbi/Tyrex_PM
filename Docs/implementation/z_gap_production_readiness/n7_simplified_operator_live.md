# N7 simplified operator live

**Status:** implemented (ceremony removed; fee-inclusive $5 cap)

## Operator authorization

Running this command **is** the authorization:

```bash
python tools/n7_live/run_n7_live_oneshot.py --live
```

No phrase, envelope, nonce, or chat confirmation.

Invalidated historical envelopes (never reuse):

- `b3a95919-73a7-43e6-a1d8-3876dd09c2b6`
- `69cff32a-5f84-4b07-902e-dc56b7b93c80`

## Fee-inclusive cap

`worst_price × qty + conservative_entry_fee ≤ $5.00`

Size downward; SKIP if venue minimum cannot fit; never raise the cap.
Exit fees never block inventory-reducing exits.

## Safeguards retained

One BTC 5m market; one entry lineage; no re-entry/reversal/rollover; Scope A;
bounded exit ladder; recon; historical positions untouched; mutations OFF at end.
