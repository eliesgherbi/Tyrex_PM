# `venue/`

Adapter layer that translates between Tyrex's canonical types and Polymarket's wire formats. Today there is one implementation: `venue/polymarket/`.

## `polymarket/`

| File | Role |
|------|------|
| `clob_bridge.py` | `PyClobBridge` — wraps `py-clob-client` (sync) behind `asyncio.to_thread`. Provides `submit_order`, `cancel_order`, `get_open_orders`, `get_balance_allowance`, etc. + `parse_venue_order_id` |
| `clob_env.py` | `try_create_clob_client(...)` — builds a `ClobClient` from env (`TYREX_*` / `POLYMARKET_*` aliases), derives API creds if not provided, resolves proxy/funder + signature type. Also `resolve_positions_wallet_address` for the data-api positions URL |
| `clob_execution.py` | Submit/cancel implementations (called from `LiveOMS`) |
| `clob_wallet_sync.py` | `refresh_wallet_from_clob(wallet, client)` — REST refresh of open orders + balance/allowance |
| `clob_heartbeat.py` | Heartbeat client + `post_heartbeat_with_recovery` (handles server-id rotation that briefly returns 400) |
| `heartbeat.py` | Pure heartbeat helpers (used by `clob_heartbeat.py`) |
| `data_api_client.py` | `DataApiClient` — async `httpx` client for `data-api/activity` (guru polling) and `data-api/positions` |
| `gamma_client.py` | `GammaClient` — `is_token_tradeable(...)` for the optional pre-submit market gate |
| `market_ws.py` | Market WebSocket subscriber |
| `user_ws.py` | User WebSocket subscriber — emits the order/trade events that drive `WalletStore` and `OrderStore` |
| `normalizers.py` | `normalize_data_api_activity_row(...)` and other wire-to-canonical converters |
| `auth.py` | API-credential derivation + signing helpers |
| `rate_limits.py` | Local rate-limit constants (Polymarket-published values) |
| `positions_sync.py` | `refresh_positions_from_data_api(...)` — REST safety net that replaces `WalletStore.positions` to bridge WS-trade gaps |

## What this layer does

- Wraps every blocking `py-clob-client` call so `asyncio` loops aren't blocked.
- Normalizes wire dicts into `core/models.py` dataclasses (`OpenOrderView`, `WalletPosition`, `TradeFillRecord`, `GuruTradeSignal`).
- Translates HTTP errors / WS reconnect events into `HealthRuntime` flags (heartbeat ok, venue-restart-suspected on HTTP 425, user-ws-stale).

## What this layer does NOT do

- No risk decisions.
- No strategy logic.
- No mutation of `WalletStore` / `OrderStore` from inside HTTP/WS handler coroutines except the *single-writer* path documented in the relevant ingestion module (e.g. `ingestion/user_stream` is the one writer for user-WS).

## Adding a new venue

Mirror this folder under `venue/<name>/`. Provide:

- A bridge equivalent to `PyClobBridge` (or a thin async client if the SDK is async).
- An execution module that implements `OMSBackend.submit / cancel`.
- A wallet-sync module that updates `WalletStore` from venue REST.
- A heartbeat / WS module that flips `HealthRuntime` flags.
- A `normalizers.py` that maps wire types to `core/models.py`.

Wire your new venue into `runtime/app.py::cmd_run` next to the Polymarket branch. The OMS contract (`OMSBackend`) is intentionally tiny so the rest of the stack does not change.
