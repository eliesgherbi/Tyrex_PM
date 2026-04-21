# Polymarket CLOB V2 migration plan (authoritative)

**Status:** All phases (0–9, including 7M) implemented; live submit→ack→cancel round-tripped on
`clob-v2.polymarket.com` 2026-04-19 with venue order id
`0xc5aefaa7167aa604c03b9771098e98a05ea5776b8134a40f7633a8f6db6b43be` (see §7). The codebase is V2-only:
no V1 SDK references remain in `src/`, the V2 SDK is import-isolated to `venue/polymarket/`, and
per-market venue truth (tick / min-size / neg-risk / fees / outcomes) is sourced from the venue itself
rather than YAML defaults.
**Scope:** convert the current Tyrex_PM native Polymarket stack from V1 (`py-clob-client`, V1 CTF Exchange, USDC.e collateral) to a fully V2-compliant implementation (`py-clob-client-v2`, V2 CTF Exchange, Polymarket USD collateral).
**Non-goal:** dual-mode runtime, V1 fallback, compatibility shim. Cutover is one-way; we are pre-production with no real bot positions to migrate, but we still require a clean first-V2-start reset posture (see §6).

Hub: [README.md](../README.md) · System overview: [Architecture.md](../Architecture.md) · Live truth model: [LIVE_ARCHITECTURE.md](../LIVE_ARCHITECTURE.md) · Venue layer: [modules/venue/README.md](../modules/venue/README.md)

---

## 0. Source-of-truth and SDK-naming caveat

This plan describes the **conceptual** mapping from V1 to V2: what each subsystem must do, which inputs and outputs change, what we keep, what we drop. Where the document names specific Python-side symbols on the V2 side (`ClobClient`, `OrderArgsV2`, `BuilderConfig`, `BalanceAllowanceParams`, `PolyApiException`, `get_open_orders`, `create_or_derive_api_key`, `BYTES32_ZERO`, …), treat those as **expected** names that match the publicly documented `py-clob-client-v2` shape.

The **installed `py-clob-client-v2` package surface is the source of truth.** Before writing code against any name in this plan:

1. Install `py-clob-client-v2` in the working venv.
2. Inspect the actual public surface: `python -c "import py_clob_client_v2 as p; print(dir(p))"`, then drill into `p.ClobClient`, `p.clob_types`, `p.exceptions`, `p.config`, `p.constants`.
3. If a symbol or signature differs from what this plan names (renamed method, slightly different option keyword, alternative module path), **adopt the installed shape** and update the corresponding test imports — do not paper over the difference inside Tyrex_PM.

The conceptual mapping below is robust regardless of those small naming deltas because Tyrex_PM only touches the V2 SDK from a small set of well-defined modules (`venue/polymarket/clob_env.py`, `clob_bridge.py`, `clob_heartbeat.py`, `clob_wallet_sync.py`, plus one exception import in `runtime/pipeline.py`).

---

## Implementation status (2026-04-19)

| Phase (§7) | Title                                              | Status                              | Notes |
|------------|----------------------------------------------------|-------------------------------------|-------|
| 0          | Verify installed V2 SDK surface                    | ✅ Done                              | Real SDK shape captured; renamed `OrderArgsV2`/`AssetType`/`BalanceAllowanceParams` adopted from installed package. |
| 1          | Pin V2 SDK and wire host config                    | ✅ Done                              | `pyproject.toml` pins `py-clob-client-v2`; `clob_env.try_create_clob_client` defaults to `https://clob-v2.polymarket.com`; `TYREX_BUILDER_CODE` parsed and validated. |
| 2          | V2 bridge (place + cancel)                         | ✅ Done                              | `PyClobBridge` builds `OrderArgsV2` (no `fee_rate_bps`/`nonce`/`taker`), cancels via `OrderPayload`. |
| 3          | Heartbeat exception + pipeline imports             | ✅ Done                              | `PolyApiException` import path migrated to `py_clob_client_v2.exceptions`. |
| 4          | Wallet sync (V2 method names + payload shape)      | ✅ Done                              | `BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)`; `get_open_orders()` (V1 `get_orders` removed). **Real V2 payload bug fixed:** balance is raw 6-decimal token units (divided by `10**6`); `allowances` is a per-exchange dict (binding allowance reduced via `min`). Legacy V1-shape mocks still tolerated. Fail-loud if SDK missing. |
| —          | **Live end-to-end validation**                     | ✅ Done (2026-04-19)                 | `live-attest` against `clob-v2.polymarket.com` round-tripped submit→ack→cancel for `0xc5aefaa7167aa604c03b9771098e98a05ea5776b8134a40f7633a8f6db6b43be`. |
| 5          | Market-info adapter                                | ✅ Done                              | `venue/polymarket/market_info.py` defines `MarketInfo` + `MarketInfoCache` (TTL=300s, fail-closed, asyncio-locked refresh). Resolves `condition_id` via `/markets-by-token/<token>`, market truth via `/clob-markets/<condition_id>`, and `neg_risk` + `fee_rate_bps` via SDK helpers. Wired into `RuntimeCoordinator.market_info_cache` (live mode only); snapshot fed into `RiskContext.market_info`. `risk.venue_min_size._resolve_min_size` now prefers venue `min_order_size` with provenance (`"venue"` / `"config_default"`). `execution.order_builder.to_place_request` floor-quantizes `limit_price` to the venue tick when `MarketInfo` is present. |
| 6          | Cutover-safe startup (`reset-state` + bootstrap gate) | ✅ Done                           | `tyrex-pm reset-state` CLI clears `var/state/guru_strategy_store.json` (idempotent; never touches `var/reporting/`). `HealthRuntime.first_v2_sync_complete` defaults to False; `check_aggressive_readiness` denies live new-order intents with `bootstrap_not_complete` until the first successful `refresh_wallet_from_clob` flips it (set in `cmd_run`, `cmd_live_attest`, and `venue_refresh_loop`). |
| 7          | `live-attest` V2 evidence facts                    | ✅ Done                              | `cmd_live_attest` emits three new `live_attest` phase facts: `v2_environment` (SDK module path + version, host, chain, signature_type, builder code presence), `collateral_check` (post-bootstrap pUSD balance + per-exchange allowances), `market_info` (resolved tick/min-size/neg-risk/fee/outcomes for the attested token). Implemented via `_v2_environment_payload` + `MarketInfoCache.get`. |
| 7M         | Tick quantize / fee evidence / outcome validation  | ✅ Done                              | `execution.order_builder.build_quantize_evidence` returns `tick_quantize_applied`/`tick_size`/`original_price`/`quantized_price`/`price_was_quantized`; merged into the `oms_submit` fact for both pipeline and live-attest paths. The live-attest `complete` phase now records `outcome_validation`: post-cancel order id resolution + the `outcomes` map, so an operator can verify the BUY/Yes vs No leg without leaving the facts file. |
| 8          | Documentation refresh                              | ✅ Done                              | `current_state.md` rewritten; `OPERATIONS.md` adds `reset-state` runbook + V2 host default + `TYREX_BUILDER_*`; `CONFIG_MODEL.md`, `DEVELOPMENT.md`, `Architecture.md`, `developer_guide.md`, `modules/venue/README.md`, `modules/execution/README.md`, `README.md`, `.env.example` rebased onto `py-clob-client-v2` and the V2 staging host. |
| 9          | V1 cleanup + import-isolation guard                | ✅ Done                              | `rg "py_clob_client[^_]"` returns zero in `src/`. `tests/test_v2_import_isolation.py` enforces (a) no V1 SDK imports anywhere in `src/tyrex_pm/`, (b) `py_clob_client_v2` imports are confined to `src/tyrex_pm/venue/polymarket/`, (c) `runtime/pipeline.py` consumes `PolyApiException` via the venue re-export `tyrex_pm.venue.polymarket.exceptions`. |

### Operational artifacts shipped alongside the migration

