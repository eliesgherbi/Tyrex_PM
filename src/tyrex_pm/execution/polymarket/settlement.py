"""Trade settlement vs order-insert status (R7C).

Key invariants:
  Order insert status != trade settlement
  MATCHED != CONFIRMED
  Planned quantity != acquired quantity
  Confirmed fill != immediately sellable balance
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Any, Callable, Protocol


class TradeSettlementStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    MATCHED = "MATCHED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"


class SettlementPhase(str, Enum):
    ENTRY_SUBMITTING = "ENTRY_SUBMITTING"
    ENTRY_MATCHED = "ENTRY_MATCHED"
    ENTRY_SETTLING = "ENTRY_SETTLING"
    ENTRY_CONFIRMED = "ENTRY_CONFIRMED"
    ACTIVE = "ACTIVE"
    EXIT_SUBMITTING = "EXIT_SUBMITTING"
    EXIT_MATCHED = "EXIT_MATCHED"
    EXIT_SETTLING = "EXIT_SETTLING"
    FLAT = "FLAT"
    FLAT_EXTERNAL_ACTION = "FLAT_EXTERNAL_ACTION"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


_TERMINAL_SUCCESS = frozenset(
    {TradeSettlementStatus.CONFIRMED, TradeSettlementStatus.MINED}
)
_TERMINAL_FAIL = frozenset({TradeSettlementStatus.FAILED})


def normalize_trade_status(raw: str | None) -> TradeSettlementStatus:
    if not raw:
        return TradeSettlementStatus.UNKNOWN
    s = str(raw).strip().upper()
    for st in TradeSettlementStatus:
        if st.value == s:
            return st
    # Venue sometimes uses lowercase / variants
    aliases = {
        "CONFIRM": TradeSettlementStatus.CONFIRMED,
        "CONFIRMED": TradeSettlementStatus.CONFIRMED,
        "MINED": TradeSettlementStatus.MINED,
        "MATCHED": TradeSettlementStatus.MATCHED,
        "RETRYING": TradeSettlementStatus.RETRYING,
        "FAILED": TradeSettlementStatus.FAILED,
        "FAIL": TradeSettlementStatus.FAILED,
    }
    return aliases.get(s, TradeSettlementStatus.UNKNOWN)


@dataclass(frozen=True)
class TradeEvidence:
    trade_id: str
    order_id: str | None
    side: str
    size: Decimal
    price: Decimal
    status: TradeSettlementStatus
    fee: Decimal | None = None
    token_id: str | None = None
    tx_hash: str | None = None
    raw_status: str | None = None


@dataclass
class InventoryEvidence:
    """Venue-proven inventory — never planned/estimated shares."""

    confirmed_acquired: Decimal = Decimal("0")
    sellable_balance: Decimal = Decimal("0")
    allowance: Decimal | None = None
    token_id: str | None = None
    funder_fp: str | None = None
    trade_statuses: list[str] = field(default_factory=list)
    exposure_low: Decimal = Decimal("0")
    exposure_high: Decimal = Decimal("0")
    uncertain: bool = False

    @property
    def sellable_qty(self) -> Decimal:
        return min(self.confirmed_acquired, self.sellable_balance)


@dataclass
class SellReadiness:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    sell_qty: Decimal = Decimal("0")
    evidence: InventoryEvidence | None = None


class SettlementClock(Protocol):
    def sleep(self, seconds: float) -> None: ...

    def now(self) -> datetime: ...


@dataclass
class FakeSettlementClock:
    """Deterministic clock for tests — no real sleep."""

    _t: datetime = field(
        default_factory=lambda: datetime(2026, 7, 17, 15, 42, 47, tzinfo=timezone.utc)
    )
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        from datetime import timedelta

        self._t = self._t + timedelta(seconds=seconds)

    def now(self) -> datetime:
        return self._t


@dataclass
class RealSettlementClock:
    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def quantize_sell_qty(qty: Decimal, step: Decimal = Decimal("0.01")) -> Decimal:
    if step <= 0:
        step = Decimal("0.01")
    if qty <= 0:
        return Decimal("0")
    steps = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return steps * step


def inventory_from_trades(
    trades: list[TradeEvidence],
    *,
    side: str = "BUY",
    require_terminal: bool = True,
) -> tuple[Decimal, list[str], bool]:
    """Sum trade sizes for side. If require_terminal, only MINED/CONFIRMED count as acquired."""
    statuses: list[str] = []
    total = Decimal("0")
    uncertain = False
    for t in trades:
        if t.side.upper() != side.upper():
            continue
        statuses.append(t.status.value)
        if t.status in _TERMINAL_FAIL:
            continue
        if t.status is TradeSettlementStatus.RETRYING:
            uncertain = True
            continue
        if require_terminal and t.status not in _TERMINAL_SUCCESS:
            if t.status is TradeSettlementStatus.MATCHED:
                uncertain = True
            continue
        total += t.size
    return total, statuses, uncertain


def evaluate_sell_readiness(
    *,
    confirmed_acquired: Decimal,
    sellable_balance: Decimal,
    allowance: Decimal | None,
    token_id: str,
    expected_token_id: str,
    funder_ok: bool,
    stream_or_rest_healthy: bool,
    bid_depth_ok: bool,
    tick_min_ok: bool,
    qty_step: Decimal = Decimal("0.01"),
) -> SellReadiness:
    blockers: list[str] = []
    if token_id != expected_token_id:
        blockers.append("TOKEN_ID_MISMATCH")
    if not funder_ok:
        blockers.append("FUNDER_PROXY_MISMATCH")
    if confirmed_acquired <= 0:
        blockers.append("NO_CONFIRMED_ACQUIRED_QTY")
    if sellable_balance <= 0:
        blockers.append("CONDITIONAL_BALANCE_ZERO")
    if allowance is not None and allowance < min(confirmed_acquired, sellable_balance):
        blockers.append("CONDITIONAL_ALLOWANCE_INSUFFICIENT")
    if not stream_or_rest_healthy:
        blockers.append("RECONCILIATION_UNHEALTHY")
    if not bid_depth_ok:
        blockers.append("BID_DEPTH_UNAVAILABLE")
    if not tick_min_ok:
        blockers.append("TICK_OR_MIN_SIZE_INVALID")
    sell_qty = quantize_sell_qty(
        min(confirmed_acquired, sellable_balance), step=qty_step
    )
    if sell_qty <= 0 and not blockers:
        blockers.append("SELL_QTY_ZERO_AFTER_QUANTIZE")
    ev = InventoryEvidence(
        confirmed_acquired=confirmed_acquired,
        sellable_balance=sellable_balance,
        allowance=allowance,
        token_id=token_id,
    )
    return SellReadiness(ok=not blockers and sell_qty > 0, blockers=blockers, sell_qty=sell_qty, evidence=ev)


@dataclass
class SettlementWaitConfig:
    max_wait_s: float = 45.0
    initial_backoff_s: float = 0.25
    max_backoff_s: float = 4.0
    max_sell_attempts: int = 1


@dataclass
class SettlementWaitResult:
    phase: SettlementPhase
    trades: list[TradeEvidence]
    confirmed_acquired: Decimal
    sellable_balance: Decimal
    allowance: Decimal | None
    trade_failed: bool
    exhausted: bool
    polls: int
    facts: list[dict[str, Any]] = field(default_factory=list)
    exposure_low: Decimal = Decimal("0")
    exposure_high: Decimal = Decimal("0")


def wait_for_entry_settlement(
    *,
    poll_trades: Callable[[], list[TradeEvidence]],
    poll_balance: Callable[[], tuple[Decimal, Decimal | None]],
    order_id: str | None,
    planned_qty: Decimal,
    clock: SettlementClock,
    config: SettlementWaitConfig | None = None,
    stream_events: Callable[[], list[TradeEvidence]] | None = None,
) -> SettlementWaitResult:
    """Bounded wait after MATCHED. Never treats insert matched as inventory."""
    cfg = config or SettlementWaitConfig()
    facts: list[dict[str, Any]] = []
    start = clock.now()
    backoff = cfg.initial_backoff_s
    polls = 0
    last_trades: list[TradeEvidence] = []
    sellable = Decimal("0")
    allowance: Decimal | None = None

    facts.append(
        {
            "event": "settlement_wait_begin",
            "order_id_suffix": None if not order_id else order_id[-10:],
            "planned_qty_not_inventory": str(planned_qty),
        }
    )

    while True:
        polls += 1
        trades = list(poll_trades())
        if stream_events is not None:
            # Stream evidence preferred when present
            streamed = stream_events()
            if streamed:
                by_id = {t.trade_id: t for t in trades}
                for t in streamed:
                    by_id[t.trade_id] = t
                trades = list(by_id.values())
        last_trades = trades
        acquired, statuses, uncertain = inventory_from_trades(trades, side="BUY")
        sellable, allowance = poll_balance()
        facts.append(
            {
                "event": "settlement_poll",
                "poll": polls,
                "statuses": statuses,
                "confirmed_acquired": str(acquired),
                "sellable_balance": str(sellable),
                "uncertain": uncertain,
            }
        )

        if any(t.status is TradeSettlementStatus.FAILED for t in trades if t.side.upper() == "BUY"):
            # Only FAIL if no successful confirmed qty
            if acquired <= 0:
                return SettlementWaitResult(
                    phase=SettlementPhase.MANUAL_INTERVENTION
                    if uncertain
                    else SettlementPhase.ENTRY_MATCHED,
                    trades=last_trades,
                    confirmed_acquired=Decimal("0"),
                    sellable_balance=sellable,
                    allowance=allowance,
                    trade_failed=True,
                    exhausted=False,
                    polls=polls,
                    facts=facts,
                    exposure_low=Decimal("0"),
                    exposure_high=planned_qty if uncertain else Decimal("0"),
                )

        if acquired > 0 and sellable > 0:
            return SettlementWaitResult(
                phase=SettlementPhase.ENTRY_CONFIRMED,
                trades=last_trades,
                confirmed_acquired=acquired,
                sellable_balance=sellable,
                allowance=allowance,
                trade_failed=False,
                exhausted=False,
                polls=polls,
                facts=facts,
                exposure_low=min(acquired, sellable),
                exposure_high=max(acquired, sellable),
            )

        if acquired > 0 and sellable <= 0:
            # Confirmed trade but balance not visible yet
            facts.append({"event": "entry_settling", "reason": "balance_not_visible"})

        elapsed = (clock.now() - start).total_seconds()
        if elapsed >= cfg.max_wait_s:
            # Exposure range — never assert planned as confirmed residual
            high = planned_qty
            if acquired > 0:
                high = max(acquired, planned_qty) if uncertain else acquired
            elif any(
                t.status is TradeSettlementStatus.MATCHED for t in trades if t.side.upper() == "BUY"
            ):
                high = planned_qty
            else:
                high = Decimal("0")
            return SettlementWaitResult(
                phase=SettlementPhase.MANUAL_INTERVENTION,
                trades=last_trades,
                confirmed_acquired=acquired,
                sellable_balance=sellable,
                allowance=allowance,
                trade_failed=False,
                exhausted=True,
                polls=polls,
                facts=facts,
                exposure_low=acquired if acquired > 0 else Decimal("0"),
                exposure_high=high,
            )

        clock.sleep(backoff)
        backoff = min(cfg.max_backoff_s, backoff * 1.5)


def detect_manual_flat(
    *,
    confirmed_acquired: Decimal,
    current_balance: Decimal,
    current_position: Decimal,
    sell_trades: list[TradeEvidence],
) -> bool:
    """True when venue proves flat after external/manual sell."""
    if confirmed_acquired <= 0:
        return current_balance <= 0 and current_position <= 0
    sold = sum((t.size for t in sell_trades if t.side.upper() == "SELL"), Decimal("0"))
    if current_balance <= 0 and current_position <= 0:
        if sold > 0 or confirmed_acquired > 0:
            return True
    return False


__all__ = [
    "TradeSettlementStatus",
    "SettlementPhase",
    "TradeEvidence",
    "InventoryEvidence",
    "SellReadiness",
    "SettlementWaitConfig",
    "SettlementWaitResult",
    "FakeSettlementClock",
    "RealSettlementClock",
    "normalize_trade_status",
    "inventory_from_trades",
    "evaluate_sell_readiness",
    "wait_for_entry_settlement",
    "detect_manual_flat",
    "quantize_sell_qty",
]
