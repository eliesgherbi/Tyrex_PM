"""YAML ``strategy.parameters`` ↔ SettlementValueConfig (unknown keys rejected)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from tyrex_pm.runtime.config_error import ConfigError
from tyrex_pm.strategies.settlement_value.config import (
    SettlementEntryConfig,
    SettlementFeeConfig,
    SettlementModelConfig,
    SettlementQualityConfig,
    SettlementSizingConfig,
    SettlementSpecConfig,
    SettlementValueConfig,
    SettlementVolatilityConfig,
)

_SECTIONS: dict[str, type] = {
    "settlement": SettlementSpecConfig,
    "model": SettlementModelConfig,
    "volatility": SettlementVolatilityConfig,
    "entry": SettlementEntryConfig,
    "sizing": SettlementSizingConfig,
    "quality": SettlementQualityConfig,
    "fees": SettlementFeeConfig,
}


def _coerce(sample: Any, value: Any, *, path: str, file: str | None) -> Any:
    if isinstance(sample, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"expected bool, got {type(value).__name__}", file=file, field=path)
        return value
    if isinstance(sample, Decimal):
        if isinstance(value, bool):
            raise ConfigError("expected decimal, got bool", file=file, field=path)
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ConfigError(f"invalid decimal {value!r}", file=file, field=path) from exc
    if isinstance(sample, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"expected int, got {value!r}", file=file, field=path)
        return value
    if isinstance(sample, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"expected number, got {value!r}", file=file, field=path)
        return float(value)
    if isinstance(sample, tuple):
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            raise ConfigError("expected a list of strings", file=file, field=path)
        return tuple(value)
    if isinstance(sample, str):
        if not isinstance(value, str):
            raise ConfigError(f"expected string, got {value!r}", file=file, field=path)
        return value
    raise ConfigError("unsupported field type", file=file, field=path)


def settlement_value_config_from_parameters(
    parameters: Mapping[str, Any] | None, *, file: str | None = None
) -> SettlementValueConfig:
    raw = dict(parameters or {})
    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        raise ConfigError(
            f"unknown field(s): {', '.join(unknown)}", file=file, field=f"parameters.{unknown[0]}"
        )
    sections: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        section_raw = raw.get(name) or {}
        if not isinstance(section_raw, Mapping):
            raise ConfigError("section must be a mapping", file=file, field=f"parameters.{name}")
        defaults = cls()
        allowed = {f.name for f in dataclasses.fields(cls)}
        extra = sorted(set(section_raw) - allowed)
        if extra:
            raise ConfigError(
                f"unknown field(s): {', '.join(extra)}",
                file=file,
                field=f"parameters.{name}.{extra[0]}",
            )
        values = {
            key: _coerce(
                getattr(defaults, key), value, path=f"parameters.{name}.{key}", file=file
            )
            for key, value in section_raw.items()
        }
        try:
            sections[name] = cls(**values)
        except ValueError as exc:
            raise ConfigError(str(exc), file=file, field=f"parameters.{name}") from exc
    try:
        return SettlementValueConfig(**sections)
    except ValueError as exc:
        raise ConfigError(str(exc), file=file, field="parameters") from exc


def settlement_value_config_as_parameters(cfg: SettlementValueConfig) -> dict[str, Any]:
    """Round-trippable plain mapping (Decimals as strings) for provenance hashing."""
    out: dict[str, Any] = {}
    for name in _SECTIONS:
        section = getattr(cfg, name)
        values: dict[str, Any] = {}
        for f in dataclasses.fields(section):
            value = getattr(section, f.name)
            if isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, tuple):
                value = list(value)
            values[f.name] = value
        out[name] = values
    return out
