# Tyrex_PM — Guru execution architecture (Nautilus-first, unified live path)

**Status:** Reflects the **current** codebase after removal of legacy/hybrid live guru execution.

**Canonical references:** [OPERATIONS.md](../OPERATIONS.md), [CONFIG_MODEL.md](../CONFIG_MODEL.md), [current_state.md](current_state.md).

---

## Execution model (operator view)

| `execution_mode` | `TradingNode` | Guru execution | Risk / reporting truth |
|------------------|----------------|----------------|-------------------------|
| **`shadow`** | Empty data/exec clients | **NoOpExecutionPort** — no venue orders | Risk runs; no positions/allowance path for live collateral |
| **`live`** | Polymarket **data + exec** factories registered | **NautilusGuruExecutionPort** → `submit_order` | **Cache** (open orders, leaves), **Portfolio** (fills / exposure), optional **capital gate** via py-clob **balance/allowance** snapshots |

**Removed from YAML (loader errors if present):** `polymarket_nautilus_live`, `polymarket_framework_submit`. **`live`** implies Nautilus clients + framework submit.

**Manifest / reporting `execution_path`:** `live` \| `shadow` (aligned with `execution_mode`). Older runs may still show `framework_submit` in manifest; post-run DQ accepts both for compatibility.

---

## Control flow (unchanged shape)

`GuruMonitorActor` / `GuruStreamActor` → bus → `CopyStrategy` → policies → sizing → `ConfiguredRiskPolicy.evaluate` → `NautilusGuruExecutionPort.submit_intent` (live).

---

## py-clob vs Nautilus (division of labor)

- **Guru orders:** **only** through Nautilus `submit_order` on live.
- **py-clob `ClobClient`:** still used for **collateral/allowance** snapshots (`ClobAllowanceStateProvider`), **dynamic instrument resolution** (Gamma + CLOB), and **optional REST order book** snapshots when cache L2 is missing — **not** for parallel guru order submit.

---

## Config surface (high level)

- **One runtime switch:** `execution_mode: shadow | live`.
- **Polymarket:** `polymarket_instrument_ids` (empty on live = zero-bootstrap / dynamic), `polymarket_dynamic_*`, Gamma URLs, book-aware **`execution_*`** knobs (live only).
- **See** [CONFIG_MODEL.md](../CONFIG_MODEL.md) for removed keys and validation rules.

---

## Historical note

Pre-2026 Tyrex supported **legacy** py-clob guru submit and **hybrid** (Nautilus node + HTTP guru orders). That surface is **removed**; the prior long-form review in this file was superseded by this document.
