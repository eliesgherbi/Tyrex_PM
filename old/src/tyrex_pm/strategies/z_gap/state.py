"""Z-Gap single-position lifecycle state (A0.7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class ZGapPhase(str, Enum):
    IDLE = "IDLE"
    ENTRY_PENDING = "ENTRY_PENDING"
    ACTIVE = "ACTIVE"
    EXIT_PENDING = "EXIT_PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


_TERMINAL = frozenset({ZGapPhase.DONE, ZGapPhase.FAILED})

_VALID_TRANSITIONS: dict[ZGapPhase, frozenset[ZGapPhase]] = {
    ZGapPhase.IDLE: frozenset({ZGapPhase.ENTRY_PENDING}),
    ZGapPhase.ENTRY_PENDING: frozenset({ZGapPhase.ACTIVE, ZGapPhase.DONE, ZGapPhase.FAILED}),
    ZGapPhase.ACTIVE: frozenset({ZGapPhase.EXIT_PENDING, ZGapPhase.FAILED}),
    ZGapPhase.EXIT_PENDING: frozenset({ZGapPhase.DONE, ZGapPhase.ACTIVE, ZGapPhase.FAILED}),
    ZGapPhase.DONE: frozenset(),
    ZGapPhase.FAILED: frozenset(),
}


class InvalidZGapTransition(Exception):
    def __init__(self, *, from_phase: ZGapPhase, to_phase: ZGapPhase, reason: str) -> None:
        self.from_phase = from_phase
        self.to_phase = to_phase
        self.reason = reason
        super().__init__(f"invalid z_gap transition {from_phase.value} -> {to_phase.value}: {reason}")


@dataclass
class ZGapLifecycleState:
    """Mutable lifecycle for one Z-Gap position per window."""

    market_id: str
    condition_id: str | None
    owner_id: str
    phase: ZGapPhase = ZGapPhase.IDLE

    selected_leg: str | None = None
    token_id: str | None = None

    entry_order_id: str | None = None
    entry_requested_shares: Decimal = Decimal("0")
    entry_filled_shares: Decimal = Decimal("0")
    entry_avg_price: Decimal | None = None
    entry_fee: Decimal | None = None
    entry_model_p: Decimal | None = None
    entry_z: Decimal | None = None
    entry_edge: Decimal | None = None
    entry_outcome_category: str | None = None
    entry_ts: datetime | None = None

    active_quantity: Decimal = Decimal("0")

    exit_order_id: str | None = None
    exit_requested_shares: Decimal = Decimal("0")
    exit_filled_shares: Decimal = Decimal("0")
    exit_avg_price: Decimal | None = None
    exit_reason: str | None = None
    exit_trigger_ts: datetime | None = None
    exit_completed_ts: datetime | None = None

    exited_this_window: bool = False
    failure_reason: str | None = None
    manual_intervention_required: bool = False
    last_update_ts: datetime | None = None

    entry_attempted: bool = False
    entry_submitted: bool = False
    position_activated: bool = False
    exit_triggered: bool = False
    exit_attempts: int = 0
    position_closed: bool = False

    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL

    def can_submit_entry(self) -> bool:
        return (
            self.phase == ZGapPhase.IDLE
            and not self.entry_attempted
            and not self.exited_this_window
        )

    def transition(self, to_phase: ZGapPhase, *, reason: str | None = None, now: datetime | None = None) -> None:
        if to_phase == self.phase:
            return
        allowed = _VALID_TRANSITIONS.get(self.phase, frozenset())
        if to_phase not in allowed:
            raise InvalidZGapTransition(from_phase=self.phase, to_phase=to_phase, reason=reason or "not_allowed")
        self.phase = to_phase
        self.last_update_ts = now
        if to_phase == ZGapPhase.FAILED and reason:
            self.failure_reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "owner_id": self.owner_id,
            "phase": self.phase.value,
            "selected_leg": self.selected_leg,
            "token_id": self.token_id,
            "entry_order_id": self.entry_order_id,
            "entry_requested_shares": str(self.entry_requested_shares),
            "entry_filled_shares": str(self.entry_filled_shares),
            "entry_avg_price": str(self.entry_avg_price) if self.entry_avg_price is not None else None,
            "entry_fee": str(self.entry_fee) if self.entry_fee is not None else None,
            "entry_model_p": str(self.entry_model_p) if self.entry_model_p is not None else None,
            "entry_z": str(self.entry_z) if self.entry_z is not None else None,
            "entry_edge": str(self.entry_edge) if self.entry_edge is not None else None,
            "entry_outcome_category": self.entry_outcome_category,
            "entry_ts": self.entry_ts.isoformat() if self.entry_ts else None,
            "active_quantity": str(self.active_quantity),
            "exit_order_id": self.exit_order_id,
            "exit_requested_shares": str(self.exit_requested_shares),
            "exit_filled_shares": str(self.exit_filled_shares),
            "exit_avg_price": str(self.exit_avg_price) if self.exit_avg_price is not None else None,
            "exit_reason": self.exit_reason,
            "exit_trigger_ts": self.exit_trigger_ts.isoformat() if self.exit_trigger_ts else None,
            "exit_completed_ts": self.exit_completed_ts.isoformat() if self.exit_completed_ts else None,
            "exited_this_window": self.exited_this_window,
            "failure_reason": self.failure_reason,
            "manual_intervention_required": self.manual_intervention_required,
            "last_update_ts": self.last_update_ts.isoformat() if self.last_update_ts else None,
            "entry_attempted": self.entry_attempted,
            "entry_submitted": self.entry_submitted,
            "position_activated": self.position_activated,
            "exit_triggered": self.exit_triggered,
            "exit_attempts": self.exit_attempts,
            "position_closed": self.position_closed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ZGapLifecycleState:
        phase_raw = str(raw.get("phase", ZGapPhase.IDLE.value))
        phase = ZGapPhase(phase_raw) if phase_raw in ZGapPhase._value2member_map_ else ZGapPhase.IDLE

        def _dec(key: str, default: str = "0") -> Decimal:
            v = raw.get(key)
            if v in (None, ""):
                return Decimal(default)
            return Decimal(str(v))

        def _opt_dec(key: str) -> Decimal | None:
            v = raw.get(key)
            if v in (None, ""):
                return None
            return Decimal(str(v))

        def _dt(key: str) -> datetime | None:
            v = raw.get(key)
            if not v:
                return None
            return datetime.fromisoformat(str(v))

        return cls(
            market_id=str(raw.get("market_id", "")),
            condition_id=raw.get("condition_id"),
            owner_id=str(raw.get("owner_id", "z_gap")),
            phase=phase,
            selected_leg=raw.get("selected_leg"),
            token_id=raw.get("token_id"),
            entry_order_id=raw.get("entry_order_id"),
            entry_requested_shares=_dec("entry_requested_shares"),
            entry_filled_shares=_dec("entry_filled_shares"),
            entry_avg_price=_opt_dec("entry_avg_price"),
            entry_fee=_opt_dec("entry_fee"),
            entry_model_p=_opt_dec("entry_model_p"),
            entry_z=_opt_dec("entry_z"),
            entry_edge=_opt_dec("entry_edge"),
            entry_outcome_category=raw.get("entry_outcome_category"),
            entry_ts=_dt("entry_ts"),
            active_quantity=_dec("active_quantity"),
            exit_order_id=raw.get("exit_order_id"),
            exit_requested_shares=_dec("exit_requested_shares"),
            exit_filled_shares=_dec("exit_filled_shares"),
            exit_avg_price=_opt_dec("exit_avg_price"),
            exit_reason=raw.get("exit_reason"),
            exit_trigger_ts=_dt("exit_trigger_ts"),
            exit_completed_ts=_dt("exit_completed_ts"),
            exited_this_window=bool(raw.get("exited_this_window", False)),
            failure_reason=raw.get("failure_reason"),
            manual_intervention_required=bool(raw.get("manual_intervention_required", False)),
            last_update_ts=_dt("last_update_ts"),
            entry_attempted=bool(raw.get("entry_attempted", False)),
            entry_submitted=bool(raw.get("entry_submitted", False)),
            position_activated=bool(raw.get("position_activated", False)),
            exit_triggered=bool(raw.get("exit_triggered", False)),
            exit_attempts=int(raw.get("exit_attempts", 0)),
            position_closed=bool(raw.get("position_closed", False)),
        )


def assert_startup_state_terminal_or_absent(raw: dict[str, Any] | None) -> None:
    """Fail closed when persisted lifecycle is non-terminal (A0.7 — no restart recovery)."""
    if not raw:
        return
    phase_raw = str(raw.get("phase", ZGapPhase.IDLE.value))
    if phase_raw not in {ZGapPhase.DONE.value, ZGapPhase.FAILED.value, ZGapPhase.IDLE.value}:
        raise RuntimeError(
            f"z_gap non-terminal persisted state at startup (phase={phase_raw}) — manual review required"
        )
