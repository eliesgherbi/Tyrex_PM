"""Complete ZGapConfig YAML ↔ typed mapping (no silent field drops)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.runtime.yaml_config.errors import ConfigError
from tyrex_pm.strategies.z_gap.config import (
    ZGapConfig,
    ZGapEntryConfig,
    ZGapFrictionConfig,
    ZGapPtbTimeQualityConfig,
    ZGapRealizationConfig,
    ZGapThesisConfig,
    ZGapTimeResolutionConfig,
    ZGapVolatilityConfig,
    validate_zgap_config,
)

_SECTION_TYPES: dict[str, type] = {
    "volatility": ZGapVolatilityConfig,
    "entry": ZGapEntryConfig,
    "realization": ZGapRealizationConfig,
    "thesis": ZGapThesisConfig,
    "time_resolution": ZGapTimeResolutionConfig,
    "ptb_time_quality": ZGapPtbTimeQualityConfig,
    "friction": ZGapFrictionConfig,
}


def zgap_section_names() -> tuple[str, ...]:
    return tuple(_SECTION_TYPES.keys())


def zgap_leaf_paths() -> frozenset[str]:
    paths: set[str] = set()
    for section, cls in _SECTION_TYPES.items():
        for f in dataclasses.fields(cls):
            if section == "friction" and f.name == "fee_curve":
                paths.add("parameters.friction.fee_curve.fee_rate")
                paths.add("parameters.friction.fee_curve.exponent")
            else:
                paths.add(f"parameters.{section}.{f.name}")
    return frozenset(paths)


def _as_decimal(value: Any, *, field: str, file: str | None) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            f"invalid decimal value {value!r}",
            file=file,
            field=field,
        ) from exc


def _reject_unknown(
    raw: Mapping[str, Any], allowed: set[str], *, file: str | None, prefix: str
) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown field(s): {', '.join(unknown)}",
            file=file,
            field=prefix + unknown[0],
        )


def _coerce_field(defaults: Any, name: str, value: Any, *, path: str, file: str | None) -> Any:
    sample = getattr(defaults, name)
    if isinstance(sample, bool):
        if not isinstance(value, bool):
            raise ConfigError(
                f"expected bool for {path}, got {type(value).__name__}",
                file=file,
                field=path,
            )
        return value
    if isinstance(sample, Decimal):
        return _as_decimal(value, field=path, file=file)
    if isinstance(sample, int) and not isinstance(sample, bool):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid int for {path}", file=file, field=path) from exc
    if isinstance(sample, float):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid float for {path}", file=file, field=path) from exc
    return value


def zgap_config_from_parameters(
    parameters: Mapping[str, Any] | None,
    *,
    file: str | None = None,
) -> ZGapConfig:
    """Build ZGapConfig from YAML parameters; omitted leaves → code defaults."""
    raw = dict(parameters or {})
    _reject_unknown(raw, set(_SECTION_TYPES), file=file, prefix="parameters.")

    def section(name: str, cls: type) -> Any:
        defaults = cls()
        sec_raw = raw.get(name)
        if sec_raw is None:
            return defaults
        if not isinstance(sec_raw, Mapping):
            raise ConfigError(
                f"parameters.{name} must be a mapping",
                file=file,
                field=f"parameters.{name}",
            )
        if name == "friction":
            return _friction_from_mapping(sec_raw, file=file)
        allowed = {f.name for f in dataclasses.fields(cls)}
        _reject_unknown(sec_raw, allowed, file=file, prefix=f"parameters.{name}.")
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in sec_raw:
                continue
            path = f"parameters.{name}.{f.name}"
            kwargs[f.name] = _coerce_field(
                defaults, f.name, sec_raw[f.name], path=path, file=file
            )
        return dataclasses.replace(defaults, **kwargs) if kwargs else defaults

    cfg = ZGapConfig(
        volatility=section("volatility", ZGapVolatilityConfig),
        entry=section("entry", ZGapEntryConfig),
        realization=section("realization", ZGapRealizationConfig),
        thesis=section("thesis", ZGapThesisConfig),
        time_resolution=section("time_resolution", ZGapTimeResolutionConfig),
        ptb_time_quality=section("ptb_time_quality", ZGapPtbTimeQualityConfig),
        friction=section("friction", ZGapFrictionConfig),
    )
    try:
        validate_zgap_config(cfg)
    except ValueError as exc:
        raise ConfigError(str(exc), file=file, field="parameters") from exc
    return cfg


def _friction_from_mapping(sec_raw: Mapping[str, Any], *, file: str | None) -> ZGapFrictionConfig:
    defaults = ZGapFrictionConfig()
    allowed = {"fee_curve", "provisional_resolve_penalty_per_share"}
    _reject_unknown(sec_raw, allowed, file=file, prefix="parameters.friction.")
    fee_curve = defaults.fee_curve
    if "fee_curve" in sec_raw:
        fc = sec_raw["fee_curve"]
        if not isinstance(fc, Mapping):
            raise ConfigError(
                "parameters.friction.fee_curve must be a mapping",
                file=file,
                field="parameters.friction.fee_curve",
            )
        _reject_unknown(
            fc, {"fee_rate", "exponent"}, file=file, prefix="parameters.friction.fee_curve."
        )
        fee_curve = FeeCurveParams(
            fee_rate=_as_decimal(
                fc.get("fee_rate", fee_curve.fee_rate),
                field="parameters.friction.fee_curve.fee_rate",
                file=file,
            ),
            exponent=_as_decimal(
                fc.get("exponent", fee_curve.exponent),
                field="parameters.friction.fee_curve.exponent",
                file=file,
            ),
        )
    penalty = defaults.provisional_resolve_penalty_per_share
    if "provisional_resolve_penalty_per_share" in sec_raw:
        penalty = _as_decimal(
            sec_raw["provisional_resolve_penalty_per_share"],
            field="parameters.friction.provisional_resolve_penalty_per_share",
            file=file,
        )
    return ZGapFrictionConfig(
        fee_curve=fee_curve,
        provisional_resolve_penalty_per_share=penalty,
    )


def zgap_config_to_parameters_dict(cfg: ZGapConfig) -> dict[str, Any]:
    def dec(v: Decimal) -> str:
        return format(v, "f")

    return {
        "volatility": {
            "half_life_s": cfg.volatility.half_life_s,
            "min_samples_s": cfg.volatility.min_samples_s,
            "jump_threshold_sigma": cfg.volatility.jump_threshold_sigma,
            "sample_interval_s": cfg.volatility.sample_interval_s,
            "tau_floor_s": cfg.volatility.tau_floor_s,
        },
        "entry": {
            "theta_take": dec(cfg.entry.theta_take),
            "theta_fill_floor": dec(cfg.entry.theta_fill_floor),
            "z_min": dec(cfg.entry.z_min),
            "z_max": dec(cfg.entry.z_max),
            "block_abs_z": dec(cfg.entry.block_abs_z),
            "tau_min_s": cfg.entry.tau_min_s,
            "tau_max_s": cfg.entry.tau_max_s,
            "expected_slippage_buy": dec(cfg.entry.expected_slippage_buy),
            "exit_friction_reserve": dec(cfg.entry.exit_friction_reserve),
            "require_repricing_edge": cfg.entry.require_repricing_edge,
            "tie_epsilon": dec(cfg.entry.tie_epsilon),
            "reject_both_legs_edge": cfg.entry.reject_both_legs_edge,
        },
        "realization": {
            "theta_rich": dec(cfg.realization.theta_rich),
            "min_exit_depth": dec(cfg.realization.min_exit_depth),
            "expected_slippage_sell": dec(cfg.realization.expected_slippage_sell),
            "slippage_included_in_executable_bid": (
                cfg.realization.slippage_included_in_executable_bid
            ),
        },
        "thesis": {
            "p_stop": dec(cfg.thesis.p_stop),
            "stop_confirm_s": cfg.thesis.stop_confirm_s,
            "reset_on_stale_model": cfg.thesis.reset_on_stale_model,
        },
        "time_resolution": {
            "flatten_before_event_end_s": cfg.time_resolution.flatten_before_event_end_s,
            "sell_vs_resolve_margin": dec(cfg.time_resolution.sell_vs_resolve_margin),
            "resolution_capability_default": (
                cfg.time_resolution.resolution_capability_default
            ),
            "ponr_before_event_end_s": cfg.time_resolution.ponr_before_event_end_s,
        },
        "ptb_time_quality": {
            "basis_max_bps": dec(cfg.ptb_time_quality.basis_max_bps),
            "max_ptb_lag_ms": cfg.ptb_time_quality.max_ptb_lag_ms,
            "max_clock_uncertainty_ms": cfg.ptb_time_quality.max_clock_uncertainty_ms,
        },
        "friction": {
            "fee_curve": {
                "fee_rate": dec(cfg.friction.fee_curve.fee_rate),
                "exponent": dec(cfg.friction.fee_curve.exponent),
            },
            "provisional_resolve_penalty_per_share": dec(
                cfg.friction.provisional_resolve_penalty_per_share
            ),
        },
    }


def assert_zgap_field_coverage() -> None:
    """Fail if ZGapConfig gains fields not covered by loader serialization."""
    expected_sections = set(_SECTION_TYPES)
    actual_sections = {f.name for f in dataclasses.fields(ZGapConfig)}
    if expected_sections != actual_sections:
        raise AssertionError(
            f"ZGapConfig sections drifted: schema={sorted(expected_sections)} "
            f"model={sorted(actual_sections)}"
        )
    paths = zgap_leaf_paths()
    serialized = zgap_config_to_parameters_dict(ZGapConfig())
    flat: set[str] = set()

    def walk(obj: Any, prefix: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{prefix}.{k}")
        else:
            flat.add(prefix)

    walk(serialized, "parameters")
    if paths != flat:
        raise AssertionError(
            f"ZGap leaf path drift: only_in_schema={sorted(paths - flat)} "
            f"only_in_serialize={sorted(flat - paths)}"
        )
