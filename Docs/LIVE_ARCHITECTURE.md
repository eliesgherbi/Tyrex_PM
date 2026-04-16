# Live architecture — VenueState, risk, and session truth

**Status:** Permanent operator / engineer reference. **Supersedes** older wording that described Nautilus `Cache` / `Portfolio` alone as “framework truth” for **deployment caps** on Polymarket **live** with **shared-wallet** reality.

**Related:** [Architecture.md](Architecture.md) (module map) · [OPERATIONS.md](OPERATIONS.md) (runbook) · [CONFIG_MODEL.md](CONFIG_MODEL.md) · [reporting_fact_model.md](reporting_fact_model.md) · [Implementation/road_map.md](Implementation/road_map.md) (governance one-pager).

---

## 1. Split truth (Tier A vs Tier B)

| Tier | Role | Authoritative for |
|------|------|-------------------|
| **Tier A — venue truth** | **`VenueState`** (fed by **`WalletSyncActor`**) + read boundary **`state_readers`** when `VenueState` is wired | **Wallet-level** positions and resting orders (HTTP-backed snapshots), **CLOB collateral / cash** freshness for capital snapshots, **deployment-budget** inputs for **filled** exposure (venue size × mark; missing mark → fallback + `venue_state_missing_mark` fact) and **pending** exposure (resting orders from the venue snapshot). **Layer A** long qty / inventory-style reads used in strategy context also go through the same wiring where applicable. |
| **Tier B — session truth** | Nautilus **`Cache`**, **`Portfolio`**, execution engine order lifecycle | **This bot’s** submitted orders, fills as seen by the framework, **`shutdown_drain`**, guru-tagged resting-order identity, internal reconciliation **intervals** (position/open checks) that keep **session** state moving toward venue — **not** a guarantee of instant agreement with Tier A for **external** activity. |

**Compose rule (live):** When `execution_mode == live` and `wallet_sync_enabled` is true (default for live), `guru_compose` constructs **`VenueState`** and passes it into **`NautilusDeploymentBudget`**, **`NautilusExecutionStateReader`**, **`NautilusAccountSnapshotProvider`**, **`NautilusPositionStateReader`**, and **`WalletSyncActor`**. There is **no** `venue_state_reads_enabled` flag; Tier A routing is **on** whenever that object exists.

**Fallback:** If `VenueState` is **not** constructed (e.g. shadow, or live with `wallet_sync_enabled: false`), **`NautilusDeploymentBudget`** uses Nautilus **cache/portfolio** for filled deployment and the execution reader without a venue snapshot — appropriate for **non-production** or explicit opt-out, **not** the recommended live shared-wallet posture.

---

## 2. End-to-end workflow (live)

1. **Guru ingest** — RTDS and/or Data API → `GuruTradeSignal` on the bus.
2. **Layer A** — `CopyStrategy` / `layer_a` filters (`static_amount`, `significance_conviction`, optional token allowlist) → `strategy_decision` / skip reasons in reporting.
3. **Risk** — `ConfiguredRiskPolicy` uses deployment math from **`NautilusDeploymentBudget`** + capital from **`DefaultCapitalStateProvider`** (account snapshot prefers venue-sourced USDC when wired). Per-order clip, token cap, portfolio cap, concurrent guru rests, capital gate, tradable health.
4. **Submit** — `NautilusGuruExecutionPort` → Polymarket adapter / CLOB.
5. **Venue / session** — Wallet sync refreshes **`VenueState`**; Nautilus processes order events and periodic checks (Tier B convergence).
6. **Reporting** — `facts.jsonl`: `risk_decision`, `deployment_budget`, `venue_state`, `wallet_sync`, `layer_a_filter`, `execution_outcome`, `order_lifecycle`, `fill`, etc.

---

## 3. What WalletSync does

**`WalletSyncActor`** (live, when enabled) polls Data API positions and CLOB orders on an interval, resolves instruments via **`GuruInstrumentDynamicController`**, and **writes** into **`VenueState`** (positions, resting `OrderSnapshot`s, collateral). It does **not** run the removed **position reconciliation** pass (no synthetic `PositionStatusReport` closes). **Startup** must reach **`wallet_sync_first_sync_complete`** as part of readiness alongside venue cash readiness.

