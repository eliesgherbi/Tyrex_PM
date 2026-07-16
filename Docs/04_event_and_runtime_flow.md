# 04 — Event and runtime flow

**Phase:** R2 implemented semantics below.

## Event vs signal vs intent vs command

| Kind | R2 status |
|------|-----------|
| Event | Implemented (`BookUpdated`, `ReferencePriceUpdated`, `TimerElapsed`) |
| Signal | Envelope only (`Signal`) |
| Intent | **Deferred to R4** |
| Command / plan | **Deferred to R4–R5** |

## Causality metadata

| Field | Meaning |
|-------|---------|
| `event_id` | Unique identity of this event |
| `correlation_id` | Broader decision/operation chain |
| `causation_id` | Direct cause; **None** for root external events |
| `ts_event` | Source occurrence time (UTC aware) |
| `ts_received` | Local ingestion/creation time (UTC aware) |
| `source` | `EventSource` enum |

Derived events keep `correlation_id` and set `causation_id` to the parent `event_id`.

## BookUpdated payload choice

**Complete normalized `BookSnapshot`**, not deltas.

Trade-off: simpler and safer for the first read-only path; no pretend sequence/gap recovery. Separate delta events may be added later if adapters need them. Executable VWAP is **not** on the event — derived in R3 market-data.

## Dispatcher semantics

| Topic | Decision |
|-------|----------|
| Ordering | Priority desc, then subscription order |
| Routing | **Exact type only** (no base-type fanout) |
| Failure | Fail-fast `DispatchError`; stop remaining handlers |
| Reentrant publish | Queue until current publication finishes |
| Subscribe during publish | Snapshot handlers; mutations apply next event |
| Duplicate callable | Rejected |
| No subscribers | Valid; `delivered=0` |
| Idempotency | **Not** in dispatcher — venue stores later (R5–R6) |

## Modes

observe / shadow / live-tiny remain application concerns (R3+). Mode must not change indicator/signal math.
