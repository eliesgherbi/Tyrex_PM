"""Configurable OMS for Z-Gap shadow enforce scenarios (A0.8)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import EnterIntent, ExitIntent


@dataclass
class OmsFillSpec:
    status: str = "matched"
    taking_amount: str | None = None
    making_amount: str | None = None


@dataclass
class ScenarioOMS:
    """OMS stub returning scripted fills per submit call."""

    buy_fills: list[OmsFillSpec] = field(default_factory=list)
    sell_fills: list[OmsFillSpec] = field(default_factory=list)
    _buy_idx: int = 0
    _sell_idx: int = 0
    submits: list[dict[str, Any]] = field(default_factory=list)

    def _next_spec(self, side: Side) -> OmsFillSpec:
        if side == Side.BUY:
            idx = min(self._buy_idx, max(0, len(self.buy_fills) - 1))
            spec = self.buy_fills[idx] if self.buy_fills else OmsFillSpec()
            self._buy_idx += 1
            return spec
        idx = min(self._sell_idx, max(0, len(self.sell_fills) - 1))
        spec = self.sell_fills[idx] if self.sell_fills else OmsFillSpec()
        self._sell_idx += 1
        return spec

    async def submit(self, ap, *, market_info=None) -> str:
        del market_info
        side = ap.intent.side
        size = ap.intent.size
        spec = self._next_spec(side)
        taking = spec.taking_amount if spec.taking_amount is not None else str(size)
        making = spec.making_amount if spec.making_amount is not None else ""
        if spec.status.lower() in {"live", "open", "delayed"}:
            taking = ""
            making = ""
        payload = {
            "status": spec.status,
            "takingAmount": taking,
            "makingAmount": making,
            "orderID": f"0x{side.value}{len(self.submits)}",
            "success": True,
        }
        self.submits.append(
            {
                "side": side.value,
                "size": str(size),
                "status": spec.status,
                "takingAmount": taking,
                "makingAmount": making,
            }
        )
        return json.dumps(payload)

    @property
    def buy_submit_count(self) -> int:
        return sum(1 for s in self.submits if s["side"] == "BUY")

    @property
    def sell_submit_count(self) -> int:
        return sum(1 for s in self.submits if s["side"] == "SELL")
