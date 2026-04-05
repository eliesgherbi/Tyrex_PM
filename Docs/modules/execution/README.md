# Module: `tyrex_pm.execution`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md) · **[Current state](../../Implementation/current_state.md)**

## A. Role

Translate approved **`OrderIntent`** into **venue actions** (or deliberate no-ops). Keeps **`py-clob-client`** and Nautilus **`submit_order`** usage **out of** `strategy/`.

## B. Boundaries

**Belongs here:** `ExecutionPort` protocol, `NoOpExecutionPort`, **`PolymarketExecutionPolicy`**, **`NautilusGuruExecutionPort`**.

**Does not belong here:** Whether to copy a guru trade (strategy + signal), or static notional limits (risk **`ConfiguredRiskPolicy`**).

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `port.py` | `ExecutionPort` protocol, `NoOpExecutionPort`. |
| `polymarket_policy.py` | **`PolymarketExecutionPolicy`** — LIMIT via **`create_and_post_order`**, min BUY notional env, optional **`on_submit_ok`** → **`note_fill_assumption`**. |
| `c3_normalize.py`, `c3_entry_guard.py`, `c3_depth.py`, `c3_book_top.py` | **C3** helpers (tick/size feasibility, slippage vs reference, depth clip, book access) — used from **`nautilus_guru_exec`** only. |
| `nautilus_guru_exec.py` | **`NautilusGuruExecutionPort`** — **`order_factory.limit`** + **`submit_order`**; dynamic/static instrument resolve; structured **`ReasonCode`** logging; optional **C3** (compose of helpers above + limit timeout via `notify_order_event`). |
| `__init__.py` | Exports. |

## D. Main interactions

- **strategy:** `CopyStrategy.submit_intent` → port.
- **runtime:** `guru_compose` selects shadow vs legacy live vs **framework** live per **`RuntimeSettings`**.
- **risk:** only legacy path uses **`on_submit_ok`** for session **`_token_open`**.

## E. Status

**Two live guru submit paths:** choose **one** per deployment via **`polymarket_framework_submit`** (requires Nautilus live). Framework path aligns guru orders with **`Cache`** for pending exposure in risk and is the **only** path with **C3** execution-quality behavior. Legacy py-clob policy is unchanged for C3.

## F. Extension guidance

- New venues: new `ExecutionPort` implementation; inject with `set_execution_port`.
- Prefer idempotency and structured errors; surface **`ReasonCode.LIVE_ORDER_*`** / guru codes in logs.
- Do not import `CopyStrategy` from execution code. If adding timer/lifecycle hooks, expose **`notify_order_event`** and document that **`CopyStrategy`** forwards `OrderEvent` when present.
