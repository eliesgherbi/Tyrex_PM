# Operator tiny-live runbook

**Supersedes** the historical admission-artifact / reviewed-fingerprint workflow
documented in older Phase 9/10 implementation reports. Those reports remain
historical evidence; this runbook is the current operator instruction.

**Authorization:** the explicit combination of `--mode live` and `--live` is the
operator’s authorization to permit bounded real venue mutations after preflight
and remaining safety checks. No admission JSON, artifact ID, reviewed fingerprint,
or `tiny_live_admitted` flag is required or accepted.

## Canonical tiny-LIVE command

```bash
python -m tyrex_pm.application.cli run \
  --mode live \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/polymarket_live.yaml \
  --runtime config/runtime/live_btc_5m.yaml \
  --run-name tiny_live_validation \
  --live
```

Without `--live`, the same command is **read-only** (authenticated preflight only;
zero venue mutations).

Do **not** pass `--admission-artifact` (removed; stale commands fail as an unknown argument).

## Mutation-disabled continuous SHADOW (market-data validation)

```text
# Authenticated read-only preflight first (nonzero exit if incomplete):
python -m tyrex_pm.application.cli live-preflight

# Continuous SHADOW across ≥2 BTC 5m rollovers (ShadowOMS only; no --live):
python -m tyrex_pm.application.cli run \
  --mode shadow \
  --strategy config/strategies/z_gap.yaml \
  --risk config/risk/tiny_live_5usd.yaml \
  --execution config/execution/shadow_live_5usd.yaml \
  --runtime config/runtime/shadow_btc_5m_continuous.yaml \
  --run-name shadow_validation
```

Do **not** pass `--live`. Do **not** use `polymarket_live.yaml` for this SHADOW path.

## Dress rehearsal (mutations disabled; stop before submit)

```text
.venv\Scripts\python.exe tools\n7_live\run_n7_dress_rehearsal.py
# optional: --attempt-credentials --dotenv .env
```

Proves config resolution, sizing/FAK construction (spy, no send), exit plan,
persistence path, and handoff — then aborts without arming mutations.

## Authorization boundary

- Only the human operator may enable real mutations (`--live` on the operator host).
- Agents/developers must **not** run the live experiment automatically.
- `TYREX_N7_FORBID_LIVE=1`, CI, and pytest continue to block real arming.
- Execution YAML defaults remain `live.enabled: false` / `live.mutations_enabled: false`.
- Internal automatic config fingerprints may appear in reports; operators do not
  create, review, or supply them to arm.

## Pre-run checklist (any failure → abort before mutation)

1. Credentials, signer, funder, proxy, and network identity pass (`live-preflight` / `n7-preflight`).
2. User stream connected and healthy.
3. Public/reference/market feeds healthy.
4. Selected-market baseline sealed.
5. Open orders zero or explicitly expected/owned.
6. Unexpected selected-market exposure zero.
7. All previous obligations resolved.
8. Reporting critical audit lane healthy.
9. Fee-inclusive entry cap ≤ `$5.00`.
10. Daily notional/loss ≤ `$5.00`.
11. FAK behavior, one-entry lineage, no same-window re-entry/reversal.
12. Exit floor, deadlines, and this handoff procedure visible.

## Expected success sequence

```text
sealed PTB and live Z-Gap decision
→ EnterIntent → risk approval → fresh pre-submit revalidation
→ persisted submission obligation → venue submission
→ match evidence → settlement / sellability
→ inventory-bounded FAK exit → terminal reconciliation → report
```

## Reporting fields

| Field | Meaning |
|---|---|
| `live_requested` | Operator supplied `--live` |
| `operator_live_armed` | Host completed real arming (mutations enabled) |
| `real_venue_mutations` | Actual mutation count |

Do not treat `live_requested=true` as proof that mutations were armed.

## After the run

See `operator_post_run_checklist.md`.
