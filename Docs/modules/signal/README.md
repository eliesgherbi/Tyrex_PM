# Module: `tyrex_pm.signal`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

**Pure decision and sizing** helpers: given a `GuruTradeSignal`, answer “take or skip?” and “what quantity?” without Nautilus, HTTP, or venue calls. **Per-order** minimum/maximum **USD deploy** (too small / too big / clip / bump) is **`risk/`**, not `signal/`.

## B. Boundaries

**Belongs here:** Entry policy, exit policy, sizing policies (proportional + optional **C2** conviction-weighted), token filter spec, structured `SignalDecision` + reason codes.

**Does not belong here:** Subscribing to the message bus, calling risk, calling execution, or reading YAML.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `token_filter_spec.py` | **`TokenFilterSpec`** — explicit `enabled` + `allowlisted`; `allows_token()` used by entry/exit. |
| `entry.py` | `GuruFollowEntryPolicy`, **`GuruMirrorExitPolicy`**, `SignalDecision`. |
| `sizing.py` | **`SizingPolicy`** protocol + **`build_sizing_policy`**: proportional `copy_scale`, optional **C2** conviction weighting + rolling average + `record_accepted_entry_size` / diagnostics. |
| `__init__.py` | Public exports for policies. |

## D. Main interactions

- **strategy:** `CopyStrategy` constructs policies and calls `evaluate` → `size` → `record_accepted_entry_size` (entries) → **`OrderIntent`** → **`RiskPolicy.evaluate`** (may adjust qty).

## E. Status

**Implemented** for v1 copy follow + mirror exit + proportional sizing + **C2** conviction (optional). See **`Implementation/plan_C2_Capital-Allocation.md`**.

## F. Extension guidance

- Add policy classes with **deterministic** inputs/outputs; return clear skip reasons for `copy_skip` logs.
- Unit test under `tests/unit/` without `TradingNode`.
- Do not import `tyrex_pm.strategy` or `nautilus_trader` from this package.
