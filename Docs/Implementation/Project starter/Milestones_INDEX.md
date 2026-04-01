# V1 Milestone index

**Execution contract:** Each `Milestones_v1_XX.md` file includes a **metadata table** (status, owner, dependencies, approvals) and a **standard review evidence** pack. Update **Status** / **Last updated** when progress changes.

Parent plan: [implementation_plan.md](./implementation_plan.md) · Product spec: [Specification.md](./Specification.md)

## Gates (process)

| Gate | Milestone | Blocks |
|------|-----------|--------|
| **Execution code** | **v1.07** Approved + **ADR-001** merged | **v1.08**, `CopyStrategy.submit_order` automation |
| **Supervised tiny live** | **v1.09** Approved + checklist | First production-like automated copy at caps |
| **“Live ready” / production language** | **v1.09** evidence linked in **v1.11** | Marketing or closure doc claims |

All milestone files: **`Milestones_v1_XX.md`** (zero-padded).

| File | Title | Purpose | Status |
|------|--------|---------|--------|
| [Milestones_v1_00.md](./Milestones_v1_00.md) | Venue, wallet, and API credential validation | L1/L2 + env wiring before platform code depends on secrets | Done |
| [Milestones_v1_01.md](./Milestones_v1_01.md) | Instrument and market metadata validation | `InstrumentId` / token mapping for fixed universe | **§9 Approved** — resolution path; ops universe TBD ([evidence](../../evidence/v1_01_approval.md)) |
| [Milestones_v1_02.md](./Milestones_v1_02.md) | Supervised minimal order lifecycle | LIMIT place → cancel/fill on one instrument | Deliverables ready — §9 **pending** |
| [Milestones_v1_03.md](./Milestones_v1_03.md) | Platform skeleton and observability baseline | `src/tyrex_pm`, config, logs, stub strategy | **§9 Approved** ([evidence](../../evidence/v1_03_approval.md)) |
| [Milestones_v1_04.md](./Milestones_v1_04.md) | Guru data pipeline | `GuruMonitorActor`, Data API, `GuruTradeSignal` | In progress (implementation) |
| [Milestones_v1_05.md](./Milestones_v1_05.md) | Signal contracts and copy decision flow (shadow) | Policies + `CopyStrategy` **without** orders | Not Started |
| [Milestones_v1_06.md](./Milestones_v1_06.md) | Risk intent pipeline | Fail-closed `RiskPolicy` + tests | Not Started |
| [Milestones_v1_07.md](./Milestones_v1_07.md) | Execution semantics and reconciliation specification | **ADR-001** — gate before v1.08 | Not Started |
| [Milestones_v1_08.md](./Milestones_v1_08.md) | Execution pipeline implementation | `PolymarketExecutionPolicy` per ADR-001 | Not Started |
| [Milestones_v1_09.md](./Milestones_v1_09.md) | Live-safe orchestration | Persistence, reconciliation snapshot, kill switch, notifier | Not Started |
| [Milestones_v1_10.md](./Milestones_v1_10.md) | Backtest runtime and historical replay | `BacktestRuntime` — **not** live readiness | Not Started |
| [Milestones_v1_11.md](./Milestones_v1_11.md) | Reporting and V1 closure |§8 evidence — **no overclaim** on live vs backtest | Not Started |

**Ordering:** **v1.07** §9 **Approved** before **v1.08** merge. **v1.09** §9 **Approved** before supervised automated copy beyond **v1.02** scratch order.
