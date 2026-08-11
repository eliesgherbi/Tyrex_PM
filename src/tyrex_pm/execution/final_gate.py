"""Last-moment execution gate over the newest authoritative BookView."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec, OrderSpec
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.market_data.book_view import BookView, LegBookSnapshot
from tyrex_pm.runtime.capabilities import CapabilityController


@dataclass(frozen=True)
class FinalGatePolicy:
    max_book_age_ms: int = 2_000
    max_candidate_age_ms: int = 5_000
    minimum_entry_depth_shares: Decimal = Decimal("0")


class FinalExecutionGate:
    """Capture after signing and refuse POST when identity or liquidity moved."""

    def __init__(
        self,
        *,
        capture_book: Callable[[], BookView | None],
        capabilities: CapabilityController,
        active_binding_id: Callable[[], str | None],
        policy: FinalGatePolicy | None = None,
    ) -> None:
        self._capture_book = capture_book
        self._capabilities = capabilities
        self._active_binding_id = active_binding_id
        self._policy = policy or FinalGatePolicy()
        self.last_view: BookView | None = None
        self.last_reason: str | None = None

    def _leg(self, view: BookView, token_id: str) -> LegBookSnapshot | None:
        if view.up.token_id == token_id:
            return view.up
        if view.down.token_id == token_id:
            return view.down
        return None

    async def __call__(self, spec: OrderSpec) -> tuple[bool, str | None]:
        view = self._capture_book()
        self.last_view = view
        reason: str | None = None
        caps = self._capabilities.snapshot(exposed=isinstance(spec, MarketSellOrderSpec))
        if isinstance(spec, MarketBuyOrderSpec) and not caps.entry_executable:
            reason = "entry_capability_blocked:" + ",".join(caps.blockers)
        elif isinstance(spec, MarketSellOrderSpec) and not caps.exit_executable:
            reason = "exit_capability_blocked:" + ",".join(caps.blockers)
        elif view is None:
            reason = "book_view_unavailable"
        elif view.binding_id != self._active_binding_id():
            reason = "active_binding_changed"
        else:
            leg = self._leg(view, spec.token_id)
            if leg is None:
                reason = "token_not_in_active_market"
            elif leg.sync_health is not SyncHealth.READY:
                reason = f"book_{leg.sync_health.value.lower()}"
            elif leg.data_age_ms is None or leg.data_age_ms > self._policy.max_book_age_ms:
                reason = "book_stale"
            elif (
                spec.metadata.get("venue_tick_size") is not None
                and leg.tick_size is not None
                and leg.tick_size != Decimal(str(spec.metadata["venue_tick_size"]))
            ):
                # Missing book tick is tolerated when preparation already locked a
                # venue tick into metadata; a changed live tick is not.
                reason = "book_tick_size_changed_after_signing"
            elif isinstance(spec, MarketBuyOrderSpec):
                ask = leg.quote.best_ask
                if ask is None:
                    reason = "no_entry_ask"
                elif ask > spec.worst_price:
                    reason = "entry_price_moved"
                elif leg.quote.ask_size_at_touch < self._policy.minimum_entry_depth_shares:
                    reason = "entry_depth_below_minimum"
            else:
                bid = leg.quote.best_bid
                if bid is None:
                    reason = "no_exit_bid"
                elif bid < spec.minimum_price:
                    reason = "exit_price_below_minimum"

        candidate_monotonic_ns = spec.metadata.get("candidate_monotonic_ns")
        if reason is None:
            try:
                if candidate_monotonic_ns is None:
                    raise ValueError("candidate monotonic timestamp is missing")
                age_ms = max(
                    0.0,
                    (time.monotonic_ns() - int(candidate_monotonic_ns)) / 1_000_000,
                )
                if age_ms > self._policy.max_candidate_age_ms:
                    reason = "candidate_stale_after_signing"
            except (TypeError, ValueError):
                reason = "candidate_monotonic_time_invalid"
        self.last_reason = reason
        return reason is None, reason
