# `market_data/`

Phase 2 **WS-authoritative** market-data primitives: book snapshots, quality gates, executable depth, decision evidence, and market readiness.

Consumed by `ExecutionPlanner`, paired-binary entry/monitor, and survival exit planning when `runtime.market_data.enabled: true`.

## Files

| File | Purpose |
|------|---------|
| `models.py` | `BookLevel`, `BookSource`, `MarketStateSnapshot`, `SourceQuality` |
| `quality.py` | `DataQualityGate`, `DecisionContext`, `QualityVerdict`, profile thresholds |
| `executable_book.py` | `ExecutableBookView`, sweep VWAP, `build_planner_evidence` |
| `decision_snapshot.py` | Material decision snapshots (entry, stop, urgent exit, …) |
| `decision_freshness.py` | Book-age diagnostics for validator / facts |
| `decision_gate.py` | Paired-binary entry blocking on readiness / quality |
| `readiness.py` / `readiness_runtime.py` | Market readiness state machine (`starting` → `trading`, pause on gap) |
| `features.py` | Optional feature builder hooks (Phase 2 M5) |

## DataQualityGate contexts

| Context | Typical use | Strictness |
|---------|-------------|------------|
| `ENTRY`, `ACTIVATION`, `TAKE_PROFIT` | New risk, advisory survival eval | Rejects on spread, depth, reconnect_gap |
| `STOP`, `URGENT_EXIT` | Stop loss, survival enforce retry | Allows `DEGRADED` / `EMERGENCY_ONLY` |
| `SURVIVAL_ENFORCE_EXIT` | Survival OMS submit evidence | Logged on decision snapshots |

Survival **enforce retry** re-plans with `URGENT_EXIT` after a pre-submit `quality_reject` — gates are not blindly loosened; context changes per design.

## WS-primary vs REST

Production target (Phase 2 M8+):

```text
WebSocket  → authoritative live book for decisions
REST       → bootstrap, reconnect resync, recovery exits only (when configured)
```

`MarketStateStore` (`state/market_store.py`) holds per-token books. Population:

- `ingestion/market_stream` — WS updates (primary)
- `runtime/market_data_runtime` — REST bootstrap / fixture inject (shadow)

Readiness transitions (`market_readiness_transition` facts) pause trading on `reconnect_gap` until fresh WS book.

## Boundaries

- Pure evaluation — no OMS, no strategy state mutation
- Gate config loaded via `runtime.config` → `build_quality_gate_from_config` in `execution/planner.py`

See [Implementation/WebSocket_event_driven_backbone/phase_2.md](../../Implementation/WebSocket_event_driven_backbone/phase_2.md).
