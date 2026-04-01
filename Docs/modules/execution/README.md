# Module: `tyrex_pm.execution`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

Translate approved **`OrderIntent`** into **venue actions** (or deliberate no-ops). Keeps `py-clob-client` and order semantics **out of** `strategy/`.

## B. Boundaries

**Belongs here:** `ExecutionPort` protocol, `NoOpExecutionPort`, `PolymarketExecutionPolicy`.

**Does not belong here:** Whether to copy a guru trade (strategy + signal), or notional limits (risk).

## C. Internal structure (implemented)

| File | Contents |
|------|----------|
| `port.py` | `ExecutionPort` protocol, `NoOpExecutionPort` (records intents; shadow). |
| `polymarket_policy.py` | `PolymarketExecutionPolicy` — LIMIT via `create_and_post_order`, min BUY notional env, structured logging, optional `on_submit_ok` callback. |
| `__init__.py` | Exports. |

## D. Main interactions

- **strategy:** `CopyStrategy.submit_intent` → port (`submit_intent(..., mode=execution_mode)`).
- **runtime:** `guru_compose` selects shadow vs live port; live builds `ClobClient` via `runtime/clob_factory.py`.
- **risk:** callback after successful submit for exposure note.

## E. Status

**Live path** uses synchronous HTTP per intent — documented latency in `polymarket_policy.py` docstring.

## F. Extension guidance

- New venues: add a class implementing `ExecutionPort`; inject with `set_execution_port`.
- Prefer **idempotency** and structured errors at this layer; surface `ReasonCode.LIVE_ORDER_*` in logs.
- Do not import `CopyStrategy` from execution code.
