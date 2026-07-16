"""Hybrid event scheduler — debounced wake for paired-binary (M6)."""

from __future__ import annotations

import asyncio
from typing import Literal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import monotonic_s

TickSource = Literal["event_wake", "timer"]


class MarketUpdateCoordinator:
    """Coalesces authoritative store / user-WS updates into debounced wake signals."""

    def __init__(self, *, debounce_ms: float = 75.0) -> None:
        self._debounce_s = max(0.0, debounce_ms / 1000.0)
        self._event = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._authoritative_store_ids: set[int] = set()
        self._coalesce_count = 0
        self._pending_notifications = 0

    @property
    def tick_lock(self) -> asyncio.Lock:
        return self._tick_lock

    def register_authoritative_store(self, store: object) -> None:
        self._authoritative_store_ids.add(id(store))

    def notify_token_update(self, token_id: TokenId, *, store: object | None = None) -> None:
        if store is not None and id(store) not in self._authoritative_store_ids:
            return
        self._coalesce_count += 1
        self._pending_notifications += 1
        self._event.set()

    def notify_user_fill(self, token_id: TokenId) -> None:
        self.notify_token_update(token_id)

    def notify_sellability_change(self, token_id: TokenId) -> None:
        self.notify_token_update(token_id)

    async def wait_for_update(
        self,
        token_ids: list[TokenId],
        *,
        timeout_s: float,
    ) -> tuple[TickSource, int]:
        _ = token_ids
        deadline = monotonic_s() + max(0.0, timeout_s)
        while True:
            remaining = deadline - monotonic_s()
            if remaining <= 0:
                coalesce = self._drain_coalesce()
                return "timer", coalesce
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                coalesce = self._drain_coalesce()
                return "timer", coalesce
            self._event.clear()
            if self._debounce_s <= 0:
                coalesce = self._drain_coalesce()
                return "event_wake", coalesce
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self._debounce_s)
            except asyncio.TimeoutError:
                coalesce = self._drain_coalesce()
                return "event_wake", coalesce

    def _drain_coalesce(self) -> int:
        count = self._coalesce_count
        self._coalesce_count = 0
        self._pending_notifications = 0
        return count


def attach_coordinator_to_authoritative_store(coord, coordinator: MarketUpdateCoordinator) -> None:
    """Wire authoritative ``coord.market_state`` apply callbacks only."""
    store = coord.market_state
    if store is None:
        return
    coordinator.register_authoritative_store(store)

    def _on_update(token_id: TokenId) -> None:
        coordinator.notify_token_update(token_id, store=store)

    if hasattr(store, "set_on_token_update"):
        store.set_on_token_update(_on_update)


def notify_coordinator_user_fill(coord, token_id: TokenId | str) -> None:
    coordinator = getattr(coord, "market_update_coordinator", None)
    if coordinator is None:
        return
    coordinator.notify_user_fill(TokenId(str(token_id)))


def notify_coordinator_sellability(coord, token_id: TokenId | str) -> None:
    coordinator = getattr(coord, "market_update_coordinator", None)
    if coordinator is None:
        return
    coordinator.notify_sellability_change(TokenId(str(token_id)))
