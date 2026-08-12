"""Protection monitor: mark in → trigger decision out (no venue I/O)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId
from tyrex_pm.core.intents import ExitIntent, FlattenIntent, new_intent_id
from tyrex_pm.protection.reasons import ProtectionReason
from tyrex_pm.protection.state import ArmedProtection, ProtectionPhase
from tyrex_pm.protection.triggers import TriggerDecision, evaluate_triggers


def observe_mark(armed: ArmedProtection, *, mark: Decimal) -> TriggerDecision:
    decision = evaluate_triggers(
        spec=armed.spec,
        mark=mark,
        entry_price=armed.entry_price,
        peak=armed.peak_mark,
        trailing_active=armed.trailing_active,
        thresholds=armed.thresholds,
    )
    armed.peak_mark = decision.peak
    armed.trailing_active = decision.trailing_active
    return decision


def mark_triggered(armed: ArmedProtection, decision: TriggerDecision) -> None:
    if not decision.fired or decision.reason is None:
        raise ValueError("cannot mark triggered without a fired decision")
    armed.phase = ProtectionPhase.TRIGGERED
    armed.trigger_reason = decision.reason.value
    armed.trigger_evidence = dict(decision.evidence)


def build_protection_intent(
    armed: ArmedProtection,
    *,
    reason: ProtectionReason,
    created_at: datetime,
    correlation_id: CorrelationId,
    causation_id: EventId | None,
    evidence: dict[str, Any],
) -> ExitIntent | FlattenIntent:
    common = {
        "intent_id": new_intent_id(),
        "strategy_id": armed.strategy_id,
        "instrument_id": armed.instrument_id,
        "market_id": armed.market_id,
        "created_at": created_at,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "reason_code": reason.value,
        "evidence": {
            **evidence,
            "attempt_id": f"protection:{armed.session_id}:{reason.value}",
            "protection_session_id": armed.session_id,
            "entry_price": str(armed.entry_price),
            "peak_mark": str(armed.peak_mark),
        },
    }
    if reason is ProtectionReason.PROTECTION_TP:
        return ExitIntent(**common)
    return FlattenIntent(**common, urgency="URGENT")
