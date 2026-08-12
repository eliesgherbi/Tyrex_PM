"""Framework protection reason codes (not strategy vocabulary)."""

from __future__ import annotations

from enum import Enum


class ProtectionReason(str, Enum):
    PROTECTION_SL = "PROTECTION_SL"
    PROTECTION_TP = "PROTECTION_TP"
    PROTECTION_TRAIL = "PROTECTION_TRAIL"
    PROTECTION_ARMED = "PROTECTION_ARMED"
    PROTECTION_DISARMED = "PROTECTION_DISARMED"
