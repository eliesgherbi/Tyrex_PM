# EXPLORATORY TRACE

> Exploratory only. Useful for intuition and M2B.5 hypothesis selection.
> Not valid for live YAML or enforcement.

- exploratory_only: True
- do_not_use_in_live_yaml: True
- overfit_warning: single-day 27-market sample; assumed latency and/or proxy formulas where applicable

- notebook: 06_prereplay_counterfactual
- notebook_06_is_not_m2b5: True
- exploratory_grid: {'assumed_latency_buffers': {'assumed_latency_100ms_buffer': {'p25': 0.0, 'p50': 0.0050000000000000044, 'p75': 0.015000000000000013, 'p90': 0.03500000000000003, 'p95': 0.05499999999999999}, 'assumed_latency_100ms_buffer_note': 'exploratory p-quantiles of |mid(t)-mid(t-(window+latency))| across windows; NOT live YAML', 'assumed_latency_250ms_buffer': {'p25': 0.0, 'p50': 0.009999999999999898, 'p75': 0.020000000000000018, 'p90': 0.040000000000000036, 'p95': 0.06000000000000005}, 'assumed_latency_250ms_buffer_note': 'exploratory p-quantiles of |mid(t)-mid(t-(window+latency))| across windows; NOT live YAML', 'assumed_latency_500ms_buffer': {'p25': 0.0, 'p50': 0.010000000000000009, 'p75': 0.020000000000000018, 'p90': 0.04999999999999999, 'p95': 0.06999999999999995}, 'assumed_latency_500ms_buffer_note': 'exploratory p-quantiles of |mid(t)-mid(t-(window+latency))| across windows; NOT live YAML', 'assumed_latency_1000ms_buffer': {'p25': 0.0, 'p50': 0.010000000000000009, 'p75': 0.030000000000000027, 'p90': 0.05999999999999994, 'p95': 0.08000000000000007}, 'assumed_latency_1000ms_buffer_note': 'exploratory p-quantiles of |mid(t)-mid(t-(window+latency))| across windows; NOT live YAML'}, 'survivor_rates': {'recovery_touched_rate': 0.1154, 'never_armed_and_lost_proxy_rate': 0.4231}, 'depth_slippage': {'fak_slippage': {'p25': 0.0, 'p50': 0.0, 'p75': 0.0, 'p90': 0.0, 'p95': 0.0}, 'fak_ladder_exploratory': [0.01, 0.02, 0.03], 'min_depth_fraction_exploratory': 0.05}, 'leadlag_case_studies': ['btc_5m_20260705_1705', 'btc_5m_20260705_1710', 'btc_5m_20260705_1715'], 'coverage': {'markets_included': 26}}
- strict_grid_note: Strict m2b5_parameter_grid remains in *_decision.json only
- purpose: Assemble hypotheses and provisional grids for later M2B.5 replay validation
