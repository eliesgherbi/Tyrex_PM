# Phase 2B Program Guardrails

**Authority:** `Docs/Implementation/Phase 2 completion/tyrex_pm_phase2b_plan.md`  
**Status:** specification drafted · **Implementation:** not started

---

## A. Phase naming rules

| Term | Meaning |
|------|---------|
| **Phase 2 (historical)** | WS-primary live execution backbone (M8/M9). **Complete.** |
| **Phase 1** | Mechanical survival / damage control. Implemented; enforce opt-in via scenarios. |
| **Phase 2B** | Event-driven data backbone and replay lab. **This program.** |
| **Phase 3** | Adaptive edge and dynamic parameter **enforcement**. Future only. |

**Rules:**

- Do **not** call Phase 2B “Phase 3.”
- Do **not** call advisory-only logic “enforcement.”
- `ParameterResolver` in Phase 2B is **pass-through / advisory logging only**.
- Phase 3.0 requires separate approval and its own spec (`spec_phase3_0_controlled_adaptive_enforcement_future.md`).

---

## B. Non-negotiable production spine

Phase 2B code sits **upstream of or parallel to** the trading path. It must not bypass or weaken:

```text
MarketStateStore
  → DataQualityGate          (market_data/quality.py)
  → DecisionSnapshot         (market_data/decision_snapshot.py)
  → strategy / survival      (strategies/paired_binary/, survival/)
  → RiskEngine               (risk/engine.py)
  → ExecutionPlanner         (execution/planner.py)
  → validate_planned_order
  → SingleWriterOMS          (execution/oms.py)
  → facts                    (reporting/facts.py)
```

**Implications:**

- Market data changes must preserve store read semantics for existing consumers.
- Facts remain **decision logs**; full book data never enters facts.
- Record-only mode must not initialize OMS, wallet, or risk pipeline.
- Replay (M2B.5) may use permissive sim contexts offline; live path unchanged.

---

## C. Global forbidden actions during Phase 2B

| Forbidden | Reason |
|-----------|--------|
| Live trading unless explicitly requested and phase-appropriate | 2B is data backbone, not strategy tuning |
| Adaptive **enforcement** during Phase 2B | Phase 3.0 only |
| Advisors submitting orders or changing effective params live | Advisory = compute + log only |
| Bypassing RiskEngine | Fail-closed spine |
| Bypassing ExecutionPlanner | Fail-closed spine |
| Bypassing SingleWriterOMS | Single-writer invariant |
| Putting full book data into facts | Facts = decisions, not market history |
| `research/` imports into `src/tyrex_pm/` | Import boundary |
| Synchronous disk writes in ingest hot path | Latency / stall risk |
| Broad refactor of `paired_binary_run.py` in M2B.0-A/B/1-A | Production loop stability |
| Adding `zstandard` before M2B.1-B | Dependency discipline |
| Adding `pandas` / `pyarrow` before M2B.3 | Dependency discipline |
| Moving survival modules to `survival/legacy/` in early milestones | Separate cleanup |
| SessionRunner extraction before prerequisites met | See plan §8 |
| Phase 1 floor-enforce promotion inside 2B milestones | Separate hardening track |

---

## D. Milestone discipline

```text
One milestone at a time.
No milestone is accepted without its required tests.
No future milestone work may be smuggled into an earlier milestone.
Every implementation report must be reviewed against its milestone spec.
Flag-default-off must preserve exact legacy behavior until explicitly enabled.
```

**Smuggling examples (reject immediately):**

- Recorder code in M2B.0-B
- Fact correlation fields in M2B.0-B
- `paired_binary_run` changes in M2B.0-B or M2B.1-A
- Replay engine stubs in M2B.1-A
- Advisor enforcement in M2B.8

---

## E. Acceptance review format

Future implementation agents must submit reports using this structure:

```text
1. What was implemented
2. What matches the milestone spec
3. What deviates (if any)
4. What files were touched (created / modified)
5. Tests run (commands + results)
6. Risks introduced
7. Required fixes (if reject or accept-with-follow-up)
8. Accept / reject / accept with follow-up
9. Next allowed action (which milestone may start next)
```

Reviewers compare the report section-by-section to the milestone spec **before** merging.

---

## F. Repo-plan alignment notes

These are confirmed repo realities; specs must respect them:

| Topic | Repo reality |
|-------|--------------|
| Hot WS path | `ingestion/market_ws_ingest.py` (not `market_stream.py` alone) |
| Book ordering | Lexicographic `book_hash` via `_handle_sequence` |
| Store depth | `MarketStateStore.store_top_n_levels` default 5 |
| `core/events.py` | Unused internal types; repurpose per plan |
| `RawMarketEvent` | `market_data/models.py` — precursor, not canonical |
| `JsonlSink` | Sync + flush per write — not for hot recording |
| `research/` | Does not exist yet |
| `tyrex-pm record` | Does not exist (`app.py` has `run`, `reset-state`, `live-attest`) |
| Dependencies | No zstd/pandas/pyarrow in `pyproject.toml` today |

---

## G. Traceability

| Document | Role |
|----------|------|
| `tyrex_pm_phase2b_plan.md` | Program authority |
| `milestones_index.md` | Status tracker |
| `spec_m2b_*` | Per-milestone implementation contracts |
| `Docs/EVENT_MODEL.md` | Event contract (created in M2B.0-A) |
| `Docs/DATA_LAKE.md` | Parquet schemas (created in M2B.3) |
