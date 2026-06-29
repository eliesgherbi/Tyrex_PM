"""Protection (TP/SL) overlay (P4 architecture_enhance).

A reusable, strategy-agnostic protection layer. It attaches to an ``owner_id``,
reads ``AllocationLedger`` + ``WalletStore`` + ``MarketStateStore``, and emits
*only* ``ExitIntent`` work units (urgency=``urgent``) when a take-profit or
stop-loss trigger fires. It never submits/cancels and never mutates allocation;
the emitted intents flow through the same generic pipeline (RiskEngine →
ExecutionPlanner → validate_planned_order → SingleWriterOMS) as any other intent.

Registration is gated on final allocation ownership (``allocation_buy_applied`` /
``CONFIRMED``) — never on submit ack, resting BUY, or MATCHED-only evidence.
"""

from tyrex_pm.protection.config import ProtectionPolicy, ProtectionSizeMode
from tyrex_pm.protection.registry import ProtectionEntry, ProtectionRegistry
from tyrex_pm.protection.monitor import ProtectionMonitor

__all__ = [
    "ProtectionPolicy",
    "ProtectionSizeMode",
    "ProtectionEntry",
    "ProtectionRegistry",
    "ProtectionMonitor",
]
