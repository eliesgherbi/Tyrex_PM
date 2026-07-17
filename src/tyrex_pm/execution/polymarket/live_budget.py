"""Persistent LiveBudgetGuard — single authority for the $5 R7 BUY envelope."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


class BudgetError(RuntimeError):
    pass


@dataclass
class LiveBudgetState:
    max_buy_notional: Decimal = Decimal("5.00")
    filled_buy_notional: Decimal = Decimal("0")
    working_buy_notional: Decimal = Decimal("0")
    uncertain_buy_notional: Decimal = Decimal("0")
    entry_attempts: int = 0
    entry_authorization_consumed: bool = False
    selected_market_id: str | None = None
    selected_instrument_id: str | None = None
    approval_artifact_id: str | None = None
    updated_at: str | None = None

    @property
    def remaining(self) -> Decimal:
        used = (
            self.filled_buy_notional
            + self.working_buy_notional
            + self.uncertain_buy_notional
        )
        return self.max_buy_notional - used

    @property
    def reserved_total(self) -> Decimal:
        return (
            self.filled_buy_notional
            + self.working_buy_notional
            + self.uncertain_buy_notional
        )

    def invariant_holds(self) -> bool:
        return self.reserved_total <= self.max_buy_notional

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_buy_notional": str(self.max_buy_notional),
            "filled_buy_notional": str(self.filled_buy_notional),
            "working_buy_notional": str(self.working_buy_notional),
            "uncertain_buy_notional": str(self.uncertain_buy_notional),
            "remaining": str(self.remaining),
            "entry_attempts": self.entry_attempts,
            "entry_authorization_consumed": self.entry_authorization_consumed,
            "selected_market_id": self.selected_market_id,
            "selected_instrument_id": self.selected_instrument_id,
            "approval_artifact_id": self.approval_artifact_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LiveBudgetState:
        return cls(
            max_buy_notional=Decimal(str(data.get("max_buy_notional", "5"))),
            filled_buy_notional=Decimal(str(data.get("filled_buy_notional", "0"))),
            working_buy_notional=Decimal(str(data.get("working_buy_notional", "0"))),
            uncertain_buy_notional=Decimal(str(data.get("uncertain_buy_notional", "0"))),
            entry_attempts=int(data.get("entry_attempts", 0)),
            entry_authorization_consumed=bool(
                data.get("entry_authorization_consumed", False)
            ),
            selected_market_id=data.get("selected_market_id"),
            selected_instrument_id=data.get("selected_instrument_id"),
            approval_artifact_id=data.get("approval_artifact_id"),
            updated_at=data.get("updated_at"),
        )


@dataclass
class LiveBudgetGuard:
    """Atomic file-backed budget authority."""

    path: Path
    state: LiveBudgetState = field(default_factory=LiveBudgetState)

    def load(self) -> LiveBudgetState:
        if not self.path.exists():
            return self.state
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.state = LiveBudgetState.from_dict(raw)
        if not self.state.invariant_holds():
            raise BudgetError("persisted budget invariant violated")
        return self.state

    def save(self) -> None:
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        if not self.state.invariant_holds():
            raise BudgetError("refusing to persist violated budget invariant")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.state.to_dict(), indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".budget_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def bind_approval(
        self,
        *,
        approval_artifact_id: str,
        market_id: str,
        instrument_id: str,
        max_buy_notional: Decimal = Decimal("5.00"),
    ) -> None:
        if max_buy_notional > Decimal("5.00"):
            raise BudgetError("max_buy_notional exceeds authorized $5 envelope")
        if self.state.approval_artifact_id and self.state.approval_artifact_id != approval_artifact_id:
            if self.state.entry_authorization_consumed or self.state.reserved_total > 0:
                raise BudgetError("cannot rebind approval while budget is in use")
        self.state.max_buy_notional = max_buy_notional
        self.state.approval_artifact_id = approval_artifact_id
        self.state.selected_market_id = market_id
        self.state.selected_instrument_id = instrument_id
        self.save()

    def can_reserve_entry(self, notional: Decimal) -> tuple[bool, str]:
        if notional <= 0:
            return False, "NON_POSITIVE_NOTIONAL"
        if self.state.entry_authorization_consumed:
            return False, "ENTRY_AUTHORIZATION_CONSUMED"
        if self.state.entry_attempts >= 1 and self.state.reserved_total > 0:
            return False, "SECOND_ENTRY_DENIED"
        if notional > self.state.remaining:
            return False, "EXCEEDS_REMAINING_BUDGET"
        if notional > self.state.max_buy_notional:
            return False, "EXCEEDS_MAX_BUY_NOTIONAL"
        return True, "OK"

    def reserve_working(self, notional: Decimal) -> None:
        ok, why = self.can_reserve_entry(notional)
        if not ok:
            raise BudgetError(why)
        self.state.working_buy_notional += notional
        self.state.entry_attempts += 1
        if not self.state.invariant_holds():
            self.state.working_buy_notional -= notional
            self.state.entry_attempts -= 1
            raise BudgetError("INVARIANT_VIOLATION")
        self.save()

    def mark_uncertain(self, notional: Decimal) -> None:
        """Move working reservation into uncertain (full possible notional)."""
        if notional > self.state.working_buy_notional:
            # Reserve additional into uncertain if needed
            extra = notional - self.state.working_buy_notional
            self.state.working_buy_notional = Decimal("0")
            self.state.uncertain_buy_notional += notional
        else:
            self.state.working_buy_notional -= notional
            self.state.uncertain_buy_notional += notional
        if not self.state.invariant_holds():
            raise BudgetError("INVARIANT_VIOLATION_UNCERTAIN")
        self.save()

    def apply_fill(self, notional: Decimal) -> None:
        n = min(notional, self.state.working_buy_notional + self.state.uncertain_buy_notional)
        # Prefer drawing down working, then uncertain
        from_working = min(n, self.state.working_buy_notional)
        self.state.working_buy_notional -= from_working
        rem = n - from_working
        self.state.uncertain_buy_notional = max(
            Decimal("0"), self.state.uncertain_buy_notional - rem
        )
        self.state.filled_buy_notional += notional
        self.state.entry_authorization_consumed = True
        if not self.state.invariant_holds():
            raise BudgetError("INVARIANT_VIOLATION_FILL")
        self.save()

    def release_working(self, notional: Decimal) -> None:
        self.state.working_buy_notional = max(
            Decimal("0"), self.state.working_buy_notional - notional
        )
        self.save()

    def release_uncertain(self, notional: Decimal) -> None:
        self.state.uncertain_buy_notional = max(
            Decimal("0"), self.state.uncertain_buy_notional - notional
        )
        self.save()

    def consume_entry_authorization(self) -> None:
        self.state.entry_authorization_consumed = True
        self.save()
