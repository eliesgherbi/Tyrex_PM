"""Minimal Z-Gap-private state for pure policy evaluation.

Does not duplicate orders, fills, portfolio quantity, or lifecycle phase.
Not persisted in F2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ThesisConfirmPhase(str, Enum):
    IDLE = "IDLE"
    CONFIRMING = "CONFIRMING"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, kw_only=True)
class ThesisConfirmState:
    """Confirmation progress using monotonic time only."""

    phase: ThesisConfirmPhase = ThesisConfirmPhase.IDLE
    adverse_since_mono_ns: int | None = None
    last_p_held: float | None = None

    def reset(self) -> ThesisConfirmState:
        return ThesisConfirmState()
