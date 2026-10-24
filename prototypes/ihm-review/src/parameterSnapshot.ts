// Generated read-only source snapshot. Regenerate with export_parameter_snapshot.py.
export const parameterSnapshot = {
  "captured": "2026-10-22",
  "defaults": {
    "settlement.functional_id": "reference_at_close_vs_open",
    "settlement.comparison": "AT_OR_ABOVE_K",
    "settlement.reference_series": "chainlink:btcusd:twap:60",
    "settlement.reference_units": "USD",
    "settlement.timestamp_meaning": "provider_source_time",
    "settlement.provenance_status": "ASSUMED",
    "settlement.evidence": [],
    "model.backend": "endpoint_diffusion",
    "model.averaging_window_s": 60.0,
    "model.spot_input": "binance_spot",
    "model.sigma_band": 0.25,
    "model.reference_band_bps": 1.0,
    "model.tau_floor_s": 1.0,
    "volatility.half_life_s": 30.0,
    "volatility.min_samples_s": 20.0,
    "volatility.jump_threshold_sigma": 4.0,
    "volatility.sample_interval_s": 1.0,
    "volatility.tau_floor_s": 1.0,
    "volatility.sigma_floor": 1e-05,
    "volatility.sigma_scale": 1.0,
    "volatility.variance_time_policy": "per_sample_v0",
    "entry.tau_min_s": 30.0,
    "entry.tau_max_s": 240.0,
    "entry.min_edge_per_share": "0.02",
    "entry.target_mode": "cap_only",
    "entry.target_profit_usdc": "3",
    "entry.max_entry_price": "0.97",
    "entry.limit_buffer_ticks": 0,
    "sizing.max_debit_usdc": "5",
    "sizing.max_loss_usdc": "5",
    "sizing.depth_participation": "1",
    "sizing.max_levels": 32,
    "sizing.share_increment": "0.01",
    "sizing.default_min_order_shares": "5",
    "quality.accepted_k_qualities": [
      "CONFIRMED_CANONICAL"
    ],
    "quality.require_verified_settlement_spec": false,
    "quality.max_clock_uncertainty_ms": 250,
    "fees.rate": "0.07",
    "fees.exponent": "1",
    "fees.provenance": "SCENARIO_ASSUMPTION_NOT_VERIFIED",
    "retry.enabled": false,
    "retry.max_attempts_per_episode": 3,
    "retry.max_attempts_per_window": 3,
    "retry.max_prepare_failures_per_episode": 3,
    "retry.max_posts_per_episode": 3,
    "retry.max_posts_per_window": 3,
    "retry.max_retry_duration_s": 30.0,
    "retry.min_retry_interval_ms": 500,
    "retry.service_backoff_ms": 1000,
    "retry.service_backoff_max_ms": 8000,
    "exit.policy": "hold_to_resolution",
    "exit.value_exit_enabled": false,
    "exit.value_exit_margin_per_share": "0.02",
    "exit.value_exit_basis": "upper",
    "exit.value_exit_trend_enabled": false,
    "exit.value_exit_trend_policy": "evaluated_bid_trend_v1",
    "exit.value_exit_trend_lookback_s": 5.0,
    "exit.value_exit_trend_min_drop": "0.02",
    "exit.value_exit_trend_confirmation_s": 2.0,
    "exit.value_exit_trend_max_gap_s": 2.0,
    "exit.thesis_stop_enabled": false,
    "exit.thesis_stop_probability": 0.3,
    "exit.exit_cutoff_tau_s": 15.0,
    "exit.residual_policy": "hold_to_resolution",
    "exit.min_exit_price": "0.01",
    "exit.max_exit_attempts_per_episode": 3,
    "reentry.max_trades_per_window": 1,
    "reentry.rearm": "signal_reset",
    "reentry.cooldown_s": 10.0,
    "reentry.lock_after_protective_stop": true,
    "reentry.lock_after_thesis_stop": true,
    "reentry.same_side_allowed": true,
    "reentry.opposite_side_allowed": false,
    "reentry.min_extra_edge_per_share": "0",
    "reentry.last_entry_tau_s": 60.0,
    "reentry.max_window_debit_usdc": "5",
    "disagreement_veto.enabled": false,
    "disagreement_veto.max_gap_pp": 0.0,
    "disagreement_veto.probability": "point",
    "disagreement_veto.market_price": "up_two_sided_mid",
    "disagreement_veto.recheck_at_dispatch": false
  },
  "fields": [
    {
      "key": "settlement.functional_id",
      "section": "settlement",
      "type": "text",
      "integer": false
    },
    {
      "key": "settlement.comparison",
      "section": "settlement",
      "type": "text",
      "integer": false
    },
    {
      "key": "settlement.reference_series",
      "section": "settlement",
      "type": "text",
      "integer": false
    },
    {
      "key": "settlement.reference_units",
      "section": "settlement",
      "type": "text",
      "integer": false
    },
    {
      "key": "settlement.timestamp_meaning",
      "section": "settlement",
      "type": "text",
      "integer": false
    },
    {
      "key": "settlement.provenance_status",
      "section": "settlement",
      "type": "text",
      "integer": false
    },
    {
      "key": "settlement.evidence",
      "section": "settlement",
      "type": "list",
      "integer": false
    },
    {
      "key": "model.backend",
      "section": "model",
      "type": "text",
      "integer": false
    },
    {
      "key": "model.averaging_window_s",
      "section": "model",
      "type": "number",
      "integer": false
    },
    {
      "key": "model.spot_input",
      "section": "model",
      "type": "text",
      "integer": false
    },
    {
      "key": "model.sigma_band",
      "section": "model",
      "type": "number",
      "integer": false
    },
    {
      "key": "model.reference_band_bps",
      "section": "model",
      "type": "number",
      "integer": false
    },
    {
      "key": "model.tau_floor_s",
      "section": "model",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.half_life_s",
      "section": "volatility",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.min_samples_s",
      "section": "volatility",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.jump_threshold_sigma",
      "section": "volatility",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.sample_interval_s",
      "section": "volatility",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.tau_floor_s",
      "section": "volatility",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.sigma_floor",
      "section": "volatility",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.sigma_scale",
      "section": "volatility",
      "type": "number",
      "integer": false
    },
    {
      "key": "volatility.variance_time_policy",
      "section": "volatility",
      "type": "text",
      "integer": false
    },
    {
      "key": "entry.tau_min_s",
      "section": "entry",
      "type": "number",
      "integer": false
    },
    {
      "key": "entry.tau_max_s",
      "section": "entry",
      "type": "number",
      "integer": false
    },
    {
      "key": "entry.min_edge_per_share",
      "section": "entry",
      "type": "number",
      "integer": false
    },
    {
      "key": "entry.target_mode",
      "section": "entry",
      "type": "text",
      "integer": false
    },
    {
      "key": "entry.target_profit_usdc",
      "section": "entry",
      "type": "number",
      "integer": false
    },
    {
      "key": "entry.max_entry_price",
      "section": "entry",
      "type": "number",
      "integer": false
    },
    {
      "key": "entry.limit_buffer_ticks",
      "section": "entry",
      "type": "number",
      "integer": true
    },
    {
      "key": "sizing.max_debit_usdc",
      "section": "sizing",
      "type": "number",
      "integer": false
    },
    {
      "key": "sizing.max_loss_usdc",
      "section": "sizing",
      "type": "number",
      "integer": false
    },
    {
      "key": "sizing.depth_participation",
      "section": "sizing",
      "type": "number",
      "integer": false
    },
    {
      "key": "sizing.max_levels",
      "section": "sizing",
      "type": "number",
      "integer": true
    },
    {
      "key": "sizing.share_increment",
      "section": "sizing",
      "type": "number",
      "integer": false
    },
    {
      "key": "sizing.default_min_order_shares",
      "section": "sizing",
      "type": "number",
      "integer": false
    },
    {
      "key": "quality.accepted_k_qualities",
      "section": "quality",
      "type": "list",
      "integer": false
    },
    {
      "key": "quality.require_verified_settlement_spec",
      "section": "quality",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "quality.max_clock_uncertainty_ms",
      "section": "quality",
      "type": "number",
      "integer": true
    },
    {
      "key": "fees.rate",
      "section": "fees",
      "type": "number",
      "integer": false
    },
    {
      "key": "fees.exponent",
      "section": "fees",
      "type": "number",
      "integer": false
    },
    {
      "key": "fees.provenance",
      "section": "fees",
      "type": "text",
      "integer": false
    },
    {
      "key": "retry.enabled",
      "section": "retry",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "retry.max_attempts_per_episode",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "retry.max_attempts_per_window",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "retry.max_prepare_failures_per_episode",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "retry.max_posts_per_episode",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "retry.max_posts_per_window",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "retry.max_retry_duration_s",
      "section": "retry",
      "type": "number",
      "integer": false
    },
    {
      "key": "retry.min_retry_interval_ms",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "retry.service_backoff_ms",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "retry.service_backoff_max_ms",
      "section": "retry",
      "type": "number",
      "integer": true
    },
    {
      "key": "exit.policy",
      "section": "exit",
      "type": "text",
      "integer": false
    },
    {
      "key": "exit.value_exit_enabled",
      "section": "exit",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "exit.value_exit_margin_per_share",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.value_exit_basis",
      "section": "exit",
      "type": "text",
      "integer": false
    },
    {
      "key": "exit.value_exit_trend_enabled",
      "section": "exit",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "exit.value_exit_trend_policy",
      "section": "exit",
      "type": "text",
      "integer": false
    },
    {
      "key": "exit.value_exit_trend_lookback_s",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.value_exit_trend_min_drop",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.value_exit_trend_confirmation_s",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.value_exit_trend_max_gap_s",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.thesis_stop_enabled",
      "section": "exit",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "exit.thesis_stop_probability",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.exit_cutoff_tau_s",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.residual_policy",
      "section": "exit",
      "type": "text",
      "integer": false
    },
    {
      "key": "exit.min_exit_price",
      "section": "exit",
      "type": "number",
      "integer": false
    },
    {
      "key": "exit.max_exit_attempts_per_episode",
      "section": "exit",
      "type": "number",
      "integer": true
    },
    {
      "key": "reentry.max_trades_per_window",
      "section": "reentry",
      "type": "number",
      "integer": true
    },
    {
      "key": "reentry.rearm",
      "section": "reentry",
      "type": "text",
      "integer": false
    },
    {
      "key": "reentry.cooldown_s",
      "section": "reentry",
      "type": "number",
      "integer": false
    },
    {
      "key": "reentry.lock_after_protective_stop",
      "section": "reentry",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "reentry.lock_after_thesis_stop",
      "section": "reentry",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "reentry.same_side_allowed",
      "section": "reentry",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "reentry.opposite_side_allowed",
      "section": "reentry",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "reentry.min_extra_edge_per_share",
      "section": "reentry",
      "type": "number",
      "integer": false
    },
    {
      "key": "reentry.last_entry_tau_s",
      "section": "reentry",
      "type": "number",
      "integer": false
    },
    {
      "key": "reentry.max_window_debit_usdc",
      "section": "reentry",
      "type": "number",
      "integer": false
    },
    {
      "key": "disagreement_veto.enabled",
      "section": "disagreement_veto",
      "type": "boolean",
      "integer": false
    },
    {
      "key": "disagreement_veto.max_gap_pp",
      "section": "disagreement_veto",
      "type": "number",
      "integer": false
    },
    {
      "key": "disagreement_veto.probability",
      "section": "disagreement_veto",
      "type": "text",
      "integer": false
    },
    {
      "key": "disagreement_veto.market_price",
      "section": "disagreement_veto",
      "type": "text",
      "integer": false
    },
    {
      "key": "disagreement_veto.recheck_at_dispatch",
      "section": "disagreement_veto",
      "type": "boolean",
      "integer": false
    }
  ],
  "yamlBase": {
    "name": "Leader 5\u00a2 / 240s",
    "source": "config/runs/settlement_value_leader_5c_240s_paper.yaml",
    "values": {
      "settlement.functional_id": "twap60_at_close_vs_open",
      "settlement.comparison": "AT_OR_ABOVE_K",
      "settlement.reference_series": "polymarket_rtds:crypto_prices_twap_sixty:btc/usd",
      "settlement.reference_units": "USD",
      "settlement.timestamp_meaning": "provider_source_time",
      "settlement.provenance_status": "ASSUMED",
      "settlement.evidence": [],
      "model.backend": "twap_proxy_diffusion",
      "model.averaging_window_s": 60.0,
      "model.spot_input": "binance_spot",
      "model.sigma_band": 0.25,
      "model.reference_band_bps": 1.0,
      "model.tau_floor_s": 1.0,
      "volatility.half_life_s": 30.0,
      "volatility.min_samples_s": 20.0,
      "volatility.jump_threshold_sigma": 4.0,
      "volatility.sample_interval_s": 1.0,
      "volatility.tau_floor_s": 1.0,
      "volatility.sigma_floor": 1e-05,
      "volatility.sigma_scale": 1.0,
      "volatility.variance_time_policy": "per_sample_v0",
      "entry.tau_min_s": 30.0,
      "entry.tau_max_s": 240.0,
      "entry.min_edge_per_share": "0.05",
      "entry.target_mode": "cap_only",
      "entry.target_profit_usdc": "3",
      "entry.max_entry_price": "0.97",
      "entry.limit_buffer_ticks": 1,
      "sizing.max_debit_usdc": "5",
      "sizing.max_loss_usdc": "5",
      "sizing.depth_participation": "1",
      "sizing.max_levels": 32,
      "sizing.share_increment": "0.01",
      "sizing.default_min_order_shares": "5",
      "quality.accepted_k_qualities": [
        "CONFIRMED_CANONICAL",
        "PROVISIONAL"
      ],
      "quality.require_verified_settlement_spec": false,
      "quality.max_clock_uncertainty_ms": 250,
      "fees.rate": "0.07",
      "fees.exponent": "1",
      "fees.provenance": "MUST_MATCH_PROVIDER_FEE_SCHEDULE",
      "retry.enabled": false,
      "retry.max_attempts_per_episode": 1,
      "retry.max_attempts_per_window": 1,
      "retry.max_prepare_failures_per_episode": 1,
      "retry.max_posts_per_episode": 1,
      "retry.max_posts_per_window": 1,
      "retry.max_retry_duration_s": 30.0,
      "retry.min_retry_interval_ms": 500,
      "retry.service_backoff_ms": 1000,
      "retry.service_backoff_max_ms": 8000,
      "exit.policy": "hold_to_resolution",
      "exit.value_exit_enabled": false,
      "exit.value_exit_margin_per_share": "0.02",
      "exit.value_exit_basis": "upper",
      "exit.value_exit_trend_enabled": false,
      "exit.value_exit_trend_policy": "evaluated_bid_trend_v1",
      "exit.value_exit_trend_lookback_s": 5.0,
      "exit.value_exit_trend_min_drop": "0.02",
      "exit.value_exit_trend_confirmation_s": 2.0,
      "exit.value_exit_trend_max_gap_s": 2.0,
      "exit.thesis_stop_enabled": false,
      "exit.thesis_stop_probability": 0.3,
      "exit.exit_cutoff_tau_s": 15.0,
      "exit.residual_policy": "hold_to_resolution",
      "exit.min_exit_price": "0.01",
      "exit.max_exit_attempts_per_episode": 3,
      "reentry.max_trades_per_window": 1,
      "reentry.rearm": "signal_reset",
      "reentry.cooldown_s": 10.0,
      "reentry.lock_after_protective_stop": true,
      "reentry.lock_after_thesis_stop": true,
      "reentry.same_side_allowed": true,
      "reentry.opposite_side_allowed": false,
      "reentry.min_extra_edge_per_share": "0",
      "reentry.last_entry_tau_s": 60.0,
      "reentry.max_window_debit_usdc": "5",
      "disagreement_veto.enabled": false,
      "disagreement_veto.max_gap_pp": 0.0,
      "disagreement_veto.probability": "point",
      "disagreement_veto.market_price": "up_two_sided_mid",
      "disagreement_veto.recheck_at_dispatch": false
    }
  },
  "scenarios": {
    "friction": {
      "values": {
        "schema_version": 1,
        "adapter": "strategy_closed_loop_v1",
        "name": "friction",
        "initial_cash": "100",
        "feed_delay_ms": 100,
        "decision_delay_ms": 0,
        "order_delay_ms": 300,
        "cancel_delay_ms": 300,
        "feedback_delay_ms": 300,
        "adverse_price_ticks": 1,
        "participation": "0.25",
        "timer_interval_ms": 1000,
        "quality_predicate": "valid_books_at_actions",
        "maker_model": "no_fill",
        "maker_queue_multiplier": "2",
        "gap_policy": "invalidate_path",
        "tie_policy": "observations_before_actions",
        "seed": 0,
        "settlement_release_delay_ms": 60000,
        "fee_policy.version": "explicit_scenario_v1",
        "fee_policy.mode": "scenario",
        "fee_policy.rate": "0.07",
        "fee_policy.exponent": 1,
        "fee_policy.quantum": "0.00001",
        "fee_policy.buy_fee_unit": "shares",
        "fee_policy.maker_rate": "0",
        "fee_policy.provenance": "SCENARIO_ASSUMPTION_NOT_VERIFIED_HISTORICAL_FEES"
      },
      "source": "config/research/scenarios/friction.yaml",
      "hash": "a3879ba685be"
    },
    "favorable": {
      "values": {
        "schema_version": 1,
        "adapter": "strategy_closed_loop_v1",
        "name": "favorable",
        "initial_cash": "100",
        "feed_delay_ms": 0,
        "decision_delay_ms": 0,
        "order_delay_ms": 0,
        "cancel_delay_ms": 0,
        "feedback_delay_ms": 0,
        "adverse_price_ticks": 0,
        "participation": "1",
        "timer_interval_ms": 1000,
        "quality_predicate": "valid_books_at_actions",
        "maker_model": "no_fill",
        "maker_queue_multiplier": "2",
        "gap_policy": "invalidate_path",
        "tie_policy": "observations_before_actions",
        "seed": 0,
        "settlement_release_delay_ms": 60000,
        "fee_policy.version": "explicit_scenario_v1",
        "fee_policy.mode": "scenario",
        "fee_policy.rate": "0.07",
        "fee_policy.exponent": 1,
        "fee_policy.quantum": "0.00001",
        "fee_policy.buy_fee_unit": "shares",
        "fee_policy.maker_rate": "0",
        "fee_policy.provenance": "SCENARIO_ASSUMPTION_NOT_VERIFIED_HISTORICAL_FEES"
      },
      "source": "config/research/scenarios/favorable.yaml",
      "hash": "bc10f82ced29"
    }
  }
};
