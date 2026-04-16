"""
Venue truth for Polymarket — Data API positions + CLOB orders + CLOB collateral.

Read-only snapshot store fed by :class:`~tyrex_pm.runtime.wallet_sync.WalletSyncActor`
(no duplicate bulk HTTP). CLOB balance polls on ``cash_poll_interval_seconds``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.identifiers import InstrumentId, Venue

from tyrex_pm.runtime.clob_collateral_money import parse_clob_collateral_usd
from tyrex_pm.runtime.state_readers import OrderSnapshot, POLYMARKET_VENUE_ID

_LOG = logging.getLogger(__name__)
_POLY_VENUE = Venue(POLYMARKET)  # same as wallet_sync._POLYMARKET_VENUE

MISSING_MARK_FALLBACK_PRICE = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class VenueStateConfig:
    """Runtime config for :class:`VenueState`."""

    ttl_seconds: float = 30.0
    cash_poll_interval_seconds: float = 10.0
    refresh_force_max_blocking_ms: int = 500


class VenueState:
    """
    Thread-safe snapshot of venue positions, resting orders, and collateral.

    HTTP ingestion runs on the wallet sync executor thread; reads are lock-protected.
    """

    __slots__ = (
        "_lock",
        "_config",
        "_fact_emit",
        "_positions",
        "_orders_snapshots",
        "_last_positions_utc",
        "_cash_total",
        "_cash_free",
        "_last_cash_utc",
        "_cash_ready",
        "_last_cash_poll_mono",
        "_last_error",
        "_cache_ref",
    )

    def __init__(
        self,
        *,
        config: VenueStateConfig,
        cache: Cache,
        fact_emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._config = config
        self._fact_emit = fact_emit
        self._positions: dict[InstrumentId, Decimal] = {}
        self._orders_snapshots: tuple[OrderSnapshot, ...] = ()
        self._last_positions_utc: datetime | None = None
        self._cash_total: Decimal | None = None
        self._cash_free: Decimal | None = None
        self._last_cash_utc: datetime | None = None
        self._cash_ready: bool = False
        self._last_cash_poll_mono: float = 0.0
        self._last_error: str | None = None
        self._cache_ref = cache

    @property
    def venue_state_cash_ready(self) -> bool:
        return self._cash_ready

    def positions(self) -> dict[InstrumentId, Decimal]:
        with self._lock:
            return dict(self._positions)

    def position_size(self, instrument_id: InstrumentId) -> Decimal | None:
        with self._lock:
            return self._positions.get(instrument_id)

    def cash_total(self) -> Decimal | None:
        with self._lock:
            return self._cash_total

    def cash_free(self) -> Decimal | None:
        with self._lock:
            return self._cash_free

    def orders_resting(self) -> tuple[OrderSnapshot, ...]:
        with self._lock:
            return self._orders_snapshots

    def orders_resting_for_instrument(
        self,
        instrument_id: InstrumentId,
    ) -> tuple[OrderSnapshot, ...]:
        iid_s = str(instrument_id)
        with self._lock:
            return tuple(s for s in self._orders_snapshots if s.instrument_id == iid_s)

    def last_success_utc(self) -> datetime | None:
        with self._lock:
            return self._last_positions_utc

    def is_stale(self, now: datetime | None = None) -> bool:
        ref = now or datetime.now(tz=UTC)
        with self._lock:
            if self._last_positions_utc is None:
                return True
            age = (ref - self._last_positions_utc).total_seconds()
            return age > self._config.ttl_seconds

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def apply_positions_and_orders_rows(
        self,
        *,
        position_rows: list[dict[str, Any]],
        orders_raw: list[dict[str, Any]] | None,
        ts_utc: datetime,
    ) -> None:
        """Update positions + resting orders from WalletSync HTTP responses."""
        cache = self._cache_ref
        pos_map: dict[InstrumentId, Decimal] = {}
        for row in position_rows:
            token_id = str(row.get("asset") or row.get("token_id") or "").strip()
            size_raw = str(row.get("size") or "0").strip()
            if not token_id:
                continue
            try:
                size = Decimal(size_raw)
            except (InvalidOperation, ValueError):
                size = Decimal(0)
            if size <= 0:
                size = Decimal(0)
            for cached in cache.instruments(venue=_POLY_VENUE):
                try:
                    if str(get_polymarket_token_id(cached.id)) == token_id:
                        pos_map[cached.id] = pos_map.get(cached.id, Decimal(0)) + size
                        break
                except ValueError:
                    continue

        snaps: list[OrderSnapshot] = []
        for od in orders_raw or []:
            s = _clob_order_to_order_snapshot(od, cache)
            if s is not None:
                snaps.append(s)

        with self._lock:
            self._positions = pos_map
            self._orders_snapshots = tuple(snaps)
            self._last_positions_utc = ts_utc
            self._last_error = None

        self._emit_venue_state(
            status="ok",
            position_count=len(pos_map),
            resting_order_count=len(snaps),
        )

    def apply_clob_balance(self, raw: dict[str, Any], ts_utc: datetime) -> None:
        """Update cash from py-clob ``get_balance_allowance`` dict."""
        p = parse_clob_collateral_usd(raw)
        bal = p.balance_usd
        with self._lock:
            if bal is not None:
                d = Decimal(str(bal))
                self._cash_total = d
                self._cash_free = d
                self._last_cash_utc = ts_utc
                self._cash_ready = True
            else:
                self._last_error = "clob_balance_unparseable"
            self._last_cash_poll_mono = time.monotonic()

        self._emit_venue_state(
            status="ok" if bal is not None else "error",
            phase="cash" if bal is None else None,
            cash_ready=self._cash_ready,
            detail=p.balance_parse_note if bal is None else None,
        )

    def maybe_poll_clob_balance(self, clob_client: Any) -> None:
        """If cash poll interval elapsed, fetch balance on current thread (executor)."""
        interval = float(self._config.cash_poll_interval_seconds)
        now_m = time.monotonic()
        with self._lock:
            if (now_m - self._last_cash_poll_mono) < interval and self._last_cash_utc is not None:
                return
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=int(__import__("os").environ.get("POLYMARKET_SIGNATURE_TYPE", "0")),
            )
            raw = clob_client.get_balance_allowance(params)
            if isinstance(raw, dict):
                self.apply_clob_balance(dict(raw), datetime.now(tz=UTC))
        except Exception:
            _LOG.warning("event=venue_state_cash_poll_fail", exc_info=True)
            with self._lock:
                self._last_error = "cash_poll_exception"
            self._emit_venue_state(status="error", phase="cash", detail="poll_exception")

    def _emit_venue_state(
        self,
        *,
        status: str,
        phase: str | None = None,
        position_count: int | None = None,
        resting_order_count: int | None = None,
        cash_ready: bool | None = None,
        detail: str | None = None,
    ) -> None:
        if self._fact_emit is None:
            return
        now = datetime.now(tz=UTC)
        with self._lock:
            lp = self._last_positions_utc
            lc = self._last_cash_utc
            pc = position_count if position_count is not None else len(self._positions)
            roc = resting_order_count if resting_order_count is not None else len(self._orders_snapshots)
            cr = cash_ready if cash_ready is not None else self._cash_ready
        payload: dict[str, Any] = {
            "status": status,
            "position_count": pc,
            "resting_order_count": roc,
            "cash_ready": cr,
            "ttl_seconds": self._config.ttl_seconds,
            "cash_poll_interval_seconds": self._config.cash_poll_interval_seconds,
            "last_positions_success_utc": lp.isoformat() if lp else None,
            "last_cash_success_utc": lc.isoformat() if lc else None,
        }
        if phase is not None:
            payload["phase"] = phase
        if detail is not None:
            payload["detail"] = detail
        self._fact_emit("venue_state", payload)

    def emit_missing_mark_fact(
        self,
        *,
        instrument_id: InstrumentId,
        token_id: str | None,
    ) -> None:
        if self._fact_emit is None:
            return
        pl: dict[str, Any] = {
            "instrument_id": str(instrument_id),
            "fallback_price": float(MISSING_MARK_FALLBACK_PRICE),
        }
        if token_id:
            pl["token_id"] = str(token_id)
        self._fact_emit("venue_state_missing_mark", pl)


def mark_price_for_instrument_usd(cache: Cache, instrument_id: InstrumentId) -> float | None:
    """Best-effort LAST price from cache; ``None`` if unavailable."""
    try:
        px = cache.price(instrument_id, price_type=PriceType.LAST)
    except Exception:
        return None
    if px is None:
        return None
    try:
        if hasattr(px, "as_double"):
            return float(px.as_double())
        if hasattr(px, "as_decimal"):
            return float(px.as_decimal())
        return float(px)
    except (TypeError, ValueError, ArithmeticError):
        return None


def filled_deployment_usd_venue(
    *,
    venue_state: VenueState,
    cache: Cache,
    token_id_filter: str | None = None,
) -> tuple[float, bool]:
    """
    Sum ``abs(size) × mark`` for venue positions; missing mark → 0.5 + ``venue_state_missing_mark``.

    Returns ``(total_usd, complete)`` — ``complete`` is False only if a positive size
    cannot be paired with an instrument in cache (should not happen after sync).
    """
    total = 0.0
    positions = venue_state.positions()
    for iid, sz in positions.items():
        if token_id_filter is not None:
            try:
                if str(get_polymarket_token_id(iid)) != str(token_id_filter):
                    continue
            except ValueError:
                continue
        abs_sz = abs(float(sz))
        if abs_sz <= 0:
            continue
        inst = cache.instrument(iid)
        if inst is None:
            return 0.0, False
        mark = mark_price_for_instrument_usd(cache, iid)
        used_fallback = False
        if mark is None:
            mark = float(MISSING_MARK_FALLBACK_PRICE)
            used_fallback = True
            tid: str | None = None
            try:
                tid = str(get_polymarket_token_id(iid))
            except ValueError:
                tid = None
            venue_state.emit_missing_mark_fact(instrument_id=iid, token_id=tid)
        total += abs_sz * float(mark)
    return total, True


def _clob_order_to_order_snapshot(
    od: dict[str, Any],
    cache: Cache,
) -> OrderSnapshot | None:
    """Best-effort map CLOB open-order dict → :class:`OrderSnapshot`."""
    asset = str(od.get("asset_id") or od.get("token_id") or "").strip()
    if not asset:
        return None
    instrument_id: InstrumentId | None = None
    for cached in cache.instruments(venue=POLYMARKET_VENUE_ID):
        try:
            if str(get_polymarket_token_id(cached.id)) == asset:
                instrument_id = cached.id
                break
        except ValueError:
            continue
    if instrument_id is None:
        return None
    side_raw = str(od.get("side", "BUY")).upper()
    side_s = "BUY" if "BUY" in side_raw else "SELL"
    price_s = od.get("price")
    if price_s is None:
        return None
    price_str = str(price_s).strip()
    orig = od.get("original_size") or od.get("size")
    matched = od.get("size_matched") or "0"
    try:
        o_f = float(str(orig))
        m_f = float(str(matched))
        leaves = max(0.0, o_f - m_f)
    except (TypeError, ValueError):
        try:
            leaves = float(str(od.get("size", "0")))
        except (TypeError, ValueError):
            leaves = 0.0
    coid = str(od.get("id") or od.get("orderID") or "clob-unknown")
    void = str(od.get("id") or coid)
    return OrderSnapshot(
        client_order_id=coid,
        venue_order_id=void,
        status="OPEN",
        side=side_s,
        quantity=str(orig) if orig is not None else str(leaves),
        leaves_quantity=str(leaves),
        price=price_str,
        instrument_id=str(instrument_id),
        tags=(),
    )


__all__ = [
    "MISSING_MARK_FALLBACK_PRICE",
    "VenueState",
    "VenueStateConfig",
    "filled_deployment_usd_venue",
    "mark_price_for_instrument_usd",
]
