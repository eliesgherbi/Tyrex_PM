# Live truth model

**Hub:** [README.md](README.md) · **System overview:** [Architecture.md](Architecture.md)

How Tyrex_PM keeps local OMS state and Polymarket venue state in sync without false fail-closes — and how the reconcile state machines are wired in code.

---

## 1. Two truths

### 1.1 Venue truth

What Polymarket says **right now**. Owned by `state.WalletStore`, populated from:

| Source | Module | Cadence |
|--------|--------|---------|
| **User WebSocket** (primary live truth) | `ingestion.user_stream.run_user_ws_ingest` | event-driven |
| **REST `/data/orders`** (resting orders backstop) | `venue.polymarket.clob_wallet_sync.refresh_wallet_from_clob` | every `TYREX_VENUE_REFRESH_S` (default = `runtime.reconcile_interval_s`, 30 s) |
| **REST `/balance-allowance`** (Polymarket USD + per-exchange allowances) | `clob_wallet_sync.refresh_wallet_from_clob` | with the open-orders refresh |
| **REST `/data-api/positions`** (position safety net) | `venue.polymarket.positions_sync.refresh_positions_from_data_api` | with the venue refresh loop, when a wallet address is resolvable |
| **Market WebSocket** (books / trades) | `ingestion.market_stream` (scaffolded; not consumed by current strategies) | event-driven |

`WalletStore.open_orders` is a **merged view**: user WS wins; REST rows fill ids WS hasn't seen yet; tombstones suppress stale REST rows for ids WS has already declared terminal.

### 1.2 Local truth

What this bot believes about its own session. Owned by `state.OrderStore`:

| Field | Meaning |
|-------|---------|
| `LocalOrder.confirmation` | `provisional` (just submitted) or `venue_confirmed` (visible in merged book). |
| `LocalOrder.submit_ack_utc` | Set by `ack_submit` after a successful venue HTTP response. |
| `LocalOrder.register_utc` | Set by `register_submit` *before* the HTTP call. Used as a fallback age signal. |
| `LocalOrder.submit_fingerprint` | sha1 hash of `(token_id, side, size, price)`; duplicate-submit guard. |
| `OrderStore.in_flight_by_token` / `in_flight_order_count` | Counter for the concurrency gate; released on ack/reject. |
| `OrderStore.pending_repair_fingerprints` | Active fingerprints; blocks resubmission while repair is pending. |
| `OrderStore.terminal_audit` | Ring buffer (capped 1024) of `filled_resolved` / `unknown_terminal` resolutions. |

---

## 2. Why both truths exist

Venue messages lag, the bot may crash mid-flight, REST and WS race, manual UI activity interleaves with automation. We need:

- **Local truth** to make fast pre-trade decisions (have we already sent this order?).
- **Venue truth** for collateral, positions, and final reconciliation.
- A **state machine** that resolves disagreements without false fail-closes.

---

## 3. Reconcile pipeline

`state.reconcile.reconcile_open_orders(wallet, orders, **kw)` is the single entrypoint. It runs three sub-state-machines in order, then writes a comparison report:

```
1) provisional repair        (resolve local rows the venue hasn't echoed yet)
2) terminal-confirmed prune  (drop venue_confirmed rows the venue dropped)
3) venue adoption            (link venue ids to recent no-vid provisional rows)
4) drift comparisons         (size/original mismatch, local-only, venue-only)
```

The function returns a `ReconcileResult` with `drift_flags` (informational), `blocking_drift_flags` (subset that fails-closes new live risk), `reconcile_severity` (`none | transient_venue_lag_candidate | size_mismatch | structural`), plus per-row decision payloads. `RuntimeCoordinator.health.apply_reconcile(res)` flips `reconcile_drift` from those blocking flags.

The pipeline emits a `reconcile` fact, **deduped** by signature so unchanged states don't flood the log (`runtime.pipeline._reconcile_signature`).

### 3.1 Provisional repair state machine

For each `LocalOrder` with `confirmation == "provisional"`:

