# R6C target-host handoff

Use this when the agent/runtime network cannot complete authenticated CLOB reads
(Cloudflare 403 / error 1010) or user-stream validation.

**Do not paste `.env` or credential values.** Return only the sanitized artifact.

## Prerequisites

- Python **>= 3.11**
- Git checkout of this repo on branch `rest_project` (or the R6 commit)
- `.env` already present on the trading machine (never emailed/pasted)

## Install

```bash
cd /path/to/Tyrex_PM
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[dev]"
```

## Environment variable names only

Required for authenticated reads (values must already exist locally):

- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_PASSPHRASE` (or `POLYMARKET_API_PASSPHRASE`)
- `POLYMARKET_FUNDER` (or `POLYMARKET_ADDRESS`)

Optional / not required for R6C preflight:

- `POLYMARKET_PK` / `POLYMARKET_SIGNATURE_TYPE` (signing dry uses synthetic keys in tests)

## 1) Public connectivity

```bash
python scripts/r6c_connectivity_diag.py
```

Expect `GET clob.polymarket.com/time` → 200 and no Cloudflare error on public paths.

## 2) Authenticated read-only preflight

```bash
tyrex-pm live-preflight \
  --dotenv .env \
  --output var/reporting/r6/live_preflight_target.json \
  --user-stream-s 8
```

Safety properties of this command:

- Composition root uses `PreflightReadClient` only (no `submit_order` / `cancel_order` methods).
- No `--enable-mutations` flag exists.
- Does **not** call `POST /heartbeats`.
- Artifact redacts secrets.

## 3) Return for review

Copy **only** `var/reporting/r6/live_preflight_target.json` (and optionally
`var/reporting/r6/connectivity_diag.json`) back for review.

Artifact must include:

- Boolean credential presence (no values)
- Per-endpoint host/path/method/status/public-vs-auth/Cloudflare flags
- Reconciliation counts + clean_empty vs unreachable
- User-stream connected/authenticated/disconnect/reconnect
- Readiness reasons

Must **not** include token IDs, wallet addresses, signatures, secrets, or exact balances.

## Expected pass shape (R6C)

- Public probes OK
- Authenticated GETs reach application auth (not Cloudflare 1010)
- Reconciliation completes (not `unreachable_account`)
- Readiness blocking reasons limited to `MUTATIONS_DISABLED` and absent R7 auth
  (plus `USER_STREAM_UNREADY` only if stream skipped)
- `mutations_attempted=false`, `heartbeat_called=false`

**Do not begin R7** until this sanitized evidence is reviewed and tiny-live is
explicitly authorized.