---

## 4. External UI / manual sells and other bots

- **You do not need** a Tyrex **strategy SELL** event or a `fill` fact on a SELL intent for **external** activity to be valid.
- **Success criterion:** Tier A **updates**: e.g. **`venue_state`** facts show **`position_count`** / implied flat exposure, **`wallet_sync`** shows **`positions_fetched: 0`** when flat, **`risk_decision`** later shows **`token_deploy_at_eval` / `portfolio_deploy_at_eval`** dropping when headroom returns, and **`tyrex_risk_ops`** / **`deployment_budget`** facts align with caps.
- **Nautilus cache/portfolio** may **lag** behind the venue for external actions; **risk deployment** for live is driven by **VenueState-backed** readers when wired — **Tier B** still matters for order lifecycle and drain.

---

## 5. Guarantees vs non-guarantees

| Guaranteed (design intent) | Not guaranteed |
|---------------------------|----------------|
| Tier A reads use **`VenueState`** when live + wallet sync compose path is active | Nautilus **Cache** is perfect real-time **wallet** truth for **external** events |
| Deployment caps use venue-backed **filled** + **pending** inputs when `VenueState` is set | A **manual** sell always produces a clean **strategy-side** audit trail |
| Startup readiness waits on **wallet sync** + **venue cash** predicates | **Exchange** never rejects orders under **bursty** concurrent BUY pressure (balance/allowance races) |
| Reporting emits structured facts for operators | **Shutdown drain** always completes within timeout under load (see ops: residual orders, cancel counts) |

---

## 6. Operational evidence (debugging)

- **Caps / headroom:** `risk_decision` (`reason_code`, `token_deploy_at_eval`, `portfolio_deploy_at_eval`); `deployment_budget` facts; `tyrex_risk_ops` in Tyrex log (`gate=token_deployment_cap` / `portfolio_deployment_cap`).
- **Venue truth:** `venue_state` (`position_count`, `resting_order_count`, `cash_ready`); `wallet_sync` (`positions_fetched`, `orders_fetched`, `http_positions_ok`).
- **Session:** `order_lifecycle`, `fill`, Nautilus logs for `ACCEPTED` / `REJECTED` / `CANCELED`.
- **Layer A:** `layer_a_filter`, `strategy_decision`.

---

## 7. Current live scenarios and obsolete settings

**Current (recommended references):**

- **`config/scenarios/venue_state_live/`** — live validation with **`VenueState`** + **`wallet_sync`**; isolated guru state under `var/scenarios/venue_state_live/` (see folder `README.md`).
- Base **`config/runtime/`** templates and **`config/strategy/`** / **`config/risk/`** as extended by scenario YAMLs.

**Obsolete (do not use):**

- **`position_reconciliation_enabled`**, **`position_reconciliation_shadow_mode`**, **`position_reconciliation_deferral_max`** — removed from code; **no** `position_reconciliation` fact type.
- **`venue_state_reads_enabled`** — removed; never add back as a migration toggle.
- **`config/scenarios/base/RUNBOOK.md`** (historical reconciliation runbook) — **deprecated**; see **`venue_state_live`** README and this doc.

**Historical-only:** `docs/implementation/venue_state_migration/` and `docs/implementation/venue_sync_truth/` archive material — **not** authoritative; use **this file** + **road_map.md** + code.

---

## 8. One-paragraph summary

**Live Polymarket guru-follow with a shared wallet:** **Tier A** is **`VenueState`** (wallet sync + HTTP snapshots) driving **deployment** and **capital account** reads for risk; **Tier B** is **Nautilus** for **this session’s** orders and lifecycle. External sells and other bots are **not** required to appear as Tyrex strategy events; operators verify **`venue_state`**, **`wallet_sync`**, and **`risk_decision` / `deployment_budget`** facts for headroom. **Nautilus** remains essential for execution and drain but is **not** described as the sole “source of truth” for **wallet-level** deployment caps anymore.
