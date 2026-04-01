# Milestone v1.01 — Instrument and market metadata validation

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.01 |
| **Title** | Instrument and market metadata validation |
| **Status** | **§9 Approved** — resolution-path validation ([evidence](../../evidence/v1_01_approval.md)) |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §7](./implementation_plan.md#7-milestone-overview) · [Specification.md](./Specification.md) |
| **Upstream dependencies** | **v1.00** — §9 **Approved** (sign-off recorded with reviewer names + date) |
| **Blocking approvals** | §9 — required before **v1.02** uses allowlist instruments |
| **Approval required from** | Technical lead |
| **Target branch / PR** | `milestone/v1_01-instruments` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 — §9 approved for **resolution pipeline**; allowlist still reference market until ops replaces universe |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Prove that **target markets** in the v1 universe map correctly to Nautilus **`BinaryOption`** / `InstrumentId` (condition id + token id), that **tick size** and **token id** match CLOB expectations, and that the process can be repeated for **every slug** in the v1 allowlist—without yet running copy logic.

---

## 2. Scope

- Define a **v1 market allowlist** (event slugs and/or market slugs) in config or a static YAML—**at least one**, **at most five** for v1.
- For each entry:
  - Resolve **Gamma/metadata** → **CLOB token id** path using the same mechanism Nautilus `PolymarketInstrumentProvider` / loader uses (prefer **`PolymarketDataLoader.from_market_slug`** or node provider equivalent).
  - Record: `instrument_id`, `token_id`, `tick_size`, `neg_risk` flag if applicable, outcome side (YES/NO token).
- Subscribe or fetch **L2 snapshot** (REST `/book` or equivalent) for each token and verify **non-empty or explain closed market**.
- Document **instrument refresh** interval implications (Nautilus default catalogue refresh).

---

## 3. Out of Scope

- Guru polling, copy strategy, risk, execution policy
- Subscribing to **>5** instruments (universe scale-out is post-v1 unless needed)
- **Historical** loader for backtest (**v1.10**)
- **WebSocket** fan-out stress beyond what one node needs for the allowlist

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.00** | **Approved** per §9; evidence link exists (same standards as v1.00 sign-off) |
| **Pinned stack** | `nautilus_trader[polymarket]` **minimum version recorded** in PR description or `docs/pins.md` / `Docs/dependency_lock.md` (exact pin can wait for v1.11 if team documents **lower bound** here) |
| **Auth** | If any resolution step requires L2 reads, **v1.00** env pattern must work on reviewer machine |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| **Allowlist file** | `config/v1_markets.yaml` (or similar) with slugs + resolved IDs |
| **Resolution script** | Prints a table: slug → `InstrumentId` → token_id → tick_size |
| **Validation note** | One paragraph per market: snapshot timestamp, bid/ask or “market closed” |
| **Tests** | Unit test: parsing helper on **frozen JSON fixture** from Gamma/CLOB |

---

## 6. Acceptance Criteria

1. For **each** allowlisted market, `get_polymarket_instrument_id`-style resolution matches a **manual** cross-check from Polymarket UI or API for **token id**.
2. Tick size used in code matches **`minimum_tick`** / builder config for that market (per py-clob rounding / Nautilus instrument).
3. REST order book fetch returns **200** OR documented 4xx with “closed/archived” and market removed from **active** copy universe.
4. Allowlist is version-controlled; no secrets in file.

---

## 7. Review evidence (standard pack)

### Required test commands

- `pytest tests/... -k market` or documented path — **pass**
- Resolution script: `python scripts/resolve_markets.py` (or equivalent) — **exit 0**, table printed

### Required log or output artifacts

- **Machine-readable table** (stdout or CSV): slug, instrument_id, token_id, tick_size, book_check status

### Required config or examples

- Checked-in `config/v1_markets.yaml` (or agreed path) **without** secrets

### Required demo scenario

- Reviewer picks **one** market row and independently verifies token id in UI/API vs table output

### Required design or ADR references

- Link Nautilus Polymarket doc sections on **BinaryOption** / instrument provider (in validation note or PR)

### Required reviewer sign-off inputs

- Sign-off: [`Docs/evidence/v1_01_approval.md`](../../evidence/v1_01_approval.md) (approval is for **resolution path**; production universe must refresh allowlist + validation notes before live copy)

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Tick size changes mid-market | Order reject | Nautilus note on dynamic ticks—plan refresh; log instrument version |
| Wrong outcome token (YES vs NO) | Inverse copy | Double-check token id against question semantics in validation note |
| Stale slug | Runtime crash | Startup validation fails fast with clear error |

---

## 9. Approval gate

| Role | Responsibility |
|------|----------------|
| **Technical lead** | Confirms token ids + tick sizes against official definitions |

**What must be reviewed**

- Resolution script output + fixtures
- Validation note per market

**Conditions that block v1.02**

- **v1.00** not **Approved**, or allowlist incomplete for the instrument used in v1.02
- Missing reviewer sign-off record

**Before starting work on**

- **v1.02**: **v1.01 §9 Approved** and instrument ID(s) for smoke order **explicitly listed** in v1.02 PR scope

**Sign-off template**

> Milestone **v1.01** **Approved**. Instrument resolution verified for markets: ___ (YYYY-MM-DD). Reviewer: ___
