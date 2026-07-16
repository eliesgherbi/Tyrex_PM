# 08 — Validation report

## R1

| Check | Result |
|-------|--------|
| Checkpoint commit | `630bac2acf67961a30b4be014d1df0434af967f1` |
| Message | `reset project with isolated legacy tree and minimal skeleton` |
| `.env` / `var/` in commit | Absent |
| Tests at R1 | 10 passed |

## R2

| Check | Result |
|-------|--------|
| Packages | `tyrex_pm.core`, `tyrex_pm.engine` |
| Events | `BookUpdated`, `ReferencePriceUpdated`, `TimerElapsed` |
| Dispatcher | Priority+order, exact-type, fail-fast, queued reentry |
| Pytest | **31 passed** (includes R1 + R2) |
| NautilusTrader | Absent |
| Intents / OMS / adapters | Not implemented (by design) |
| R3 started | **No** |

### Files created (R2)

```text
src/tyrex_pm/core/
  __init__.py
  ids.py
  clock.py
  numerics.py
  instruments.py
  snapshots.py
  events.py
  indicators.py
  signals.py
  facts.py
src/tyrex_pm/engine/
  __init__.py
  dispatcher.py
tests/
  test_core_clock_ids.py
  test_core_events_snapshots.py
  test_core_envelopes.py
  test_event_dispatcher.py
  test_r2_architecture.py
```

### Rejected alternatives

- Delta book events in R2 (deferred).
- Base-type dispatcher fanout (no consumer).
- Immediate recursive reentrant publish (harder to reason about).
- Intent type hierarchy (R4).
- Venue tick rounding in core (adapter/execution later).
- Z-Gap TimeAuthority (Z1).
