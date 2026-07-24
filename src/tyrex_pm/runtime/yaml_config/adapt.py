"""Adapt ResolvedRunConfig → ObserveConfig for existing hosts."""

from __future__ import annotations

from pathlib import Path

from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.runtime.config import (
    ObserveConfig,
    SourceMode,
    ZGapObserveRuntimeConfig,
)
from tyrex_pm.runtime.yaml_config.resolve import ResolvedRunConfig, RunMode
from tyrex_pm.strategies.z_gap.config import ZGapConfig


def zgap_observe_runtime_from_resolved(resolved: ResolvedRunConfig) -> ZGapObserveRuntimeConfig:
    """Compatibility wiring block for ObserveHost (window/PTB/timers/capability).

    Strategy mathematics come from ``resolved.zgap`` (pure). ``target_notional``
    is copied from **risk** for legacy readers; hosts must prefer ``risk``.
    """
    z: ZGapConfig = resolved.zgap
    rt = resolved.runtime
    return ZGapObserveRuntimeConfig(
        window_id=rt.window_id,
        ptb_k=rt.ptb_k,
        fee_rate=z.friction.fee_curve.fee_rate,
        fee_exponent=z.friction.fee_curve.exponent,
        target_notional=resolved.risk.target_notional,
        evaluate_interval_s=rt.evaluate_interval_s,
        timer_eval_count=rt.timer_eval_count,
        half_life_s=z.volatility.half_life_s,
        min_samples_s=z.volatility.min_samples_s,
        sample_interval_s=z.volatility.sample_interval_s,
        tau_floor_s=z.volatility.tau_floor_s,
        jump_threshold_sigma=z.volatility.jump_threshold_sigma,
        theta_take=z.entry.theta_take,
        z_min=z.entry.z_min,
        z_max=z.entry.z_max,
        tau_min_s=z.entry.tau_min_s,
        tau_max_s=z.entry.tau_max_s,
        basis_max_bps=z.ptb_time_quality.basis_max_bps,
        expected_slippage_buy=z.entry.expected_slippage_buy,
        expected_slippage_sell=z.realization.expected_slippage_sell,
        reject_both_legs_edge=z.entry.reject_both_legs_edge,
        theta_rich=z.realization.theta_rich,
        p_stop=z.thesis.p_stop,
        stop_confirm_s=z.thesis.stop_confirm_s,
        flatten_before_event_end_s=z.time_resolution.flatten_before_event_end_s,
        resolution_capability=rt.resolution_capability,
        ponr_before_event_end_s=z.time_resolution.ponr_before_event_end_s,
        resolution_evidence_path=rt.resolution_evidence_path,
    )


def output_path_for_run(resolved: ResolvedRunConfig) -> Path:
    mode = resolved.mode.value
    name = resolved.run_name or "unnamed"
    return Path("var/reporting/yaml_run") / name / f"{mode}_facts.jsonl"


def adapt_to_observe_config(
    resolved: ResolvedRunConfig,
    *,
    output_path: Path | None = None,
) -> ObserveConfig:
    """Build ObserveConfig consumed by ObserveHost / ShadowHost.

    External ``runtime.source`` → internal ``ObserveConfig.mode``.
    External ``signal_max_book_spread`` → ``max_book_spread``.
    Complete ``ZGapConfig`` is attached as ``zgap_pure`` so binding never uses
    the incomplete flat bridge.

    Live mode uses the N7 host via ``yaml_config.live_run`` — do not adapt here.
    """
    if resolved.mode is RunMode.LIVE:
        raise ValueError(
            "adapt_to_observe_config does not support --mode live; "
            "use yaml_config.live_run.run_yaml_live"
        )
    rt = resolved.runtime
    source = SourceMode.FIXTURE if rt.source == "fixture" else SourceMode.LIVE
    out = output_path if output_path is not None else output_path_for_run(resolved)
    # SHADOW requires ShadowConfig; OBSERVE may omit OMS when enable_oms is false.
    shadow = None
    if resolved.mode is RunMode.SHADOW or resolved.shadow.enable_oms:
        shadow = resolved.shadow
    return ObserveConfig(
        mode=source,
        output_path=out,
        binance_symbol=rt.binance_symbol,
        momentum_lookback=rt.momentum_lookback,
        momentum_threshold=rt.momentum_threshold,
        max_book_spread=rt.signal_max_book_spread,
        freshness=rt.freshness,
        runtime_duration=rt.runtime_duration,
        fixture_path=rt.fixture_path,
        event_slug=rt.event_slug,
        event_url=rt.event_url,
        condition_id=rt.condition_id,
        evaluate_on_reference=rt.evaluate_on_reference,
        momentum_min_samples=rt.momentum_min_samples,
        risk=resolved.risk,
        shadow=shadow,
        strategy_kind="z_gap",
        z_gap=zgap_observe_runtime_from_resolved(resolved),
        zgap_pure=resolved.zgap,
        run_name=resolved.run_name,
    )


def binding_kwargs_from_observe(config: ObserveConfig) -> dict:
    """Kwargs for build_strategy_binding preserving complete ZGapConfig."""
    zg = config.z_gap
    assert zg is not None
    kwargs: dict = {
        "strategy_kind": config.strategy_kind,
        "zgap_runtime": zg,
        "clock": None,  # caller supplies
        "window_id": zg.window_id,
        "target_notional": (
            config.risk.target_notional if config.risk is not None else zg.target_notional
        ),
        "fee_curve": FeeCurveParams(fee_rate=zg.fee_rate, exponent=zg.fee_exponent),
    }
    if config.zgap_pure is not None:
        kwargs["zgap_config"] = config.zgap_pure
    return kwargs