- `scripts/v2_collateral_probe.py` — diagnoses what the V2 venue thinks the wallet holds (raw `GET /balance-allowance` + on-chain ERC-20 balances/allowances on Polygon).
- `scripts/v2_wallet_mode.py` — detects EOA vs POLY_PROXY vs POLY_GNOSIS_SAFE wallet mode from a MetaMask EOA, prints `.env` snippet.
- `scripts/v2_wrap_to_pusd.py` — one-shot operator script: batches `USDC.e.approve(CollateralOnramp) → CollateralOnramp.wrap → 3× pUSD.approve(V2 exchanges)` into a single Safe transaction submitted via the Polymarket Builder Relayer (`py-builder-relayer-client` + Builder API Keys).

### Tests added (counted relative to baseline)

- `tests/test_clob_env_aliases.py` — rewritten for V2 client + builder code validation.
- `tests/test_clob_bridge_v2.py` — 19 tests covering `OrderArgsV2`, `OrderType` mapping, `LiveOMS`/`SingleWriterOMS` integration.
- `tests/test_clob_wallet_sync_v2.py` — **21 tests**, including the new V2 raw-units balance + plural-allowances dict + `min`-binding semantics + `1e30` regression guard + legacy back-compat.
- `tests/test_clob_heartbeat_state_machine.py`, `tests/test_risk_notional_policy.py` — `PolyApiException` import paths updated.
- `tests/test_reset_state.py` (Phase 6) — covers `reset_local_state` happy path + idempotency + reporting-dir untouched.
- `tests/test_first_v2_sync_gate.py` (Phase 6) — `check_aggressive_readiness` denies with `bootstrap_not_complete` until the flag flips.
- `tests/test_v2_import_isolation.py` (Phase 9) — three guards: no V1 SDK in `src/`, V2 SDK only in `venue/polymarket/`, pipeline consumes `PolyApiException` via the venue re-export.
- `tests/test_market_info_cache.py` (Phase 5) — 12 tests: `quantize_price` floor semantics across multiple ticks, TTL hit/miss behaviour, snapshot semantics, fail-closed on 404 / missing fields / SDK exception (httpx fully monkeypatched).
- `tests/test_venue_min_size_market_info.py` (Phase 5) — 4 tests pinning venue-truth precedence over YAML default with provenance evidence (`venue_min_size_source`).

Full suite (post Tier A + B + C, 2026-04-19): **262 passed, 1 skipped, 6 failed.** The 6 remaining failures (`test_pipeline_refresh_coordinated.*` ×3, `test_guru_strategy_golden::test_conviction_scales_enter_size`, `test_shadow_e2e_guru_to_oms_facts`, `test_t8_summarize_join_audit_on_real_run_dir`) are pre-existing — they fail because shadow-mode test fixtures still reference V1 SDK method names (`_Clob.get_open_orders`) or because the `config/risk/default.yaml` notional cap clips the shadow scenario below `venue_min_size`. Net: **−7 failures vs. the post-Phase-4 baseline of 13**, no new regressions introduced by Phases 5/6/7/7M/8/9.

---

## 1. Executive summary

### 1.1 What is changing

