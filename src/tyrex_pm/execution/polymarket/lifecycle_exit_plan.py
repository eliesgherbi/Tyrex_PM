"""Side-correct lifecycle exit planning (R7E).

BUY limit = maximum acceptable purchase price.
SELL limit = minimum acceptable sale price (marketable against bids).

Never reuse entry ``sized.limit_price`` as the SELL limit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.market_data.executable import book_quote, executable_vwap
from tyrex_pm.planning.exit_planner import round_sell_price_to_tick
from tyrex_pm.execution.polymarket.settlement import quantize_sell_qty


class ExitUrgency(str, Enum):
    NORMAL = "NORMAL"
    EMERGENCY = "EMERGENCY"


class ExitPlanStatus(str, Enum):
    PLANNED = "PLANNED"
    WAIT_NO_BIDS = "WAIT_NO_BIDS"
    REFUSE_FLOOR = "REFUSE_FLOOR"
    REFUSE_DEPTH = "REFUSE_DEPTH"
    REFUSE_STALE_BOOK = "REFUSE_STALE_BOOK"
    REFUSE_QTY = "REFUSE_QTY"
    REFUSE_ENTRY_PRICE_REUSE = "REFUSE_ENTRY_PRICE_REUSE"
    REFUSE_SLIPPAGE = "REFUSE_SLIPPAGE"
    REFUSE_SPREAD = "REFUSE_SPREAD"


@dataclass(frozen=True)
class ExitPricePolicy:
    """Price floors are minimum acceptable SELL prices (not BUY ceilings)."""

    normal_floor: Decimal = Decimal("0.01")
    emergency_floor: Decimal = Decimal("0.01")
    max_book_age_ms: int = 2000
    require_full_depth: bool = True
    # Worst accepted bid may not fall more than this below best bid (NORMAL).
    max_slippage_from_touch: Decimal = Decimal("0.05")
    # Bid-ask spread ceiling; None disables.
    max_book_spread: Decimal | None = Decimal("0.20")


@dataclass(frozen=True)
class ExitRetryPolicy:
    max_attempts: int = 3
    cooldown_s: float = 0.5
    backoff_multiplier: float = 1.5
    max_cooldown_s: float = 4.0


# Defaults align with tyrex_pm.runtime.r7_lifecycle_policy (single source of truth
# for operator docs); keep constructors usable without importing runtime.
DEFAULT_EXIT_PRICE_POLICY = ExitPricePolicy()
DEFAULT_EXIT_RETRY_POLICY = ExitRetryPolicy()


@dataclass(frozen=True)
class LifecycleExitPlan:
    status: ExitPlanStatus
    urgency: ExitUrgency
    limit_price: Decimal | None
    quantity: Decimal
    best_bid: Decimal | None
    worst_accepted_price: Decimal | None
    expected_vwap: Decimal | None
    executable_bid_depth: Decimal
    book_fingerprint: str | None
    book_age_ms: int | None
    tick_size: Decimal | None
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ExitPlanStatus.PLANNED and self.limit_price is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "urgency": self.urgency.value,
            "ok": self.ok,
            "limit_price": None if self.limit_price is None else str(self.limit_price),
            "quantity": str(self.quantity),
            "best_bid": None if self.best_bid is None else str(self.best_bid),
            "worst_accepted_price": (
                None if self.worst_accepted_price is None else str(self.worst_accepted_price)
            ),
            "expected_vwap": None if self.expected_vwap is None else str(self.expected_vwap),
            "executable_bid_depth": str(self.executable_bid_depth),
            "book_fingerprint": self.book_fingerprint,
            "book_age_ms": self.book_age_ms,
            "tick_size": None if self.tick_size is None else str(self.tick_size),
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


def book_fingerprint(book: BookSnapshot) -> str:
    payload = {
        "bids": [[str(l.price), str(l.quantity)] for l in book.bids[:10]],
        "asks": [[str(l.price), str(l.quantity)] for l in book.asks[:10]],
        "ts": book.ts_event.isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def book_from_clob_levels(
    *,
    token_id: str,
    bids: Sequence[Mapping[str, Any]] | Sequence[Sequence[Any]],
    asks: Sequence[Mapping[str, Any]] | Sequence[Sequence[Any]],
    ts_event: datetime | None = None,
) -> BookSnapshot:
    def _levels(raw: Sequence[Any], *, reverse: bool) -> tuple[BookLevel, ...]:
        out: list[BookLevel] = []
        for row in raw:
            if isinstance(row, Mapping):
                price = Decimal(str(row.get("price")))
                qty = Decimal(str(row.get("size") or row.get("quantity") or "0"))
            else:
                price = Decimal(str(row[0]))
                qty = Decimal(str(row[1]))
            if qty <= 0:
                continue
            out.append(BookLevel(price=price, quantity=qty))
        out.sort(key=lambda x: x.price, reverse=reverse)
        return tuple(out)

    return BookSnapshot(
        instrument_id=InstrumentId(f"token:{token_id[-12:]}"),
        ts_event=ts_event or datetime.now(timezone.utc),
        bids=_levels(bids, reverse=True),
        asks=_levels(asks, reverse=False),
    )


def compute_sell_qty_cap(
    *,
    confirmed_acquired: Decimal,
    sellable_balance: Decimal,
    remaining_after_confirmed_exits: Decimal | None = None,
    qty_step: Decimal = Decimal("0.01"),
) -> Decimal:
    """Strict quantity ownership for lifecycle exits."""
    caps = [confirmed_acquired, sellable_balance]
    if remaining_after_confirmed_exits is not None:
        caps.append(remaining_after_confirmed_exits)
    return quantize_sell_qty(min(caps), step=qty_step)


def plan_lifecycle_fak_sell(
    *,
    book: BookSnapshot | None,
    quantity: Decimal,
    tick_size: Decimal | None,
    now: datetime | None = None,
    urgency: ExitUrgency = ExitUrgency.NORMAL,
    policy: ExitPricePolicy | None = None,
    entry_buy_limit: Decimal | None = None,
    min_order_size: Decimal | None = None,
) -> LifecycleExitPlan:
    """Plan a marketable FAK SELL from bid-side liquidity only."""
    pol = policy or DEFAULT_EXIT_PRICE_POLICY
    now = now or datetime.now(timezone.utc)
    qty = quantize_sell_qty(quantity, step=Decimal("0.01"))
    floor = pol.emergency_floor if urgency is ExitUrgency.EMERGENCY else pol.normal_floor

    if qty <= 0:
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_QTY,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=None,
            worst_accepted_price=None,
            expected_vwap=None,
            executable_bid_depth=Decimal("0"),
            book_fingerprint=None,
            book_age_ms=None,
            tick_size=tick_size,
            reason="SELL_QTY_NON_POSITIVE",
        )

    if book is None:
        return LifecycleExitPlan(
            status=ExitPlanStatus.WAIT_NO_BIDS,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=None,
            worst_accepted_price=None,
            expected_vwap=None,
            executable_bid_depth=Decimal("0"),
            book_fingerprint=None,
            book_age_ms=None,
            tick_size=tick_size,
            reason="MISSING_EXIT_BOOK",
        )

    age_ms = int(max(0.0, (now - book.ts_event).total_seconds() * 1000))
    fp = book_fingerprint(book)
    if age_ms > pol.max_book_age_ms:
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_STALE_BOOK,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=None,
            worst_accepted_price=None,
            expected_vwap=None,
            executable_bid_depth=Decimal("0"),
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="EXIT_BOOK_STALE",
            evidence={"max_book_age_ms": pol.max_book_age_ms},
        )

    quote = book_quote(book)
    if quote.best_bid is None or quote.best_bid <= 0 or not book.bids:
        return LifecycleExitPlan(
            status=ExitPlanStatus.WAIT_NO_BIDS,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=None,
            worst_accepted_price=None,
            expected_vwap=None,
            executable_bid_depth=Decimal("0"),
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="NO_EXECUTABLE_BIDS",
        )

    if (
        pol.max_book_spread is not None
        and quote.best_ask is not None
        and quote.spread is not None
        and quote.spread > pol.max_book_spread
    ):
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_SPREAD,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=quote.best_bid,
            worst_accepted_price=None,
            expected_vwap=None,
            executable_bid_depth=Decimal("0"),
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="EXIT_BOOK_SPREAD_EXCEEDED",
            evidence={
                "spread": str(quote.spread),
                "max_spread": str(pol.max_book_spread),
            },
        )

    vwap = executable_vwap(book.bids, qty, side="SELL")
    depth = vwap.filled_qty
    if pol.require_full_depth and not vwap.sufficient:
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_DEPTH,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=quote.best_bid,
            worst_accepted_price=None,
            expected_vwap=vwap.vwap,
            executable_bid_depth=depth,
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="INSUFFICIENT_BID_DEPTH",
            evidence={"requested": str(qty), "available": str(depth)},
        )

    # Marketable FAK SELL limit = worst (lowest) bid level consumed for the size.
    # Walking bids descending: last consumed level is the worst accepted price.
    remaining = qty
    worst = quote.best_bid
    for level in book.bids:
        if remaining <= 0:
            break
        take = min(remaining, level.quantity)
        if take > 0:
            worst = level.price
        remaining -= take

    limit = round_sell_price_to_tick(worst, tick_size)
    if limit < floor:
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_FLOOR,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=quote.best_bid,
            worst_accepted_price=worst,
            expected_vwap=vwap.vwap,
            executable_bid_depth=depth,
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="BELOW_EXIT_PRICE_FLOOR",
            evidence={"floor": str(floor), "limit": str(limit)},
        )

    if (
        urgency is ExitUrgency.NORMAL
        and quote.best_bid - worst > pol.max_slippage_from_touch
    ):
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_SLIPPAGE,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=quote.best_bid,
            worst_accepted_price=worst,
            expected_vwap=vwap.vwap,
            executable_bid_depth=depth,
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="EXIT_SLIPPAGE_FROM_TOUCH_EXCEEDED",
            evidence={
                "best_bid": str(quote.best_bid),
                "worst": str(worst),
                "max_slippage": str(pol.max_slippage_from_touch),
            },
        )

    # Hard guard: never submit a SELL that equals a known entry BUY ceiling when
    # the bid side is strictly below that ceiling (incident regression).
    if entry_buy_limit is not None and limit >= entry_buy_limit and quote.best_bid < entry_buy_limit:
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_ENTRY_PRICE_REUSE,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=quote.best_bid,
            worst_accepted_price=worst,
            expected_vwap=vwap.vwap,
            executable_bid_depth=depth,
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="WOULD_REUSE_BUY_LIMIT_ABOVE_BEST_BID",
            evidence={
                "entry_buy_limit": str(entry_buy_limit),
                "best_bid": str(quote.best_bid),
                "planned_limit": str(limit),
            },
        )

    if min_order_size is not None and qty < min_order_size and urgency is ExitUrgency.NORMAL:
        return LifecycleExitPlan(
            status=ExitPlanStatus.REFUSE_QTY,
            urgency=urgency,
            limit_price=None,
            quantity=qty,
            best_bid=quote.best_bid,
            worst_accepted_price=worst,
            expected_vwap=vwap.vwap,
            executable_bid_depth=depth,
            book_fingerprint=fp,
            book_age_ms=age_ms,
            tick_size=tick_size,
            reason="BELOW_MIN_ORDER_SIZE",
        )

    return LifecycleExitPlan(
        status=ExitPlanStatus.PLANNED,
        urgency=urgency,
        limit_price=limit,
        quantity=qty,
        best_bid=quote.best_bid,
        worst_accepted_price=worst,
        expected_vwap=vwap.vwap,
        executable_bid_depth=depth,
        book_fingerprint=fp,
        book_age_ms=age_ms,
        tick_size=tick_size,
        reason=None,
        evidence={
            "style": "marketable_fak_sell_bid_side",
            "entry_buy_limit_not_used": True,
            "floor": str(floor),
            "bid_levels_used": True,
        },
    )


def is_fak_no_match_error(error: str | None) -> bool:
    if not error:
        return False
    e = error.lower()
    return "no orders found to match" in e or "fak order" in e


def next_retry_cooldown(attempt_index: int, policy: ExitRetryPolicy | None = None) -> float:
    pol = policy or DEFAULT_EXIT_RETRY_POLICY
    delay = pol.cooldown_s * (pol.backoff_multiplier ** max(0, attempt_index))
    return float(min(delay, pol.max_cooldown_s))
