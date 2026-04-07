"""
**Deployment budget** — single accounting basis for per-order, per-token, and portfolio caps.

**Pending deployment:** sum of ``leaves_quantity × limit_price`` for open Polymarket orders
(venue-scoped), same gross resting semantics as historical Phase B pending (BUY and SELL rests
both add positive USD).

**Filled deployment:** per open position, ``abs(signed_qty) × avg_px_open`` (entry-style
notional from Nautilus position state — **no** live mark / ``net_exposure`` / cache quote).

Caps in :class:`~tyrex_pm.risk.configured.ConfiguredRiskPolicy` compare ``order_deploy``,
``token_deploy + order_deploy``, and ``portfolio_deploy + order_deploy`` against YAML limits.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.runtime.state_readers import (
    POLYMARKET_VENUE_ID,
    NautilusExecutionStateReader,
    instrument_id_for_outcome_token,
)


def _decimal_or_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        if hasattr(x, "as_double"):
            return float(x.as_double())
        if hasattr(x, "as_decimal"):
            return float(x.as_decimal())
        if isinstance(x, Decimal):
            return float(x)
        return float(x)
    except (TypeError, ValueError, ArithmeticError):
        return None


def position_entry_deployment_usd(position: Any) -> float | None:
    """
    USD **deployment** for one open position: ``abs(signed_qty) * avg_px_open``.

    Returns ``None`` when qty or average open price cannot be parsed (caller decides strictness).
    """
    qty_f = _decimal_or_float(getattr(position, "signed_qty", None))
    px_f = _decimal_or_float(getattr(position, "avg_px_open", None))
    if qty_f is None or px_f is None:
        return None
    return abs(qty_f) * px_f


class NautilusDeploymentBudget:
    """
    Canonical deployment read path: Polymarket venue, this node's ``Cache`` / ``Portfolio`` /
    open orders (same scope as historical framework-truth gates).
    """

    __slots__ = ("_cache", "_portfolio", "_exec", "_static")

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

    def order_deploy_usd(self, price_ref: float | None, quantity: float) -> float | None:
        """``price_ref * quantity`` when price present; else ``None``."""
        if price_ref is None:
            return None
        return float(price_ref) * float(quantity)

    def pending_polymarket_usd(self) -> tuple[float, bool, str | None]:
        """
        Gross pending deployment — all Polymarket open rests (``leaves ×`` limit).

        Returns ``(total, ok, err)``. ``ok`` is ``False`` when any in-scope order lacks a
        parseable limit price or leaves (fail-closed for cap math).
        """
        total = 0.0
        for snap in self._exec.list_open_orders(venue=POLYMARKET_VENUE_ID):
            try:
                iid = InstrumentId.from_str(snap.instrument_id)
            except ValueError:
                continue
            if iid.venue != POLYMARKET_VENUE_ID:
                continue
            if snap.price is None:
                return 0.0, False, "deployment: open order missing limit price"
            try:
                leaves = float(snap.leaves_quantity)
                px = float(snap.price)
            except ValueError:
                return 0.0, False, "deployment: invalid leaves_quantity or price on open order"
            if leaves < 0 or px < 0:
                return 0.0, False, "deployment: negative leaves or limit price"
            total += leaves * px
        return total, True, None

    def pending_usd_for_token(self, token_id: str) -> tuple[float, bool, str | None]:
        """Pending deployment for one outcome ``token_id`` (CLOB id string)."""
        total = 0.0
        for snap in self._exec.list_open_orders(venue=POLYMARKET_VENUE_ID):
            try:
                iid = InstrumentId.from_str(snap.instrument_id)
            except ValueError:
                continue
            if iid.venue != POLYMARKET_VENUE_ID:
                continue
            try:
                if get_polymarket_token_id(iid) != str(token_id):
                    continue
            except ValueError:
                continue
            if snap.price is None:
                return 0.0, False, "deployment: open order missing limit price"
            try:
                leaves = float(snap.leaves_quantity)
                px = float(snap.price)
            except ValueError:
                return 0.0, False, "deployment: invalid leaves_quantity or price on open order"
            if leaves < 0 or px < 0:
                return 0.0, False, "deployment: negative leaves or limit price"
            total += leaves * px
        return total, True, None

    def filled_polymarket_usd(self) -> tuple[float, bool]:
        """
        Sum filled deployment across **open** Polymarket positions.

        Returns ``(sum, complete)``. ``complete`` is ``False`` if any non-flat position
        yields ``None`` from :func:`position_entry_deployment_usd`.
        """
        total = 0.0
        try:
            positions = self._cache.positions_open(venue=POLYMARKET_VENUE_ID)
        except (TypeError, AttributeError):
            positions = ()
        for pos in positions or ():
            iid = getattr(pos, "instrument_id", None)
            if iid is None:
                continue
            if self._portfolio.is_flat(iid):
                continue
            leg = position_entry_deployment_usd(pos)
            if leg is None:
                return 0.0, False
            total += leg
        return total, True

    def filled_usd_for_token(self, token_id: str) -> tuple[float, bool]:
        """Filled deployment for ``token_id`` (open positions only)."""
        iid_target = instrument_id_for_outcome_token(
            self._cache,
            token_id,
            static_token_to_instrument=self._static,
        )
        if iid_target is None:
            return 0.0, True
        total = 0.0
        try:
            positions = self._cache.positions_open(venue=POLYMARKET_VENUE_ID)
        except (TypeError, AttributeError):
            positions = ()
        for pos in positions or ():
            iid = getattr(pos, "instrument_id", None)
            if iid != iid_target:
                continue
            if self._portfolio.is_flat(iid):
                continue
            leg = position_entry_deployment_usd(pos)
            if leg is None:
                return 0.0, False
            total += leg
        return total, True

    def portfolio_deployment_usd(self) -> tuple[float, bool, str | None]:
        """
        ``pending_all + filled_all`` with consistent semantics.

        Returns ``(total, ok, err)`` where ``ok`` embeds pending parse failures or filled
        incompleteness (filled incomplete ⇒ ``ok=False``, ``err`` descriptive).
        """
        pend, p_ok, p_err = self.pending_polymarket_usd()
        if not p_ok:
            return 0.0, False, p_err
        filled, f_ok = self.filled_polymarket_usd()
        if not f_ok:
            return 0.0, False, "deployment: could not resolve filled deployment for a position"
        return pend + filled, True, None

    def token_deployment_usd(self, token_id: str) -> tuple[float, bool, str | None]:
        """``pending_on_token + filled_on_token``."""
        p_tok, p_ok, p_err = self.pending_usd_for_token(token_id)
        if not p_ok:
            return 0.0, False, p_err
        f_tok, f_ok = self.filled_usd_for_token(token_id)
        if not f_ok:
            return 0.0, False, "deployment: could not resolve filled deployment for token"
        return p_tok + f_tok, True, None

    def token_deployment_usd_with_policy(
        self,
        token_id: str,
        *,
        strict_filled: bool,
    ) -> tuple[float, bool, str | None]:
        """
        Like :meth:`token_deployment_usd`, but when ``strict_filled`` is false and the filled leg
        cannot be resolved, return **pending only** (filled treated as zero — underestimation).
        """
        p_tok, p_ok, p_err = self.pending_usd_for_token(token_id)
        if not p_ok:
            return 0.0, False, p_err
        f_tok, f_ok = self.filled_usd_for_token(token_id)
        if not f_ok:
            if strict_filled:
                return 0.0, False, "deployment: could not resolve filled deployment for token"
            return p_tok, True, None
        return p_tok + f_tok, True, None

    def portfolio_deployment_usd_with_policy(
        self,
        *,
        strict_filled: bool,
    ) -> tuple[float, bool, str | None]:
        """
        Portfolio total with optional lenient filled leg (``strict_filled`` false ⇒ pending only
        when filled aggregate fails).
        """
        pend, p_ok, p_err = self.pending_polymarket_usd()
        if not p_ok:
            return 0.0, False, p_err
        filled, f_ok = self.filled_polymarket_usd()
        if not f_ok:
            if strict_filled:
                return 0.0, False, "deployment: could not resolve filled deployment for a position"
            return pend, True, None
        return pend + filled, True, None


__all__ = [
    "NautilusDeploymentBudget",
    "position_entry_deployment_usd",
]
