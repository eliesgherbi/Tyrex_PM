# Module: `tyrex_pm.execution`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md) · **[Current state](../../Implementation/current_state.md)** · **[DEVELOPER.md](DEVELOPER.md)**

## A. Role

Translate approved **`OrderIntent`** into **venue actions** (or deliberate no-ops). Keeps Nautilus **`submit_order`** usage **out of** `strategy/` (live). **`py-clob-client`** remains in **`runtime/`** for allowance snapshots and dynamic instrument resolution — not for guru order submit.

## B. Boundaries

**Belongs here:** `ExecutionPort` protocol, `NoOpExecutionPort`, **`NautilusGuruExecutionPort`**.

**Does not belong here:** Whether to copy a guru trade (strategy + signal), or static notional limits (risk **`ConfiguredRiskPolicy`**).

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `port.py` | `ExecutionPort` protocol, `NoOpExecutionPort`. |
| `c3_normalize.py`, `c3_entry_guard.py`, `c3_depth.py`, `c3_book_top.py` | Book / grid helpers — **`c3_normalize`** applies mandatory tick/step quantize (not a runtime YAML knob). |
| `nautilus_guru_exec.py` | **`NautilusGuruExecutionPort`** — resolve instrument, optional book hooks (`execution_*` YAML), internal quantize, **`submit_order`**; structured **`ReasonCode`** logging; limit timeout via `notify_order_event`. |
| `__init__.py` | Exports. |

## D. Main interactions

- **strategy:** `CopyStrategy.submit_intent` → port.
- **runtime:** `guru_compose` selects shadow vs live (**Nautilus** port only for live).

## E. Status

**Single live guru path:** **`NautilusGuruExecutionPort`** — submits through Nautilus (**Tier B** session). **Risk** deployment caps use **`VenueState`**-backed readers when live + wallet sync (**Tier A**); execution does not reimplement that math. Optional book hooks from runtime YAML; always instrument grid quantize before submit.

## F. Extension guidance

Prefer extending **`NautilusGuruExecutionPort`** (or small helpers) over adding duplicate submit mechanisms; keep **`CopyStrategy`** thin.

