# Road map — governance and direction

## Architectural truth model (authoritative)

**As of the VenueState decision (2026 Q2),** Tyrex does **not** treat Nautilus `Cache` / `Portfolio` as **universal venue truth** for Polymarket **live** trading when the wallet is **shared** or subject to **external activity** (manual trades, other bots, guru fills outside this node’s event stream).

The project operates on a **split-truth** architecture:

| Tier | Source of truth | Use |
|------|-----------------|-----|
| **Tier A — venue truth** | Direct Polymarket HTTP (Data API, CLOB), aggregated in **`VenueState`** and exposed through **`runtime/state_readers.py`** | Risk gates, deployment caps, sizing, strategy decisions that must reflect **wallet-level** positions, resting orders, and collateral |
| **Tier B — session truth** | Nautilus **`Cache` / `Portfolio`** | This bot’s order lifecycle, own fills, shutdown drain, guru-tagged concurrent-order counts — anything **session-local** by definition |

**Do not** “fix” Tier A drift by patching reconciliation into Nautilus portfolio semantics as the long-term answer. Position reconciliation is **deprecated** (disable in config, then remove per plan). **Do** route Tier A reads through the VenueState migration.

**Operator / engineer reference (supersedes migration-folder detail):** [`LIVE_ARCHITECTURE.md`](../LIVE_ARCHITECTURE.md).

**Historical planning notes (non-authoritative):** [`venue_state_migration/README.md`](venue_state_migration/README.md) and files under [`venue_state_migration/`](venue_state_migration/) — migration-era; may mention removed flags.

**Historical note:** Older docs may still say “Nautilus-first” for **Tier A** deployment. Where that conflicts with this file or **LIVE_ARCHITECTURE**, **those two govern** for shared-wallet Polymarket live behavior.

---

## Backlog and phases (archived pointer)

Earlier phased plans and stabilization sequencing live alongside this governance statement:

- Post-refactor hardening: [`refactor_lifecycle/stabilization_roadmap.md`](refactor_lifecycle/stabilization_roadmap.md)
- Layer A filter plans and other implementation folders under `Docs/Implementation/` remain valid for **feature** scope; they must be read in light of **split-truth** above when touching risk, deployment, or live sizing.

---

## What this road map is not

- It is **not** a full product backlog (strategies, backtesting, PnL ledger).
- It does **not** replace [`Architecture.md`](../Architecture.md) for module-level design — it **aligns** the **truth-model assumption** with [`LIVE_ARCHITECTURE.md`](../LIVE_ARCHITECTURE.md) for live Polymarket Tier A vs Tier B.
