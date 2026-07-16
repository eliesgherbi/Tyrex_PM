# EXPLORATORY TRACE

> Exploratory only. Useful for intuition and M2B.5 hypothesis selection.
> Not valid for live YAML or enforcement.

- exploratory_only: True
- do_not_use_in_live_yaml: True
- overfit_warning: single-day 27-market sample; assumed latency and/or proxy formulas where applicable

- notebook: 05_pm_btc_chainlink_leadlag
- feed_role_note: Binance may reflect trader reaction. Chainlink reflects settlement/reference. Do not collapse them.
- case_study_market_ids: ['btc_5m_20260705_1705', 'btc_5m_20260705_1710', 'btc_5m_20260705_1715']
- per_feed_offset_summary: {'chainlink': {'p25': 3085.22, 'p50': 3708.877, 'p75': 4348.905000000001, 'p90': 4978.1585000000005, 'p95': 5347.772499999999}, 'binance': {'p25': 1978.9809999999998, 'p50': 2510.767, 'p75': 3140.1805000000004, 'p90': 4233.386, 'p95': 4605.7311}}
- clock_sync_summary: {'p25': 1978.981, 'p50': 2510.767, 'p75': 3140.1805000000004, 'p90': 4233.386, 'p95': 4605.7311}
- chainlink_crossing_count: 167
- binance_crossing_count: not_computable (minute-bucket alignment insufficient)
- pm_reaction_lag_estimate_ms: not_computable (aligned PM/BTC/CL pairs insufficient)
- manual_story_notes: ['btc_5m_20260705_1705: exploratory visual case study — inspect plot_market_story panel', 'btc_5m_20260705_1710: exploratory visual case study — inspect plot_market_story panel', 'btc_5m_20260705_1715: exploratory visual case study — inspect plot_market_story panel']
- alignment_issue_hypothesis: High recv/source offsets and minute-level merge limit strict lead-lag; event-aligned replay needed in M2B.5.
- plot_artifacts: ['05_feed_offset_diagnostic.png', '05_market_story_btc_5m_20260705_1705.png', '05_market_story_btc_5m_20260705_1710.png', '05_market_story_btc_5m_20260705_1715.png']
