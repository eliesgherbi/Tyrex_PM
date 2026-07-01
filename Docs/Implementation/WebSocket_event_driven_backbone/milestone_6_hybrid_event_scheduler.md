# Milestone 6 — Hybrid Event Scheduler

## Objective

Add **event-driven wake-ups** to the existing paired-binary loop with **debounce**, **non-reentrancy**, and **user WS wake** — without a full pub/sub rewrite.

## Why this milestone exists

Poll loop adds latency on top of stale REST/WS books. WS updates should wake the strategy immediately while preserving saga timeouts, max_runtime, and health timers.

## Current codebase state

**Implemented (Group C / C1):** `MarketUpdateCoordinator` in `runtime/market_update_coordinator.py`; `paired_binary_run.py` hybrid wait loop with `tick_lock`; authoritative `MarketStateStore.set_on_token_update` callback; user fill + sellability wake; facts `paired_binary_tick_source` + `decision_coalesce_count`.

**Still REST-authoritative:** Store notifications fire from REST bootstrap/refresh on `coord.market_state` until M8. Shadow WS store does not register with coordinator.

## Target behavior

```text
MarketStateStore.apply_* (authoritative) → MarketUpdateCoordinator.notify_token_update
User WS fill / sellability change → notify_pair_update

Coordinator debounces (max_decision_rate_per_market_ms: 75 for crypto_5m)
paired_binary_run:
  async with pair_tick_lock:   # never concurrent ticks per pair
    await wait(event, timeout=poll_interval)
    tick() using latest state only

Rules:
  coalesce market updates during debounce window
  always evaluate latest state
  drop old decision opportunities, not old state
  never run two paired_binary ticks concurrently for the same pair
  wake on market WS updates AND relevant user WS fill/sellability updates
  timer still fires for timeouts and health (even without WS events)
```

Do **not** run strategy on every WebSocket message.

## Files likely touched

- `src/tyrex_pm/runtime/paired_binary_run.py` — hybrid wait + tick lock
- `src/tyrex_pm/state/market_store.py` — optional `on_update` callback (authoritative only)
- `src/tyrex_pm/runtime/market_data_runtime.py` — wire callback
- `src/tyrex_pm/runtime/allocation_runtime.py` — sellability wake
- `src/tyrex_pm/ingestion/user_stream.py` — notify on fill events

## New files likely created

- `src/tyrex_pm/runtime/market_update_coordinator.py`
- `tests/test_market_update_coordinator.py`
- `tests/test_paired_binary_event_wake.py`
- `tests/test_tick_non_reentrancy.py`

## Contracts / interfaces

```python
class MarketUpdateCoordinator:
    def notify_token_update(self, token_id: TokenId) -> None: ...
    def notify_user_fill(self, token_id: TokenId) -> None: ...
    def notify_sellability_change(self, token_id: TokenId) -> None: ...
    async def wait_for_update(
        self, token_ids: list[TokenId], *, timeout_s: float
    ) -> Literal["event_wake", "timer"]: ...
```

## Config changes

```yaml
runtime:
  paired_binary:
    poll_interval_s: 1.0
    max_decision_rate_per_market_ms: 75   # initial crypto_5m value
```

## Facts / observability changes

- `paired_binary_tick_source` — `event_wake` | `timer`
- `decision_coalesce_count` — WS messages coalesced per tick

## Tests to add or update

- **Burst of 10 WS updates → one decision tick after debounce**
- **Concurrent tick prevented by lock/guard**
- **Timer tick still happens without WS events**
- **User fill event wakes activation/sellability logic**
- Event wake reduces synthetic stop-plan latency vs poll-only

## Acceptance criteria

- [x] Burst of 10 store updates → one decision tick after debounce (`test_market_update_coordinator.py`)
- [x] Concurrent tick prevented by lock/guard (`test_tick_non_reentrancy.py`)
- [x] Timer tick still happens without market events (`test_market_update_coordinator.py`)
- [x] User fill event wakes coordinator (`notify_coordinator_user_fill` wired; coordinator unit tests)
- [ ] Event wake reduces synthetic stop-plan latency vs poll-only (deferred to M9 live benchmark)
- [ ] Live run shows mix of `event_wake` and `timer` facts (requires post-merge live run)
- [ ] CPU bounded under message burst (requires load test)

## Risks

- Debounce 75ms may miss sub-75ms opportunities — acceptable for Phase 2; tune in M9 if needed
- Callback coupling — inject coordinator; do not hard-code in store

## Open questions

None — debounce default fixed at 75ms for crypto_5m.

## Definition of done

Coordinator integrated; non-reentrancy enforced; user WS wake wired; unit/integration tests pass. Live mix of `event_wake`/`timer` facts validated before M8 sign-off.

## Not in scope

- Full pub/sub bus
- Replacing poll loop entirely
- Multi-strategy scheduler
- Shadow store notifications (authoritative store only)
