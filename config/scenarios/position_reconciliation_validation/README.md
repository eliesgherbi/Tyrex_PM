# Scenario: `position_reconciliation_validation`

Two-phase live validation of the position reconciliation feature (venue-truth cache sync).

- **Strategy / Risk:** Identical to `layer_a_follow` — tight demo caps so portfolio cap is easily reached.
- **Runtime (shadow):** `live_polymarket_shadow.yaml` — reconciliation pass runs and emits facts but does **not** mutate engine state. Use first.
- **Runtime (live):** `live_polymarket_live.yaml` — reconciliation pass sends `PositionStatusReport` to the engine, mutating cache. Use after shadow validation passes.

## Quick start

### Shadow mode (Phase 1)

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/position_reconciliation_validation/guru_follow.yaml \
  --risk-conf config/scenarios/position_reconciliation_validation/guru_follow_risk.yaml \
  --live-conf config/scenarios/position_reconciliation_validation/live_polymarket_shadow.yaml
```

### Live mode (Phase 2)

```bash
python scripts/run_guru.py \
  --strategy-conf config/scenarios/position_reconciliation_validation/guru_follow.yaml \
  --risk-conf config/scenarios/position_reconciliation_validation/guru_follow_risk.yaml \
  --live-conf config/scenarios/position_reconciliation_validation/live_polymarket_live.yaml
```

See **`RUNBOOK.md`** for detailed validation steps, pass/fail criteria, and log/fact grep patterns.
