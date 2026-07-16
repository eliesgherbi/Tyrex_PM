"""Minimal indicator-result envelope (calculations land in R3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import EventId


@dataclass(frozen=True, slots=True, kw_only=True)
class IndicatorResult:
    """Typed envelope for one indicator observation.

    ``value`` is intentionally ``Any`` so structured results are allowed later
    without forcing every indicator into a single float.
    """

    indicator_id: str
    observed_at: datetime
    value: Any
    source_event_id: EventId | None = None

    def __post_init__(self) -> None:
        if not self.indicator_id.strip():
            raise ValueError("indicator_id must be non-empty")
        object.__setattr__(
            self, "observed_at", require_utc(self.observed_at, field_name="observed_at")
        )
