# 04 — Event and runtime flow

**Phase:** R3 observe path implemented.

## Event vs signal vs intent vs command

| Kind | Status |
|------|--------|
| Ingress events | `BookSnapshotReceived`, `BookDeltaReceived`, `TickSizeChanged`, `ReferencePriceUpdated` |
| Store view event | `BookUpdated` (complete reconstructed book) |
| Signal | `DirectionalSignal` → `Signal` envelope |
| Observe decision | `WOULD_ENTER_*` / `HOLD` / `SKIP` (not an intent) |
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

Observe evaluations set `causation_id` to the triggering `ReferencePriceUpdated.event_id`. Facts propagate the same `correlation_id` / `run_id`.

## Book path (Option B)

```text
venue book / price_change / tick_size_change
  → adapter normalize (no authoritative book ownership)
  → MarketStateStore apply
       snapshot → replace book, initialized=true
       delta without snapshot → recovery_required
       tick_size_change / reconnect → invalidate until snapshot
  → publish BookUpdated(complete BookSnapshot)
```

Executable VWAP / mid / spread are derived from store books at decision time — not attached to ingress events.

## Freshness

Evaluated at decision time with the injected clock. Not a latched `is_fresh=True` store flag. Distinguishes `UNINITIALIZED`, `STALE`, `FUTURE_TIMESTAMP`, `FRESH`. Thresholds live in `FreshnessConfig`.

## Dispatcher semantics (unchanged from R2)

Priority desc, subscription order; exact-type routing; fail-fast; queued reentrant publish; handler list snapshotted; no venue idempotency.

## Runtime host

One `ObserveHost` composition root for fixture and live modes. Live adapters are swapped in by `run_live_observe`. BTC next-window slug selection is an operations helper (`discover-btc-window` / `next_btc_updown_slug`), not a second strategy loop.

## Modes

`observe` implemented (fixture + public live). `shadow` / `live-tiny` remain R4–R7. Mode must not change indicator/signal math.
