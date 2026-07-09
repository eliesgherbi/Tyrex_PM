# EXPLORATORY TRACE

> Exploratory only. Useful for intuition and M2B.5 hypothesis selection.
> Not valid for live YAML or enforcement.

- exploratory_only: True
- do_not_use_in_live_yaml: True
- overfit_warning: single-day 27-market sample; assumed latency and/or proxy formulas where applicable

- notebook: 04_depth_slippage_vacuum
- depth_p50_p90_by_tte_bucket: {'T-120_60': {'p25': 111.0, 'p50': 180.0, 'p75': 429.395, 'p90': 1520.75, 'p95': 2593.14}, 'T-60_0': {'p25': 132.4, 'p50': 185.8, 'p75': 405.95, 'p90': 1596.885, 'p95': 2468.3625}, 'T+0_60': {'p25': 106.99000000000001, 'p50': 144.89999999999998, 'p75': 247.15, 'p90': 989.4000000000001, 'p95': 1744.7769999999998}}
- top_of_book_size_summary: {'p25': 112.0, 'p50': 179.95, 'p75': 426.17, 'p90': 1527.5500000000002, 'p95': 2587.9599999999987}
- simulated_5share_fak_slippage: {'p25': 0.0, 'p50': 0.0, 'p75': 0.0, 'p90': 0.0, 'p95': 0.0}
- depth_evaporation_event_count: 2335
- median_spread: 0.01
- strict_no_go_explanation: Strict liquidity_vacuum_warning is no-go because median spread and evaporation signals on this tiny sample indicate thin-book risk; exploratory candidates are NOT enablement.
- vacuum_candidate_features_correlation_table: {'spread_vs_evap': 'positive exploratory association on sample', 'depth_vs_slippage': 'inverse exploratory association on sample'}
- provisional_fak_ladder_exploratory: [0.01, 0.02, 0.03]
- provisional_min_depth_fraction_exploratory: 0.05
