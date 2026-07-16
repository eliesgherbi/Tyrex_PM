"""Runtime execution modes (R4)."""

from __future__ import annotations

from enum import Enum


class RuntimeMode(str, Enum):
    """Application mode — must not alter market data or signal math."""

    OBSERVE = "OBSERVE"
    SHADOW = "SHADOW"
    LIVE_TINY = "LIVE_TINY"
