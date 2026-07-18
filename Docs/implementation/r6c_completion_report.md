# R6C — Operational readiness gate (completion report)

**Stop after R6C. Do not begin R7 without sanitized target-host evidence + explicit tiny-live authorization.**

## 1. Endpoint taxonomy (corrected terminology)

Official authentication guide ([docs.polymarket.com/api-reference/authentication](https://docs.polymarket.com/api-reference/authentication)):

> The Gamma API, Data API, and CLOB read endpoints (orderbook, prices, spreads) require no authentication.

| Category | Host | Path | Method | Auth |
|----------|------|------|--------|------|
| Public market-data | `clob.polymarket.com` | `/time` | GET | No |
| Public market-data | `clob.polymarket.com` | `/book` | GET | No |
| Public market-data | `clob.polymarket.com` | `/price`, `/spread` | GET | No |
| Public market-data | `gamma-api.polymarket.com` | `/markets` | GET | No |
| Public Data API | `data-api.polymarket.com` | `/positions` | GET | No (user address query param; not CLOB L2) |
| Authenticated account | `clob.polymarket.com` | `/data/orders` | GET | L2 |
| Authenticated account | `clob.polymarket.com` | `/data/trades` | GET | L2 |
| Authenticated account | `clob.polymarket.com` | `/balance-allowance` | GET | L2 |
| Authenticated mutating | `clob.polymarket.com` | `/heartbeats` | POST | L2 — **not called in R6C** |
| Authenticated mutating | `clob.polymarket.com` | `/order` | POST/DELETE | L2 — **not called** |

Positions endpoint used: **`GET https://data-api.polymarket.com/positions`** — public Data API (address-scoped query). Does **not** use CLOB L2 headers.

Replace ambiguous “CLOB L2 reads failed” with per-request host/path/method/status/Cloudflare/app-auth-reached evidence in artifacts.

## 2. Connectivity (agent_runtime)

Environment: Cursor agent / local Windows runtime.

### Public diagnostics (`scripts/r6c_connectivity_diag.py`)

| Host | Path | Method | Status | Cloudflare | App auth reached |
|------|------|--------|--------|------------|------------------|
| clob.polymarket.com | (dns) | DNS | ok | — | — |
| clob.polymarket.com | (tls) | TLS | ok (TLSv1.3) | — | — |
| clob.polymarket.com | `/time` | GET | **200** | none | n/a (public) |
| clob.polymarket.com | `/book` | GET | **404** (synthetic token) | none | n/a — origin reached |
| gamma-api.polymarket.com | `/markets` | GET | **200** | none | n/a |
| data-api.polymarket.com | `/markets` | GET | 404 | none | n/a (path may differ) |

**Diagnosis:** Public CLOB market-data is reachable. R6B Cloudflare **403 / error 1010** is **not** reproduced on this re-run for authenticated GETs. No bypass attempted.

### Authenticated / Data API (`tyrex-pm live-preflight`)

| Host | Path | Method | Category | Status | Cloudflare | App auth reached |
|------|------|--------|----------|--------|------------|------------------|
| clob.polymarket.com | `/data/orders` | GET | authenticated_account | **401** | none | **yes** |
| clob.polymarket.com | `/data/trades` | GET | authenticated_account | **401** | none | **yes** |
| clob.polymarket.com | `/balance-allowance` | GET | authenticated_account | **401** | none | **yes** |
| data-api.polymarket.com | `/positions` | GET | public_data_api | **200** | none | n/a |

Reconciliation: `UNRESOLVED` / `unreachable_account=true` because CLOB L2 account reads failed auth (401), not Cloudflare. Data API positions retrieved (row counts only in artifact). Heartbeat **not called**.

**Remaining:** Fix L2 credential/HMAC acceptance (401) on this host or trading host; then re-run with `--user-stream-s`. Handoff: [scripts/r6c_target_host_handoff.md](../../scripts/r6c_target_host_handoff.md).

## 3. Mutation-impossible preflight

Command:

```bash
tyrex-pm live-preflight --dotenv .env --output var/reporting/r6/live_preflight.json
```

- Binds `PreflightReadClient` / `ReadOnlyAccountTransport` only — **no** `submit_order` / `cancel_order` methods.
- Does not import `PolymarketTransport` mutation surface or `LiveOMS` submit path.
- No `--enable-mutations` CLI flag.
- Heartbeat never called.
- Secrets redacted in artifact.

## 4. User stream

Validated in code path `user_stream_readonly.py`. Bounded observe; disconnect clears readiness; reconnect + reconcile restores when clean. Idle account (no order/trade events) is acceptable. **No order created to force events.**

Run on target host with `--user-stream-s 8`.

## 5. Reconciliation acceptance

Implemented classifications; clean empty ≠ `missing_evidence`/unreachable; unknown externals never mutated; position mismatch / missing evidence block entry; repeated reconcile idempotent.

R6C **pass** on target host requires authenticated evidence retrieved and reconciliation not `unreachable_account`, with readiness clean except `MUTATIONS_DISABLED` + absent R7 auth.

## 6. R5.1 storm (deterministic stress — not live empirical)

Fixture-driven storm in `tests/test_r6c_r51_storm.py` vs R5 baseline:

| Metric | R5 baseline | R5.1 controlled result |
|--------|-------------|------------------------|
| Signals | 2,123 | **2,123** |
| Entry intents | 89 | **2** |
| Exit intents | 488 | **2** |
| Duplicate denials | 489 | **0** |
| Commands | 3 | **4** |
| Final lifecycle | Residual possible | **FLAT** |

Label: **deterministic stress validation** (fixture), not live empirical.

## 7. Host unification

`TradingHost = ObserveHost`; `ShadowHost` thin OMS hooks only. Architecture tests enforce one evaluate/intent/facts/startup path. PAPER / SHADOW_EXECUTION rename deferred (compatibility-safe later). No `LiveHost`.

## 8. Signing dry validation

`signing_dry.py` — CLOB V2 domain `version: "2"`, chainId 137, synthetic vectors, HMAC deterministic, `would_send=false`, real private key never used in logs.

## 9. Heartbeat semantics (unresolved — do not call)

Official sources:

- [Send heartbeat](https://docs.polymarket.com/api-reference/trade/send-heartbeat) — `POST /heartbeats`
- [Create order — Heartbeat](https://docs.polymarket.com/trading/orders/create) — if a valid heartbeat is not received within ~10s (+5s buffer) after heartbeats are used, **all open orders are cancelled**; `heartbeat_id` chaining (empty string first)

**R6C policy:** Heartbeat readiness remains **unresolved**. Merely starting then stopping heartbeats can change cancellation behavior. **Not called** without separate explicit approval.

## 10. Safety checklist

| Check | Status |
|-------|--------|
| Mutation methods unreachable from preflight | Yes (structural) |
| No submit/cancel/approval/on-chain | Yes |
| `.env` unchanged | Required / verified at commit |
| Secrets absent from artifacts | Redaction + tests |
| LIVE_TINY dispatch disabled | Tests |
| No `old/` import / no Nautilus | Tests |

## 11. Remaining unknowns / blockers

1. CLOB L2 authenticated GETs return **401** (app auth reached; not Cloudflare) — credential/HMAC acceptance must be fixed before R6C operational pass.
2. User-stream connect/auth/disconnect/reconcile not yet run with `--user-stream-s` against a working L2 session.
3. Heartbeat session arming semantics if ever started (unresolved — do not call).
4. R7 tiny-live not authorized.

**R6C code/safety gate complete. Operational account observation incomplete until L2 401 cleared and sanitized target evidence reviewed.**

**Stop. Do not begin R7.**
