# Milestone v1.03 — Platform skeleton and observability baseline

| Field | Value |
|-------|-------|
| **Milestone ID** | v1.03 |
| **Title** | Platform skeleton and observability baseline |
| **Status** | **§9 Approved** ([evidence](../../evidence/v1_03_approval.md)) |
| **Owner** | *Assign at kickoff* |
| **Related plan** | [implementation_plan.md §5–§7](./implementation_plan.md#5-repository--package-structure) |
| **Upstream dependencies** | **v1.00** §9 **Approved** (recommended before node smoke; **hard** before any live node with secrets) |
| **Blocking approvals** | **v1.03 §9 Approved** — v1.04 tracks its own §9 gate |
| **Approval required from** | Technical lead |
| **Target branch / PR** | `milestone/v1_03-skeleton` |
| **Date created** | 2026-03-27 |
| **Last updated** | 2026-03-27 — §9 approved; paths corrected to `src/tyrex_pm/` |

**Index:** [Milestones_INDEX.md](./Milestones_INDEX.md)

---

## 1. Objective

Establish the **repository and package skeleton** under **`src/tyrex_pm/`**, **configuration validation** at startup, and **structured logging** conventions so every later milestone emits **reviewable** logs (correlation fields, level discipline) when components are stubbed or partially implemented.

---

## 2. Scope

- Create packages: `core`, `data`, `signal`, `risk`, `execution`, `indicator`, `strategy`, `runtime`, `reporting` (may contain `__init__.py` + placeholders only).
- Add **project packaging** (`pyproject.toml`): Python version, `nautilus_trader[polymarket]` dependency with **lower bound**; dev deps (`pytest`, `ruff`).
- Implement **`AppConfig`** (or equivalent): loads YAML/env; validates **required fields** for live vs backtest mode.
- Define **logging helper** in `core`: JSON or key=value structure; required fields: `timestamp`, `level`, `component`, `event`, `correlation_id` (optional).
- **`BaseComposableStrategy`** stub: subclass Nautilus `Strategy`; `on_start` logs `event=strategy_started` with `trader_id`.
- **`LiveRuntime` stub**: function `build_trading_node_config()` returns config object **or** builds node with **no** registered actors beyond defaults—**must run** `TradingNode` briefly (connect/disconnect) against Polymarket **data-only** if **v1.00** connectivity holds; otherwise document **skip** with written reason + lead waiver on AC.

---

## 3. Out of Scope

- `GuruMonitorActor` implementation (**v1.04**)
- Real copy, risk, or execution policies (**v1.05+**)
- Historical loaders, backtest engine wiring (**v1.10**)
- Full CI/CD—document **local** `ruff` / `pytest` until CI exists

---

## 4. Dependencies

| Dependency | Detail |
|------------|--------|
| **v1.00** | **Approved** before merging code that loads `POLYMARKET_*` in CI or shared environments |
| **v1.01** | *Optional* for example `InstrumentId` string in sample YAML |

---

## 5. Deliverables

| Artifact | Description |
|----------|-------------|
| `pyproject.toml` + `src/tyrex_pm/**` | Package layout per [implementation_plan.md](./implementation_plan.md) |
| `config/v1.example.yaml` | Example non-secret config |
| `core/logging_config.py` / `core/app_config.py` | Log helpers + validated app YAML |
| `strategy/base.py` | `BaseComposableStrategy` stub |
| `runtime/live_stub.py` | Short node bootstrap |
| `tests/test_config_load.py` | Invalid config raises; valid minimal loads |

---

## 6. Acceptance Criteria

1. `python -m pytest tests/test_config_load.py` passes.
2. Invalid config (missing required key) fails **before** node construction with **one** clear error listing the key.
3. Stub strategy logs structured line on start: `component=strategy`, `event=strategy_started`.
4. `ruff check src` passes (rule set in `pyproject` or `ruff.toml`).
5. `README.md` or `Docs/` pointer: how to run stub node ≤60s smoke **or** waiver paragraph if skip.

---

## 7. Review evidence (standard pack)

### Required test commands

```bash
python -m pytest tests/test_config_load.py -v
ruff check src scripts tests examples
```

### Required log or output artifacts

- One **sample** `strategy_started` log line (redacted)

### Required config or examples

- `config/v1.example.yaml` in PR

### Required demo scenario

- Reviewer runs stub node smoke **or** reads written waiver + lead approval for skip

### Required design or ADR references

- **N/A**

### Required reviewer sign-off inputs

- PR approval with tree listing `src/tyrex_pm/` packages attached or in CI artifact

---

## 8. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Nautilus node smoke needs creds | AC blocked | Time-box; document waiver path |
| Over-engineered DI | Slows delivery | Stub only |

---

## 9. Approval gate

| Role | Responsibility |
|------|----------------|
| **Technical lead** | Confirms layout matches plan and tests pass |

**What must be reviewed**

- Package tree + config validation behavior

**Conditions that block v1.04**

- Missing §9 **Approved** record
- `tests/test_config_load.py` failing on default branch

**Sign-off template**

> Milestone **v1.03** **Approved**. Skeleton + observability baseline merged ___ (date). Reviewer: ___
