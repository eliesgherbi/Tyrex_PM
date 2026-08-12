"""Strict YAML → Ask70Config mapping (no silent drops)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.runtime.config_error import ConfigError
from tyrex_pm.strategies.ask70.config import Ask70Config


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], *, file: str | None) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown ask70 field(s): {', '.join(unknown)}",
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


def ask70_config_from_parameters(
    parameters: Mapping[str, Any], *, file: str | None = None
) -> Ask70Config:
    raw = dict(parameters)
    _reject_unknown(
        raw,
        {
            "entry_ask_threshold",
            "leg_preference",
            "max_price_pad",
            "tau_min_s",
            "tau_max_s",
            "max_clock_uncertainty_ms",
        },
        file=file,
    )
    required = {
        "entry_ask_threshold",
        "leg_preference",
        "max_price_pad",
        "tau_min_s",
        "tau_max_s",
        "max_clock_uncertainty_ms",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ConfigError(
            f"missing ask70 field(s): {', '.join(missing)}",
            file=file,
            field=f"parameters.{missing[0]}",
        )
    try:
        return Ask70Config(
            entry_ask_threshold=_decimal(
                raw["entry_ask_threshold"], field="parameters.entry_ask_threshold", file=file
            ),
            leg_preference=str(raw["leg_preference"]),  # type: ignore[arg-type]
            max_price_pad=_decimal(
                raw["max_price_pad"], field="parameters.max_price_pad", file=file
            ),
            tau_min_s=float(raw["tau_min_s"]),
            tau_max_s=float(raw["tau_max_s"]),
            max_clock_uncertainty_ms=float(raw["max_clock_uncertainty_ms"]),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc), file=file, field="parameters") from exc
