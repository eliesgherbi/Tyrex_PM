# Live parity attestation — acceptance record

**Status:** ACCEPTED (Phase 12 closeout)  
**Date (UTC context):** 2026-04-16  
**Gate:** `IMPLEMENTATION_PLAN.md` §12 — *Live parity attestation (binary)*

## Command (representative successful run)

```bash
tyrex-pm live-attest \
  --scenario live_attest \
  --token-id "8501497159083948713316135768103773293754490207922884688769443031624417212426" \
  --size 5 \
  --price 0.01 \
  --side BUY
```

*(Minimum size for that market was 5 shares; smaller size was rejected by venue rules.)*

## Scenario / policy

| Field | Value |
|--------|--------|
| Scenario | `live_attest` (`config/scenarios/live_attest.yaml`) |
| Execution | Live CLOB |
| Signing | `signature_type=1` (Polymarket proxy) + funder set in env |
| Wallet | Designated operator wallet (secrets not recorded here) |

## Venue confirmation (operator logs)

The following **HTTP outcomes** were observed for the accepted run:

| Step | Endpoint / action | Result |
|------|-------------------|--------|
| API creds | `GET .../auth/derive-api-key` | 200 |
| Collateral | `GET .../balance-allowance?...signature_type=1` | 200 |
| Heartbeat | `POST .../v1/heartbeats` | 200 |
| Order book params | `GET .../tick-size`, `neg-risk`, `fee-rate` | 200 |
| **Submit** | `POST .../order` | **200** |
| **Cancel** | `DELETE .../order` | **200** |

**Interpretation:** **Real** limit order posted and **canceled** on Polymarket CLOB; heartbeat accepted; proxy signing path consistent with allowance URL.

## Expected artifact shape (under `var/reporting/runs/<run_id>/`)

When copied into `ops/parity_attestation/runs/<run_id>/`, verification should show:

- **`run_summary.json`:** `"outcome": "ok"`, `"run_kind": "live_attest"`, exit success.
- **`facts.jsonl`:** rows including `fact_type` / correlation for:
  - `live_attest` (phases bootstrap → readiness → complete)
  - `intent` / risk as emitted by the run
  - `oms_submit` with venue response
  - `oms_cancel`
  - `health` (start/stop; optional heartbeat transition rows)
  - `reconcile` if venue refresh wrote during the run

**Note:** This repository copy may omit the raw `var/` tree; the table above is the **primary operator evidence** archived for parity sign-off. Populate `runs/<run_id>/` from local disk if a reproducible file bundle is required in-repo.

## Run ID

**UUID:** Populate from local `var/reporting/runs/` after attest (directory name = `run_id`). Not required for plan checklist once this record and optional folder copy exist.

---

*Record maintained for Phase 12 parity declaration; no private keys or `.env` contents.*
