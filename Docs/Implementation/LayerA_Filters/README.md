# Layer A — Filter layer (planning)

**Layer A** is the **signal-side** layer between guru bus **`GuruTradeSignal`** delivery and **sizing → `OrderIntent` → risk → execution**. It is **class-based**, **configurable**, and lives under **`src/tyrex_pm/signal/`** when implemented — **not** in risk/runtime bookkeeping.

**Specs:** design notes in this folder. **Implementation:** `src/tyrex_pm/signal/layer_a/`, `runtime/layer_a_context.py`. **Operator example:** bundled scenario **`config/scenarios/layer_a_follow/`** (see its `README.md`); default strategy template **`config/strategy/guru_follow.yaml`** includes a backward-compatible `filters:` block (all off).

## Rule kinds

| Kind | Question | v1 examples |
|------|-----------|-------------|
| **Gating** | Should this signal proceed? | Token allowlist (from top-level `token_filter`), static USD floor, BUY-only median significance |
| **Interpretation** | How should we interpret this signal before sizing? | `exit_filter`: `mirror_guru` vs `full_exit` (follower position–based exit qty) |

Both use one **`LayerAOutcome`** (`accept`, `reason_code`, `detail`, `metadata`) and one orchestrator; interpretation filters set **JSON-safe `metadata`** (e.g. `exit_qty_mode`) instead of only pass/deny.

## Roadmap boundary

Aligned with **`Docs/Architecture.md`** / **`Docs/CONFIG_MODEL.md`**: **thin `CopyStrategy`**, policy in **`signal/`**, **Nautilus-first** wiring. **Capital, deployment budget, allowance, and framework portfolio truth** stay in **`risk/`** and **`runtime/`** (`state_readers` boundary). Layer A may call **injected `LayerAContext`** for follower position (**`full_exit`**) — it does **not** duplicate deployment caps or embed `Portfolio` reads in strategy code.

## v1 rule set

| Doc | Rule |
|-----|------|
| [00_general_plan.md](00_general_plan.md) | Architecture, **`LayerAOutcome`**, evaluation order, config + reporting, implementation order |
| [01_exit_filter_plan.md](01_exit_filter_plan.md) | Exit **interpretation**; `full_exit`; frozen deny reasons; runtime injection |
| [02_static_amount_significance_plan.md](02_static_amount_significance_plan.md) | Static USD **gating** on entry |
| [03_significance_conviction_plan.md](03_significance_conviction_plan.md) | Median vs **prior BUY** notionals; cold start **pass**; strict `>` median; **`observe_buy`** |

## Code anchors

`src/tyrex_pm/strategy/copy_strategy.py`, `src/tyrex_pm/signal/entry.py`, `src/tyrex_pm/data/guru_ingest_pipeline.py`, `src/tyrex_pm/runtime/guru_compose.py`, `src/tyrex_pm/config/loaders.py`.

## Status

These documents are **implementation-ready** for v1 (frozen behaviors, failure modes, reporting, and tests). Minor follow-ups (exact enum spellings, `BotSellValidateStrategy` integration smoke) are noted in `00_general_plan.md` §16.
