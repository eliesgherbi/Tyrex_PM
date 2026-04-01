# Milestone v1.04 — Guru data pipeline

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.04 |
| **Title** | Guru data pipeline |
| **Status** | In progress — core pipeline implemented; §9 pending |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §7](./implementation_plan.md#7-milestone-overview) · Spec §4.2 Data |
| **Upstream dependencies** | **v1.03** §9 **Approved** (this repo: recorded in `Docs/evidence/v1_03_approval.md`) |
| **Blocking approvals** | §9 — before **v1.05** wires `CopyStrategy` |
| **Approval required from** | Technical lead |
| **Target branch / PR** | `milestone/v1_04-guru-pipeline` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 — `GuruMonitorActor` + client + tests landed |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Implement **`GuruMonitorActor`** (Nautilus `Actor`) that polls the **Polymarket Data API** for a **configured wallet address**, normalizes responses into **`GuruTradeSignal`**, **deduplicates** by venue trade id (or documented stable id), respects **rate limits**, and publishes signals on the **message bus** (or equivalent actor subscription pattern)—with **no order placement** in this module.

---

## 2. Scope

- HTTP client **adapter** interface + **`httpx`** (or single chosen library) implementation.
- Config: `guru_wallet_address`, `poll_interval_seconds`, `data_api_base_url`, **cursor** / `since` parameter strategy documented (use official query params per Data API docs).
- **`GuruTradeSignal`** fields **minimum**: `source_trade_id`, `ts_event`, `side` (buy/sell), `token_id` **or** `instrument_id` (document ownership), `size_raw`, `price_raw` if present, `raw_payload_ref` optional.
- **Dedup:** In-memory LRU or persistent **last_seen_id** file under `var/` (gitignored) for dev—document production intent.
- **429 / throttle:** Exponential backoff with **cap**; jitter; log `event=poller_backoff` with `retry_after` if header present.
- **Unit tests** with fixtures; mock server acceptable.

---

## 3. Out of Scope

- **Instrument resolution** inside actor (unless trivial—may pass `token_id` only)
- **Websocket** guru feed
- **Multiple** guru wallets (v1 = **one**)
- **Signal policy** interpretation (**v1.05**)

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.03** | **Approved** — `core` logging + config patterns available |
| **v1.00** | *Not required* for public Data API reads; document if endpoint needs auth |
| **Prior deliverable** | **v1.01** allowlist **optional** here; **required** before **v1.05** for allowlist filtering |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| `src/tyrex_pm/data/guru_monitor.py` | `GuruMonitorActor` + `GuruMonitorActorConfig` |
| `src/tyrex_pm/core/types.py` | `GuruTradeSignal` |
| `src/tyrex_pm/data/data_api_client.py` | Rate-aware HTTP (`httpx`) |
| `tests/unit/test_guru_dedup.py` | Dedup + parsing |
| `tests/integration/test_guru_actor_mocked.py` | Duplicate id across pages |
| `tests/test_architecture_guards.py` | No execution symbols in `data/` |

---

## 6. Acceptance Criteria

1. Fixture tests: **exactly one** `GuruTradeSignal` per unique `source_trade_id` across simulated restart when cursor/`last_seen` used.
2. HTTP 429: **no** tight-loop; ≥1 `poller_backoff` log before retry (test).
3. **Zero** `submit_order` / `order_factory` / `ExecutionClient` in `data/` — evidence: `rg` output in PR or `tests/test_architecture_guards.py`.
4. Logs: `event=guru_signal_emitted` | `guru_poll_tick` with `correlation_id=source_trade_id` on emit.

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
pytest tests/unit/test_guru_dedup.py tests/integration/test_guru_actor_mocked.py -v
rg "submit_order|order_factory|ExecutionClient" src/tyrex_pm/data || exit 0  # expect no matches; document if exceptions
```

### Required log or output artifacts

- Optional 5-minute **redacted** dry-run log against real API (poll spacing visible)

### Required config or examples

- `guru_wallet_address` + poll settings in `config/v1.example.yaml` (use **placeholder** address)

### Required demo scenario

- Reviewer confirms dedup test covers **restart** semantics

### Required design or ADR references

- Link [Polymarket Data API rate limits](https://docs.polymarket.com/quickstart/introduction/rate-limits) in module docstring or PR

### Required reviewer sign-off inputs

- Architecture note **1 paragraph**: cursor/pagination vs API

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Data API field instability | Parse errors | Version fixtures; `guru_parse_error` |
| High guru trade rate | 429 | Backoff; poll floor |

---

## 9. Approval gate

**What must be reviewed**

- Test output + architecture guard
- Actor does not import execution stack

**Conditions that block v1.05**

- **v1.04** not **Approved**
- `GuruTradeSignal` schema unstable (breaking changes after approval require **re-approval** or version bump doc)

**Sign-off template**

> Milestone **v1.04** **Approved**. Guru pipeline merged ___ . Reviewer: ___
