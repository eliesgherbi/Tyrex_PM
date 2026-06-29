"""Protection registry (P4 architecture_enhance).

Holds active protection entries keyed by ``(owner_id, token_id)``. Registration
is gated on final allocation ownership: callers must use
:func:`register_if_allocation_final` (or pass a status that
``state.fill_state.is_allocation_final`` accepts) so protection never attaches on
submit ack, resting BUY, or MATCHED-only evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.protection.config import ProtectionPolicy
from tyrex_pm.protection.trigger_eval import resolve_thresholds
from tyrex_pm.state import fill_state


def _key(owner_id: str, token_id: TokenId | str) -> str:
    return f"{owner_id}|{token_id}"


@dataclass
class ProtectionEntry:
    owner_id: str
    token_id: TokenId
    entry_price: Decimal
    policy: ProtectionPolicy
    parent_correlation_id: str
    take_profit_trigger_price: Decimal | None = None
    stop_loss_trigger_price: Decimal | None = None
    threshold_evidence: dict[str, str] = field(default_factory=dict)
    #: Set once a trigger fires + an exit is emitted, preventing a double-sell.
    triggered: bool = False
    trigger_kind: str | None = None
    #: Last observed price emitted as a tick, used to dedup protection_tick facts.
    last_tick_price: Decimal | None = None


class ProtectionRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ProtectionEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, owner_id: str, token_id: TokenId | str) -> ProtectionEntry | None:
        return self._entries.get(_key(owner_id, token_id))

    def active_entries(self) -> list[ProtectionEntry]:
        return [e for e in self._entries.values() if not e.triggered]

    def all_entries(self) -> list[ProtectionEntry]:
        return list(self._entries.values())

    def register(
        self,
        *,
        owner_id: str,
        token_id: TokenId,
        entry_price: Decimal,
        policy: ProtectionPolicy,
        parent_correlation_id: str,
    ) -> ProtectionEntry:
        if entry_price <= 0:
            raise ValueError("protection entry_price must be positive")
        tp, sl, evidence = resolve_thresholds(policy, entry_price)
        entry = ProtectionEntry(
            owner_id=owner_id,
            token_id=token_id,
            entry_price=entry_price,
            policy=policy,
            parent_correlation_id=parent_correlation_id,
            take_profit_trigger_price=tp,
            stop_loss_trigger_price=sl,
            threshold_evidence=evidence,
        )
        self._entries[_key(owner_id, token_id)] = entry
        return entry

    def remove(self, owner_id: str, token_id: TokenId | str) -> None:
        self._entries.pop(_key(owner_id, token_id), None)


def register_if_allocation_final(
    registry: ProtectionRegistry,
    *,
    status: str | None,
    owner_id: str,
    token_id: TokenId,
    entry_price: Decimal,
    policy: ProtectionPolicy,
    parent_correlation_id: str,
) -> ProtectionEntry | None:
    """Register protection only when ``status`` is allocation-final (CONFIRMED).

    Returns the entry on success, ``None`` when the status is not final (submit
    ack / MATCHED / MINED / RETRYING / FAILED / unknown).
    """
    if not fill_state.is_allocation_final(status):
        return None
    if not policy.has_triggers():
        return None
    return registry.register(
        owner_id=owner_id,
        token_id=token_id,
        entry_price=entry_price,
        policy=policy,
        parent_correlation_id=parent_correlation_id,
    )
