# EXPLORATORY TRACE

> Exploratory only. Useful for intuition and M2B.5 hypothesis selection.
> Not valid for live YAML or enforcement.

- exploratory_only: True
- do_not_use_in_live_yaml: True
- overfit_warning: single-day 27-market sample; assumed latency and/or proxy formulas where applicable

- notebook: 01_coverage_quality
- markets_total: 27
- markets_included: 26
- markets_excluded: 1
- ptb_present_rate: 0.963
- final_reference_present_rate: 0.963
- gap_rate_summary: {'p25': 0.9290004999999999, 'p50': 0.933601, 'p75': 0.946167, 'p90': 0.9484746, 'p95': 0.9511726}
- gap_rate_usable_as_feature: False
- gap_rate_usability_note: diagnostic_only per strict verdict; do not use as strategy feature
- dropped_events_summary: 0
- corrupt_rows_summary: 0
- label_validity_status_counts: {'valid_proxy': 25, 'missing_ptb': 1, 'partial': 1}
- ws_seq_gap_audit: {'total_events': 1208764, 'ws_seq_gap_count': 1208097, 'gap_fraction': 0.9994, 'verdict': 'diagnostic_only'}
- excluded_markets: [{'market_id': 'btc_5m_20260705_1700', 'exclusion_reason': 'price_to_beat_missing'}]
- plot_artifacts: ['01_included_excluded_bar.png', '01_gap_rate_distribution.png', '01_ptb_final_reference.png']
