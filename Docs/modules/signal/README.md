# Module: `tyrex_pm.signal`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md) · **[DEVELOPER.md](DEVELOPER.md)**

## A. Role

**Pure decision and sizing** helpers: given a `GuruTradeSignal`, answer “take or skip?” and “what quantity?” without Nautilus, HTTP, or venue calls. **Per-order** minimum/maximum **USD deploy** (too small / too big / clip / bump) is **`risk/`**, not `signal/`.

## B. Boundaries

**Belongs here:** **`layer_a/`** — composable guru signal filters (gating + exit interpretation); sizing policies (proportional + optional conviction-weighted); token filter spec; legacy **`entry.py`** policies (`SignalDecision`) for reuse/tests.

**Does not belong here:** Subscribing to the message bus, calling risk, calling execution, or reading YAML.

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `token_filter_spec.py` | **`TokenFilterSpec`** — explicit `enabled` + `allowlisted`; `allows_token()` used by entry/exit. |
| `layer_a/` | **`LayerAOrchestrator`**, gating + **`ExitInterpretationFilter`**; wired from strategy YAML **`filters:`** (see `CONFIG_MODEL.md`). **`full_exit`** long-qty context is implemented by **`runtime/layer_a_context.NautilusLayerAContext`** — **Tier A** **`VenueState`** when composed, else **`Portfolio`**. |
| `entry.py` | `GuruFollowEntryPolicy`, **`GuruMirrorExitPolicy`**, `SignalDecision`. |
| `sizing.py` | **`SizingPolicy`** protocol + **`build_sizing_policy`**: proportional `copy_scale`, optional conviction weighting + rolling average + `record_accepted_entry_size` / diagnostics. |
| `__init__.py` | Public exports for policies. |

## D. Main interactions

- **strategy:** `CopyStrategy` runs **`LayerAOrchestrator`** (token + optional static/median + exit interpretation), then `size` → `record_accepted_entry_size` (entries) → **`OrderIntent`** → **`RiskPolicy.evaluate`** (may adjust qty).

## E. Status

**Implemented** for v1 copy follow + mirror exit + proportional sizing + optional conviction. See **[DEVELOPER.md](DEVELOPER.md)**.

## F. Extension guidance

- Add policy classes with **deterministic** inputs/outputs; return clear skip reasons for `copy_skip` logs.
- Unit test under `tests/unit/` without `TradingNode`.
- Do not import `tyrex_pm.strategy` or `nautilus_trader` from this package.
