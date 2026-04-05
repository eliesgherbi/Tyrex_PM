# Module: `tyrex_pm.signal`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

**Pure decision and sizing** helpers: given a `GuruTradeSignal`, answer “take or skip?” and “what quantity?” without Nautilus, HTTP, or venue calls.

## B. Boundaries

**Belongs here:** Entry policy, exit policy, sizing policies (proportional + optional **C2** conviction-weighted), follow-worthiness gate (min follow notional), structured `SignalDecision` + reason codes.

**Does not belong here:** Subscribing to the message bus, calling risk, calling execution, or reading YAML.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `token_filter_spec.py` | **`TokenFilterSpec`** — explicit `enabled` + `allowlisted`; `allows_token()` used by entry/exit. |
| `entry.py` | `GuruFollowEntryPolicy`, **`GuruMirrorExitPolicy`**, `SignalDecision`. |
| `sizing.py` | **`SizingPolicy`** protocol + **`build_sizing_policy`**: proportional `copy_scale`, optional **C2** conviction weighting + rolling average + `record_accepted_entry_size` / diagnostics. |
| `follow_worthiness.py` | **`FollowWorthinessGate`** — **C2** min-follow-notional check (policy, pre–risk). |
| `__init__.py` | Public exports for policies. |

## D. Main interactions

- **strategy:** `CopyStrategy` constructs policies and calls `evaluate` → `size` → `record_accepted_entry_size` (entries) → **`FollowWorthinessGate.evaluate`** → `OrderIntent`.
- **core:** responses reference `ReasonCode`; inputs are `GuruTradeSignal`.

## E. Status

**Implemented** for v1 copy follow + mirror exit + proportional sizing + **C2** (feature-flagged conviction sizing + min-follow-notional gate). See **`Implementation/plan_C2_Capital-Allocation.md`**.

## F. Extension guidance

- Add policy classes with **deterministic** inputs/outputs; return clear skip reasons for `copy_skip` logs.
- Unit test under `tests/unit/` without `TradingNode`.
- Do not import `tyrex_pm.strategy` or `nautilus_trader` from this package.
