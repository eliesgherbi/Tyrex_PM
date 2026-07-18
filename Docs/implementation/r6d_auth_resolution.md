# R6D — L2 authentication resolution

**Stop before R7. Mutations remain disabled. `.env` unchanged.**

## Historical implementation

| Item | Detail |
|------|--------|
| Factory | `old/src/tyrex_pm/venue/polymarket/clob_env.py` → `try_create_clob_client()` |
| Bridge | `old/src/tyrex_pm/venue/polymarket/clob_bridge.py` |
| Wallet sync | `old/src/tyrex_pm/venue/polymarket/clob_wallet_sync.py` (`get_open_orders`, `get_balance_allowance`) |
| User WS | `old/src/tyrex_pm/ingestion/user_stream.py` |
| Client | Official **`py-clob-client-v2`** (not custom HMAC) |
| Version pin | `old/pyproject.toml` `[live]`: `py-clob-client-v2>=1.0.0` |
| Host | `https://clob.polymarket.com` (V2 production) |
| Evidence | `old/ops/parity_attestation/ATTESTATION_RECORD.md` (2026-04-16) — derive-api-key / balance-allowance / order post-cancel HTTP 200 with `signature_type=1` + funder |

### Historical call graph

```text
tyrex-pm CLI (old/runtime/app.py)
  → _maybe_load_dotenv()
  → try_create_clob_client()
       → PK: TYREX_PRIVATE_KEY > POLYMARKET_PK
       → funder: TYREX_FUNDER > POLYMARKET_FUNDER  (client ctor only)
       → signature_type: TYREX_* > POLYMARKET_* > 0
       → ClobClient(...); set_api_creds(env triple or derive)
  → SDK create_level_2_headers
       → POLY_ADDRESS = signer.address()   ← EOA from private key
  → get_open_orders / get_balance_allowance / …
```

### V1/V2 classification

| Historical behavior | Classification |
|---------------------|----------------|
| `py-clob-client-v2` | Still valid (V2) |
| Host `clob.polymarket.com` | Still valid |
| `POLY_ADDRESS` = signer EOA | Still valid |
| Funder on client ctor | Still valid |
| `get_open_orders` | Still valid (replaces V1 `get_orders`) |
| Custom Tyrex HMAC | N/A — was not used in production |

## Root cause of R6 401

**Exact cause:** R6 `load_l2_credentials` set `POLY_ADDRESS` from `POLYMARKET_ADDRESS` **or `POLYMARKET_FUNDER`**. With `signature_type≠0` and a distinct deposit/proxy funder, the API key (bound to the signer EOA) was sent with the **wrong** address → HTTP **401**.

| Concern | Historical | Pre-R6D R6 | Official V2 | Verdict |
|---------|------------|------------|-------------|---------|
| API host | clob.polymarket.com | same | same | OK |
| Client/SDK | py-clob-client-v2 | custom urllib HMAC | py-clob-client-v2 | Prefer SDK |
| `POLY_ADDRESS` | signer EOA | **funder** | signer EOA | **Regression** |
| Funder | ctor only | misused as POLY_ADDRESS | ctor / custody | Fixed |
| Secret decode | urlsafe b64 | urlsafe b64 | urlsafe b64 | OK |
| Timestamp | Unix seconds | Unix seconds | Unix seconds | OK |
| HMAC message | ts+method+path[+body] | same | same | OK |
| Query in HMAC | not included | not included | not included | OK |

Identity booleans (sanitized, from live preflight):

- `private_key_derives_valid_signer`: true  
- `pre_r6d_would_use_funder_as_poly_address`: true  
- `poly_address_role` (current): **signer**  
- `signer_equals_funder`: false  

## Final implementation choice

**Option A — Wrap official Polymarket V2 client** (`SdkReadonlyTransport`).

Why:

- Matches the proven historical path and official HMAC (`create_level_2_headers`).
- Authenticated reads succeeded immediately after identity fix.
- Tyrex still owns strategy/risk/OMS/reconcile/portfolio/lifecycle/reporting.
- Custom HMAC kept as fallback (`l2_hmac.py`) aligned to official vectors; not the primary production path.

Optional dep: `tyrex-pm[live]` → `py-clob-client-v2`, `eth-account`.

## Operational validation

| Check | Result |
|-------|--------|
| Authenticated REST | `get_open_orders`, `get_trades`, `get_balance`, `get_positions` all **ok** |
| Transport | `official_py_clob_client_v2_readonly` |
| User stream | connected, authenticated, pong, disconnect/reconnect; idle (0 events); **no order created** |
| Reconciliation | venue evidence retrieved; `POSITION_MISMATCH` vs empty local (observation-only); `unreachable_account=false` |
| Readiness reasons | **`MUTATIONS_DISABLED` only** (+ R7 auth absent) |
| Mutations | none (`credential_create_or_derive_called=false`, heartbeat not called) |
| `.env` SHA256 | `27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772` unchanged |
| Tests | **198 passed** |

Artifact: `var/reporting/r6/live_preflight_r6d.json` (gitignored).

## Remaining for R7 (not started)

1. Explicit tiny-live authorization.  
2. Heartbeat supervisor separately approved (still unresolved — do not call).  
3. Hydrate local portfolio before trading so `POSITION_MISMATCH` does not block entry.  
4. Enable `mutations_enabled` only under operator gate.

**Stop before R7.**
