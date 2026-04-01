# Module: `tyrex_pm.reporting`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

Placeholder for **run reports**, post-trade analytics, and exports — **not** used on the hot copy path today.

## B. Boundaries

**Will belong here:** Aggregations over fills, PnL snapshots, structured exports.

**Does not belong here:** Real-time order submission, guru polling, or `CopyStrategy` internals.

## C. Internal structure

`__init__.py` — package stub (“Run reports (v1.11)” in code comment).

## D. Main interactions

None yet; future consumers might log bus events or read external stores.

## E. Status

**Placeholder only.**

## F. Extension guidance

Implement reporting **off the critical path** (separate process or async worker) to avoid adding latency to `submit_intent`. Prefer reading **immutable event logs** or **DB** rather than importing strategy private methods.
