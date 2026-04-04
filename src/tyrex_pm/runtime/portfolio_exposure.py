"""
Phase B **B1** — canonical portfolio exposure aggregation (read layer only).

**Normative contract:** ``Docs/Implementation/Phase_B_planing.md`` §§4.1–4.3, §4.6–4.7.

Computes:

- ``E_pending`` — gross resting notional (Polymarket open orders, ``leaves ×`` limit price)
- ``E_filled_net`` — sum of signed ``Portfolio.net_exposure(instrument_id, price=mark)``
- ``E_portfolio`` — ``E_pending + abs(E_filled_net)`` (locked scalar for future B2 cap)

**No cap / deny logic here** — **B2** ``ConfiguredRiskPolicy`` calls :meth:`aggregate` and
enforces ``max_portfolio_notional_usd_open`` using ``RiskSettings.fail_on_unresolved_portfolio_exposure``.

Mark resolution (§4.6):

1. **Intent instrument** (``intent.token_id`` resolved to ``InstrumentId``): ``intent.price_ref``
   when not ``None``.
2. **Other instruments:** ``Cache.price`` with ``PriceType`` **LAST → MID → MARK**,
   then ``Cache.mark_price`` → ``MarkPriceUpdate.value`` if present
   (*Nautilus 1.x;* pin in tests).

**Default strictness:** When ``fail_on_unresolved`` is **true** (B2 default via settings),
any non-flat Polymarket instrument **without** a resolved mark yields ``complete=False``.
There is **no** silent omission from the filled leg. When ``fail_on_unresolved`` is
**false** (explicit opt-in), non-flat instruments with unresolved marks are **skipped**
in the filled sum—**underestimation**; surfaced via ``omitted_instruments_non_flat``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.core.types import OrderIntent
from tyrex_pm.runtime.state_readers import (
    POLYMARKET_VENUE_ID,
    NautilusExecutionStateReader,
    instrument_id_for_outcome_token,
)


def _nautilus_price_obj_to_float(obj: object | None) -> float | None:
    if obj is None:
        return None
    try:
        if hasattr(obj, "as_double"):
            return float(obj.as_double())  # type: ignore[no-any-return, union-attr]
        if hasattr(obj, "as_decimal"):
            return float(obj.as_decimal())  # type: ignore[no-any-return, union-attr]
        return float(obj)  # type: ignore[arg-type]
    except (TypeError, ValueError, ArithmeticError):
        return None


def cache_best_mark_float(cache: Cache, instrument_id: InstrumentId) -> float | None:
    """
    Best-effort **venue cache** mark for ``instrument_id`` (§4.6 priority **2**).

    Order: ``LAST``, ``MID``, ``MARK`` (:class:`PriceType`), then ``mark_price`` update
    ``value``. Returns ``None`` if nothing is present.
    """
    for pt in (PriceType.LAST, PriceType.MID, PriceType.MARK):
        raw = cache.price(instrument_id, pt)
        x = _nautilus_price_obj_to_float(raw)
        if x is not None:
            return x
    mpu = cache.mark_price(instrument_id, 0)
    if mpu is not None:
        val = getattr(mpu, "value", None)
        x = _nautilus_price_obj_to_float(val)
        if x is not None:
            return x
    return None


@dataclass(frozen=True, slots=True)
class PortfolioExposureAggregate:
    """
    Result of :meth:`NautilusPortfolioExposureAggregator.aggregate`.

    When ``complete`` is ``False``, ``e_portfolio`` is ``None`` — **B2** must fail-closed
    for portfolio cap evaluation. Numeric legs may still be populated for telemetry when
    one leg failed independently; see ``pending_complete`` / ``filled_complete``.
    """

    complete: bool
    #: ``E_pending`` when :attr:`pending_complete` else undefined (0.0 if leg aborted).
    pending_notional_usd: float
    pending_complete: bool
    #: ``E_filled_net`` when :attr:`filled_complete` else undefined (0.0 if leg aborted).
    filled_net_exposure_usd: float
    filled_complete: bool
    #: ``E_portfolio = E_pending + abs(E_filled_net)`` iff :attr:`complete`.
    e_portfolio: float | None
    #: Human-readable reason when not :attr:`complete`.
    error: str | None
    #: Non-flat instruments skipped when ``fail_on_unresolved`` is ``False`` (underestimation).
    omitted_instruments_unresolved_mark: tuple[str, ...]


class NautilusPortfolioExposureAggregator:
    """
    Single canonical **Phase B** portfolio exposure read path for this node.

    **Venue scope:** ``POLYMARKET`` only; **node scope:** injected ``Cache`` /
    ``Portfolio`` only (§4.1).

    Uses :class:`~tyrex_pm.runtime.state_readers.NautilusExecutionStateReader` for open
    orders (same ``Cache.orders_open`` surface as Phase A pending).
    """

    __slots__ = (
        "_portfolio",
        "_cache",
        "_exec",
        "_static",
    )

    def __init__(
        self,
        portfolio: Portfolio,
        cache: Cache,
        execution_reader: NautilusExecutionStateReader,
        static_token_to_instrument: Mapping[str, str],
    ) -> None:
        self._portfolio = portfolio
        self._cache = cache
        self._exec = execution_reader
        self._static = static_token_to_instrument

    def aggregate(
        self,
        intent: OrderIntent | None = None,
        *,
        fail_on_unresolved: bool = True,
    ) -> PortfolioExposureAggregate:
        """
        Compute §4.2–§4.3 scalars.

        :param intent: When set, :attr:`OrderIntent.price_ref` is the mark for the
            instrument resolved from :attr:`OrderIntent.token_id` (§4.6 **1**).
        :param fail_on_unresolved: When ``True`` (B2 default), unresolved marks for
            **non-flat** instruments make ``complete=False``. When ``False``, those
            instruments are **omitted** from ``E_filled_net`` and listed in
            :attr:`PortfolioExposureAggregate.omitted_instruments_unresolved_mark` (**unsafe**).
        """
        e_pending, p_ok, p_err = self._compute_pending()
        e_filled, f_ok, omit, f_err = self._compute_filled_net(
            intent,
            fail_on_unresolved=fail_on_unresolved,
        )

        complete = p_ok and f_ok
        parts: list[str] = []
        if not p_ok and p_err:
            parts.append(p_err)
        if not f_ok and f_err:
            parts.append(f_err)
        err = "; ".join(parts) if parts else None

        e_pf: float | None = None
        if complete:
            e_pf = e_pending + abs(e_filled)

        return PortfolioExposureAggregate(
            complete=complete,
            pending_notional_usd=e_pending,
            pending_complete=p_ok,
            filled_net_exposure_usd=e_filled,
            filled_complete=f_ok,
            e_portfolio=e_pf,
            error=err,
            omitted_instruments_unresolved_mark=omit,
        )

    def _compute_pending(self) -> tuple[float, bool, str | None]:
        """§4.2 — gross sum of ``leaves ×`` limit for Polymarket open orders."""
        total = 0.0
        for snap in self._exec.list_open_orders(venue=POLYMARKET_VENUE_ID):
            try:
                iid = InstrumentId.from_str(snap.instrument_id)
            except ValueError:
                continue
            if iid.venue != POLYMARKET_VENUE_ID:
                continue
            if snap.price is None:
                return (
                    0.0,
                    False,
                    "pending: open order missing limit price",
                )
            try:
                leaves = float(snap.leaves_quantity)
                px = float(snap.price)
            except ValueError:
                return (
                    0.0,
                    False,
                    "pending: invalid leaves_quantity or price on open order",
                )
            if leaves < 0 or px < 0:
                return (
                    0.0,
                    False,
                    "pending: negative leaves or price",
                )
            total += leaves * px
        return total, True, None

    def _compute_filled_net(
        self,
        intent: OrderIntent | None,
        *,
        fail_on_unresolved: bool,
    ) -> tuple[float, bool, tuple[str, ...], str | None]:
        """§4.3 — sum of signed ``net_exposure`` at resolved marks for non-flat instruments."""
        intent_iid: InstrumentId | None = None
        if intent is not None:
            intent_iid = instrument_id_for_outcome_token(
                self._cache,
                intent.token_id,
                static_token_to_instrument=self._static,
            )

        filled_sum = 0.0
        omitted: list[str] = []

        for inst in self._cache.instruments(venue=POLYMARKET_VENUE_ID):
            iid = inst.id
            if self._portfolio.is_flat(iid):
                continue

            mark_f = self._resolve_mark(iid, intent=intent, intent_iid=intent_iid)
            if mark_f is None:
                msg = f"filled: unresolved mark for non-flat instrument {iid}"
                if fail_on_unresolved:
                    return 0.0, False, tuple(omitted), msg
                omitted.append(str(iid))
                continue

            inst_obj = self._cache.instrument(iid)
            if inst_obj is None:
                if fail_on_unresolved:
                    return (
                        0.0,
                        False,
                        tuple(omitted),
                        f"filled: instrument not in cache {iid}",
                    )
                omitted.append(str(iid))
                continue

            try:
                px_obj = inst_obj.make_price(Decimal(str(mark_f)))
            except (ValueError, TypeError, ArithmeticError):
                if fail_on_unresolved:
                    return (
                        0.0,
                        False,
                        tuple(omitted),
                        f"filled: could not build Price for mark on {iid}",
                    )
                omitted.append(str(iid))
                continue

            money = self._portfolio.net_exposure(iid, price=px_obj)
            if money is None:
                if fail_on_unresolved:
                    return (
                        0.0,
                        False,
                        tuple(omitted),
                        f"filled: net_exposure unavailable for {iid}",
                    )
                omitted.append(str(iid))
                continue

            if hasattr(money, "as_double"):
                contrib = float(money.as_double())  # type: ignore[union-attr]
            elif hasattr(money, "as_decimal"):
                contrib = float(money.as_decimal())  # type: ignore[union-attr]
            else:
                contrib = float(money)

            filled_sum += contrib

        return filled_sum, True, tuple(omitted), None

    def _resolve_mark(
        self,
        iid: InstrumentId,
        *,
        intent: OrderIntent | None,
        intent_iid: InstrumentId | None,
    ) -> float | None:
        """§4.6 — intent price_ref for intent instrument, else cache prices."""
        if (
            intent is not None
            and intent_iid is not None
            and iid == intent_iid
            and intent.price_ref is not None
        ):
            return float(intent.price_ref)
        return cache_best_mark_float(self._cache, iid)


# Re-export venue constant for tests that assert Polymarket-only scope.
__all__ = [
    "NautilusPortfolioExposureAggregator",
    "PortfolioExposureAggregate",
    "cache_best_mark_float",
]
