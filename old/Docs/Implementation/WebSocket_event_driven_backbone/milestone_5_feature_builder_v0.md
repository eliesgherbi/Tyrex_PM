# Milestone 5 — FeatureBuilder v0

## Objective

Implement **FeatureBuilder v0** — a minimal information-preparation layer that standardizes basic book-derived fields for quality, planner evidence, and reporting. **No strategy decision changes in Phase 2.**

## Why this milestone exists

Phase 2 must prove the event-driven backbone delivers the information future strategy work needs, without adding OBI, BTC, or ML. v0 unifies spread/mid/depth/effective-price fields across decision snapshots and prepares modularity for FeatureBuilder v1/v2 later.

## Purpose (explicit)

```text
1. Prove the backbone provides information needed for future strategy enhancements
2. Standardize basic book-derived facts for quality, planner, and reporting
3. Prepare modularity for v1/v2 without implementing advanced signals in Phase 2
```

**NOT in FeatureBuilder v0:**

```text
OBI, microprice, BTC/RTDS, volatility windows, aggressor flow,
ML features, advanced signal scoring, new strategy logic
```

## Current codebase state

No `FeatureBuilder` module. Spread/mid computed ad hoc in store and planner paths.

## Target behavior

```python
features = FeatureBuilder.build(
    snap: MarketStateSnapshot,
    *,
    size: Decimal,
    quality_report: DataQualityReport | None = None,
    executable_buy: ExecutableBookView | None = None,
    executable_sell: ExecutableBookView | None = None,
) -> BasicFeatureSnapshot

pair_features = FeatureBuilder.build_pair(
    pair: PairMarketSnapshot,
    *,
    size: Decimal,
    quality_report: DataQualityReport | None = None,
    ...
) -> BasicFeatureSnapshot  # includes pair_snapshot_id
```

Consumed by M7 `decision_snapshot` facts — **not** by strategy entry/stop logic in Phase 2.

## Files likely touched

- `src/tyrex_pm/strategies/paired_binary/facts.py` — include features in decision_snapshot
- `src/tyrex_pm/market_data/decision_snapshot.py` ([M7](milestone_7_latency_facts_and_speed_assessment.md))
- `src/tyrex_pm/reporting/schema_v2.py`

## New files likely created

- `src/tyrex_pm/market_data/features.py` — `FeatureBuilder`, `BasicFeatureSnapshot`
- `tests/test_feature_builder_v0.py`

## Contracts / interfaces

### Inputs

```text
MarketStateSnapshot
PairMarketSnapshot (optional, for pair_snapshot_id)
ExecutableBookView (buy/sell where relevant)
DataQualityReport (for quality_verdict reference)
configured size (for depth_at_size)
```

### Output: `BasicFeatureSnapshot`

```python
@dataclass(frozen=True)
class BasicFeatureSnapshot:
    snapshot_id: str
    pair_snapshot_id: str | None
    source: BookSource
    book_age_ms: int
    spread: Decimal | None
    mid: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    best_bid_size: Decimal | None
    best_ask_size: Decimal | None
    depth_at_size: Decimal | None
    effective_bid_at_size: Decimal | None   # from ExecutableBookView sell-side walk
    effective_ask_at_size: Decimal | None   # from ExecutableBookView buy-side walk
    sweep_vwap_buy: Decimal | None
    sweep_vwap_sell: Decimal | None
    quality_verdict: QualityVerdict | None  # reference from report
    missing_fields: tuple[str, ...]          # explicit null reasons
```

## Config changes

```yaml
runtime:
  market_data:
    features:
      v0_enabled: true
      depth_size_from_strategy: true   # use position_size from paired_binary config
```

## Facts / observability changes

- `decision_snapshot.features` — serialized `BasicFeatureSnapshot` (M7)
- No separate strategy facts — features ride on decision_snapshot

## Tests to add or update

- Deterministic features from fixed snapshot fixture
- Missing bid/ask → explicit null fields + `missing_fields` reason
- `snapshot_id` matches decision/planner evidence `snapshot_id`
- **No** OBI/microprice/BTC/volatility fields on `BasicFeatureSnapshot`
- Pair build sets `pair_snapshot_id`

## Acceptance criteria

- [ ] FeatureBuilder v0 used in `decision_snapshot` facts ([M7](milestone_7_latency_facts_and_speed_assessment.md))
- [ ] Feature fields present for entry, stop, and TP decisions
- [ ] **No strategy decision changes** based on FeatureBuilder v0 (grep: strategy modules must not branch on feature fields)
- [ ] All v0 field tests pass

## Risks

- Scope creep into v1 indicators — code review gate: only listed fields allowed

## Open questions

None — v0 scope fixed.

## Definition of done

`FeatureBuilder` module shipped; tests green; wired into decision_snapshot builder in M7; strategy logic unchanged.

## Not in scope

- FeatureBuilder v1 (OBI, microprice, volatility, BTC)
- Strategy branching on features
- ML pipelines
