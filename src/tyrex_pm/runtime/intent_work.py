"""Reusable intent work units for the risk → OMS pipeline (guru, scheduled exits, etc.)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tyrex_pm.core.models import Intent


@dataclass(frozen=True)
class IntentWorkUnit:
    """One intent to run through :func:`tyrex_pm.runtime.pipeline.process_intent_work_unit`."""

    intent: Intent
    correlation_id: str
    intent_fact_extensions: dict[str, Any] = field(default_factory=dict)
