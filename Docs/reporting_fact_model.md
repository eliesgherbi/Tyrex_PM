# Reporting fact model (v1)

Structured run observability when **`reporting_enabled: true`** in runtime YAML. Facts append to **`var/reporting/runs/<run_id>/facts.jsonl`**; **`manifest.json`** records paths and data-quality flags; **`python -m tyrex_pm.reporting summarize --run-dir …`** produces **`summary.json`** / **`summary.md`**.

**Code:** `src/tyrex_pm/reporting/` — schema in `schema/facts_v1.py`, join keys in `schema/joins.md` (duplicate table below for convenience).

---

## Join keys (per fact type)

| Fact type | Primary join keys |
|-----------|-------------------|
| `run_manifest` | `run_id` |
| `config_snapshot` | `run_id` |
| `guru_signal` | `run_id`, `correlation_id` |
| `strategy_decision` | `run_id`, `correlation_id` |
| `sizing` | `run_id`, `correlation_id` |
| `risk_decision` | `run_id`, `correlation_id`, optional `account_snapshot_seq` |
| `execution_intent` | `run_id`, `correlation_id` |
| `normalization` | `run_id`, `correlation_id`, optional `client_order_id` |
| `execution_outcome` | `run_id`, `correlation_id`, optional `client_order_id` |
| `order_lifecycle` | `run_id`, `client_order_id`, optional `venue_order_id`, `correlation_id` |
| `fill` | `run_id`, `client_order_id`, optional `venue_order_id`, `correlation_id`, `fill_event_id` |
| `account_snapshot` | `run_id`, `account_snapshot_seq`, optional `correlation_id` |
| `reconciliation` | `run_id`, optional `client_order_id` |

Full list: `src/tyrex_pm/reporting/schema/joins.md`.

---

## Semantics (short)

- **`correlation_id`:** stable guru-trade identity (e.g. transaction hash + outcome token), carried from signal through risk and execution.
- **`risk_decision`:** includes deployment fields (`order_deploy_usd_at_eval`, token/portfolio deploy), clip/bump flags (`max_notional_policy_clipped`, `min_notional_policy_bumped`), and optional capital snapshots when observability is on.
- **`normalization`:** instrument grid quantize; `skipped_submit: true` + `exec_instrument_quantize_skip` when venue min size cannot be satisfied without exceeding risk-approved qty.
- **Capital:** `account_snapshot` and enriched `risk_decision` rows may include Nautilus cash vs py-clob balance strings; see `capital_canonical_balance_source` fields in facts.

---

## Further reading

- **Module guide:** [modules/reporting/DEVELOPER.md](modules/reporting/DEVELOPER.md) (if present) or [modules/reporting/README.md](modules/reporting/README.md)
- **Operators:** [OPERATIONS.md](OPERATIONS.md) — log grep + reporting paths
