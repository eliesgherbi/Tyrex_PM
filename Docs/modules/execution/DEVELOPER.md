# Developer guide — `tyrex_pm.execution`

[README](README.md) · [Architecture](../../Architecture.md) · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md)

## Responsibility

Translate **risk-approved** `OrderIntent` into **Nautilus `submit_order`** (live) or structured no-op logging (shadow via **`NoOpExecutionPort`**). Owns **instrument resolution**, optional **order-book** checks, **mandatory tick/step quantize**, and **lifecycle** hooks (e.g. limit timeout timers).

## Position in the pipeline

```
CopyStrategy.submit_intent → ExecutionPort
  → NautilusGuruExecutionPort (live) → quantize → submit_order
```

Strategy must not call `submit_order` directly.

## Core components

| File | Role |
|------|------|
| `port.py` | `ExecutionPort` protocol, `NoOpExecutionPort`. |
| `nautilus_guru_exec.py` | **`NautilusGuruExecutionPort`** — main live path, logging, reporting facts. |
| `c3_normalize.py` | **`quantize_limit_order_for_instrument`** — always applied before submit (not optional). |
| `c3_entry_guard.py`, `c3_depth.py`, `c3_book_top.py` | Optional YAML-gated book behavior (`execution_*` in runtime). |

**Naming:** `c3_*` filenames are historical; behavior is **“execution helpers”** — see CONFIG_MODEL `execution_*` keys.

## Data flow (live)

1. Resolve `InstrumentId` / `Cache` state (dynamic instruments, guru token id).
2. Optional **entry guard** (slippage ticks vs guru `price_ref`).
3. Optional **depth clip** to top-of-book size × cap.
4. **Quantize** price/qty to venue grid; if risk qty cannot fit min size → **`exec_instrument_quantize_skip`** (no submit).
5. `submit_order` + reporting (`execution_outcome`, `normalization`); Nautilus **session** events (fills, lifecycle) feed **Tier B** — distinct from **Tier A** wallet snapshots used in **risk** for deployment caps.

## Integration points

- **Strategy:** only calls `submit_intent`; **`on_order_event`** may forward to `notify_order_event` on the port for timeout cancellation.
- **Runtime:** `guru_compose` selects `NoOp` vs `NautilusGuru` and passes `Clock`, `Cache`, reporting emitter, runtime flags.
- **Risk:** execution trusts **approved** qty; quantize may **reduce** further but must not exceed risk intent without skipping (quantize skip path).

## Extension patterns

- Add optional behaviors as **small helpers** + a branch in **`NautilusGuruExecutionPort`**; keep **one** live submit implementation.
- New venue: new `ExecutionPort` implementation — do not fork guru strategy logic.

## Pitfalls

- **Quantize skip is not a risk deny** — it appears in execution facts / `instrument_quantize_skip` rollups in summarize.
- **Book strict mode:** can skip when L2 missing — operators enable intentionally.
- **Do not** reintroduce operator-facing “venue alignment” YAML — size policy is **risk**; grid fit is **always** internal.

## Tests

`tests/unit/test_c3_execution.py`, `tests/test_nautilus_guru_exec.py`.
