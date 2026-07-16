# Phase 1 live run log template

Copy one row per run.

| run_id | market_id | profile | survival_enabled | enforce_module | entry_prices_ui | exit_prices_ui | manual_ui_pnl | bot_pnl_status | final_state | survivor_phase_reached | target_mode | full_recovery_target | selected_target | stall_detected | trailing_triggered | enforce_exit_submitted | notes |
|--------|-----------|---------|------------------|----------------|-----------------|----------------|---------------|----------------|-------------|------------------------|-------------|----------------------|-----------------|----------------|--------------------|------------------------|-------|
| | btc_5m_YYYYMMDD_HHMM | advisory / target_only / trailing_enforce / stall_enforce | true | none / trailing_stop / stall_exit | yes/no | yes/no/loser | | tentative/final/unavailable | DONE/FAILED | y/n | full_recovery/breakeven/... | | | y/n | y/n | y/n | |

## Example row (advisory)

| paired_binary_phase1_tiny_1782974031 | btc_5m_20260702_0640 | advisory | true | none | 0.48/0.53 | 0.38/0.88 | (fill from PM UI) | tentative | DONE | y | full_recovery | 0.847 | 0.847 | n | y | n | YES stop, NO TP, trailing advisory triggered |

## Validator fields to paste

- `phase1_classification`
- `pnl_status`
- `pnl_blocker_for_enforcement`
- `manual_ui_pnl_required`
