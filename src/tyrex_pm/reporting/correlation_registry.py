"""client_order_id ↔ correlation_id map (framework path)."""

from __future__ import annotations


class OrderCorrelationRegistry:
    __slots__ = ("_by_coid",)

    def __init__(self) -> None:
        self._by_coid: dict[str, str] = {}

    def register(self, client_order_id: str, correlation_id: str) -> None:
        self._by_coid[str(client_order_id)] = str(correlation_id)

    def correlation_for(self, client_order_id: str) -> str | None:
        return self._by_coid.get(str(client_order_id))
