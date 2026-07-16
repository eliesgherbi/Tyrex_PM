# EXPLORATORY TRACE

> Exploratory only. Useful for intuition and M2B.5 hypothesis selection.
> Not valid for live YAML or enforcement.

- exploratory_only: True
- do_not_use_in_live_yaml: True
- overfit_warning: single-day 27-market sample; assumed latency and/or proxy formulas where applicable

- notebook: 03_survivor_reachability
- proxy_warning: These are research proxy formulas. They are not live survival replay. Use only to decide what M2B.5 should test first.
- episode_count: 26
- recovery_touched_count: 3
- recovery_touched_rate: 0.1154
- never_armed_and_lost_proxy_count: 11
- never_armed_and_lost_proxy_rate: 0.4231
- mfe_quantiles: {'p25': 0.10500000000000001, 'p50': 0.1575, 'p75': 0.2275, 'p90': 0.33499999999999996, 'p95': 0.38}
- mae_quantiles: {'p25': -0.25, 'p50': -0.18, 'p75': -0.10250000000000001, 'p90': -0.09, 'p95': -0.0825}
- giveback_quantiles: {'p25': 0.0175, 'p50': 0.07500000000000001, 'p75': 0.23750000000000002, 'p90': 0.44, 'p95': 0.5925}
- time_to_peak_quantiles: not_computable (proxy path lacks explicit peak timestamp)
- split_rates: {'tte_bucket|full_window_proxy': {'episodes': ['btc_5m_20260705_1705', 'btc_5m_20260705_1710', 'btc_5m_20260705_1715', 'btc_5m_20260705_1720', 'btc_5m_20260705_1725', 'btc_5m_20260705_1730', 'btc_5m_20260705_1735', 'btc_5m_20260705_1740', 'btc_5m_20260705_1745', 'btc_5m_20260705_1750', 'btc_5m_20260705_1755', 'btc_5m_20260705_1800', 'btc_5m_20260705_1805', 'btc_5m_20260705_1810', 'btc_5m_20260705_1815', 'btc_5m_20260705_1820', 'btc_5m_20260705_1825', 'btc_5m_20260705_1830', 'btc_5m_20260705_1835', 'btc_5m_20260705_1840', 'btc_5m_20260705_1845', 'btc_5m_20260705_1850', 'btc_5m_20260705_1855', 'btc_5m_20260705_1900', 'btc_5m_20260705_1905', 'btc_5m_20260705_1910'], 'recovery_touched': 3, 'never_armed_and_lost': 11, 'n': 26, 'recovery_touched_rate': 0.1154, 'never_armed_and_lost_rate': 0.4231, 'insufficient_cell': False}, 'dist_tercile|low': {'episodes': ['btc_5m_20260705_1705', 'btc_5m_20260705_1735', 'btc_5m_20260705_1750', 'btc_5m_20260705_1755', 'btc_5m_20260705_1800', 'btc_5m_20260705_1805', 'btc_5m_20260705_1810', 'btc_5m_20260705_1815', 'btc_5m_20260705_1820'], 'recovery_touched': 3, 'never_armed_and_lost': 1, 'n': 9, 'recovery_touched_rate': 0.3333, 'never_armed_and_lost_rate': 0.1111, 'insufficient_cell': False}, 'dist_tercile|high': {'episodes': ['btc_5m_20260705_1710', 'btc_5m_20260705_1730', 'btc_5m_20260705_1845', 'btc_5m_20260705_1850', 'btc_5m_20260705_1855', 'btc_5m_20260705_1900', 'btc_5m_20260705_1905', 'btc_5m_20260705_1910'], 'recovery_touched': 0, 'never_armed_and_lost': 5, 'n': 8, 'recovery_touched_rate': 0.0, 'never_armed_and_lost_rate': 0.625, 'insufficient_cell': False}, 'dist_tercile|mid': {'episodes': ['btc_5m_20260705_1715', 'btc_5m_20260705_1720', 'btc_5m_20260705_1725', 'btc_5m_20260705_1740', 'btc_5m_20260705_1745', 'btc_5m_20260705_1825', 'btc_5m_20260705_1830', 'btc_5m_20260705_1835', 'btc_5m_20260705_1840'], 'recovery_touched': 0, 'never_armed_and_lost': 5, 'n': 9, 'recovery_touched_rate': 0.0, 'never_armed_and_lost_rate': 0.5556, 'insufficient_cell': False}}
- plot_artifacts: ['03_mfe_mae_scatter.png', '03_giveback_histogram.png']