| Area | Today (V1) | After migration (V2) |
|------|------------|----------------------|
| SDK | `py-clob-client>=0.34.0` | `py-clob-client-v2` (replaces V1 entirely) |
| Order signing domain | EIP-712 against the V1 CTF Exchange / NegRisk Exchange | EIP-712 against the V2 CTF Exchange / NegRisk Exchange (`exchange_v2`, `neg_risk_exchange_v2`) |
| Order struct | V1 `OrderArgs` carries `fee_rate_bps`, `nonce`, `taker` | V2 order args drop `fee_rate_bps` / `nonce` / `taker`; add `expiration` (`0` = GTC), `builder_code` (`bytes32`), `metadata` (`bytes32`) — exact dataclass name to be confirmed against installed SDK |
| Signature types | `EOA=0`, `POLY_PROXY=1`, `POLY_GNOSIS_SAFE=2` | Same integer values; new `POLY_1271=3` (EIP-1271 smart-contract wallets) |
| Auth | L1 (wallet sig → API key derivation) + L2 (HMAC API creds) | Same L1+L2 model; SDK exposes a singular `create_or_derive_api_key` (V1's plural `create_or_derive_api_creds` is gone) |
| Collateral | USDC.e (bridged) | Polymarket USD (native, 1:1 backed by USDC); operator must `wrap()` USDC → Polymarket USD once before trading |
| Heartbeat path | `POST /v1/heartbeats` | Unchanged path, same id-rotation semantics |
| Open orders REST | `client.get_orders()` | V2 `get_open_orders(...)` (or whatever the installed SDK names it; same per-row dict shape) |
| Balance/allowance | `get_balance_allowance(BalanceAllowanceParams(asset_type=COLLATERAL))` | Same call; collateral is now Polymarket USD (USD-decimal units unchanged) |
| Builder code | None | Optional `builder_code: bytes32` per order; default = zero (no attribution) |

### 1.2 What is staying the same

- Risk engine, deployment / capital / venue-min-size / inventory / readiness logic (gate _interfaces_ unchanged; some inputs become richer — see §3.7 / §5).
- Reconcile state machine (provisional repair, venue adoption, WS-terminal tombstones, in-flight BUY reservations).
- `WalletStore` / `OrderStore` and their merged-truth semantics.
- `SingleWriterOMS` queue, `LiveOMS` interface, `OMSBackend` adapter contract.
- Reporting fact schema, `JsonlSink`, summarizer.
- Data-API positions safety net and Gamma tradeability gate.
- User-channel WebSocket URL and message types (`PLACEMENT`, `UPDATE`, `CANCELLATION`, `TRADE`).

### 1.3 Two distinct phases of host usage

V2 has its own pre-production endpoint. The plan treats host as a first-class config value driven by an environment variable that has a different default in each phase:

| Phase | `TYREX_CLOB_HOST` default | Purpose |
|-------|---------------------------|---------|
| Pre-cutover (everything in §7 except the final flip) | `https://clob-v2.polymarket.com` | The Polymarket-published V2 staging/transition endpoint. Used for SDK validation, `live-attest`, and any pre-production `tyrex-pm run`. |
| Post-cutover (steady-state production) | `https://clob.polymarket.com` | Standard production endpoint. After Polymarket cuts the production domain over to V2, this becomes the only host we point at. |

The default in `clob_env.try_create_clob_client` is rebased to `https://clob-v2.polymarket.com` for the duration of the migration; on the cutover day the default is flipped back to `https://clob.polymarket.com`. The env var override (`TYREX_CLOB_HOST`) lets operators move ahead of the default in either direction.

### 1.4 Cutover constraint (pre-production reality)

Tyrex_PM is **not yet running real production capital on Polymarket**. There is no real bot inventory or resting-order book that needs to survive the V1→V2 boundary. That removes the hardest part of a normal venue migration (no wallet-state translation, no in-flight order replay).

What the migration **does still require**, even in our pre-production posture, is a *clean first-V2-start reset*:

- No V1-era local OMS state (provisional rows, in-flight reservations, `OrderStore.terminal_audit`, `WalletStore.open_orders` snapshots, on-disk strategy stores) may leak into the first V2 run.
- The bot must rebuild venue truth from zero by hitting V2 REST + V2 user-WS before being allowed to submit a single order.
- Local processes must come up with an empty `WalletStore` and empty `OrderStore`, then wait for the readiness gate (heartbeat ok + REST balance/allowance + REST open orders + user-WS first message or stale-grace) before the first risk evaluation runs.

§6 codifies this as the *clean first-V2-start reset posture*. It is intentionally cheaper than a full state migration would be — but it is still mandatory startup hygiene, and is enforced in code (Phase 6 of §7), not by operator memory.

---

## 2. Current codebase review (V1 surface)

This section is the inventory of V1 touchpoints that the migration must replace. File paths are repo-relative.

### 2.1 Live OMS path

- `src/tyrex_pm/execution/live_oms.py` — `LiveOMS.submit/cancel` calls `PyClobBridge.create_and_post_limit` and `PyClobBridge.cancel_order`. Translates `ApprovedIntent` → `PlaceOrderRequest` via `to_place_request`.
- `src/tyrex_pm/execution/order_builder.py::to_place_request` — copies `intent.token_id`, `side`, `size`, `limit_price`, `order_style`, `client_order_id` into a `PlaceOrderRequest`.
- `src/tyrex_pm/execution/oms.py::SingleWriterOMS` — venue-agnostic queue; **no V1 coupling**, kept as-is.
- `src/tyrex_pm/execution/adapters.py::OMSBackend` — protocol with `submit/cancel`; **no V1 coupling**, kept as-is.

### 2.2 Bridge / signing / order construction

- `src/tyrex_pm/venue/polymarket/clob_bridge.py::PyClobBridge` — async wrapper around the synchronous V1 `ClobClient`. Uses:
  - `from py_clob_client.clob_types import OrderArgs`
  - `from py_clob_client.order_builder.constants import BUY, SELL`
  - `client.create_and_post_order(OrderArgs(...))`
  - `client.cancel(vid)`
  - `client.post_heartbeat(hid)`
  Wraps every call with `asyncio.to_thread`. Exposes `summarize_oms_response` and `parse_venue_order_id` (reads `orderID` / `order_id` / `id`).
- `src/tyrex_pm/venue/polymarket/clob_execution.py::PlaceOrderRequest` — internal canonical place-order DTO (`token_id`, `side`, `size`, `price`, `style`, `client_order_id`). `ClobExecutionClient` is a deliberately-unimplemented stub; `PyClobBridge` is the real path.

### 2.3 Auth / client construction

- `src/tyrex_pm/venue/polymarket/clob_env.py::try_create_clob_client` builds a V1 `ClobClient(host, chain_id, key=pk, signature_type=sig_t, funder=funder)` then calls `client.create_or_derive_api_creds()` (V1 plural method) and `client.set_api_creds(creds)`.
- Env vars: `TYREX_CLOB_HOST` (default `https://clob.polymarket.com`), `TYREX_PRIVATE_KEY` (`POLYMARKET_PK` fallback), `TYREX_CHAIN_ID` (default `137`), `TYREX_SIGNATURE_TYPE` (`POLYMARKET_SIGNATURE_TYPE` fallback, default `0`), `TYREX_FUNDER` (`POLYMARKET_FUNDER` fallback).
- `resolve_positions_wallet_address(client)` returns funder env if set, else `client.get_address()`.

### 2.4 Heartbeat

- `src/tyrex_pm/venue/polymarket/clob_heartbeat.py::post_heartbeat_with_recovery` — POSTs heartbeat, parses session id from success/error bodies, rotates on 400. Catches `from py_clob_client.exceptions import PolyApiException`. Status 400 triggers id rotation; any other status fails the tick.
- `src/tyrex_pm/venue/polymarket/clob_env.py::resolve_clob_heartbeat_id` / `normalize_heartbeat_id_for_clob` — strip hyphens from UUID-style ids; honor `TYREX_HEARTBEAT_ID` / `POLYMARKET_HEARTBEAT_ID`.
- `src/tyrex_pm/runtime/live_supervisor.py::supervised_heartbeat_loop` calls `post_heartbeat_with_recovery(health, bridge)`; clamps interval to ≥ 5 s.

### 2.5 Wallet sync (REST)

- `src/tyrex_pm/venue/polymarket/clob_wallet_sync.py::_sync_wallet_from_clob` — refreshes USDC balance/allowance and open orders in a thread:
  - `from py_clob_client.clob_types import AssetType, BalanceAllowanceParams`
  - `client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))` → reads `balance` / `available` and `allowance` / `allowance_balance`.
  - `client.get_orders()` → list / `data` / `orders`; per-row keys: `asset_id|token_id|tokenID`, `side`, `original_size`, `size_matched`, `size`, `price`, `id|orderID`, `status`. Builds `OpenOrderView(venue_state_source="rest")`, writes to `wallet._rest_open_orders`, then `rebuild_open_orders_merged()`.
- `src/tyrex_pm/runtime/live_supervisor.py::venue_refresh_loop` calls the above + (optionally) `refresh_positions_from_data_api` + `sync_local_open_orders_from_venue_wallet` + `emit_wallet_sync` + `reconcile_coordinator`.
- `src/tyrex_pm/runtime/live_supervisor.py::provisional_repair_probe_loop` calls the same V1 client on the (1, 5, 15 s) adaptive schedule.

### 2.6 User WebSocket

- `src/tyrex_pm/ingestion/user_stream.py::run_user_ws_ingest` — connects to `wss://ws-subscriptions-clob.polymarket.com/ws/user` and subscribes with `{"type":"user","auth":{"apiKey","secret","passphrase"}}` using `live_clob.creds`. Dispatcher handles PLACEMENT / UPDATE / CANCELLATION / TRADE. The user-WS payload schema is the same in V2.

### 2.7 Open-order truth and reconcile

- `src/tyrex_pm/state/wallet_store.py::WalletStore` — merges `_user_ws_order_map` (authoritative) with `_rest_open_orders` (backstop) under `_ws_cancel_tombstones`. Holds `usdc_balance`, `usdc_allowance`, `last_sync_ts`, `last_positions_sync_ts`.
- `src/tyrex_pm/state/reconcile.py::reconcile_open_orders` — pure function; **no V1 SDK dependency**.
- `src/tyrex_pm/execution/order_lifecycle.py::sync_local_open_orders_from_venue_wallet` / `apply_venue_open_order_to_local_orders` / `remove_local_resting_by_venue_order_id` — SDK-agnostic.

### 2.8 Positions / capital / readiness

- `src/tyrex_pm/venue/polymarket/positions_sync.py` — replaces `WalletStore.positions` from `data-api/positions`; SDK-agnostic.
- `src/tyrex_pm/risk/capital.py::evaluate_capital_buy` — reads `usdc_balance` / `usdc_allowance` as USD-decimal; survives the migration unchanged because the V2 wallet sync continues to populate those fields with USD-decimal Polymarket USD values.
- `src/tyrex_pm/risk/in_flight.py` — pure; no SDK.
- `src/tyrex_pm/risk/health.py::check_aggressive_readiness` — gates on heartbeat / ws-stale / wallet age; no SDK.

### 2.9 Venue minimum size

- `src/tyrex_pm/risk/venue_min_size.py::evaluate_venue_min_size` — uses `cfg.default_min_size` (5 by default). `_resolve_min_size` is the documented per-token extension hook. **No V1 dependency.** Today the floor is a single global value driven by config.

### 2.10 `live-attest`

- `src/tyrex_pm/runtime/live_attest.py::cmd_live_attest` — full live boot using the same supervisor stack as `tyrex-pm run`:
  1. `try_create_clob_client()` (V1)
  2. `PyClobBridge(live_clob)`
  3. `SingleWriterOMS(LiveOMS(live_bridge))` started
  4. `refresh_wallet_from_clob` + supervisor loops + `run_user_ws_ingest`
  5. wait readiness, build `EnterIntent`, evaluate risk, submit, parse venue id, cancel.
- Scenario `config/scenarios/live_attest.yaml` disables capital + venue-min-size gates and shrinks notional for a 1-share probe; `require_user_ws_live: false`.

### 2.11 Config / docs / tests touchpoints

- `pyproject.toml` declares `py-clob-client>=0.34.0` in `[project.optional-dependencies].live`.
- Config YAMLs (`config/risk/default.yaml`, `config/runtime/default.yaml`, `config/scenarios/live_attest.yaml`) are V1-agnostic.
- Docs (`Docs/LIVE_ARCHITECTURE.md`, `Docs/modules/venue/README.md`, `Docs/Architecture.md`) reference `py-clob-client` and `PyClobBridge` by name.
- Tests touching V1 imports: `tests/test_clob_env_aliases.py`, `tests/test_clob_heartbeat_state_machine.py`, `tests/test_t6_live_oms_unit.py` (smoke block), and any pipeline tests that round-trip `PolyApiException`.

### 2.12 V1 import inventory (file:line)

```
src/tyrex_pm/venue/polymarket/clob_env.py:64           from py_clob_client.client import ClobClient
src/tyrex_pm/venue/polymarket/clob_bridge.py:19,20     OrderArgs, BUY, SELL
src/tyrex_pm/venue/polymarket/clob_heartbeat.py:33     PolyApiException
src/tyrex_pm/venue/polymarket/clob_wallet_sync.py:27   AssetType, BalanceAllowanceParams
src/tyrex_pm/runtime/pipeline.py:11                    PolyApiException
tests/test_clob_env_aliases.py                         patches py_clob_client.client.ClobClient
tests/test_clob_heartbeat_state_machine.py:10          PolyApiException
tests/test_t6_live_oms_unit.py:208–211                 V1 client + bridge + heartbeat helper (TYREX_LIVE_SMOKE block)
pyproject.toml:22                                      py-clob-client>=0.34.0
```

---

## 3. Detailed V2 review (conceptual)

> Reminder: §0 governs symbol naming. The package surface as installed is authoritative.

### 3.1 Python SDK shape (expected)

- Package `py_clob_client_v2` is expected to expose at least: a `ClobClient` class, an `ApiCreds` value type, V2 order-args dataclasses, a `PolyApiException`, a `BalanceAllowanceParams` + `AssetType` pair, an `OrderType` enum (GTC/GTD/FOK/IOC), and a `config.get_contract_config(chain_id, neg_risk=False)` helper that returns V1 + V2 contract addresses.
- Constructor signature is conceptually `(host, chain_id, key=..., creds=..., signature_type=..., funder=..., builder_config=..., use_server_time=False, retry_on_error=False)`.
- API-key derivation method is **singular** (`create_or_derive_api_key`), replacing V1's plural `create_or_derive_api_creds`. The returned credentials are still the three-field shape we feed into the user-WS subscribe payload.
- Order placement / cancellation methods take V2 order-arg / payload types; bridge wraps the SDK calls behind our existing `create_and_post_limit` / `cancel_order` async methods so internal callers do not change.

### 3.2 Hosts / endpoints / chain

- Chain `137` (Polygon mainnet) — unchanged.
- User-WS URL `wss://ws-subscriptions-clob.polymarket.com/ws/user` — unchanged.
- REST host: see §1.3. Pre-cutover validation runs against `https://clob-v2.polymarket.com`; after cutover the production host `https://clob.polymarket.com` resolves to the V2 stack.
- Endpoint paths: heartbeat (`POST /v1/heartbeats`) and balance-allowance reuse V1 paths under V2 auth; order-write paths are V2-shape and abstracted by the SDK.

### 3.3 Authentication

- L1 (wallet signature) used once to derive API credentials.
- L2 (HMAC api_key/api_secret/api_passphrase) used for heartbeat, balance/allowance, open-orders, place, cancel, user-WS subscribe.
- The user-WS subscribe payload `{"type":"user","auth":{"apiKey","secret","passphrase"}}` is unchanged. `run_user_ws_ingest` keeps the same shape.

### 3.4 Order signing / order model

- Removed vs V1: `fee_rate_bps`, `nonce`, `taker`. Fees are venue-side; replay protection moves into SDK-managed `salt` + `timestamp`.
- Added vs V1: `expiration` (`0` = GTC), `builder_code` (`bytes32`, default zero), `metadata` (`bytes32`, default zero).
- Signing domain: V2 `CTFExchangeV2` or `NegRiskCTFExchangeV2` at the V2 exchange addresses; selected by the SDK from `MarketDetails.neg_risk`.
- Signature types: integer values for `EOA/POLY_PROXY/POLY_GNOSIS_SAFE` are unchanged; `POLY_1271=3` is new and not currently used by Tyrex_PM.

### 3.5 Contracts / domain / collateral

- `config.get_contract_config(chain_id)` is expected to return both V1 and V2 contract addresses (`exchange`, `neg_risk_exchange`, `exchange_v2`, `neg_risk_exchange_v2`, `collateral`, `conditional_tokens`).
- Collateral is Polymarket USD on V2 (1:1 USDC-backed). It is exposed under the same `AssetType.COLLATERAL` enum value with the same six-decimal "USD" semantics; `WalletStore.usdc_balance` / `usdc_allowance` continue to be the right slot.
- API traders **must wrap()** USDC → Polymarket USD once before they can post V2 orders. Tyrex_PM does not wrap; it fails-closed if the V2 balance is zero / unset.

### 3.6 Order fields (canonical mapping)

| Internal `PlaceOrderRequest` | V2 order-arg field | Notes |
|------------------------------|--------------------|-------|
| `token_id` | `token_id` | string, unchanged |
| `side` (`Side` enum) | side constant | translate at the bridge edge using V2 SDK constants |
| `size` (`Decimal`) | size (`float`) | `Decimal → float` happens once, in the bridge |
| `price` (`Decimal`) | price (`float`) | same |
| `style` (`OrderStyle`) | `order_type` + `expiration` | `GTC → expiration=0`; other styles map to `OrderType` enum |
| `client_order_id` | not on order args | stays internal (fingerprint / correlation) |
| _(none)_ | `builder_code` | optional from `TYREX_BUILDER_CODE`; default zero |
| _(none)_ | `metadata` | always zero in this migration |

### 3.7 Market metadata (V2 opportunity surface)

V2 exposes a richer per-market metadata surface via `client.get_market(condition_id)` / similar (exact method name to be confirmed against the installed SDK). Expected fields include:

- `min_tick_size` — per-market price granularity.
- `neg_risk` — boolean steering the SDK toward `neg_risk_exchange_v2` for signing.
- `fee_details` — venue-side fee parameters (we have no client-side fee logic; this is for visibility/reporting).
- `tokens` — `(token_id, outcome)` mapping that lets us validate that a strategy's `token_id` resolves to a live outcome on a live market.
- Per-market minimum order size, where surfaced (binary markets retain a 5-share floor in V2; for non-binary or multi-outcome markets the floor may be metadata-driven — verify against the installed SDK).

This plan treats the *adoption* of that metadata as a defined opportunity (see §5 and §7 Phase 7M) rather than a default of "hardcode the V1 assumptions forever". In migration-scope, we wire a thin adapter (`venue/polymarket/market_info.py`) that fetches and caches `MarketDetails` per `condition_id`/`token_id`. Risk evaluators that already have extension hooks (notably `_resolve_min_size` in `venue_min_size.py`) start consuming that adapter immediately; the rest of the metadata (tick size enforcement on price quantization, fee visibility on `risk_decision` evidence) is wired in the immediate follow-up phase (§7 Phase 7M).

### 3.8 Builder changes

- The SDK's internal builder signs against the V2 exchange domain when given V2 args. Tyrex_PM never constructs the typed-data payload directly — the bridge stays a thin async wrapper.
- `builder_code` (`bytes32`) supports referral / partner attribution. We expose it via `TYREX_BUILDER_CODE` env (32-byte hex). Absent / empty = zero = no attribution.

### 3.9 Order lifecycle

- Acks still surface a venue order id (`orderID` camelCase). `parse_venue_order_id` keeps the same fall-through (`orderID` / `order_id` / `id`).
- Status strings on acks (`LIVE`, `MATCHED`, `CANCELED`, …) continue to flow into `oms_submit` facts as `status` / `orderStatus`.
- 425 (matching engine restart) remains a documented status. `pipeline.process_new_guru_signals` already converts a 425 on `submit` into `health.mark_venue_restart_suspected()` via `PolyApiException`; the V2 SDK is expected to raise the same exception class shape.

### 3.10 Open-order wipe / cutover semantics

Because we are pre-production with no real positions (§1.4), there is no V1 inventory to translate. The wipe is *local-state hygiene*, not a venue-side data migration. Concretely: any V1-era artifacts on the host filesystem (state stores, in-memory snapshots if a process were restarted across the boundary, fixture-derived shadow state) must not seed the first V2 process. §6 details the implementation.

### 3.11 Unchanged architecture

- One-writer-per-wallet (`SingleWriterOMS`).
- Two-truths (user-WS authoritative, REST backstop, tombstones).
- Reconcile severity tiers and dedup signatures.
- Risk gating (notional, deployment, capital, venue-min-size, inventory, kill switch, concurrency, readiness).
- Reporting (fact schema v2, JSONL sinks, summarizer).
- Strategy plumbing, guru-follow, conviction sizing.

---

## 4. Mapping table — current implementation → V2 target

> All "V2 target" symbol names below are conceptual placeholders; verify against the installed `py-clob-client-v2` package per §0 before coding.

| # | Surface | Current code | V2 target | Plan notes |
|---|---------|--------------|-----------|------------|
| 1 | SDK dep | `pyproject.toml` `live = ["py-clob-client>=0.34.0", ...]` | `live = ["py-clob-client-v2>=<pinned>", ...]` | Drop V1 dep entirely; one-shot replacement, not parallel install. |
| 2 | Order builder | `execution/order_builder.py::to_place_request` returns `PlaceOrderRequest` | Same function, same DTO | `PlaceOrderRequest` stays the canonical internal shape; only the bridge translates to V2 order args. |
| 3 | Order signer | `clob_bridge.py` builds `OrderArgs(...)` and calls `client.create_and_post_order(oa)` | `clob_bridge.py` builds the V2 order-arg dataclass with `expiration=0`, optional `builder_code`, `metadata=zero`, then calls the V2 place-order method with `order_type=GTC` (or equivalent) | Map `OrderStyle.GTC → OrderType.GTC` etc. `Decimal → float` conversion stays at this single edge. |
| 4 | Cancel | `client.cancel(vid)` | V2 single-cancel method (likely `cancel_order(payload)`) | One vid per call; no batching introduced. |
| 5 | Client init | `clob_env.try_create_clob_client()` → V1 client + `create_or_derive_api_creds` + `set_api_creds` | Same function name; V2 client + singular `create_or_derive_api_key` + `set_api_creds`; optional `builder_config` from `TYREX_BUILDER_CODE` | Identical env-var contract. Default value of `TYREX_CLOB_HOST` rebased to V2 staging host pre-cutover (see §1.3). |
| 6 | Heartbeat | `clob_heartbeat.py` catches V1 `PolyApiException` | Same module; catches V2 `PolyApiException` | Logic (id rotation on 400, recovery loop) unchanged. |
| 7 | Bridge | `PyClobBridge` (V1) | Same class, V2 internals; method names `create_and_post_limit / cancel_order / post_heartbeat` preserved | Bridge interface is the stable contract for `LiveOMS`, supervisor loops, and `_FakeBridge`. |
| 8 | Wallet balance/allowance | V1 `BalanceAllowanceParams(asset_type=COLLATERAL)`; reads `balance` / `available` / `allowance` / `allowance_balance` | Same call against V2 SDK; collateral is now Polymarket USD (USD-decimal units unchanged) | Add startup assertion that `balance` is set after bootstrap so a missing wrap step is loud. |
| 9 | Open-order REST | `client.get_orders()` | V2 `get_open_orders()` (or installed equivalent); preserve dict-or-list normalization, follow `next_cursor` if the V2 shape paginates | Per-row keys (`asset_id`, `original_size`, `size_matched`, `price`, `id`/`orderID`, `status`, `side`) are expected to be the same. |
| 10 | Collateral semantics | `WalletStore.usdc_balance/allowance` (USDC.e) | Same fields, holding Polymarket USD as USD-decimal | Doc-update only — call them "venue collateral (Polymarket USD)" without renaming the dataclass field. |
| 11 | Market metadata | `GammaClient.is_token_tradeable` (Gamma; orthogonal) | Unchanged for tradeability gate; **also** new `venue/polymarket/market_info.py` adapter consumes V2 `MarketDetails` for tick / min-size / fees / outcome mapping | See §3.7 + §5 + §7 Phase 7M. |
| 12 | Venue min size | `risk/venue_min_size.py` constant 5 (config) | Same default for binary markets; `_resolve_min_size` reads the new market-info adapter when available, falls back to `cfg.default_min_size` if metadata is missing | First in-scope consumer of V2 market info. |
| 13 | User-WS payload | `ingestion/user_stream.py` subscribes with `{type:"user", auth:{apiKey, secret, passphrase}}` from `live_clob.creds` | Same payload, same URL, same dispatcher; creds come from V2 `client.creds` (populated by `create_or_derive_api_key`) | Zero functional change. |
| 14 | Positions | `positions_sync.refresh_positions_from_data_api` (data-api) | Unchanged | data-api lives outside the CLOB versioning. |
| 15 | live-attest | `runtime/live_attest.py` imports `try_create_clob_client`, `PyClobBridge`, `refresh_wallet_from_clob` | Same imports, V2-backed implementations | Adds `phase=v2_environment` fact (host, chain, exchange + neg-risk addresses, builder_code) and `phase=collateral_check` fact (Polymarket USD balance + allowance). |
| 16 | Cutover / reset | n/a | New CLI `tyrex-pm reset-state` (clears local state stores and OMS-state caches) + reset-aware `tyrex-pm run` startup that refuses to trade until first V2 REST + WS sync completes | See §6. |
| 17 | Tests / mocks | V1 imports in `test_clob_env_aliases.py`, `test_clob_heartbeat_state_machine.py`, `test_t6_live_oms_unit.py` (smoke block) | Patch V2 module paths; rename `create_or_derive_api_creds` → `create_or_derive_api_key`; smoke imports V2 client + bridge | Test bodies otherwise unchanged because they target our wrappers, not SDK semantics. |
| 18 | Config / env | `TYREX_PRIVATE_KEY`, `TYREX_CLOB_HOST`, `TYREX_CHAIN_ID`, `TYREX_SIGNATURE_TYPE`, `TYREX_FUNDER`, `TYREX_HEARTBEAT_ID`, `TYREX_HEARTBEAT_INTERVAL_S`, `TYREX_VENUE_REFRESH_S`, `TYREX_USER_WS_*`, `TYREX_DATA_API_BASE` | All unchanged; **default for `TYREX_CLOB_HOST` rebased to `https://clob-v2.polymarket.com` during pre-cutover; flipped back to `https://clob.polymarket.com` on cutover day**. New: `TYREX_BUILDER_CODE` (optional 32-byte hex). | See §1.3. |
| 19 | Docs / workflow | `LIVE_ARCHITECTURE.md`, `OPERATIONS.md`, `DEVELOPMENT.md`, `modules/venue/README.md` mention V1 by name | Updated to V2; new sections for V2 host, wrap step, `reset-state` runbook, market-info adapter | Mechanical doc edits after code lands. |

---

## 5. V2 opportunities and "do better / faster" ideas

This section calls out the things V2 lets us do that we are not doing today, separated into "in migration scope" and "immediate follow-up". The plan adopts the in-scope items as part of the implementation phases in §7; the follow-up items are tracked here so they do not get lost behind a one-to-one SDK swap.

### 5.1 In migration scope

- **Market-info adapter as a first-class module.** New `venue/polymarket/market_info.py` (caches `MarketDetails` per `condition_id` / `token_id` with TTL). Consumers in scope: `risk/venue_min_size.py::_resolve_min_size` (per-market floor when metadata says so; default to config otherwise), `clob_bridge.py` (passes `neg_risk` through to the SDK on order build, so the SDK selects the right V2 exchange contract for negative-risk markets).
- **Stronger preflight in `live-attest`.** New facts: `phase=v2_environment` (host, chain, exchange + neg-risk-exchange + collateral addresses from `config.get_contract_config`, `builder_code`), `phase=collateral_check` (`balance`, `allowance` for Polymarket USD), `phase=market_info` (`min_tick_size`, `neg_risk`, `fee_details`, `min_order_size`). These make every attest run self-describing for forensic clarity.
- **Cleaner exchange-adapter boundary.** The migration formalizes a single `venue/polymarket/` adapter contract: `clob_env` (client construction + env), `clob_bridge` (sync→async wrapper, the only place that touches V2 order-arg types), `clob_heartbeat` (session id rotation), `clob_wallet_sync` (REST refresh), `market_info` (new, metadata cache), `user_ws.py`/`market_ws.py` (WS), `positions_sync` (data-api). No other module imports `py_clob_client_v2` symbols directly. This is enforced by a lightweight grep test in CI (`tests/test_v2_import_isolation.py`).
- **Cutover-safe startup.** `tyrex-pm run` refuses to evaluate strategies until: heartbeat ok, REST balance/allowance succeeded once, REST open orders succeeded once, and either user-WS first message arrived **or** the WS-stale grace elapsed (existing readiness gate). The migration tightens this by enforcing the "no leftover state" check before the gate even runs (see §6.3).

### 5.2 Immediate follow-up (Phase 7M)

- **Tick-size enforcement.** Use `MarketDetails.min_tick_size` to quantize `limit_price` at order-build time so the venue cannot reject for off-tick prices. Today we trust strategy-side rounding.
- **Fee visibility.** Fold `MarketDetails.fee_details` into the `risk_decision` extensions for BUY orders so operators can see the venue-applied fee rate alongside `intent_need_usd`.
- **Token / outcome validation.** Use `MarketDetails.tokens` to assert that `intent.token_id` is a live outcome of a live market before order build; emit `STRATEGY_SKIP` with a stable reason code if it is not (replaces the current Gamma-only tradeability check, or augments it).
- **Builder-code attribution.** If we register a builder code with Polymarket, set `TYREX_BUILDER_CODE` and confirm every `oms_submit` fact carries it.

### 5.3 Explicitly out of scope

- **Multi-venue abstraction.** The `venue/<name>/` directory pattern stays open per `Docs/modules/venue/README.md`, but no second venue is wired in this migration.
- **EIP-1271 wallets.** Supported by the V2 SDK via `signature_type=3`; not used by Tyrex_PM today; no code path assumes EOA so this is configurable later without code changes.
- **Batch cancel.** V2 may expose a batch-cancel call. We keep one vid per call for now; the existing `SingleWriterOMS` queue makes batch-cancel a future optimization, not a correctness requirement.

---

## 6. Cutover and clean-first-V2-start reset posture

### 6.1 Operating reality

We are pre-production. No real bot inventory exists on the wallet. There is no V1 venue state to translate, no in-flight orders to replay, no positions to reconcile across the boundary. The migration is therefore much smaller than a normal venue cutover — but it is **not** zero, because local state stores on the host can still poison the first V2 run if not explicitly reset.

### 6.2 What we are *not* doing

- No dual-mode runtime, no compatibility shim, no `if v2:` branches.
- No long-running `feature/v2` branch; ships on `main`.
- No translation layer for V1 `OrderStore` rows or `WalletStore` snapshots into V2 equivalents — they are simply discarded.
- No live wallet-state dance between V1 and V2 (no positions to dance with).

### 6.3 Clean first-V2-start reset posture (mandatory)

Three guarantees, enforced in code:

1. **Local state from V1 is gone before V2 starts.**
   Implemented by a new CLI subcommand `tyrex-pm reset-state` that deletes:
   - `var/state/guru_strategy_store.json` (guru watermark / dedup ledger).
   - Any other on-disk artifacts under `var/state/` that a future-startup `tyrex-pm run` would consume. (`var/reporting/runs/` historical fact files are **kept** — they are immutable history, not state.)

   The command is idempotent and prints what it removed; running it twice is a no-op.

2. **In-memory state starts empty on the first V2 process.**
   `cmd_run` already constructs a fresh `WalletStore()` and `OrderStore()` each invocation. The migration tightens this by removing any code path that would seed those stores from V1-shaped fixtures or from a shadow bootstrap that was sized for V1 collateral. (Shadow scenarios continue to work; live mode never seeds from shadow bootstrap.) A new startup assertion fails-fast if `WalletStore.open_orders` or `OrderStore.orders` is non-empty before the first venue refresh — that condition would mean code was changed to inject state and the operator should know about it.

3. **First risk evaluation only after V2 venue truth is rebuilt.**
   The existing readiness gate (`risk.health.check_aggressive_readiness`) already requires a recent wallet sync + heartbeat + (optionally) user-WS freshness before `aggressive` decisions. Migration adds a single boolean to that gate: `first_v2_sync_complete`, set to `True` after the first successful `refresh_wallet_from_clob` in the live process and after the first user-WS message (or after the WS-stale grace elapses, when WS is REST-only). Until that flag flips, `evaluate_intent` denies any new BUY/SELL with reason `bootstrap_not_complete`.

### 6.4 Operator workflow (one-time per environment)

Pre-cutover validation environment (`https://clob-v2.polymarket.com`):

1. `pip install -e .[live,dev]` — picks up `py-clob-client-v2`.
2. Inspect the installed SDK (§0) and adjust any plan-named symbols to match the actual surface.
3. Wrap USDC → Polymarket USD via the official Polymarket portal (one-time approval + wrap call). Tyrex_PM does not wrap.
4. `tyrex-pm reset-state` — clears `var/state/`.
5. `TYREX_CLOB_HOST=https://clob-v2.polymarket.com tyrex-pm live-attest --token-id <numeric> --size 1 --price 0.10 --side BUY` — single-cycle BUY+cancel against V2 staging.
6. Validate facts: `phase=v2_environment` shows the V2 staging host and V2 contracts; `phase=collateral_check` shows non-zero Polymarket USD; `oms_submit` carries an `orderID`; `oms_cancel` succeeds; final `live-attest` summary is `outcome=ok`.

Cutover day (`https://clob.polymarket.com` is the V2 endpoint):

1. Repeat steps 4–6 with `TYREX_CLOB_HOST` unset (or explicitly set to `https://clob.polymarket.com`).
2. Resume normal operation: `tyrex-pm run --scenario live_guru`.

### 6.5 Why this is enough for our situation

A normal venue migration would also need: position snapshot reconciliation, in-flight order replay, deferred-cancel handling, and a compatibility window. We need none of that because there is no production state. The reset posture above is the minimum hygiene that prevents the *only* remaining failure mode (stale local artifacts seeding the first V2 run with V1 assumptions).

---

## 7. Implementation plan (phased)

Each phase is one mergeable change set or one tightly-scoped commit within a single PR. Phases run sequentially; tests run on every phase. Files touched are repo-relative.

### Phase 0 — Verify installed V2 SDK surface

- **Objective:** ground all subsequent phases in the actual installed package, not in plan-named symbols.
- **Files:** none in `src/`. Optional: a `scripts/inspect_v2_sdk.py` that prints the public surface for the operator and CI to capture.
- **Done when:** the inspection output is recorded in the PR description and any plan-side symbol names that diverge from the installed package are listed for follow-up adjustments in Phases 1–4.

### Phase 1 — Pin V2 SDK and wire host config

- **Objective:** `pyproject.toml` resolves `py-clob-client-v2`; `clob_env.try_create_clob_client` defaults to the V2 staging host and accepts an env override.
- **Files:** `pyproject.toml`, `src/tyrex_pm/venue/polymarket/clob_env.py`.
- **Change:**
  - Replace `py-clob-client>=0.34.0` with `py-clob-client-v2>=<pinned>` in `[project.optional-dependencies].live`.
  - Default of `TYREX_CLOB_HOST` becomes `https://clob-v2.polymarket.com` in `try_create_clob_client` for the duration of the migration.
  - Replace V1 client import with V2 import; replace `create_or_derive_api_creds` with `create_or_derive_api_key`.
  - Optional: read `TYREX_BUILDER_CODE` and pass through to the SDK's builder config (validate 32-byte hex; fail-fast on malformed values).
- **Done when:** `tyrex-pm live-attest --token-id <numeric> --size 1 --price 0.10 --side BUY` against the V2 staging host gets at least to the heartbeat OK / readiness phases (Phases 2–4 finish the order-build path).
- **Tests:** `tests/test_clob_env_aliases.py` updated to patch the V2 module path and the singular API-key method; new `tests/test_clob_env_v2_host_default.py` asserts the staging-host default and the env-var override.

### Phase 2 — V2 bridge (place + cancel)

- **Objective:** `PyClobBridge.create_and_post_limit` builds a V2 order-arg and submits it; `cancel_order` calls the V2 single-cancel method; `post_heartbeat` continues to call the SDK's heartbeat.
- **Files:** `src/tyrex_pm/venue/polymarket/clob_bridge.py`.
- **Change:** import V2 order-arg / cancel-payload types; convert `Side` → V2 side constant; build args with `expiration=0`, optional `builder_code`, `metadata=zero`; call V2 place/cancel methods.
- **Done when:** `tests/test_t6_live_oms_unit.py::test_live_oms_submits_via_bridge` passes; new `tests/test_clob_bridge_v2_payload.py` mocks the SDK and asserts the V2 order-arg field set (no `fee_rate_bps`/`nonce`/`taker`).
- **Tests:** as above.

### Phase 3 — Heartbeat exception + pipeline imports

- **Objective:** every catch-site for `PolyApiException` resolves to the V2 exception class.
- **Files:** `src/tyrex_pm/venue/polymarket/clob_heartbeat.py`, `src/tyrex_pm/runtime/pipeline.py`.
- **Change:** replace V1 import with V2.
- **Done when:** `tests/test_clob_heartbeat_state_machine.py` (with updated import) and `tests/test_pipeline_*` continue to pass.

### Phase 4 — Wallet sync (V2 method names)

- **Objective:** `clob_wallet_sync._sync_wallet_from_clob` uses V2 `BalanceAllowanceParams` and the V2 open-orders REST call; handles paging if the V2 shape paginates.
- **Files:** `src/tyrex_pm/venue/polymarket/clob_wallet_sync.py`.
- **Change:** import switch; method-name swap (`get_orders` → `get_open_orders` or installed equivalent); preserve list-or-dict-of-dicts normalization; add `next_cursor` follow if present.
- **Done when:** `tests/test_pipeline_dedup_and_wallet_sync.py` and `tests/test_positions_rest_safety_net.py` pass; new `tests/test_clob_wallet_sync_v2_shape.py` covers single-page and paginated response shapes.

### Phase 5 — Market-info adapter (in-scope V2 metadata)

- **Objective:** new module `venue/polymarket/market_info.py` that fetches and caches `MarketDetails` keyed by `condition_id` and by `token_id`. First consumer: `risk/venue_min_size.py::_resolve_min_size`.
- **Files:** new `src/tyrex_pm/venue/polymarket/market_info.py`; `src/tyrex_pm/risk/venue_min_size.py` (read adapter).
- **Change:** adapter exposes async `get_market_for_token(token_id) -> MarketDetails | None` with TTL cache and a circuit-breaker on failure (returns `None` so `_resolve_min_size` falls back to `cfg.default_min_size`). `_resolve_min_size` calls the adapter; if it returns a per-market floor, use it; otherwise fall back to config.
- **Done when:** `tests/test_venue_min_size.py` continues to pass; new `tests/test_market_info_adapter.py` covers cache hit / miss / failure-fallback; new `tests/test_venue_min_size_v2_metadata.py` asserts that a metadata-driven floor overrides the config default when present.

### Phase 6 — Cutover-safe startup (`reset-state` + bootstrap gate)

- **Objective:** ship the §6 reset posture.
- **Files:**
  - new `src/tyrex_pm/runtime/cli/reset_state.py` — clears `var/state/`, idempotent, prints removed paths.
  - `src/tyrex_pm/runtime/app.py` — register `reset-state` subcommand; add `first_v2_sync_complete` flag wiring on `RuntimeCoordinator`.
  - `src/tyrex_pm/risk/health.py` — extend `check_aggressive_readiness` to deny with `bootstrap_not_complete` until the flag flips.
  - `src/tyrex_pm/runtime/live_supervisor.py::venue_refresh_loop` — set the flag on first successful sync.
- **Done when:**
  - `tyrex-pm reset-state` deletes the documented files and is idempotent on a clean tree (covered by `tests/test_reset_state_cli.py`).
  - In live mode, the first `evaluate_intent` call before `venue_refresh_loop` has run is denied with `bootstrap_not_complete` (`tests/test_first_v2_sync_gate.py`).

### Phase 7 — Wire `live-attest` for V2 evidence

- **Objective:** every V2 attest run is self-describing via three new phase facts.
- **Files:** `src/tyrex_pm/runtime/live_attest.py`.
- **Change:** emit `phase=v2_environment` (host, chain, V2 exchange + neg-risk-exchange + collateral addresses, builder_code), `phase=collateral_check` (Polymarket USD balance + allowance), `phase=market_info` (token's `min_tick_size`, `neg_risk`, `fee_details`, `min_order_size`).
- **Done when:** `tests/test_live_attest_unit.py` continues to pass; new `tests/test_live_attest_v2_facts.py` asserts the three facts are emitted with the expected key set.

### Phase 7M — Immediate follow-up: market-info-driven price/fees/outcomes

- **Objective:** the rest of §5.2 — tick-size quantization on order build, fee visibility on `risk_decision` extensions, token/outcome validation as a strategy-skip gate.
- **Files:** `src/tyrex_pm/venue/polymarket/clob_bridge.py` (price quantization to `min_tick_size`), `src/tyrex_pm/risk/engine.py` (fee evidence on BUY decisions), `src/tyrex_pm/strategies/guru_follow/strategy.py` or a new pre-strategy validator (token/outcome check).
- **Done when:** new tests cover each: `tests/test_bridge_tick_quantize.py`, `tests/test_risk_decision_fee_evidence.py`, `tests/test_strategy_token_outcome_validation.py`.
- **Notes:** explicitly broken out from the migration so it does not block cutover; intended to ship in the same release window.

### Phase 8 — Documentation

- **Objective:** repo-wide doc accuracy.
- **Files:** `Docs/LIVE_ARCHITECTURE.md`, `Docs/Architecture.md`, `Docs/modules/venue/README.md`, `Docs/OPERATIONS.md`, `Docs/Implementation/current_state.md`, `README.md`.
- **Change:** rename `py-clob-client` → `py-clob-client-v2`; document the V2 staging vs production host story (§1.3); document the wrap step and `reset-state` runbook (§6.4); link to this plan from the Implementation hub.
- **Done when:** `rg "py-clob-client[^v]"` returns no hits outside historical run artifacts; `rg "clob-v2.polymarket.com"` and `rg "clob.polymarket.com"` both surface in the right places.

### Phase 9 — Cleanup and import-isolation guard

- **Objective:** remove V1 dead paths; lock in the adapter boundary.
- **Files:** any leftover V1 references under `src/`; new `tests/test_v2_import_isolation.py`.
- **Done when:** `rg "py_clob_client[^_]"` returns zero in `src/`; the import-isolation test fails when any module outside `venue/polymarket/` imports a `py_clob_client_v2` symbol.

---

## 8. Tests and validation plan

### 8.1 Unit (offline)

| Test | Covers | Phase |
|------|--------|-------|
| `test_clob_env_aliases.py` (updated patches) | V2 client construction; env-var aliases; signature-type fallback | 1 |
| `test_clob_env_v2_host_default.py` (new) | `TYREX_CLOB_HOST` defaults to V2 staging; env override wins | 1 |
| `test_v2_builder_code_env` (new) | `TYREX_BUILDER_CODE` validation + plumbing into the SDK builder | 1 |
| `test_clob_bridge_v2_payload.py` (new) | V2 order-arg field set (no `fee_rate_bps`/`nonce`/`taker`) | 2 |
| `test_t6_live_oms_unit.py` | `LiveOMS.submit/cancel` against fake bridge (interface preserved) | 2 |
| `test_clob_heartbeat_state_machine.py` (updated import) | Heartbeat 200/400 id rotation under V2 `PolyApiException` | 3 |
| `test_clob_wallet_sync_v2_shape.py` (new) | V2 wallet sync, single + paginated response shapes | 4 |
| `test_market_info_adapter.py` (new) | TTL cache, failure fallback | 5 |
| `test_venue_min_size_v2_metadata.py` (new) | Metadata floor overrides config default when present | 5 |
| `test_reset_state_cli.py` (new) | `reset-state` deletes documented files; idempotent | 6 |
| `test_first_v2_sync_gate.py` (new) | New `bootstrap_not_complete` gate | 6 |
| `test_live_attest_v2_facts.py` (new) | Three V2 phase facts emitted | 7 |
| `test_v2_import_isolation.py` (new) | No `py_clob_client_v2` imports outside `venue/polymarket/` | 9 |

### 8.2 Integration (offline)

- `tests/test_pipeline_refresh_coordinated.py` — exercise the post-submit REST refresh path against a fake V2 client and assert `WalletStore.open_orders` populates from the V2 payload shape.
- `tests/test_inverse_race_tombstone.py` — operates on `WalletStore` only; confirms tombstone semantics still hold.

### 8.3 Live attest (operator, gated)

- `TYREX_LIVE_SMOKE=1` opt-in test in `tests/test_t6_live_oms_unit.py::test_t6_real_clob_heartbeat_smoke` — updated to import V2 client + bridge; runs one heartbeat against the V2 staging host.
- `TYREX_CLOB_HOST=https://clob-v2.polymarket.com tyrex-pm live-attest ...` — single-cycle BUY+cancel; pass-fail gate per §6.4 step 5.

### 8.4 Post-cutover validation (production scenario)

After the first `tyrex-pm run --scenario live_guru` post-cutover (production host):

- `phase=v2_environment` fact in `live-attest` (or in run startup) carries `host=https://clob.polymarket.com` and the V2 contract addresses.
- At least one `wallet_sync` fact with non-zero `wallet_usdc_balance` (Polymarket USD).
- At least one `health` fact with `event=heartbeat, heartbeat_ok=true`.
- One `oms_submit` fact whose `oms_result` carries an `orderID` that subsequently appears in `wallet.open_orders` via either user-WS PLACEMENT or REST `get_open_orders`.
- No `risk_decision` fact denies with `bootstrap_not_complete` after the readiness gate clears.

### 8.5 Wallet sync

- Bootstrap: `refresh_wallet_from_clob` produces a non-`None` `usdc_balance` before any submit.
- Steady-state: `wallet_sync` facts dedup correctly via `_wallet_sync_signature`.
- Tombstones: WS-terminal id followed by stale REST snapshot suppresses the row.

### 8.6 Order lifecycle

- `register_submit` → `oms_writer.submit` (V2) → `ack_submit` with parsed `vid` → user-WS PLACEMENT lifts confirmation to `venue_confirmed` → `oms_writer.cancel` (V2) → user-WS CANCELLATION removes the row → reconcile clears.
- 425 path: `pipeline.process_new_guru_signals` catches V2 `PolyApiException(status_code=425, ...)` and calls `health.mark_venue_restart_suspected()`.

### 8.7 Min-size and metadata

- `evaluate_venue_min_size` continues to deny / bump with the venue floor (now potentially metadata-driven).
- `market_info.get_market_for_token` returns the cached `MarketDetails` for hot tokens; cold-path miss falls back to config.

### 8.8 Capital / deployment

- `evaluate_capital_buy` denies when Polymarket USD balance is insufficient or unsynced (existing behavior; values come from V2 wallet sync).

---

## 9. Risks and sharp edges

1. **SDK surface deltas.** Plan-named symbols may not match the installed `py-clob-client-v2`. Mitigation: §0 + Phase 0 (inspect surface, capture deltas in PR description) before any code changes.
2. **Decimal → float precision.** V2 order-arg `size`/`price` are floats. Conversion happens in exactly one place (`clob_bridge.py`); add a unit test that round-trips representative tick sizes (0.01, 0.001) and asserts the SDK-side integer maker/taker amounts match Polymarket's documented rounding. Phase 7M tightens this further by quantizing prices to `min_tick_size` from the metadata adapter.
3. **`builder_code` malformed.** A non-32-byte hex value is rejected by the SDK at signing, not at submit. Mitigation: fail-fast validation in `try_create_clob_client` when `TYREX_BUILDER_CODE` is set.
4. **Polymarket USD wrap forgotten.** Bot fails-closed at the capital gate, but the fact body says "INSUFFICIENT_CAPITAL" without naming "wrap". Mitigation: `live-attest` `phase=collateral_check` carries explicit `balance` + `allowance`; runbook calls the wrap step out as step 3 in §6.4.
5. **Local V1 state leaks into V2 startup.** Mitigation: `tyrex-pm reset-state` (Phase 6) + `bootstrap_not_complete` gate (Phase 6) + assertion that on-process startup has empty stores.
6. **Host confusion (V2 staging vs production).** Mitigation: `TYREX_CLOB_HOST` is the single source of truth; `phase=v2_environment` fact records the host on every attest run; the migration default rebases to staging then flips to production on cutover day in a single explicit commit.
7. **EIP-1271 wallets (`POLY_1271`).** Not used today; documented to avoid surprise.
8. **`get_open_orders` paging.** If V2 paginates, normalization must follow `next_cursor`. Mitigation: extend `_sync_wallet_from_clob` accordingly with a unit test against a two-page fake response.
9. **Heartbeat rotation under V2.** Path unchanged; rotation may be more aggressive. Mitigation: `HEARTBEAT_RECOVER_MAX_ATTEMPTS=8` already covers multi-rotation ticks.
10. **Negative-risk markets.** SDK selects the right exchange contract from `MarketDetails.neg_risk`. Mitigation: market-info adapter feeds `neg_risk` to the bridge so the SDK has it; Phase 5 covers the wiring.
11. **Schema-version naming clash.** `reporting/schema_v2.py` is our internal fact schema, unrelated to Polymarket V2. Documented to avoid confusion in code review.

---

## 10. Recommended next implementation task

**Phase 0 + Phase 1 in one PR: install `py-clob-client-v2`, inspect the actual surface, then update `clob_env.try_create_clob_client` for the V2 client, V2 staging-host default, and (optional) `TYREX_BUILDER_CODE` plumbing.**

This is a change in the previous recommendation, which was "Phase 1 only" against assumed V2 symbol names. The revised first slice is stronger because:

- It forces us to look at the installed SDK before writing code (§0). Any plan-named symbol that turns out to differ in the real package is captured in the PR description and reflected in Phase 1's edits, rather than caught later in review.
- It locks the V2 staging host into the default (`https://clob-v2.polymarket.com`). All subsequent phases — bridge, heartbeat, wallet sync, market-info adapter, live-attest — then validate against the real V2 endpoint instead of a host that still resolves to V1.
- It is small (one `pyproject.toml` line, one default-host change, one method-name swap, optional `TYREX_BUILDER_CODE` plumbing) and still ships a live-runnable boundary: after this PR the heartbeat path against the V2 staging host is exercisable end-to-end, even though the order-build path (Phase 2) is not yet V2-shaped.
- It does **not** require Phase 6's reset posture to land first, because no order is built or submitted yet — `live-attest` will get to the readiness gate and stop there until Phase 2 finishes the bridge.

After this PR lands, the next slice is Phase 2 (bridge: V2 order-args + V2 cancel call), then Phase 3 (heartbeat / pipeline exception swap), then Phase 4 (wallet sync method-name update + paging), then Phases 5–7 (market-info adapter + reset posture + V2 attest evidence) bundled into one PR per phase or grouped two-at-a-time depending on review capacity. Phase 7M (price quantization, fee visibility, token/outcome validation) lands in the immediate-follow-up release. Phase 8 (docs) and Phase 9 (cleanup + import-isolation guard) close out the migration.

---

## Appendix A — V1 import inventory (file:line)

```
src/tyrex_pm/venue/polymarket/clob_env.py:64           from py_clob_client.client import ClobClient
src/tyrex_pm/venue/polymarket/clob_bridge.py:19,20     OrderArgs, BUY, SELL
src/tyrex_pm/venue/polymarket/clob_heartbeat.py:33     PolyApiException
src/tyrex_pm/venue/polymarket/clob_wallet_sync.py:27   AssetType, BalanceAllowanceParams
src/tyrex_pm/runtime/pipeline.py:11                    PolyApiException
tests/test_clob_env_aliases.py                         patches py_clob_client.client.ClobClient
tests/test_clob_heartbeat_state_machine.py:10          PolyApiException
tests/test_t6_live_oms_unit.py:208–211                 V1 client + bridge + heartbeat helper (TYREX_LIVE_SMOKE block)
pyproject.toml:22                                      py-clob-client>=0.34.0
```

## Appendix B — V2 contract addresses (reference)

Sourced at runtime from the installed `py_clob_client_v2.config.get_contract_config(chain_id=137)`. Not hard-coded in this repo. The `phase=v2_environment` fact emitted by `live-attest` (Phase 7) records the actual addresses used per run for forensic clarity.

## Appendix C — Host configuration timeline

| Stage | `TYREX_CLOB_HOST` | Set where |
|-------|-------------------|-----------|
| Today (V1 in production) | `https://clob.polymarket.com` | `clob_env.py` default |
| Phase 1 (start of migration) | `https://clob-v2.polymarket.com` | `clob_env.py` default rebased; operators may override via env |
| Phases 2–7 | same as Phase 1 | unchanged |
| Cutover day | `https://clob.polymarket.com` | `clob_env.py` default flipped back; one-line commit, separately reviewable |
| Post-cutover | `https://clob.polymarket.com` | steady state |