| Decision | Reason | Outcome |
|----------|--------|---------|
| `confirmed_open_order` | Merged book has the venue id. | No row change here; `sync_local_open_orders_from_venue_wallet` upgrades to `venue_confirmed`. |
| `filled_resolved` | User-WS trade evidence (CONFIRMED/MINED/MATCHED) covers `original_size`. | Drop row + audit. |
| `pending_within_grace` | `ack_age ≤ submit_grace_s`. | Non-blocking. |
| `unknown_terminal` | `ack_age ≥ unknown_terminal_timeout_s`, WS fresh, no venue restart, still absent. | Drop row + audit. Non-blocking. |
| `blocked_absent` | Past grace, below terminal timeout. | **Blocking** (`local_open_not_on_venue`). |
| `blocked_unsafe_to_resolve` | WS stale OR venue restart suspected (425). | **Blocking**; never auto-resolve. |
| `blocked_absent` (no ack ts) | `submit_ack_utc` is None — no age signal. | **Blocking** until explicit evidence. |

Defaults: `submit_grace_s=15`, `unknown_terminal_timeout_s=60`, `adoption_grace_s=5` (see `runtime/config.py`).

### 3.2 Venue adoption state machine

When the merged book shows a `venue_order_id` the local store doesn't track, the adoption matcher looks for a no-vid provisional row submitted within `adoption_grace_s` and matching on **token + side**, with **size within tolerance** (`abs <= 0.5` or `relative <= 1%`) and **price within `0.005`**:

| Decision | Match basis | Outcome |
|----------|-------------|---------|
| `adopted` | strong: token+side+size+price | venue id linked to local row in-place; **non-blocking**. |
| `deferred` | weak: token+side; size or price diverges, still inside grace | **non-blocking** until grace expires. |
| `blocked` | no candidate or grace expired | **blocking** `venue_open_not_tracked_locally`. |

This closed the "REST sees an order before local OMS links it" race that previously fail-closed the bot during normal acks.

### 3.3 WS-terminal tombstones (the inverse race)

When user WS reports an order as terminal (`CANCELLATION` or UPDATE with `remaining<=0`), `WalletStore.user_ws_remove_order(vid)` stamps a tombstone in `_ws_cancel_tombstones`. Subsequent REST snapshots that briefly still show that id are filtered out by `rebuild_open_orders_merged` for `_WS_CANCEL_TOMBSTONE_TTL_S` (600 s).

The `reconcile` fact carries `tombstoned_rest_vids` so operators can distinguish "real venue-only order" from "stale REST resurrection caught by tombstone".

### 3.4 In-flight BUY reservations

The merged `WalletStore.open_orders` view doesn't include an order until user WS or the REST poll catches up — but the venue has already locked collateral. Without compensation, the deployment + capital gates would approve a second BUY that the venue then rejects with "not enough balance / allowance".

`risk.in_flight.derive_in_flight_buy_reservations(orders, wallet)` synthesizes `OpenOrderView` rows from any provisional `LocalOrder` (BUY, `remaining>0`, `limit_price` set) whose `venue_order_id` isn't already in the wallet view. These flow through `RuntimeCoordinator.build_risk_context` into `RiskContext.in_flight_buy_reservations`, where:

- `risk.deployment` adds them to per-token + portfolio reserved USD.
- `risk.capital` subtracts them from effective free balance + allowance.

Releases are implicit (no separate ledger):

| Trigger | Lifecycle hook |
|---------|----------------|
| Venue ack — vid appears in `wallet.open_orders` | dedup-by-vid skips it |
| Venue reject (HTTP 4xx, 425, network) | `release_after_ack` removes the local row |
| Full fill / matched-out via WS UPDATE | `apply_venue_open_order_to_local_orders` removes it |
| UI cancel via venue refresh | `remove_local_resting_by_venue_order_id` removes it |
| Provisional repair drops a stale row | row gone |
| Shadow instant fill | `ack_submit` drops the row |

`risk_decision` and `wallet_sync` facts always carry `in_flight_reserved_usd_total` + `_by_token` so an operator can see what was reserved at the moment of decision.

---

## 4. Health and supervision

`runtime.health_runtime.HealthRuntime` holds:

