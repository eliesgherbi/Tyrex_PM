# Test strategy

## Unit tests

| Area | File(s) | Focus |
|------|---------|--------|
| `VenueState` | New `tests/unit/test_venue_state.py` | TTL, staleness, `refresh` timeout → **`venue_state`** fact with `status=refresh_timeout`, thread-safe apply/read, `apply_clob_balance` → **`venue_state_cash_ready`**, CLOB poll interval default **10 s** / validation **floor 3.0**. |
| **Feature flag** | `tests/test_state_readers.py` (and deployment tests) | **`venue_state_reads_enabled=false`:** `list_open_orders` / account snapshot / position exposure match **cache/portfolio** paths (`state_readers.py` 247–251, 308–311, 436). **`true`:** same methods read **`VenueState`** mocks — **no** direct `VenueState` import outside boundary. |
| **Cost basis** | `tests/test_deployment_budget.py` | When flag **true**, filled USD = **venue size × mark**; **missing mark** → **0.5** multiplier + assert **`venue_state_missing_mark`** emitted (or fact_emit mock). **Replaces** `position_entry_deployment_usd` Nautilus path for that mode (`deployment_budget.py` 47–57). |
| `layer_a_context` | New or extend tests | With flag **true**, `follower_long_qty` from venue size; **false** from `portfolio.net_position`. |
| `bot_sell_validate` | `tests/unit/test_bot_sell_validate_strategy.py` | Same flag behavior for long inventory. |
| `loaders` | `tests/test_split_config_loaders.py` | `venue_state_reads_enabled` default **false**; `venue_state_cash_poll_interval_seconds` default **10.0**, reject **< 3.0**; flag **removed** after Step 5 — delete tests for key. |

## Integration tests

| Scenario | Method |
|----------|--------|
| WalletSync → VenueState | `tests/unit/test_wallet_sync_actor.py` — after mock cycle, `VenueState.positions()` matches injected HTTP rows. |
| Capital merge | `DefaultCapitalStateProvider` with flag **true** / **false** — account snapshot source matches architecture (`capital/provider.py`). |
| **Two-gate readiness** | New tests for `StartupReadinessGate` (or health adapter): **READY** only if **`wallet_sync_first_sync_complete`** **and** **`venue_state_cash_ready`**; failing either leaves **not ready** (exact reason codes in implementation). |

## Live / staging validation

| Check | Signal |
|-------|--------|
| Flag **false** | `venue_state` heartbeats; Tier A behavior matches historical baselines. |
| Flag **true** | `venue_state.status` mostly `ok`; `venue_state_missing_mark` rate acceptable; caps vs UI spot-check. |
| Readiness | Logs / health show both gates **true** before declaring tradable (per ops runbook). |

## Tests to delete or rewrite (Step 5)

- `tests/unit/test_position_reconciliation.py` — remove with reconciliation code.
- **Feature-flag tests** that assert `venue_state_reads_enabled` routing — **replace** with “Tier A always VenueState” assertions (or delete branch coverage).

## What not to automate (charter)

- Full **PnL** parity across external activity.
- **Backtesting** with `VenueState`.
