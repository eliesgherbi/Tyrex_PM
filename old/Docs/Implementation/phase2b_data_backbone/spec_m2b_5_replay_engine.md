# M2B.5 — Replay engine + golden replay-diff

## 1. Purpose in simple terms

Replay recorded `MarketEvent` streams through the **actual** strategy/survival/monitor code offline and diff emitted facts against live `facts.jsonl`. This becomes the **canonical validation** that replaces ad-hoc fact-grep validators over time. Enables counterfactual parameter sweeps and synthetic episode generation.

## 2. Boundary

### In scope

- `research/replay/` package
- `EventLogReader`, `SimClock`, `ReplayRunner`
- `SimRiskContext` (permissive offline risk)
- `FillModel` implementing `OMSBackend` — FAK at recorded depth
- Golden replay-diff CI test (≥2 live runs)
- Synthetic episode generator (entry rule over all recorded markets)
- Sweep harness (static Phase 1 params only)
- Import isolation: live never imports `research/`

### Out of scope

- Managed-rest / GTC queue-position modeling (documented blind spot)
- Adaptive policy enforcement
- Live behavior change
- Fill-level equality (fill **decisions** included; fill timestamps/amounts excepted)

## 3. Current repo reality

| Path | State |
|------|-------|
| `scripts/replay_survival_advisory.py` | Facts-only survival summary — not event replay |
| `tests/test_replay_survival_advisory.py` | Tests facts script |
| M2B.0-B | `project_event`, sequencer |
| M2B.0-C | `trigger_event_id` for joins |
| M2B.1-B | Recorded JSONL.zst corpus |
| `PairedBinaryMonitor` | `strategies/paired_binary/monitor.py` |
| `survival/advisory.py` | Live survival path |

## 4. Files to create

| File | Why |
|------|-----|
| `research/replay/reader.py` | `EventLogReader` |
| `research/replay/clock.py` | `SimClock` |
| `research/replay/runner.py` | `ReplayRunner` |
| `research/replay/fill_model.py` | `FillModel` |
| `research/replay/risk_context.py` | `SimRiskContext` |
| `research/replay/sweep.py` | Parameter grid harness |
| `research/replay/synthetic_episodes.py` | Episode generator |
| `tests/test_replay_fact_diff_golden.py` | CI golden diff |
| `tests/fixtures/recordings/` | Trimmed committed segments |

## 5. Files allowed to modify

| File | Modification |
|------|--------------|
| `survival/*`, `monitor.py` | **Only** if needed for injectable clock / pure replay hooks — minimal, behind replay-only imports from research |

Prefer: replay passes `now=` into existing APIs without changing live defaults.

## 6. Forbidden files / modules

```text
execution/oms.py live path changes
risk/engine.py live path changes
paired_binary_run.py production loop changes
```

## 7. Interfaces and contracts

### ReplayRunner

```python
class ReplayRunner:
    def run(
        self,
        events: Iterable[MarketEvent],
        *,
        app_config: AppConfig,
        static_params: bool = True,
    ) -> ReplayResult:
        """Returns emitted facts + state timeline."""
```

### SimClock

Time = event `recv_ts`; timer ticks from recorded `session_timer_tick` or synthesized for counterfactual cadence.

### FillModel

FAK fills at recorded resting depth at limit-or-better; latency draw from measured live decision→ack distribution.

### Golden diff

Compare fact sequences: same `fact_type` order and decision payloads; ignore fill execution details per plan.

## 8. Runtime flags and rollback

Offline only — no live flag. Rollback: disable CI golden job.

## 9. Tests required

| Test | Proves |
|------|--------|
| `test_replay_fact_diff_golden.py` | ≥2 trimmed recordings match live facts |
| `test_replay_synthetic_episodes.py` | Generator produces episodes |
| `test_research_import_isolation.py` | Still passes |

## 10. Regression tests required

Full live pytest green. M2B.0 equivalence tests green.

## 11. Acceptance criteria

- [ ] ≥2 golden replay-diff green in CI
- [ ] ≥50 synthetic episodes/day from one recorded day (test scale)
- [ ] Sweep harness produces PnL/CVaR metrics per plan §12
- [ ] Blind spot documented: no GTC queue model
- [ ] Live `src/` does not import `research.replay`

## 12. Divergence risks

| Risk | Control |
|------|---------|
| Changing live monitor for replay | Injectable clock only |
| Requiring fill equality | Explicit excepted fields |
| Smuggling advisor enforce | Out of scope |

## 13. Review checklist

- [ ] Golden fixtures <5MB committed
- [ ] Diff tolerances documented
- [ ] SimRiskContext cannot submit live orders

## 14. Done / not done examples

**Done:** CI job `test_replay_fact_diff_golden` passes on 2 runs.  
**Not done:** DynamicStopPolicy enforce (Phase 3).

## 15. Next milestone dependency

**M2B.6–M2B.9** depend on replay harness.

**Handoff:** `ReplayRunner`, golden fixtures, sweep API.
