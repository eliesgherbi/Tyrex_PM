"""Reusable live signal-state store for external BTC/Chainlink feeds and PTB (A0.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Feed / PTB freshness labels (explicit, testable).
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_MISSING = "missing"
FRESHNESS_PENDING = "pending"
FRESHNESS_OBSERVED = "observed"
FRESHNESS_OBSERVED_FROM_LOG = "observed_from_log"
FRESHNESS_LATE = "late"
FRESHNESS_UNTRUSTED = "untrusted"

# Basis status — distinct from feed freshness.
BASIS_FRESH = "fresh"
BASIS_UNTRUSTED = "untrusted"
BASIS_MISSING = "missing"

DEFAULT_BINANCE_MAX_AGE_MS = 2000.0
DEFAULT_CHAINLINK_MAX_AGE_MS = 3000.0
DEFAULT_BOOK_TICKER_FALLBACK_STALE_MS = 2000.0
DEFAULT_PTB_LATE_THRESHOLD_MS = 2000.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _age_ms(now: datetime, ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() * 1000.0


def _feed_freshness(age_ms: float | None, *, max_age_ms: float) -> str:
    if age_ms is None:
        return FRESHNESS_MISSING
    if age_ms <= max_age_ms:
        return FRESHNESS_FRESH
    return FRESHNESS_STALE


def compute_basis_bps(binance_price: Decimal, chainlink_price: Decimal) -> Decimal:
    """basis_bps = ((S - S_CL) / S_CL) * 10000 using Decimal math."""
    if chainlink_price <= 0:
        raise ValueError("chainlink_price must be positive")
    return ((binance_price - chainlink_price) / chainlink_price) * Decimal("10000")


@dataclass(frozen=True)
class SignalSnapshot:
    """Immutable point-in-time external signal state."""

    binance_price: Decimal | None
    binance_source_ts: datetime | None
    binance_recv_ts: datetime | None
    binance_age_ms: float | None
    binance_freshness: str

    chainlink_price: Decimal | None
    chainlink_source_ts: datetime | None
    chainlink_recv_ts: datetime | None
    chainlink_age_ms: float | None
    chainlink_freshness: str

    price_to_beat: Decimal | None
    ptb_status: str
    ptb_observed_ts: datetime | None
    ptb_lag_ms: float | None

    basis_bps: Decimal | None
    basis_status: str

    feed_reject_reason: str | None
    snapshot_ts: datetime

    @property
    def chainlink_fresh(self) -> bool:
        return self.chainlink_freshness == FRESHNESS_FRESH


@dataclass
class _BinanceState:
    book_price: Decimal | None = None
    book_source_ts: datetime | None = None
    book_recv_ts: datetime | None = None
    trade_price: Decimal | None = None
    trade_source_ts: datetime | None = None
    trade_recv_ts: datetime | None = None


@dataclass
class _ChainlinkState:
    price: Decimal | None = None
    source_ts: datetime | None = None
    recv_ts: datetime | None = None


@dataclass
class _PtbState:
    price: Decimal | None = None
    status: str = FRESHNESS_PENDING
    observed_ts: datetime | None = None
    lag_ms: float | None = None


@dataclass
class SignalStateStore:
    """In-memory authoritative snapshot for Binance, Chainlink, PTB, and basis."""

    binance_max_age_ms: float = DEFAULT_BINANCE_MAX_AGE_MS
    chainlink_max_age_ms: float = DEFAULT_CHAINLINK_MAX_AGE_MS
    book_ticker_fallback_stale_ms: float = DEFAULT_BOOK_TICKER_FALLBACK_STALE_MS
    ptb_late_threshold_ms: float = DEFAULT_PTB_LATE_THRESHOLD_MS

    _binance: _BinanceState = field(default_factory=_BinanceState)
    _chainlink: _ChainlinkState = field(default_factory=_ChainlinkState)
    _ptb: _PtbState = field(default_factory=_PtbState)
    _binance_connected: bool = False
    _chainlink_connected: bool = False

    def mark_binance_connected(self, *, connected: bool) -> None:
        self._binance_connected = connected

    def mark_chainlink_connected(self, *, connected: bool) -> None:
        self._chainlink_connected = connected

    def update_binance(
        self,
        price: Decimal,
        *,
        source_ts: datetime | None,
        recv_ts: datetime,
        stream: str = "bookTicker",
    ) -> None:
        if stream == "bookTicker":
            self._binance.book_price = price
            self._binance.book_source_ts = source_ts
            self._binance.book_recv_ts = recv_ts
        elif stream == "aggTrade":
            self._binance.trade_price = price
            self._binance.trade_source_ts = source_ts
            self._binance.trade_recv_ts = recv_ts
        else:
            self._binance.trade_price = price
            self._binance.trade_source_ts = source_ts
            self._binance.trade_recv_ts = recv_ts

    def update_chainlink(
        self,
        price: Decimal,
        *,
        source_ts: datetime | None,
        recv_ts: datetime,
    ) -> None:
        self._chainlink.price = price
        self._chainlink.source_ts = source_ts
        self._chainlink.recv_ts = recv_ts

    def update_price_to_beat(
        self,
        price: Decimal | None,
        *,
        status: str,
        observed_ts: datetime | None = None,
        lag_ms: float | None = None,
    ) -> None:
        normalized = str(status or FRESHNESS_PENDING).strip().lower()
        if normalized in {"complete"}:
            normalized = FRESHNESS_OBSERVED
        if normalized in {FRESHNESS_OBSERVED, FRESHNESS_OBSERVED_FROM_LOG} and price is None:
            normalized = FRESHNESS_PENDING
        elif price is not None and normalized in {FRESHNESS_OBSERVED, FRESHNESS_OBSERVED_FROM_LOG}:
            if lag_ms is not None and lag_ms > self.ptb_late_threshold_ms:
                normalized = FRESHNESS_LATE
        self._ptb.price = price if normalized not in {FRESHNESS_PENDING, FRESHNESS_MISSING} else price
        self._ptb.status = normalized
        self._ptb.observed_ts = observed_ts
        self._ptb.lag_ms = lag_ms

    def lock_price_to_beat(
        self,
        price: Decimal,
        *,
        status: str,
        observed_ts: datetime | None = None,
        lag_ms: float | None = None,
    ) -> None:
        """Lock boundary K into the store (experimental PTB capture path)."""
        self.update_price_to_beat(
            price,
            status=status,
            observed_ts=observed_ts,
            lag_ms=lag_ms,
        )

    def _select_binance(self, now: datetime) -> tuple[Decimal | None, datetime | None, datetime | None]:
        book_age = _age_ms(now, self._binance.book_recv_ts)
        use_book = (
            self._binance.book_price is not None
            and book_age is not None
            and book_age <= self.book_ticker_fallback_stale_ms
        )
        if use_book:
            return self._binance.book_price, self._binance.book_source_ts, self._binance.book_recv_ts
        if self._binance.trade_price is not None:
            return (
                self._binance.trade_price,
                self._binance.trade_source_ts,
                self._binance.trade_recv_ts,
            )
        if self._binance.book_price is not None:
            return self._binance.book_price, self._binance.book_source_ts, self._binance.book_recv_ts
        return None, None, None

    def volatility_price_observation(
        self,
        now: datetime | None = None,
    ) -> tuple[Decimal, datetime] | None:
        """Price + exchange timestamp for EWMA sigma (prefers aggTrade ticks)."""
        ts = now or _utc_now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if self._binance.trade_price is not None:
            trade_ts = self._binance.trade_source_ts or self._binance.trade_recv_ts
            trade_age = _age_ms(ts, self._binance.trade_recv_ts)
            if trade_ts is not None and trade_age is not None and trade_age <= self.binance_max_age_ms:
                return self._binance.trade_price, trade_ts
        selected = self._select_binance(ts)
        if selected[0] is None:
            return None
        price, source_ts, recv_ts = selected
        obs_ts = source_ts or recv_ts
        if obs_ts is None:
            return None
        return price, obs_ts

    def _compute_basis(
        self,
        *,
        binance_price: Decimal | None,
        chainlink_price: Decimal | None,
        chainlink_freshness: str,
    ) -> tuple[Decimal | None, str]:
        if binance_price is None or chainlink_price is None:
            return None, BASIS_MISSING
        if chainlink_freshness != FRESHNESS_FRESH:
            return compute_basis_bps(binance_price, chainlink_price), BASIS_UNTRUSTED
        return compute_basis_bps(binance_price, chainlink_price), BASIS_FRESH

    def snapshot(self, now: datetime | None = None) -> SignalSnapshot:
        ts = now or _utc_now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        b_price, b_source, b_recv = self._select_binance(ts)
        b_age_ts = b_source if b_source is not None else b_recv
        b_age = _age_ms(ts, b_age_ts)
        b_fresh = _feed_freshness(b_age, max_age_ms=self.binance_max_age_ms)

        c_age_ts = self._chainlink.source_ts if self._chainlink.source_ts is not None else self._chainlink.recv_ts
        c_age = _age_ms(ts, c_age_ts)
        c_fresh = _feed_freshness(c_age, max_age_ms=self.chainlink_max_age_ms)

        basis_bps, basis_status = self._compute_basis(
            binance_price=b_price,
            chainlink_price=self._chainlink.price,
            chainlink_freshness=c_fresh,
        )

        reject_reason: str | None = None
        if b_fresh == FRESHNESS_MISSING:
            reject_reason = "binance_missing"
        elif b_fresh == FRESHNESS_STALE:
            reject_reason = "binance_stale"
        elif c_fresh == FRESHNESS_MISSING:
            reject_reason = "chainlink_missing"
        elif c_fresh == FRESHNESS_STALE:
            reject_reason = "chainlink_stale"

        return SignalSnapshot(
            binance_price=b_price,
            binance_source_ts=b_source,
            binance_recv_ts=b_recv,
            binance_age_ms=b_age,
            binance_freshness=b_fresh,
            chainlink_price=self._chainlink.price,
            chainlink_source_ts=self._chainlink.source_ts,
            chainlink_recv_ts=self._chainlink.recv_ts,
            chainlink_age_ms=c_age,
            chainlink_freshness=c_fresh,
            price_to_beat=self._ptb.price,
            ptb_status=self._ptb.status,
            ptb_observed_ts=self._ptb.observed_ts,
            ptb_lag_ms=self._ptb.lag_ms,
            basis_bps=basis_bps,
            basis_status=basis_status,
            feed_reject_reason=reject_reason,
            snapshot_ts=ts,
        )

    def is_ready_for_observe(self, now: datetime | None = None) -> tuple[bool, str | None]:
        """True when Binance and Chainlink feeds are present and fresh (PTB optional for observe)."""
        snap = self.snapshot(now=now)
        if snap.binance_freshness == FRESHNESS_MISSING:
            return False, "binance_missing"
        if snap.binance_freshness == FRESHNESS_STALE:
            return False, "binance_stale"
        if snap.chainlink_freshness == FRESHNESS_MISSING:
            return False, "chainlink_missing"
        if snap.chainlink_freshness == FRESHNESS_STALE:
            return False, "chainlink_stale"
        return True, None

    def freshness_summary(self, now: datetime | None = None) -> dict[str, Any]:
        snap = self.snapshot(now=now)
        return {
            "binance": {
                "connected": self._binance_connected,
                "freshness": snap.binance_freshness,
                "age_ms": snap.binance_age_ms,
                "last_source_ts": snap.binance_source_ts.isoformat() if snap.binance_source_ts else None,
                "last_recv_ts": snap.binance_recv_ts.isoformat() if snap.binance_recv_ts else None,
                "price": str(snap.binance_price) if snap.binance_price is not None else None,
            },
            "chainlink": {
                "connected": self._chainlink_connected,
                "freshness": snap.chainlink_freshness,
                "age_ms": snap.chainlink_age_ms,
                "last_source_ts": snap.chainlink_source_ts.isoformat() if snap.chainlink_source_ts else None,
                "last_recv_ts": snap.chainlink_recv_ts.isoformat() if snap.chainlink_recv_ts else None,
                "price": str(snap.chainlink_price) if snap.chainlink_price is not None else None,
                "fresh": snap.chainlink_fresh,
            },
            "ptb": {
                "status": snap.ptb_status,
                "price_to_beat": str(snap.price_to_beat) if snap.price_to_beat is not None else None,
                "observed_ts": snap.ptb_observed_ts.isoformat() if snap.ptb_observed_ts else None,
                "lag_ms": snap.ptb_lag_ms,
            },
            "basis": {
                "basis_bps": str(snap.basis_bps) if snap.basis_bps is not None else None,
                "basis_status": snap.basis_status,
            },
            "observe_ready": self.is_ready_for_observe(now=now)[0],
            "snapshot_ts": snap.snapshot_ts.isoformat(),
        }
