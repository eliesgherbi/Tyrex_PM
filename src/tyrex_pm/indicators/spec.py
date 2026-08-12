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

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("indicator name must be non-empty")
        if not self.source.strip():
            raise ValueError("indicator source must be non-empty")
        if any(h <= 0 for h in self.horizons_ms):
            raise ValueError("horizons_ms must be positive")
        if any(level <= 0 for level in self.levels):
            raise ValueError("levels must be positive")

    @property
    def all_sources(self) -> tuple[str, ...]:
        return (self.source, *self.extra_sources)

    @property
    def instance_id(self) -> str:
        extras = ",".join(self.extra_sources)
        horizons = ",".join(str(h) for h in self.horizons_ms)
        levels = ",".join(str(level) for level in self.levels)
        return f"{self.name}|{self.source}|{extras}|h={horizons}|l={levels}"