| Field | Set by |
|-------|--------|
| `reconcile_drift` | `apply_reconcile(res)` from blocking drift flags. |
| `heartbeat_ok` / `clob_session_ok` | `supervised_heartbeat_loop` (`venue.polymarket.clob_heartbeat`). |
| `venue_truth_stale` | `user_ws_staleness_loop` (config-driven thresholds; default 45 s stale, 20 s grace). |
| `venue_truth_inconsistent` | `apply_reconcile(res)` from any drift flags. |
| `user_ws_rest_only` | Set when user WS is disabled (`TYREX_USER_WS_DISABLE=1` or no API creds). |
| `venue_restart_suspected` | Bumped on a 425 `submit`/`cancel`; suppresses `unknown_terminal` auto-resolution. |
| `user_ws_last_msg_ts` | Touched by every user-WS message. |
| `first_v2_sync_complete` | Flipped by the first successful `refresh_wallet_from_clob` in live mode (`cmd_run`, `cmd_live_attest`, `venue_refresh_loop`). Defaults to True for shadow + tests. |

`risk.health.check_aggressive_readiness(ctx, runtime, readiness)` is the single readiness gate: it requires `usdc_balance` set + recent `last_wallet_sync_ts` (when configured) + heartbeat (when live + `require_heartbeat_live`) + user-WS not stale (when live + `require_user_ws_live`) + no `reconcile_drift` + (in live mode) `first_v2_sync_complete = True` (else denies with `bootstrap_not_complete`). Any failure denies with a stable reason code.

**Per-market venue truth (Phase 5):** alongside the wallet/order truths above, `runtime.coordinator.RuntimeCoordinator` owns an optional `market_info_cache: MarketInfoCache | None` (live mode only). It resolves `tick_size`, `min_order_size`, `neg_risk`, `fee_rate_bps`, and `outcomes` per token from `/markets-by-token` + `/clob-markets` + V2 SDK helpers (`venue/polymarket/market_info.py`, TTL = 300 s, fail-closed). The snapshot is plumbed into `RiskContext.market_info`, which `risk.venue_min_size` reads to prefer venue truth over the YAML default and which `execution.order_builder` reads to floor-quantize `limit_price` to the venue tick before submit. Shadow mode passes `None` and the same code paths fall back to YAML defaults.

### 4.1 Background supervisors (`runtime.live_supervisor`)

| Loop | Cadence | Purpose |
|------|---------|---------|
| `supervised_heartbeat_loop` | `TYREX_HEARTBEAT_INTERVAL_S` (clamped to ≥ 5 s) | POST `/v1/heartbeats`; recover server id on 400. |
| `venue_refresh_loop` | `TYREX_VENUE_REFRESH_S` (default = `reconcile_interval_s`) | REST refresh wallet + positions; emit `wallet_sync`; reconcile. |
| `provisional_repair_probe_loop` | adaptive (1, 5, 15 s while provisionals exist) | Faster path to provisional resolution than the main refresh. |
| `user_ws_staleness_loop` | every 1 s | Toggle `venue_truth_stale` on user-WS silence. |

---

## 5. Operator-facing guarantees

- **One writer per wallet** — `SingleWriterOMS` serializes submit/cancel onto one queue.
- **Risk is fail-closed** — every gate returns a stable reason code; missing inputs deny.
- **Reconcile is observable** — every state-machine outcome is in the `reconcile` fact.
- **No silent drops** — terminal resolutions go to `OrderStore.terminal_audit` and the `reconcile` fact.
- **Reproducibility** — USD evidence is quantized to 6 decimals; `wallet_sync` and `reconcile` facts dedup so reports diff cleanly across runs.

---

## 6. Where to look next

- **Reconcile policy summary string:** `state.reconcile.RECONCILE_POLICY_SUMMARY` (also emitted on every `reconcile` fact as `reconcile_policy_summary`).
- **All risk reason codes:** `core/reason_codes.py`.
- **Fact types and payload shapes:** [reporting_fact_model.md](reporting_fact_model.md).
- **Per-policy risk evidence:** [modules/risk/README.md](modules/risk/README.md).
