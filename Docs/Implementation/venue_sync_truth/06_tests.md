# 06 — Test Plan

## Unit-level seams

### 1. `WalletSyncActor` — sync cycle logic

**File:** `tests/unit/test_wallet_sync_actor.py`

| Test case | Setup | Assert |
|-----------|-------|--------|
| **Happy path: discovers 2 new instruments** | Mock Data API returns 2 positions with distinct condition_ids. Mock py-clob returns 1 open order on a 3rd condition_id. Cache has 0 Polymarket instruments. | `WalletSyncResult.instruments_newly_added == 3`. All 3 instruments in cache. `first_sync_complete == True`. |
| **No new instruments (all cached)** | Cache pre-seeded with 3 instruments matching the wallet positions/orders. | `instruments_newly_added == 0`. `first_sync_complete == True`. |
| **Resolution failure for 1 instrument** | Data API returns 2 positions. CLOB `get_market` raises for condition_id #2. | `instruments_newly_added == 1`. `resolution_failures == 1`. `first_sync_complete == True` (partial success does not block startup). |
| **Data API failure** | `fetch_wallet_position_rows` raises `httpx.HTTPError`. | `positions_fetched == 0`. Cycle completes without crashing. `first_sync_complete == True` (HTTP failure does not block indefinitely). |
| **py-clob failure** | `get_orders()` raises. | `orders_fetched == 0`. Positions still processed. |
| **Deduplication across cycles** | Run 2 cycles. Second cycle: same wallet state. | Second cycle: `instruments_newly_added == 0`. No re-resolution. |
| **New instrument appears between cycles** | Cycle 1: 2 positions. Cycle 2: 3 positions (1 new). | Cycle 2: `instruments_newly_added == 1`. |

### 2. `GuruInstrumentDynamicController.resolve_and_activate_by_condition_and_token`

**File:** `tests/unit/test_guru_instrument_dynamic.py` (extend existing)

| Test case | Assert |
|-----------|--------|
| Success: valid condition_id + token_id | Instrument in cache, outcome detail is `""`. |
| Already cached | Returns existing instrument, detail `""`. |
| CLOB API returns error string | `instrument is None`, detail is `"clob_error_string"`. |
| Parse failure | `instrument is None`, detail is `"parse_failed"`. |

### 3. `StartupReadinessGate` — wallet sync clause

**File:** `tests/unit/test_startup_readiness.py` (extend existing)

| Test case | Assert |
|-----------|--------|
| `wallet_sync_ready` is None | No change to existing behavior. |
| `wallet_sync_ready()` returns False | `NOT_READY` with reason `"startup_wallet_sync_pending"`. |
| `wallet_sync_ready()` returns True, other clauses pass | `READY`. |
| `wallet_sync_ready()` returns True, capital gate fails | `NOT_READY` with capital reason (wallet sync does not short-circuit other checks). |

### 4. `NautilusLiveExecutionHealthSource` — wallet sync awareness

**File:** `tests/unit/test_tradable_state_health_risk.py` (extend existing)

| Test case | Assert |
|-----------|--------|
| Reconciliation done, wallet_sync_ready None | `HEALTHY` (existing behavior). |
| Reconciliation done, wallet_sync_ready False | `UNKNOWN_BOOTSTRAP` with reason `"wallet_sync_pending"`. |
| Reconciliation done, wallet_sync_ready True | `HEALTHY`. |
| Reconciliation not done, wallet_sync_ready True | `UNKNOWN_BOOTSTRAP` with existing reason. |

### 5. Config loaders

**File:** `tests/test_split_config_loaders.py` (extend existing)

| Test case | Assert |
|-----------|--------|
| Missing `wallet_sync_enabled` in YAML | Defaults to `True` for live, `False` for shadow. |
| `wallet_sync_enabled: true` with `execution_mode: shadow` | Validation error. |
| `wallet_sync_poll_interval_seconds: 3.0` | Validation error (below floor). |
| `wallet_sync_poll_interval_seconds: 30.0` | Accepted. |

## Integration scenarios

### Scenario 1: Human buys on never-loaded market

**Setup:**
- Bot is running live with `wallet_sync_enabled: true`.
- Mock py-clob `get_orders()` initially returns empty.
- Mock Data API `/positions` initially returns empty.

**Sequence:**
1. Bot starts. Wallet sync completes first cycle (nothing to discover). Readiness gate → READY.
2. Simulate: Data API `/positions` now returns a position on condition_id `NEW_MARKET`.
3. Wait for next wallet sync cycle (≤15s).
4. Assert: `NEW_MARKET` instrument is in cache.
5. Wait for next position check cycle (≤45s).
6. Assert: position is in `Cache.positions_open`. `NautilusDeploymentBudget.filled_polymarket_usd()` includes it.

**Pass criteria:** Deployment budget reflects the human-placed position within `poll_interval + position_check_interval`.

### Scenario 2: Human cancels bot's resting order

**Setup:**
- Bot has a resting order on a cached market.
- `Cache.orders_open` includes the order.

**Sequence:**
1. Simulate: order is canceled on venue (adapter WS delivers cancellation event).
2. Assert: `Cache.orders_open` no longer includes the order. `NautilusDeploymentBudget.pending_polymarket_usd()` decreases.

**Pass criteria:** This should work today (instrument is already cached, WS is subscribed). Test confirms no regression.

### Scenario 3: Cap reopen after manual exit

**Setup:**
- Portfolio deployment cap is $100.
- Bot has $90 deployed (positions + resting orders).
- Bot's risk gate rejects a $15 order (would exceed cap).

**Sequence:**
1. Simulate: human exits $50 of positions on venue.
2. Wait for reconciliation to propagate.
3. Assert: `portfolio_deployment_usd()` returns ~$40.
4. Submit $15 order intent to risk policy.
5. Assert: risk policy approves (40 + 15 = 55 < 100).

**Pass criteria:** Risk gate correctly reopens capacity after venue-side exit.

### Scenario 4: Reconnect with pre-existing wallet state

**Setup:**
- Bot stopped and restarted.
- Wallet has 5 open positions across 5 markets.
- Only 2 are in `polymarket_instrument_ids` (static config).

**Sequence:**
1. Compose runs: warmup seeds 5 instruments (from Data API `/positions`).
2. WalletSyncActor runs first cycle: confirms 5 instruments in cache.
3. Startup reconciliation runs: engine reconciles all 5.
4. Assert: all 5 positions in `Cache.positions_open`.

**Pass criteria:** All wallet positions visible regardless of static config.

### Scenario 5: Instrument not found / dynamic activation edge case

**Setup:**
- Wallet has a position on a market that Gamma API does not return (archived or delisted).
- `resolve_binary_option_for_condition_and_token` fails with `"clob_error_string"`.

**Sequence:**
1. WalletSyncActor tries to resolve. Fails.
2. Assert: failure logged with detail. `first_sync_complete` still becomes True. Other instruments resolved normally.
3. On next cycle: same failure. No crash, no infinite retry storm.

**Pass criteria:** Graceful degradation. Unresolvable instruments are logged and skipped, not fatal.

## What "pass" means

| Category | Criteria |
|----------|----------|
| Unit tests | All assertions pass. No state leaks between tests. |
| Integration tests | Deployment budget values match expected within $0.01. Timing within stated bounds. |
| Regression | All existing tests in `tests/` continue to pass with no modification. |
| Shadow mode | `wallet_sync_enabled: false` (default for shadow) — all existing shadow behavior unchanged. |
| Config backward compat | Existing YAML files without new keys load successfully with sensible defaults. |
