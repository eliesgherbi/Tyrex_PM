"""Reusable OMS boundary (ShadowOMS now; live Polymarket adapter in R6)."""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.core.commands import CancelOrderCommand, SubmitOrderCommand
from tyrex_pm.core.ids import OrderId


class OMS(Protocol):
    def submit(self, command: SubmitOrderCommand) -> OrderId: ...

    def cancel(self, command: CancelOrderCommand) -> None: ...

    def stop(self) -> None: ...