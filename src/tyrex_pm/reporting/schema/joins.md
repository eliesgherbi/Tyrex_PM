# Reporting join contract (SCH-01)

| Fact type | Primary join keys |
|-----------|-------------------|
| `run_manifest` | `run_id` |
| `config_snapshot` | `run_id` |
| `guru_signal` | `run_id`, `correlation_id` |
| `guru_shadow_compare` | `run_id`, `correlation_id` |
| `health_anomaly` | `run_id`, optional `correlation_id` |
| `strategy_decision` | `run_id`, `correlation_id` |
| `sizing` | `run_id`, `correlation_id` |
| `risk_decision` | `run_id`, `correlation_id`, optional `account_snapshot_seq` (same-run link to `account_snapshot`) |
| `execution_intent` | `run_id`, `correlation_id` |
| `normalization` | `run_id`, `correlation_id`, optional `client_order_id` |
| `book_constraint` | `run_id`, `correlation_id` |
| `execution_outcome` | `run_id`, `correlation_id`, optional `client_order_id`, optional `external_order_id` |
| `order_correlation_map` | `run_id`, `correlation_id`, `client_order_id` |
| `order_lifecycle` | `run_id`, `client_order_id`, optional `venue_order_id`, `correlation_id` |
| `fill` | `run_id`, `client_order_id`, optional `venue_order_id`, `correlation_id`, `fill_event_id` |
| `account_snapshot` | `run_id`, `account_snapshot_seq`, optional `correlation_id` |
| `exposure` | `run_id`, `correlation_id` (optional for aggregate-only rows) |
| `position` | `run_id`, `instrument_id` |
| `component_status` | `run_id` |
| `report_pipeline_health` | `run_id` |
| `reconciliation` | `run_id`, optional `client_order_id` |

**Historical runs:** if guru orders did not flow through Nautilus, `external_order_id` on `execution_outcome` may be the primary correlation key and order lifecycle facts may be sparse — `data_quality.legacy_execution_truth_partial` may apply. **Current live** uses **`NautilusGuruExecutionPort`** only.
