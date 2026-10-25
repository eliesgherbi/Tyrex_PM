"""Parameterized indicator subscription declared by a strategy InputContract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class IndicatorSpec:
    """One reusable producer instance (name + source + parameters)."""

    name: str
    source: str
    extra_sources: tuple[str, ...] = ()
    horizons_ms: tuple[int, ...] = ()
    levels: tuple[int, ...] = ()
    params: tuple[tuple[str, str], ...] = ()
    # An entry is not sent until this indicator is ready (host final gate, live and replay).
    # Not part of the instance id: it is a host rule about the value, not the value.
    gates_entry: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("indicator name must be non-empty")
        if not self.source.strip():
            raise ValueError("indicator source must be non-empty")
        if any(h <= 0 for h in self.horizons_ms):
            raise ValueError("horizons_ms must be positive")
        if any(level <= 0 for level in self.levels):
            raise ValueError("levels must be positive")
        keys = [key for key, _value in self.params]
        if any(not key.strip() for key in keys):
            raise ValueError("indicator param keys must be non-empty")
        if len(keys) != len(set(keys)):
            raise ValueError("indicator param keys must be unique")

    @property
    def all_sources(self) -> tuple[str, ...]:
        return (self.source, *self.extra_sources)

    @property
    def param_map(self) -> dict[str, str]:
        return dict(self.params)

    @property
    def instance_id(self) -> str:
        extras = ",".join(self.extra_sources)
        horizons = ",".join(str(h) for h in self.horizons_ms)
        levels = ",".join(str(level) for level in self.levels)
        param_part = ",".join(f"{key}={value}" for key, value in self.params)
        return f"{self.name}|{self.source}|{extras}|h={horizons}|l={levels}|p={param_part}"
