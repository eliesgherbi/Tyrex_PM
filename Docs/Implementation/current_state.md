# Tyrex_PM — implementation status (maintainer hub)

**Purpose:** What the **code in `src/tyrex_pm/`** does today, where to look when something fails, and how live vs shadow differ.

**Navigation:** [Documentation index](../README.md) · [Architecture](../Architecture.md) · [OPERATIONS](../OPERATIONS.md) · [End-to-end review](end_to_end_review_logic.md)

**Historical planning (not behavior spec):** [road_map.md](road_map.md) is an archived phased plan; treat this file and Architecture as the live description of the product.

---

## 1. Layer responsibilities

| Layer | Responsibility |
|-------|----------------|
| **`data/`** | Guru **poll** (`GuruMonitorActor`) + optional **RTDS stream** (`GuruStreamActor`) → **`GuruSignalPipeline`** → `GuruTradeSignal` on the bus (`guru_ingest_mode`: `poll_only` / `rtds_shadow` / `rtds_primary`). |
| **`strategy/`** | `CopyStrategy`: entry/exit policies → sizing → `OrderIntent` → **`risk.evaluate`** → **`execution.submit_intent`**. Forwards **`on_order_event`** to the execution port when **`notify_order_event`** exists (limit-order timeout cleanup). **No** `Cache` / `Portfolio` imports (guarded by tests). |
| **`risk/`** | `ConfiguredRiskPolicy`: pre-trade gates from **`RiskSettings`** + injected readers (pending/filled deployment, capital, concurrent rests). |
| **`execution/`** | `ExecutionPort`: **`NoOpExecutionPort`** (shadow), **`NautilusGuruExecutionPort`** (live — `submit_order`; optional book guard / depth clip / limit timeout). |
| **`runtime/`** | `build_guru_trading_node`: `TradingNode`, factories, **`GuruTradingAssembly`**, reader injection, optional RTDS actor, compose-time validation for shadow vs live. |
| **`runtime/state_readers.py`** | Canonical read boundary for Nautilus `Cache` / `Portfolio` and py-clob allowance. |
| **`reporting/`** | When **`reporting_enabled`**: durable **`facts.jsonl`**, manifest, optional SQLite + **`summarize`**. See [reporting_fact_model.md](../reporting_fact_model.md). |

---

## 2. Live vs shadow matrix

| Mode | `TradingNode` clients | Submit | Pending deployment (caps) | Filled deployment (caps) |
|------|----------------------|--------|---------------------------|-------------------------|
| Shadow | Empty | `NoOpExecutionPort` | N/A | N/A |
| Live | Polymarket data + exec | `NautilusGuruExecutionPort` → `submit_order` | Cache open orders: leaves × limit price | Open positions: abs(qty) × avg_px_open (`NautilusDeploymentBudget`) |

Instrument resolution (live): **`GuruInstrumentDynamicController`** (Gamma + CLOB + `Cache`); empty `polymarket_instrument_ids` implies zero-bootstrap dynamic resolution when live.

---

## 3. Ingest (RTDS + poll)

**Implemented:** RTDS `activity`/`trades`, `proxyWallet` match to strategy `guru_wallet_address`, dedup id `transactionHash:asset` when available, reconnect + liveness + REST gap-fill, poll fallback in `rtds_primary`, stream compare mode in `rtds_shadow`.

**Operators:** [OPERATIONS.md](../OPERATIONS.md) § Guru ingestion; validation scripts `scripts/guru_shadow_report.py`, `scripts/guru_primary_report.py`, `scripts/spike_rtds_activity.py`.

---

## 4. Configuration (short)

- **Runtime:** `guru_ingest_mode`, RTDS URLs/timeouts, gap-fill, `polymarket_*`, optional **`execution_*`** (live book hooks), **`reporting_*`** — [CONFIG_MODEL.md](../CONFIG_MODEL.md).
- **Strategy:** `copy_scale`, optional **conviction** sizing fields — same doc.
- **Risk:** Per-order min/max **deploy** policies (`deny` / `cap`), token/portfolio deployment caps, concurrent guru rests, collateral reserve, capital gate — same doc. **Shadow** cannot combine finite portfolio cap or concurrent rests or positive reserve with lack of live framework readers (compose raises).
- **Secrets:** `.env` only.

---

## 5. Failure classes (where to look)

| Symptom | Typical cause |
|---------|----------------|
| `copy_skip` + `risk_denied` | `ConfiguredRiskPolicy` — see reason / `ReasonCode` |
| `RISK_INSUFFICIENT_*`, `RISK_ACCOUNT_UNAVAILABLE`, `RISK_ALLOWANCE_UNAVAILABLE`, reserve breach | Capital gate / collateral reserve — [OPERATIONS.md](../OPERATIONS.md) |
| `RISK_*_DEPLOYMENT_*`, concurrent rests | Deployment-budget and rest-count gates |
| `RISK_MIN_ORDER_NOTIONAL` | Risk YAML min deploy + `deny` policy |
| `exec_instrument_quantize_skip` | Risk-approved qty below venue **min_quantity** or unquantizable grid — execution skips submit |
| `GURU_INSTRUMENT_*`, `GURU_DYNAMIC_*` | Resolution / cache / warmup |
| Cap vs intuition | Deployment uses **cost basis** pending + filled, not mark-to-market — `runtime/deployment_budget.py` |

---

## 6. Restart

`TradingNodeConfig`: **`load_state=False`**, **`save_state=False`** in Tyrex compose. Post-restart truth comes from **Nautilus + adapter** reconciliation and optional guru cache warmup — not a separate Tyrex ledger.

**Live checklist** (restart, denials, reporting): [phase_b_operational_validation.md](phase_b_operational_validation.md) and [Runbooks/deployment_budget_live_validation.md](../Runbooks/deployment_budget_live_validation.md).

---

## 7. What to read next

| Topic | Document |
|-------|----------|
| Operators & logs | [OPERATIONS.md](../OPERATIONS.md) |
| YAML fields | [CONFIG_MODEL.md](../CONFIG_MODEL.md) |
| Module boundaries | [developer_guide.md](../developer_guide.md), [modules/README.md](../modules/README.md) |
| End-to-end signal → outcome | [end_to_end_review_logic.md](end_to_end_review_logic.md) |
| Archived roadmap | [road_map.md](road_map.md) |
