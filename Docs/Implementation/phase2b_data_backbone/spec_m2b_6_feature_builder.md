# M2B.6 — FeatureBuilder

## 1. Purpose in simple terms

Extend `market_data/features.py` into an incremental per-event `FeatureBuilder` producing a `FeatureVector` — **identical code** used in live (logged on decisions) and replay. No live **decisions** consume features in Phase 2B.

## 2. Boundary

### In scope

- Incremental O(1)-per-event feature computation
- Live/replay parity golden test
- Deprecate `execution/liquidity_guard.py`, `execution/slippage.py` stubs
- Optional 250ms feature dumps in record mode (config flag)
- <1ms/tick budget test

### Out of scope

- Strategy using features for entry/exit
- Alpha signals (d_sigma, regime, ML)
- Risk/planner changes

## 3. Current repo reality

| Path | State |
|------|-------|
| `market_data/features.py` | `FeatureBuilder` v0 — `BasicFeatureSnapshot` from store capture |
| `execution/liquidity_guard.py` | Empty stub |
| `execution/slippage.py` | Stub |
| M2B.5 | Replay provides event stream for parity test |

## 4. Files to create

| File | Why |
|------|-----|
| `tests/test_feature_live_replay_parity.py` | Byte-identical vectors |
| `tests/test_feature_builder_perf.py` | <1ms budget |

## 5. Files allowed to modify

| File | Modification |
|------|--------------|
| `market_data/features.py` | Incremental builder API |
| `market_data/models.py` | `FeatureVector` dataclass if needed |
| `strategies/paired_binary/observability.py` | Log feature snapshot on decisions (observe only) |

## 6. Forbidden files / modules

```text
risk/engine.py, execution/planner.py — no gate changes
paired_binary/monitor.py decision logic
```

## 7. Interfaces and contracts

```python
class FeatureBuilder:
    def on_event(self, event: MarketEvent, store: MarketStateStore) -> None: ...
    def current_vector(self) -> FeatureVector: ...
    def reset(self) -> None: ...
```

Feature groups per plan §13: state vector, PM microstructure, execution risk, strategy state — **compute only**.

## 8. Runtime flags and rollback

```yaml
runtime:
  market_data:
    features:
      emit_on_decisions: true   # log only
      record_dump_interval_ms: null  # optional record mode
```

## 9. Tests required

Parity golden + perf budget.

## 10. Regression tests required

Replay-diff still green. Paired-binary runtime unchanged decisions.

## 11. Acceptance criteria

- [ ] Same events → byte-identical `FeatureVector` live vs replay
- [ ] Perf test <1ms/tick median
- [ ] No decision logic reads features

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Entry gate uses features | Code review monitor/entry_eval |
| Different code paths live vs replay | Single FeatureBuilder module |

## 13. Review checklist

- [ ] Parity test committed
- [ ] liquidity_guard/slippage marked deprecated in docstrings

## 14. Done / not done examples

**Done:** Features logged on `decision_snapshot` when observability on.  
**Not done:** Dynamic entry threshold from features.

## 15. Next milestone dependency

**M2B.8** advisors may read feature tables offline; live still no consume.
