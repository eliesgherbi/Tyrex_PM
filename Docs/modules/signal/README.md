# Module: `tyrex_pm.signal`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

**Pure decision and sizing** helpers: given a `GuruTradeSignal`, answer “take or skip?” and “what quantity?” without Nautilus, HTTP, or venue calls.

## B. Boundaries

**Belongs here:** Entry policy, exit policy, proportional sizing, structured `SignalDecision` + reason codes.

**Does not belong here:** Subscribing to the message bus, calling risk, calling execution, or reading YAML.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `entry.py` | `GuruFollowEntryPolicy`, **`GuruMirrorExitPolicy`**, `SignalDecision`. |
| `sizing.py` | `ProportionalSizingPolicy` (`copy_scale`). |
| `__init__.py` | Public exports for policies. |

## D. Main interactions

- **strategy:** `CopyStrategy` constructs policies and calls `evaluate` / `size` in `_handle_branch`.
- **core:** responses reference `ReasonCode`; inputs are `GuruTradeSignal`.

## E. Status

**Implemented** for v1 copy follow + mirror exit + proportional sizing.

## F. Extension guidance

- Add policy classes with **deterministic** inputs/outputs; return clear skip reasons for `copy_skip` logs.
- Unit test under `tests/unit/` without `TradingNode`.
- Do not import `tyrex_pm.strategy` or `nautilus_trader` from this package.
