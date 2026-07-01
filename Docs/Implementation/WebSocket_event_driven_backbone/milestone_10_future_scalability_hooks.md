# Milestone 10 — Future Scalability Hooks (Documentation Only)

## Objective

Document **interfaces and extension points** for future strategy and signal work **without implementing** them in Phase 2.

## Why this milestone exists

Phase 2 delivers FeatureBuilder **v0**, WS-authoritative backbone, and M9 measurement. Teams need a clear map for v1/v2 features without scope creep during M0–M9.

## Current codebase state

After Phase 2: FeatureBuilder v0, DataQualityGate, MarketStateStore v2, ExecutableBookView, decision snapshots, WS-primary path.

## Target behavior

**Documentation-only.** No production code for listed features.

## Extension hooks (documented, not built)

### FeatureBuilder v1 / v2

Build on `BasicFeatureSnapshot` / `MarketStateSnapshot`:

```text
OBI, microprice, realized volatility, BTC signed delta, aggressor flow
Hook: src/tyrex_pm/market_data/features.py — extend with v1 module
NOT Phase 2 (v0 only implemented)
```

### External price feeds (BTC / RTDS)

```text
ExternalPriceStore; market_profiles.*.require_external_price: true
Quality gate extension — NOT Phase 2
```

### Trade / aggressor-flow models

```text
RawTradeEvent, NormalizedTradeEvent
NOT Phase 2
```

### Single-leg A/B, advanced survivor, ML

```text
NOT Phase 2 — only after M9 verdict
```

### Full raw-event replay

```text
Rotate compressed WS logs; replay to store
Phase 2: decision_snapshot + snapshot_id reference only
```

## Files likely touched

- This document
- Cross-links in [phase_2.md](phase_2.md)

## Contracts / interfaces (future sketch)

```python
# FUTURE v1 — NOT Phase 2
@dataclass(frozen=True)
class FeatureVectorV1(BasicFeatureSnapshot):
    obi: Decimal | None = None
    microprice: Decimal | None = None
    btc_signed_delta: Decimal | None = None
    volatility_30s: Decimal | None = None
```

## Config changes (future keys only)

```yaml
runtime:
  market_data:
    external_feeds:
      rtds_btc:
        enabled: false
  features:
    v1_enabled: false
    obi_enabled: false
```

## Acceptance criteria

- [ ] Each future capability has hook target path
- [ ] Explicit "NOT Phase 2" on each item
- [ ] No implementation PRs without M9 completion

## Definition of done

Doc reviewed; linked from phase_2.md; team agrees Phase 2 ends at **M9**.

## Not in scope

All listed extensions — implementation deferred.
