"""Strict YAML → FeatureProbeConfig mapping (no silent drops)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.runtime.config_error import ConfigError
from tyrex_pm.strategies.feature_probe.config import FeatureProbeConfig

_ALLOWED = {
    "emit_entry",
    "tau_min_s",
    "tau_max_s",
    "max_clock_uncertainty_ms",
    "entry_ask_threshold",
    "max_price_pad",
}
_REQUIRED = {
    "emit_entry",
    "tau_min_s",
    "tau_max_s",
    "max_clock_uncertainty_ms",
}


def _reject_unknown(raw: Mapping[str, Any], *, file: str | None) -> None:
    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        raise ConfigError(
            f"unknown feature_probe field(s): {', '.join(unknown)}",
            file=file,
            field=f"parameters.{unknown[0]}",
        )


def _decimal(value: Any, *, field: str, file: str | None) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"invalid decimal {value!r}", file=file, field=field) from exc
    if not result.is_finite():
        raise ConfigError(f"{field} must be finite", file=file, field=field)
    return result


def _boolean(value: Any, *, field: str, file: str | None) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field} must be an explicit boolean", file=file, field=field)


def feature_probe_config_from_parameters(
    parameters: Mapping[str, Any], *, file: str | None = None
) -> FeatureProbeConfig:
    raw = dict(parameters)
    _reject_unknown(raw, file=file)
    missing = sorted(_REQUIRED - set(raw))
    if missing:
        raise ConfigError(
            f"missing feature_probe field(s): {', '.join(missing)}",
            file=file,
            field=f"parameters.{missing[0]}",
        )
    try:
        return FeatureProbeConfig(
            emit_entry=_boolean(raw["emit_entry"], field="parameters.emit_entry", file=file),
            tau_min_s=float(raw["tau_min_s"]),
            tau_max_s=float(raw["tau_max_s"]),
            max_clock_uncertainty_ms=float(raw["max_clock_uncertainty_ms"]),
            entry_ask_threshold=(
                Decimal("0.70")
                if "entry_ask_threshold" not in raw
                else _decimal(
                    raw["entry_ask_threshold"],
                    field="parameters.entry_ask_threshold",
                    file=file,
                )
            ),
            max_price_pad=(
                Decimal("0.08")
                if "max_price_pad" not in raw
                else _decimal(
                    raw["max_price_pad"],
                    field="parameters.max_price_pad",
                    file=file,
                )
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc), file=file, field="parameters") from exc
