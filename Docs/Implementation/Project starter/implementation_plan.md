# Polymarket Platform — V1 Implementation Plan (General)

**Inputs:** [Specification.md](./Specification.md) · [NautilusTrader Polymarket](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/polymarket.md) · [Polymarket CLOB](https://docs.polymarket.com/developers/CLOB/introduction) · [Authentication](https://docs.polymarket.com/developers/CLOB/authentication) · [Rate limits](https://docs.polymarket.com/quickstart/introduction/rate-limits)

**Related:** Executable work is broken down in **[Milestones_INDEX.md](./Milestones_INDEX.md)** and per-milestone files **`Milestones_v1_XX.md`**. Each milestone file includes a **metadata table** (ID, status, owner, dependencies, approvals, branch, dates) and a **standard review evidence** section so reviews are repeatable.

---

## 1. Purpose of this document

This file is the **general** implementation plan: architecture, approach, structure, contracts at a glance, milestone **overview**, dependencies, testing philosophy, and risks. It is **not** a task checklist. For **review gates, acceptance criteria, and evidence**, use the milestone documents.

---

## 2. Executive summary

**Objective:** Deliver a **stable, modular NautilusTrader-based base** with a **reference `CopyStrategy`**, preserving separation of concerns, **fail-closed** risk, **backtest/live parity** of the strategy class, and venue-specific execution isolation—without optimizing strategy sophistication (per Specification).

**Implementability:** Polymarket is supported in Nautilus via data + execution clients and `PolymarketDataLoader` for research/backtest. The work is primarily **composition** (policies, actors, thin services) around Nautilus primitives—not a parallel OMS.

**Top risks (managed across milestones):**

| Risk | Mitigation direction |
|------|---------------------|
| CLOB quantity/TIF/“market” semantics vs generic trading intuition | Freeze semantics in **Milestone v1.07** before **v1.08** coding |
| ~1s order signing; stale quotes | Serialize per `guru_trade_id`; band + fallback rules in execution spec |
| Incomplete venue order history; experimental reconciliation flags | Document expectations in v1.07; **disable** `generate_order_history_from_trades` in v1 |
| Rate limits (Data API, CLOB) | Token bucket, cursors, backoff; validated in guru milestone |
| Wrong `signature_type` / `funder` | Validated in **v1.00** before other live work |

---

## 3. Implementation approach

- **Runtime shape (v1 live):** One `TradingNode`, one `CopyStrategy` (`Strategy`), one `GuruMonitorActor` (`Actor`), one notifier `Actor`.
- **Guru path:** Data API **polling** → `GuruTradeSignal` → bus/mailbox. **No orders** in the data module (Specification §7).
- **Orchestration:** `CopyStrategy` calls **signal → sizing → risk → execution** policies. **Thesis** (guru mirror) vs **protective** exits use **distinct reason codes** in telemetry.
- **Execution:** Translate **`OrderIntent` → Nautilus `Order`** via `PolymarketExecutionPolicy` (adapter/`order_factory`; e.g. `quote_quantity=True` for market **BUY** per Nautilus Polymarket doc).
- **Backtest:** Same strategy class; historical trades (+ optional book data) via `PolymarketDataLoader`; guru timeline via recorded or derived events.

**Explicit gates (see milestones):**

- **Gate A:** Credentials and wallet model validated (**v1.00**) before instrument/order milestones.
- **Gate B:** **Written** execution + reconciliation semantics reviewed (**v1.07**) before **v1.08** submits copy-driven orders at scale.
- **Gate C:** Shadow telemetry + risk fail-closed behavior reviewed (**v1.05–v1.06**) before **v1.09** live-safe orchestration with real-money path enabled.

---

## 4. Architecture summary

| Module | Responsibility | Primary realization | Must not contain |
|--------|----------------|---------------------|------------------|
| **Platform core** | Config, domain types, persistence **ports**, telemetry helpers | `platform/core` package | Orders, guru HTTP |
| **Data** | Live MD via adapter; guru polling; loaders; cache-facing services | `GuruMonitorActor`, loaders, `*Service` | `submit_order`, risk decisions |
| **Signal** | Entry/exit **hypotheses** | Policy objects, pure functions | Account enforcement, venue protocol |
| **Risk** | Approve/deny/resize **intent**; emergency stop | `RiskPolicy`, `SizingPolicy`, `PortfolioGuard` | HTTP, order building |
| **Execution** | Intent → `Order`; reconciliation **adapter** | `PolymarketExecutionPolicy`, `OrderReconciliationService` | Guru thesis |
| **Indicator** | Feature framework (v1: interface only) | `IndicatorProvider` protocol | Trading decisions |
| **Strategy** | Wire policies; emit decision/skip telemetry | `BaseComposableStrategy`, `CopyStrategy` | Book/signing/REST internals |
| **Runtime** | Live/backtest bootstrap | `LiveRuntime`, `BacktestRuntime` | Business rules |
| **Reporting** | Post-run aggregates, skip stats | `platform/reporting` | Order submission |

Thesis vs protective: **guru mirror** exits originate in **signal**; **protective** flatten caps originate in **risk**—both may yield intents but **reason codes differ**.

---

## 5. Repository / package structure

```text
src/tyrex_pm/     # Python import package (name avoids stdlib `platform` shadowing)
  core/           # config, types, telemetry, persistence protocols
  data/           # GuruMonitorActor, loaders, cache-facing services
  signal/         # entry/exit policies
  risk/           # risk, sizing, guards
  execution/      # execution policies, reconciliation wrapper
  indicator/      # protocols + noop
  strategy/       # BaseComposableStrategy, CopyStrategy
  runtime/        # live/backtest factories
  reporting/      # collectors, exporters
examples/
tests/
  unit/
  integration/
config/
scripts/
```

---

## 6. Key interfaces / contracts (summary)

| Contract | Role |
|----------|------|
| `GuruTradeSignal` | Normalized guru activity; produced by data/history loaders only |
| `EntrySignalPolicy` / `GuruFollowEntryPolicy` | Entry hypothesis + skip reason |
| `ExitSignalPolicy` / `GuruMirrorExitPolicy` | Mirrored exit hypothesis |
| `SizingPolicy`, `CopyRiskPolicy` | Requested size/notional; copy clamps |
| `RiskPolicy`, `PortfolioGuard` | Fail-closed intent gate + resize |
| `ExecutionPolicy`, `PolymarketExecutionPolicy` | Approved intent → `Order`(s) |
| `BaseComposableStrategy` | Injection + uniform telemetry hooks |
| `OrderReconciliationService` | Startup/resume alignment with **open** orders + positions |

Full behavioral boundaries are in **Specification §7** and elaborated under **v1.07** (semantics doc).

---

## 7. Milestone overview

Ordered gates and deliverables are in **`Milestones_v1_00.md` … `Milestones_v1_11.md`**. Summary:

| ID | Focus |
|----|--------|
| **v1.00** | Venue auth + L1/L2 credentials |
| **v1.01** | Instruments + market metadata for chosen universe |
| **v1.02** | Supervised minimal order lifecycle on one instrument |
| **v1.03** | Repo skeleton + structured logging + config validation |
| **v1.04** | Guru monitor + Data API polling + `GuruTradeSignal` |
| **v1.05** | Signal policies + copy decision path in **shadow** mode (no copy orders) |
| **v1.06** | Risk pipeline + fail-closed behavior |
| **v1.07** | **Execution + reconciliation specification** (approval gate) |
| **v1.08** | Execution policy implementation + tests |
| **v1.09** | Live-safe orchestration + persistence + kill switch + notifier |
| **v1.10** | Backtest runtime + historical replay |
| **v1.11** | Reporting + V1 closure evidence |

**Index with titles:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 8. Dependency overview

| Category | Notes |
|----------|--------|
| Runtime | `nautilus_trader[polymarket]` — pin after first green CI |
| Guru HTTP | `httpx` or `aiohttp` (one stack) |
| Config | `pydantic` or `msgspec` |
| Persistence | `sqlite3` / `aiosqlite` consistent with node threading |
| Dev | `pytest`, `pytest-asyncio`, `ruff` |

Wrap **Data API** calls behind an internal client for tests. Treat **`generate_order_history_from_trades`** and py-clob/Nautilus bumps as **regression-sensitive**.

---

## 9. Testing strategy (overview)

| Layer | Intent |
|-------|--------|
| **Unit** | Policies and pure transforms; no network |
| **Integration** | Mock HTTP for guru; Nautilus backtest engine with recorded data |
| **Live-safe** | Kill switch, caps, duplicate-signal handling; optional notifier ping |
| **Pre–real-money** | Checklist in **v1.09** / **v1.11** (allowances, reconciliation dry run, signing latency budget) |

Detailed **acceptance criteria** per milestone are in the milestone files.

---

## 10. Key risks and open questions

| Timing | Item |
|--------|------|
| Before **v1.04** | Exact Data API query shape + cursor/dedup strategy for guru wallet under [rate limits](https://docs.polymarket.com/quickstart/introduction/rate-limits) |
| Before **v1.08** | Band width ε, TIF choice for fallback limit, FOK vs GTC for “marketable” attempts—**fixed in v1.07 doc** |
| During implementation | SQLite vs JSON store; notifier transport |
| Post-v1 | Multi-guru, dashboards, advanced execution |

---

## 11. Recommended implementation sequence

1. Complete **v1.00 → v1.03** (credentials, instruments, tiny supervised orders, skeleton + logs).
2. Build **guru + shadow copy path** (**v1.04–v1.05**) — telemetry before risking automation.
3. Add **risk** (**v1.06**), then **freeze execution semantics** (**v1.07** — **approval required**).
4. Implement **execution** (**v1.08**), then **live-safe shell** (**v1.09**).
5. Run **backtest** (**v1.10**), then **reporting + closure** (**v1.11**).

---

## 12. V1 scope reminder (non-goals)

V1 **does not** include: best guru selection, portfolio optimization, rich dashboards, AI strategies, multi-strategy productization, or execution micro-optimization beyond a robust baseline (Specification §2).

---

## Document control

- **General plan:** this file (`implementation_plan.md`).
- **Milestone index:** [Milestones_INDEX.md](./Milestones_INDEX.md).
- **Per-milestone execution/review:** `Milestones_v1_00.md` … `Milestones_v1_11.md` (same folder).
- **Product intent:** [Specification.md](./Specification.md).

*Revision: planning package — general plan + twelve milestone decomposition files.*
