"""Deterministic serialization of resolved configuration for --show-config."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.yaml_config.resolve import ResolvedRunConfig, RunMode
from tyrex_pm.runtime.yaml_config.zgap_schema import zgap_config_to_parameters_dict


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value).replace("\\", "/")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import fields

        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    return str(value)


def resolved_to_show_dict(resolved: ResolvedRunConfig) -> dict[str, Any]:
    """Complete effective configuration including applied defaults."""
    rt = resolved.runtime
    sh = resolved.shadow
    risk_block: dict[str, Any] = {
        "runtime_mode": resolved.risk.runtime_mode.value,
        "target_notional": format(resolved.risk.target_notional, "f"),
        "max_notional": format(resolved.risk.max_notional, "f"),
        "min_price": format(resolved.risk.min_price, "f"),
        "max_price": format(resolved.risk.max_price, "f"),
        "max_spread": format(resolved.risk.max_spread, "f"),
        "min_liquidity_notional": format(resolved.risk.min_liquidity_notional, "f"),
        "no_entry_before_close_s": resolved.risk.no_entry_before_close.total_seconds(),
        "duplicate_lifetime_s": resolved.risk.duplicate_lifetime.total_seconds(),
        "kill_switch_active": resolved.risk.kill_switch_active,
    }
    if resolved.n7_mapping is not None:
        risk_block["live_limits"] = {
            "max_buy_collateral": resolved.n7_mapping["max_buy_collateral"],
            "max_daily_notional": resolved.n7_mapping["max_daily_notional"],
            "max_daily_loss": resolved.n7_mapping["max_daily_loss"],
            "max_positions_per_window": resolved.n7_mapping["max_positions_per_window"],
            "max_entry_lineages": resolved.n7_mapping["max_entry_lineages"],
            "allow_same_window_reentry": resolved.n7_mapping["allow_same_window_reentry"],
            "allow_same_window_reversal": resolved.n7_mapping["allow_same_window_reversal"],
            "skip_if_min_exceeds_cap": resolved.n7_mapping["skip_if_min_exceeds_cap"],
        }

    if resolved.execution_kind == "polymarket_live" and resolved.n7_mapping is not None:
        execution_block: dict[str, Any] = {
            "kind": "polymarket_live",
            "polymarket_live": {
                "order_style": resolved.n7_mapping["order_style"],
                "live": _jsonable(resolved.n7_mapping["live"]),
                "timing": _jsonable(resolved.n7_mapping["timing"]),
            },
        }
    else:
        execution_block = {
            "kind": "shadow",
            "shadow": {
                "enable_oms": sh.enable_oms,
                "max_position_notional": format(sh.max_position_notional, "f"),
                "max_total_exposure": format(sh.max_total_exposure, "f"),
                "max_hold_s": sh.max_hold.total_seconds(),
                "flatten_before_close_s": sh.flatten_before_close.total_seconds(),
                "exit_on_flat": sh.exit_on_flat,
                "persistence_path": str(sh.persistence_path).replace("\\", "/"),
                "cancel_unfilled_residual": sh.cancel_unfilled_residual,
                "fee_rate": format(sh.fee_rate, "f"),
                "fee_model_id": sh.fee_model_id,
                "entry_retry_cooldown_s": sh.entry_retry_cooldown_s,
                "entry_max_attempts": sh.entry_max_attempts,
                "exit_retry_cooldown_s": sh.exit_retry_cooldown_s,
                "exit_max_normal_retries": sh.exit_max_normal_retries,
                "exit_escalate_after": sh.exit_escalate_after,
                "require_flat_for_promote": sh.require_flat_for_promote,
                "fills": {
                    "model_id": sh.fill_model_id,
                    "latency_ms": sh.fill_latency_ms,
                    "extra_slip_ticks": format(sh.fill_extra_slip_ticks, "f"),
                    "tick_size": format(sh.fill_tick_size, "f"),
                },
            },
        }

    return {
        "mode": resolved.mode.value,
        "requested_mode": resolved.mode.value,
        "effective_mode": resolved.mode.value,
        "run_name": resolved.run_name,
        "strategy": {
            "strategy": resolved.strategy_kind,
            "parameters": zgap_config_to_parameters_dict(resolved.zgap),
        },
        "risk": risk_block,
        "execution": execution_block,
        "runtime": {
            "source": rt.source,
            "fixture_path": None
            if rt.fixture_path is None
            else str(rt.fixture_path).replace("\\", "/"),
            "binance_symbol": rt.binance_symbol,
            "signal_max_book_spread": format(rt.signal_max_book_spread, "f"),
            "freshness": {
                "book_threshold_ms": rt.freshness.book_threshold_ms,
                "reference_threshold_ms": rt.freshness.reference_threshold_ms,
                "future_tolerance_ms": rt.freshness.future_tolerance_ms,
                "timestamp_basis": rt.freshness.timestamp_basis.value,
            },
            "runtime_duration_s": None
            if rt.runtime_duration is None
            else rt.runtime_duration.total_seconds(),
            "evaluate_on_reference": rt.evaluate_on_reference,
            "evaluate_interval_s": rt.evaluate_interval_s,
            "timer_eval_count": rt.timer_eval_count,
            "window_id": rt.window_id,
            "ptb_k": format(rt.ptb_k, "f"),
            "resolution_capability": rt.resolution_capability,
            "resolution_evidence_path": rt.resolution_evidence_path,
            "event_slug": rt.event_slug,
            "event_url": rt.event_url,
            "condition_id": rt.condition_id,
            "momentum_lookback_ms": int(rt.momentum_lookback.total_seconds() * 1000),
            "momentum_threshold": format(rt.momentum_threshold, "f"),
            "momentum_min_samples": rt.momentum_min_samples,
            "require_ssr_price_match": rt.require_ssr_price_match,
            "market_family": rt.market_family,
            "one_shot": rt.one_shot,
        },
        "provenance": {
            "strategy_path": str(resolved.strategy_path).replace("\\", "/"),
            "risk_path": str(resolved.risk_path).replace("\\", "/"),
            "execution_path": str(resolved.execution_path).replace("\\", "/"),
            "runtime_path": str(resolved.runtime_path).replace("\\", "/"),
            "scenario_name": resolved.scenario_name,
            "scenario_path": None
            if resolved.scenario_path is None
            else str(resolved.scenario_path).replace("\\", "/"),
            "host_binding": (
                "n7_operator_oneshot" if resolved.mode is RunMode.LIVE else resolved.mode.value
            ),
        },
    }
