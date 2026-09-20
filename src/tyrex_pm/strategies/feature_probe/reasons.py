"""feature_probe reason codes."""

from __future__ import annotations

from enum import Enum


class FeatureProbeReason(str, Enum):
    FEATURE_OBSERVED = "FEATURE_OBSERVED"
    FEATURE_NOT_READY = "FEATURE_NOT_READY"
    SKIP_NO_BOOK = "SKIP_NO_BOOK"
    HOLD_POSITION = "HOLD_POSITION"
    BLOCKED = "BLOCKED"
    ENTRY_ASK_HIT = "ENTRY_ASK_HIT"
    WAIT_NO_ASK_HIT = "WAIT_NO_ASK_HIT"
