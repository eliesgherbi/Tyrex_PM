# Runbook — Supervised order lifecycle smoke (milestone v1.02)

**Goal:** On **one** instrument validated under v1.01, prove **LIMIT** submit → venue **ack** → **cancel** (or documented **FILLED** exit) using `examples/order_lifecycle_smoke.py`.

**Parity note:** The smoke uses **py-clob-client** directly (same L2 credential pattern as `scripts/verify_polymarket_auth.py`). A future Nautilus `TradingNode` + `PolymarketExecutionClient` path should reuse the **same token id, tick size, and sizing discipline** documented here.

## Preconditions

1. **v1.00** and **v1.01** approved for your environment (see `Docs/evidence/`).
2. **`POLYMARKET_*` secrets** loaded per `Docs/Runbooks/polymarket_operator_v1_00.md` (never commit secrets).
3. **Instrument row locked:** token id, tick size, and slug match `config/v1_markets.yaml` + `Docs/validation/v1_01_resolution_notes.md` for the smoke market (or your **operational** row after you replace the reference allowlist).
4. **Balances and allowances:** USDC.e and exchange **allowances** adequate for the **authorized max notional** (re-check via your v1.00 / venue tooling).
5. **Commercial gate:** supervisor-signed **max loss** / notional cap for this run (attach to evidence pack when filing §9).

## Venue sizing — share minimum vs BUY notional (important)

The CLOB enforces **more than one** limit on a BUY:

1. **`min_order_size`** (shares / conditional amount) — returned on the order book; the smoke script surfaces this in the dry-run **Plan** as `min_order_size`.
2. **Minimum BUY notional in USDC** — the venue may reject a BUY when the **dollar amount** is too small. Operators have observed errors like:  
   `invalid amount for a marketable BUY order ($0.5), min size: $1`  
   i.e. **estimated notional `price × size` must meet a floor** (commonly **$1** on the BUY path the team hit).

**Dry-run Plan fields (BUY):**

- `estimated_buy_notional_usd` — `price × size` after any auto-adjustments.
- `min_buy_notional_usd_assumption` — floor the script applies (default **1.0**).
- `size_adjusted_for_min_buy_notional` — set if size was raised to satisfy that floor.

**Retry pattern (example):** at `price = 0.10`, `min_order_size = 5` gives **$0.50** notional → reject. Use `TYREX_SMOKE_SIZE=10` (or let the script bump size) so **`price × size ≥ $1`** and supervisor cap still holds.

**Override:** set `TYREX_SMOKE_MIN_BUY_NOTIONAL_USD` if your venue copy documents a different floor; use **`0`** to disable the client-side notional check (venue may still enforce server-side rules).

**SELL** smoke may use different venue rules; if you see a similar validation error on SELL, capture it in evidence and extend this runbook.

## Exact operator checklist (copy for the supervisor)

Complete **in order**; **abort** if any step fails (see *Abort* below).

| Step | Check | Operator initial | Supervisor initial |
|------|--------|------------------|--------------------|
| 1 | Confirm **no other** test orders **in flight** on the same token | | |
| 2 | Record **token id** (full string) from v1.01 table: `________________` | | |
| 3 | Record **max notional authorized** (USDC): `________` · **supervisor name**: `________` | | |
| 4 | Run `python scripts/verify_polymarket_auth.py` → **exit 0** | | |
| 5 | **Dry-run:** `python examples/order_lifecycle_smoke.py --token-id "<TOKEN>"` → review **Plan**: tick, `min_order_size`, **`estimated_buy_notional_usd`** (BUY), any `size_adjusted_*` flags | | |
| 6 | Confirm **price × size** (BUY notional) and **supervisor cap** — not only share count | | |
| 7 | **Live:** `set TYREX_ORDER_SMOKE_CONFIRM=I_UNDERSTAND` (PowerShell: `$env:TYREX_ORDER_SMOKE_CONFIRM='I_UNDERSTAND'`) | | |
| 8 | Run `python examples/order_lifecycle_smoke.py --token-id "<TOKEN>" --execute` | | |
| 9 | Capture **stdout** (redacted) showing **order_id** and **cancel** response | | |
| 10 | Confirm **terminal state** in UI or `get_order`: **CANCELED** (or **FILLED** per incident procedure) | | |
| 11 | If **FILLED**: record fill details, **notional vs cap**, and stop — do **not** run again without new approval | | |

**Environment overrides (optional):** `TYREX_SMOKE_SIZE`, `TYREX_SMOKE_SIDE`, `TYREX_SMOKE_PRICE`, `TYREX_SMOKE_MIN_BUY_NOTIONAL_USD` (default `1`; set `0` to disable client-side BUY notional bump—see *Venue sizing* above).

## Abort conditions

- Unexpected **HTTP 4xx/5xx** from CLOB, signature errors, or **nonce mismatch**.
- Venue validation such as **minimum BUY notional** / “invalid amount for a marketable BUY” — fix **Plan** (increase `TYREX_SMOKE_SIZE` or adjust price), **dry-run again**, do not blindly retry live.
- Order **resting at an unintended price** vs plan, or **duplicate** `order_id` / duplicate script instance.
- **Slippage / fill** when the runbook intended a **far-from-market** LIMIT smoke — escalate before any second action.
- Any doubt about **token id** vs UI (YES/NO token confusion).

## After the run

- Store redacted logs + **authorized max vs actual** table under `Docs/evidence/` (operator convention).
- Complete §9 sign-off in `Milestones_v1_02.md`.

## Incident log template (paste into evidence)

| Field | Value |
|--------|--------|
| Wall time (UTC) | |
| token_id (prefix redacted) | |
| client / script | `examples/order_lifecycle_smoke.py` |
| venue_order_id | |
| States observed | submitted → … → terminal |
| Terminal state | CANCELED / FILLED |
| Authorized max (USDC) | |
| Actual notional (if filled) | |
| Venue validation message (if any) | |
| Notes | |
