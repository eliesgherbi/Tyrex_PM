"""Last-moment execution gate over the newest authoritative BookView.

``dispatch_check`` is the pure decision: it reads only its arguments, so live (real
monotonic clock, live capabilities) and research replay (virtual time, recorded book
view) apply the same rules. ``FinalExecutionGate`` binds it to live I/O.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.execution.orders import (
    LimitOrderSpec,
    MarketBuyOrderSpec,
    MarketSellOrderSpec,
    OrderSide,
    OrderSpec,
)
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.market_data.book_view import BookView, LegBookSnapshot
from tyrex_pm.runtime.capabilities import CapabilityController

# Semantic version of dispatch_check; part of replay provenance.
DISPATCH_CHECK_VERSION = "dispatch_check_v1"


@dataclass(frozen=True)
class FinalGatePolicy:
    max_book_age_ms: int = 2_000
    max_candidate_age_ms: int = 5_000
    minimum_entry_depth_shares: Decimal = Decimal("0")


@dataclass(frozen=True)
class DispatchFacts:
    """Everything ``dispatch_check`` may read besides the order and the book view."""

    now_monotonic_ns: int
    entry_executable: bool = True
    exit_executable: bool = True
    capability_blockers: tuple[str, ...] = ()
    active_binding_id: str | None = None
    entry_tau_s: float | None = None
    entry_cutoff_s: float | None = None
    features_ready: bool | str = True
    startup_buy_block: str | None = None


def is_entry_spec(spec: OrderSpec) -> bool:
    return isinstance(spec, MarketBuyOrderSpec) or (
        isinstance(spec, LimitOrderSpec) and spec.side is OrderSide.BUY
    )


def is_exit_spec(spec: OrderSpec) -> bool:
    return isinstance(spec, MarketSellOrderSpec) or (
        isinstance(spec, LimitOrderSpec) and spec.side is OrderSide.SELL
    )


def leg_for_token(view: BookView, token_id: str) -> LegBookSnapshot | None:
    if view.up.token_id == token_id:
        return view.up
    if view.down.token_id == token_id:
        return view.down
    return None


def dispatch_check(
    spec: OrderSpec,
    view: BookView | None,
    facts: DispatchFacts,
    policy: FinalGatePolicy,
) -> str | None:
    """Return the blocking reason, or None when the order may be dispatched."""
    reason: str | None = None
    is_exit = is_exit_spec(spec)
    is_entry = is_entry_spec(spec)
    if is_entry and facts.startup_buy_block:
        reason = facts.startup_buy_block
    if reason is None and is_entry and facts.entry_cutoff_s is not None:
        tau = facts.entry_tau_s
        if tau is None or tau < facts.entry_cutoff_s:
            reason = "ENTRY_CUTOFF_PASSED"
    if reason is None and is_entry:
        ready = facts.features_ready
        if ready is False:
            reason = "FEATURE_NOT_READY"
        elif isinstance(ready, str) and ready:
            reason = ready
    if reason is None and is_entry and not facts.entry_executable:
        reason = "entry_capability_blocked:" + ",".join(facts.capability_blockers)
    elif reason is None and is_exit and not facts.exit_executable:
        reason = "exit_capability_blocked:" + ",".join(facts.capability_blockers)
    elif reason is None and view is None:
        reason = "book_view_unavailable"
    elif reason is None and view.binding_id != facts.active_binding_id:
        reason = "active_binding_changed"
    elif reason is None:
        leg = leg_for_token(view, spec.token_id)
        if leg is None:
            reason = "token_not_in_active_market"
        elif leg.sync_health is not SyncHealth.READY:
            reason = f"book_{leg.sync_health.value.lower()}"
        elif leg.data_age_ms is None or leg.data_age_ms > policy.max_book_age_ms:
            reason = "book_stale"
        elif (
            spec.metadata.get("venue_tick_size") is not None
            and leg.tick_size is not None
            and not getattr(leg, "tick_from_binding", False)
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
            elif leg.quote.ask_size_at_touch < policy.minimum_entry_depth_shares:
                reason = "entry_depth_below_minimum"
        elif isinstance(spec, LimitOrderSpec):
            post_only = bool(spec.metadata.get("post_only"))
            if spec.side is OrderSide.BUY:
                ask = leg.quote.best_ask
                if ask is None:
                    reason = "no_entry_ask"
                elif post_only and spec.limit_price >= ask:
                    reason = "post_only_would_cross"
            else:
                bid = leg.quote.best_bid
                if bid is None:
                    reason = "no_exit_bid"
                elif post_only and spec.limit_price <= bid:
                    reason = "post_only_would_cross"
        else:
            bid = leg.quote.best_bid
            if bid is None:
                reason = "no_exit_bid"
            elif bid < spec.minimum_price:
                reason = "exit_price_below_minimum"

    if reason is None:
        candidate_monotonic_ns = spec.metadata.get("candidate_monotonic_ns")
        try:
            if candidate_monotonic_ns is None:
                raise ValueError("candidate monotonic timestamp is missing")
            age_ms = max(
                0.0,
                (facts.now_monotonic_ns - int(candidate_monotonic_ns)) / 1_000_000,
            )
            if age_ms > policy.max_candidate_age_ms:
                reason = "candidate_stale_after_signing"
        except (TypeError, ValueError):
            reason = "candidate_monotonic_time_invalid"
    return reason


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
        self.entry_tau_s: Callable[[], float | None] | None = None
        self.entry_cutoff_s: float | None = None
        self.features_ready: Callable[[], bool | str] | None = None
        self.startup_buy_block: Callable[[], str | None] | None = None
        self.note_stream_gap: Callable[[], None] | None = None

    @property
    def policy(self) -> FinalGatePolicy:
        return self._policy

    def _leg(self, view: BookView, token_id: str) -> LegBookSnapshot | None:
        return leg_for_token(view, token_id)

    async def __call__(self, spec: OrderSpec) -> tuple[bool, str | None]:
        view = self._capture_book()
        self.last_view = view
        is_entry = is_entry_spec(spec)
        caps = self._capabilities.snapshot(exposed=is_exit_spec(spec))
        # Only evaluate the hooks the pure check would read, in its order.
        startup_block = (
            self.startup_buy_block() if is_entry and self.startup_buy_block else None
        )
        tau = None
        if is_entry and self.entry_cutoff_s is not None and self.entry_tau_s is not None:
            tau = self.entry_tau_s()
        features: bool | str = True
        if is_entry and self.features_ready is not None and not startup_block:
            features = self.features_ready()
        facts = DispatchFacts(
            now_monotonic_ns=time.monotonic_ns(),
            entry_executable=caps.entry_executable,
            exit_executable=caps.exit_executable,
            capability_blockers=tuple(caps.blockers),
            active_binding_id=self._active_binding_id(),
            entry_tau_s=tau,
            entry_cutoff_s=self.entry_cutoff_s,
            features_ready=features,
            startup_buy_block=startup_block,
        )
        reason = dispatch_check(spec, view, facts, self._policy)
        if is_entry and view is not None and self.note_stream_gap is not None:
            leg = leg_for_token(view, spec.token_id)
            if leg is not None and leg.sync_health is not SyncHealth.READY:
                self.note_stream_gap()
        self.last_reason = reason
        return reason is None, reason
