# Operator runbook — Milestone v1.00 (Polymarket auth)

**Milestone:** [Milestones_v1_00.md](../Implementation/Project%20starter/Milestones_v1_00.md)  
**References:** [Polymarket authentication](https://docs.polymarket.com/developers/CLOB/authentication) · [Nautilus Polymarket env vars](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/polymarket.md)

## Who signs vs who funds

| `signature_type` | Wallet model | **Signing key** | **Funder (balance / USDC.e)** |
|------------------|--------------|-----------------|------------------------------|
| `0` | EOA | Your EOA private key | Same EOA address |
| `1` | Polymarket Magic / email proxy | Exported PK from Polymarket settings | **Proxy** address shown on Polymarket profile |
| `2` | Browser wallet / Gnosis Safe proxy | Your wallet PK | **Proxy** address shown on Polymarket profile |

If you have a normal Polymarket.com account, funds are usually in a **proxy**; use type `1` or `2` and set **`POLYMARKET_FUNDER`** to that **proxy** address (public), not a secret.

## Environment variables (aligned with Nautilus naming)

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYMARKET_PK` | Yes | Hex private key (**secret**). Same role as docs’ `PRIVATE_KEY`. |
| `POLYMARKET_FUNDER` | Yes (types 1,2); EOA for type 0 | Polygon address holding USDC.e (**public**). |
| `POLYMARKET_SIGNATURE_TYPE` | No | `0`, `1`, or `2`. Default: `0`. |
| `POLYMARKET_API_KEY` | No | If omitted, script derives via L1. |
| `POLYMARKET_API_SECRET` | No | L2 secret (**do not log**). |
| `POLYMARKET_PASSPHRASE` | No | L2 passphrase (**do not log**). |

Optional: `POLYMARKET_CLOB_HOST` (default `https://clob.polymarket.com`), `POLYMARKET_CHAIN_ID` (default `137`).

**Optional:** `TYREX_PM_DOTENV` — absolute or relative path to an env file to load instead of `<repo_root>/.env`.

## `.env` file (local)

- Place **`.env`** in the **repository root** (next to `pyproject.toml`), **or** point `TYREX_PM_DOTENV` at another file.
- Use **`.env.example`** as a template (copy to `.env`); never commit real secrets.
- **Precedence:** for each variable name, if it is **already set** in the process environment before the script runs, that value is kept. Values from `.env` only fill in **missing** keys (`python-dotenv` with `override=False`).

## Procedure

1. Install project deps: `pip install -e .` from repo root (installs `python-dotenv`).
2. Provide variables **either** via **`.env`** at repo root **or** shell export (or mix: shell wins on conflicts).
3. Run from repo root: `python scripts/verify_polymarket_auth.py`
4. Expect first line either `Config: merged .env …` or `Config: no .env loaded …`, then `OK: L2-authenticated read succeeded` and a **redacted** summary (no API secret / passphrase / PK printed).

**L2 triple rule:** if you set any of `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_PASSPHRASE`, you must set **all three**; otherwise omit all three and the script derives L2 via L1 using `POLYMARKET_PK`.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `INVALID_SIGNATURE` | Wrong `POLYMARKET_PK` or wrong `POLYMARKET_SIGNATURE_TYPE` |
| Invalid funder | `POLYMARKET_FUNDER` not the proxy shown on [Polymarket settings](https://polymarket.com/settings) |
| `NONCE_ALREADY_USED` | API key creation nonce conflict — use **derive** path per Polymarket auth doc |
| `balance: 0` after success | Often means **no collateral** visible for this `signature_type` / **funder** in the CLOB view — confirm **USDC.e** on the **funder** address on Polygon and allowances (see Nautilus `set_allowances.py` before real trades). |
| `allowance` vs `allowances` | The CLOB may return **`allowances`** (object/array) instead of a single **`allowance`** string; the verification script handles both and prints a short summary. |

## Evidence for milestone review (no secrets)

Attach to PR or `Docs/evidence/`:

- Timestamp, script exit code `0`
- Log lines showing `L2 method: get_balance_allowance` (or fallback) and **high-level** result keys only (`balance`, `allowance` as presence — truncate values if policy requires)
- Checklist: `signature_type=…`, `funder=0x…` (**public** only)
