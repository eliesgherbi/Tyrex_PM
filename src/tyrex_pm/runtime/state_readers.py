"""
Nautilus-backed live state readers — **single canonical read path** for execution cache
and portfolio account state.

**Package-source-confirmed** (installed Nautilus): open orders via ``Cache.orders_open`` /
``Cache.order``; account via ``Portfolio.account(venue)``. Allowance is **not** a
first-class Nautilus field for Polymarket — it is read via py-clob in
:class:`ClobAllowanceStateProvider` (Tyrex-owned boundary).

Do **not** import this module from ``CopyStrategy``; inject readers into risk/runtime only.

**Phase B B1:** Multi-instrument **portfolio** scalars live in :mod:`tyrex_pm.runtime.portfolio_exposure`
(`NautilusPortfolioExposureAggregator` uses this module’s execution reader + token resolution).

**Phase B B3:** Guru-origin resting-order identity for concurrency caps — use
:func:`is_guru_resting_order` only (risk must not embed raw heuristics). **Active stack (Tyrex
1.x + Nautilus orders):** **Tier 1** — ``Order.tags`` with prefix ``guru_cid=`` (set in
``nautilus_guru_exec`` when the adapter preserves tags on cached orders). **Tier 3 fallback**
— ``ClientOrderId`` ``TX`` + 26 lowercase hex chars, matching
``_client_order_id_from_guru_correlation`` (when tags are missing on snapshots). Tier 2 is this
single helper encapsulating 1 then 3.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Venue
from nautilus_trader.portfolio.portfolio import Portfolio
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.clob_factory import build_clob_client_from_env

# **Package-source-confirmed:** adapter exports ``Venue`` instance for Polymarket.
POLYMARKET_VENUE_ID: Venue = POLYMARKET_VENUE


def instrument_id_for_outcome_token(
    cache: Cache,
    token_id: str,
    *,
    static_token_to_instrument: Mapping[str, str] | None = None,
) -> InstrumentId | None:
    """
    Resolve guru outcome ``token_id`` to an :class:`~nautilus_trader.model.identifiers.InstrumentId`
    using the YAML map first, then a **cache-only** scan of Polymarket instruments (no HTTP).

    Returns ``None`` when the instrument is not yet in ``Cache`` (zero-bootstrap gap until
    dynamic resolve or warmup seeds it).
    """
    if static_token_to_instrument:
        mapped = static_token_to_instrument.get(str(token_id))
        if mapped:
            return InstrumentId.from_str(mapped)
    for cached in cache.instruments(venue=POLYMARKET_VENUE_ID):
        try:
            if str(get_polymarket_token_id(cached.id)) == str(token_id):
                return cached.id
        except ValueError:
            continue
    return None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    """
    Immutable view of a Nautilus :class:`~nautilus_trader.model.orders.base.Order`.

    **Pending notional** must use :attr:`leaves_quantity`: Nautilus initializes
    ``leaves_qty == quantity`` and reduces ``leaves_qty`` on each fill
    (*package-source-confirmed* ``model/orders/base.pyx``), while ``quantity`` stays the
    original order size.
    """

    client_order_id: str
    venue_order_id: str | None
    status: str
    side: str
    #: Original order quantity (Nautilus ``order.quantity``).
    quantity: str
    #: Remaining working quantity (Nautilus ``order.leaves_qty``) — use for resting notional.
    leaves_quantity: str
    price: str | None
    instrument_id: str
    #: Nautilus ``Order.tags`` (``list[str]``) when surfaced on cache orders; **B3 tier 1**.
    tags: tuple[str, ...] = ()


# --- Phase B B3 guru-order identity (``Phase_B_planing.md`` §5) ----------------

# Must match :func:`tyrex_pm.execution.nautilus_guru_exec._guru_tag`.
GURU_ORDER_TAG_PREFIX = "guru_cid="
# Tier 3 fallback: ``nautilus_guru_exec._client_order_id_from_guru_correlation`` (TX + hex).
_GURU_COID_PREFIX = "TX"
_GURU_COID_HEX_LEN = 26


def is_guru_resting_order(snap: OrderSnapshot) -> bool:
    """
    Return whether an open-order :class:`OrderSnapshot` is Tyrex **guru framework** submit.

    **Preference:** (1) any tag starting with :data:`GURU_ORDER_TAG_PREFIX`; (2) **fallback**
    ``ClientOrderId`` matching Tyrex ``TX`` + 26-hex pattern (technical debt / adapter may strip
    tags). **Risk** calls this only via :meth:`NautilusExecutionStateReader.count_guru_resting_orders_open`.
    """
    for t in snap.tags:
        if t.startswith(GURU_ORDER_TAG_PREFIX):
            return True
    cid = snap.client_order_id
    if not cid.startswith(_GURU_COID_PREFIX):
        return False
    body = cid[len(_GURU_COID_PREFIX) :]
    if len(body) != _GURU_COID_HEX_LEN:
        return False
    try:
        int(body, 16)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """
    Timestamped account view from ``Portfolio.account(venue)``.

    ``balances`` is best-effort: populated when the account object exposes ``to_dict``;
    otherwise ``raw_summary`` may hold a short diagnostic string.
    """

    venue: str
    captured_at_utc: datetime
    account_present: bool
    balances: dict[str, Any] | None
    raw_summary: str | None


@dataclass(frozen=True, slots=True)
class AllowanceSnapshot:
    """Timestamped py-clob ``get_balance_allowance`` result (COLLATERAL)."""

    captured_at_utc: datetime
    raw: dict[str, Any]


def _order_side_str(side: OrderSide) -> str:
    # OrderSide is IntEnum; str() gives qualified name in some versions — use name.
    if hasattr(side, "name"):
        return str(side.name)
    return str(side)


def _order_status_str(status: OrderStatus) -> str:
    if hasattr(status, "name"):
        return str(status.name)
    return str(status)


def _order_qty_str(qty_obj: Any) -> str:
    qv = qty_obj.as_decimal() if hasattr(qty_obj, "as_decimal") else str(qty_obj)
    if isinstance(qv, Decimal):
        return format(qv, "f")
    return str(qv)


def _order_to_snapshot(order: Any) -> OrderSnapshot:
    qty = order.quantity
    qty_s = _order_qty_str(qty)
    leaves = getattr(order, "leaves_qty", None)
    if leaves is not None:
        leaves_s = _order_qty_str(leaves)
    else:
        leaves_s = qty_s
    pr_raw = getattr(order, "price", None)
    if pr_raw is None:
        pr_s: str | None = None
    else:
        pr_s = (
            pr_raw.as_decimal()
            if hasattr(pr_raw, "as_decimal")
            else str(pr_raw)
        )
        if isinstance(pr_s, Decimal):
            pr_s = format(pr_s, "f")
    vid = getattr(order, "venue_order_id", None)
    venue_order_id = str(vid) if vid is not None else None
    ins = order.instrument_id
    tags_raw = getattr(order, "tags", None)
    if not tags_raw:
        tags_t: tuple[str, ...] = ()
    else:
        tags_t = tuple(str(x) for x in tags_raw)
    return OrderSnapshot(
        client_order_id=str(order.client_order_id),
        venue_order_id=venue_order_id,
        status=_order_status_str(order.status),
        side=_order_side_str(order.side),
        quantity=str(qty_s),
        leaves_quantity=str(leaves_s),
        price=str(pr_s) if pr_s is not None else None,
        instrument_id=str(ins),
        tags=tags_t,
    )


class NautilusExecutionStateReader:
    """
    Canonical Tyrex read path for **open orders** and **order lookup**.

    Reads **only** from the live ``TradingNode``'s ``Cache``
    (**Spike-observed** / **Package-source-confirmed**).
    """

    __slots__ = ("_cache",)

    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    @property
    def cache(self) -> Cache:
        return self._cache

    def list_open_orders(
        self,
        *,
        venue: Venue | None = None,
        instrument_id: InstrumentId | None = None,
    ) -> tuple[OrderSnapshot, ...]:
        """
        **Package-source-confirmed:** ``Cache.orders_open(venue=..., instrument_id=...)``.
        """
        orders = self._cache.orders_open(
            venue=venue,
            instrument_id=instrument_id,
        )
        return tuple(_order_to_snapshot(o) for o in orders)

    def get_order(self, client_order_id: ClientOrderId | str) -> OrderSnapshot | None:
        """
        **Package-source-confirmed:** ``Cache.order(client_order_id)``.
        """
        coid = (
            client_order_id
            if isinstance(client_order_id, ClientOrderId)
            else ClientOrderId(str(client_order_id))
        )
        order = self._cache.order(coid)
        if order is None:
            return None
        return _order_to_snapshot(order)

    def count_guru_resting_orders_open(self, *, venue: Venue | None = None) -> int:
        """
        Count **guru-origin** open orders (Phase B B3) using :func:`is_guru_resting_order`.

        When ``venue`` is ``None``, counts across all venues returned by ``Cache.orders_open``.
        For Polymarket-only caps, pass ``venue=POLYMARKET_VENUE_ID``.
        """
        orders = self._cache.orders_open(venue=venue, instrument_id=None)
        n = 0
        for o in orders:
            if is_guru_resting_order(_order_to_snapshot(o)):
                n += 1
        return n


class NautilusAccountSnapshotProvider:
    """
    Reads **framework account** state via ``Portfolio.account(venue)``.

    Until the Polymarket exec client has emitted account events, ``account_present`` may
    be false — **Docs-confirmed** / **Spike-observed** lifecycle.
    """

    __slots__ = ("_portfolio", "_venue")

    def __init__(self, portfolio: Portfolio, venue: Venue | None = None) -> None:
        self._portfolio = portfolio
        self._venue = venue or POLYMARKET_VENUE_ID

    def snapshot(self) -> AccountSnapshot:
        """Timestamped snapshot; capture time always recorded (Phase A)."""
        ts = _utc_now()
        acc = self._portfolio.account(self._venue)
        if acc is None:
            return AccountSnapshot(
                venue=str(self._venue),
                captured_at_utc=ts,
                account_present=False,
                balances=None,
                raw_summary=None,
            )
        balances: dict[str, Any] | None = None
        raw_summary: str | None = None
        to_dict = getattr(type(acc), "to_dict", None)
        if callable(to_dict):
            try:
                balances = to_dict(acc)  # type: ignore[misc]
            except (TypeError, ValueError):
                raw_summary = repr(acc)
        else:
            raw_summary = repr(acc)
        return AccountSnapshot(
            venue=str(self._venue),
            captured_at_utc=ts,
            account_present=True,
            balances=balances,
            raw_summary=raw_summary,
        )


class ClobAllowanceStateProvider:
    """
    **Tyrex-owned** allowance/balance read for Polymarket **collateral** via py-clob.

    Nautilus does not replace this API in Step 3 — centralize here instead of scattering
    ``get_balance_allowance`` in strategy or ad-hoc execution code (**Repo-confirmed** pattern:
    ``scripts/verify_polymarket_auth.py``).
    """

    __slots__ = ("_client", "_signature_type")

    def __init__(self, client: ClobClient, *, signature_type: int = 0) -> None:
        self._client = client
        self._signature_type = signature_type

    @classmethod
    def from_runtime(cls, runtime: RuntimeSettings) -> ClobAllowanceStateProvider:
        import os

        client = build_clob_client_from_env(runtime)
        sig = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        return cls(client, signature_type=sig)

    def snapshot(self) -> AllowanceSnapshot:
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=self._signature_type,
        )
        result = self._client.get_balance_allowance(params)
        if not isinstance(result, dict):
            raise TypeError(
                f"get_balance_allowance expected dict, got {type(result).__name__}",
            )
        return AllowanceSnapshot(captured_at_utc=_utc_now(), raw=dict(result))


class NautilusPositionStateReader:
    """
    Framework-backed **filled** exposure via ``Portfolio.net_exposure``.

    Polymarket outcomes use ``BinaryOption`` instruments;
    ``net_exposure(instrument_id, price=...)`` aggregates open positions using the venue price
    supplied (Tyrex uses the guru signal / order reference price as mark when gating).

    **Blocked / adapter-dependent:** correctness requires the Polymarket adapter to emit position
    events so ``Portfolio`` stays aligned with venue holdings — Tyrex only reads.
    """

    __slots__ = ("_portfolio", "_cache", "_static")

    def __init__(
        self,
        portfolio: Portfolio,
        cache: Cache,
        static_token_to_instrument: Mapping[str, str],
    ) -> None:
        self._portfolio = portfolio
        self._cache = cache
        self._static = static_token_to_instrument

    def instrument_id_for_token(self, token_id: str) -> InstrumentId | None:
        return instrument_id_for_outcome_token(
            self._cache,
            token_id,
            static_token_to_instrument=self._static,
        )

    def filled_exposure_usd_best_effort(
        self,
        token_id: str,
        mark_price: float | None,
    ) -> float | None:
        """
        Best-effort USD exposure from open **positions** for ``token_id``.

        Returns ``float`` (including ``0.0`` when flat), or ``None`` when the instrument is
        unknown, the mark is missing, or ``net_exposure`` cannot be computed.
        """
        if mark_price is None:
            return None
        iid = self.instrument_id_for_token(token_id)
        if iid is None:
            return None
        inst = self._cache.instrument(iid)
        if inst is None:
            return None
        try:
            px = inst.make_price(Decimal(str(mark_price)))
        except (ValueError, TypeError, ArithmeticError):
            return None
        money = self._portfolio.net_exposure(iid, price=px)
        if money is None:
            return None
        if hasattr(money, "as_double"):
            return float(money.as_double())
        if hasattr(money, "as_decimal"):
            return float(money.as_decimal())  # type: ignore[arg-type]
        return float(money)


@runtime_checkable
class PositionStateReader(Protocol):
    def filled_exposure_usd_best_effort(
        self,
        token_id: str,
        mark_price: float | None,
    ) -> float | None: ...


@runtime_checkable
class ExecutionStateReader(Protocol):
    """Planning-level protocol for injection / typing."""

    def list_open_orders(
        self,
        *,
        venue: Venue | None = None,
        instrument_id: InstrumentId | None = None,
    ) -> tuple[OrderSnapshot, ...]: ...

    def get_order(self, client_order_id: ClientOrderId | str) -> OrderSnapshot | None: ...

    def count_guru_resting_orders_open(self, *, venue: Venue | None = None) -> int: ...


@runtime_checkable
class AccountSnapshotSource(Protocol):
    def snapshot(self) -> AccountSnapshot: ...


@runtime_checkable
class AllowanceSnapshotSource(Protocol):
    def snapshot(self) -> AllowanceSnapshot: ...
