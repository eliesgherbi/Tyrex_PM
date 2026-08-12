"""Framework-owned position protection (SL / TP / trailing).

Strategies declare ``ProtectionSpec`` via run config. The runtime arms after
confirmed exposure and emits ``ExitIntent`` / ``FlattenIntent`` only.
"""

from tyrex_pm.protection.monitor import build_protection_intent, mark_triggered, observe_mark
from tyrex_pm.protection.reasons import ProtectionReason
from tyrex_pm.protection.registry import ProtectionRegistry
from tyrex_pm.protection.spec import (
    ProtectionSpec,
    ProtectionSpecError,
    protection_spec_from_mapping,
)
from tyrex_pm.protection.state import ArmedProtection, ProtectionPhase
from tyrex_pm.protection.triggers import ResolvedThresholds, TriggerDecision, evaluate_triggers, resolve_thresholds

__all__ = [
    "ArmedProtection",
    "ProtectionPhase",
    "ProtectionReason",
    "ProtectionRegistry",
    "ProtectionSpec",
    "ProtectionSpecError",
    "ResolvedThresholds",
    "TriggerDecision",
    "build_protection_intent",
    "evaluate_triggers",
    "mark_triggered",
    "observe_mark",
    "protection_spec_from_mapping",
    "resolve_thresholds",
]
