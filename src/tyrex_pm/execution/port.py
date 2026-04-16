"""
Execution port: shadow `OrderIntent` sink vs live Nautilus ``submit_order``.

Live guru execution: :class:`~tyrex_pm.execution.nautilus_guru_exec.NautilusGuruExecutionPort`.
"""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.core.types import OrderIntent


class ExecutionPort(Protocol):
    def submit_intent(self, intent: OrderIntent, *, mode: str) -> None: ...


class NoOpExecutionPort:
    """Records intents; never talks to venues (shadow / tests)."""

    def __init__(self) -> None:
        self.records: list[tuple[OrderIntent, str]] = []
        self.virtual_exit_records: list[
            tuple[OrderIntent, str, str, int, bool]
        ] = []

    def submit_intent(self, intent: OrderIntent, *, mode: str) -> None:
        self.records.append((intent, mode))

    def submit_virtual_exit_intent(
        self,
        intent: OrderIntent,
        *,
        mode: str,
        order_style: str,
        aggression_ticks: int,
        use_rest_book: bool,
    ) -> None:
        self.virtual_exit_records.append(
            (intent, mode, order_style, aggression_ticks, use_rest_book),
        )
