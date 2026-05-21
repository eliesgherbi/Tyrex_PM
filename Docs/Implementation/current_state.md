# Implementation hub

**Status:** native Polymarket stack is implemented under `src/tyrex_pm/` and migrated to **Polymarket V2** (`py-clob-client-v2`, V2 CTF Exchange, Polymarket USD collateral).

**V2 migration progress:** see [V2_migration_plan.md](V2_migration_plan.md) for the historical phase table and live-attest evidence. The top-level docs are the current operator/developer source of truth.

| Layer | State |
|-------|-------|
| Venue SDK | `py-clob-client-v2` (V1 SDK fully removed; enforced by `tests/test_v2_import_isolation.py`). |
| Default CLOB host | `https://clob.polymarket.com` (post-cutover V2 production). Stale `https://clob-v2.polymarket.com` env overrides are rewritten with a warning because that transition host now redirects auth endpoints. |
| Wallet model | EOA / `POLY_PROXY` / `POLY_GNOSIS_SAFE` via `TYREX_SIGNATURE_TYPE`; `POLYMARKET_SIGNATURE_TYPE` fallback. |
| Collateral | Polymarket USD (pUSD, `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`); USDC.e is wrapped via `scripts/v2_wrap_to_pusd.py` using the official `py_builder_relayer_client` SDK + Builder API Key auth. |
| Bridge | `venue/polymarket/clob_bridge.PyClobBridge` builds `OrderArgsV2`, posts via `create_and_post_order`, cancels via `OrderPayload`. |
| Wallet sync | `venue/polymarket/clob_wallet_sync.refresh_wallet_from_clob` uses V2 `BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)`; raw 6-decimal token units are scaled to USD; `allowances` is a per-exchange dict and the binding allowance is `min(...)`. |
| Live attestation | `tyrex-pm live-attest` round-tripped submit→ack→cancel against the pre-cutover transition host on 2026-04-19; post-cutover attest runs should use the default `https://clob.polymarket.com` host and emit V2 evidence facts (`v2_environment`, `collateral_check`, `market_info`, plus `tick_quantize_*` on `oms_submit` and `outcome_validation` on `complete`). |
| First-V2-start hygiene | `tyrex-pm reset-state` clears `var/state/guru_strategy_store.json`; `HealthRuntime.first_v2_sync_complete` gates new-order risk eval until the first venue truth rebuild succeeds. |
| Per-market venue truth | `venue/polymarket/market_info.MarketInfoCache` (TTL=300s, fail-closed, asyncio-locked) resolves `tick_size` / `min_order_size` / `neg_risk` / `fee_rate_bps` / `outcomes` from `/markets-by-token` + `/clob-markets` + SDK helpers. Wired into `RuntimeCoordinator` (live mode) and surfaced via `RiskContext.market_info`; `risk.venue_min_size` prefers venue truth over the YAML default; `execution.order_builder` floor-quantizes `limit_price` to the venue tick before submit. |

**Read next:**

- **Migration plan + status table:** [V2_migration_plan.md](V2_migration_plan.md)
- **Native PM design (pre-V2 architecture, still authoritative for non-venue layers):** [native_pm_rebuild/IMPLEMENTATION_PLAN.md](native_pm_rebuild/IMPLEMENTATION_PLAN.md) · [native_pm_rebuild/ARCHITECTURE.md](native_pm_rebuild/ARCHITECTURE.md) · [native_pm_rebuild/EVENT_CATALOG.md](native_pm_rebuild/EVENT_CATALOG.md)
- **Architecture (overview):** [../Architecture.md](../Architecture.md)
- **Live truth model:** [../LIVE_ARCHITECTURE.md](../LIVE_ARCHITECTURE.md)
- **Operations runbook (incl. `reset-state`):** [../OPERATIONS.md](../OPERATIONS.md)
