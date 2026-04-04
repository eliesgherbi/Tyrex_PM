"""
Fail-closed risk gate from :class:`~tyrex_pm.config.loaders.RiskSettings`.

**Phase B exposure contract (normative):** ``Docs/Implementation/Phase_B_planing.md`` §§4–7.
Portfolio-wide exposure is **Polymarket-only**, **this node only** — computed only via
:class:`~tyrex_pm.runtime.portfolio_exposure.NautilusPortfolioExposureAggregator` (**B1**),
not duplicated here. **B2** enforces ``max_portfolio_notional_usd_open`` on the framework
path; **B0** rejects unsupported configs at compose time.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.runtime.portfolio_exposure import NautilusPortfolioExposureAggregator
from tyrex_pm.runtime.state_readers import (
    POLYMARKET_VENUE_ID,
    AccountSnapshotSource,
    AllowanceSnapshotSource,
    ExecutionStateReader,
    PositionStateReader,
)

_LOG = logging.getLogger(__name__)

# Grep-friendly operational prefix (Tyrex-owned; distinct from Nautilus component names).
_TYREX_RISK_OPS_EVENT = "tyrex_risk_ops"


def _ops_snippet(text: str | None, max_len: int = 220) -> str:
    """Single-line, length-bounded fragment for operator logs (no secrets)."""
    if not text:
        return ""
    s = " ".join(text.split())
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_clob_balance_allowance(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    def to_f(k: str) -> float | None:
        v = raw.get(k)
        if v is None:
            return None
        try:
            return float(str(v).strip())
        except ValueError:
            return None

    return to_f("balance"), to_f("allowance")


class ConfiguredRiskPolicy:
    """
    Session-local exposure accounting (approximates open notional per token on py-clob path).

    **Framework guru submit:** open-order pending exposure uses **leaves quantity** from Nautilus
    ``Cache`` (see :class:`~tyrex_pm.runtime.state_readers.OrderSnapshot`). Filled exposure
    for per-token caps uses :class:`~tyrex_pm.runtime.state_readers.NautilusPositionStateReader`
    when injected. Capital gating uses timestamped account + py-clob allowance snapshots with
    configurable staleness (**Phase A closure**).

    **Phase B (B2):** When ``max_portfolio_notional_usd_open`` is finite, ``evaluate`` calls
    the injected :class:`~tyrex_pm.runtime.portfolio_exposure.NautilusPortfolioExposureAggregator`
    (same B1 snapshot: current intent **not** yet in ``Cache.orders_open``). **Incomplete** B1
    aggregates (``complete=False`` or ``e_portfolio is None``) always deny with
    ``RISK_PORTFOLIO_EXPOSURE_UNRESOLVED``. ``fail_on_unresolved_portfolio_exposure=False`` only
    allows **underestimation** when the aggregate is still **complete** with
    ``omitted_instruments_unresolved_mark`` (B1 partial-marks path); it does **not** skip the gate
    for broken/incomplete snapshots.

    **B3** uses :meth:`~tyrex_pm.runtime.state_readers.NautilusExecutionStateReader.count_guru_resting_orders_open`
    (guru identity via :func:`~tyrex_pm.runtime.state_readers.is_guru_resting_order` only).
    **B4** ``collateral_reserve_usd``: py-clob **balance** (``get_balance_allowance`` shape) from the same
    allowance snapshot / TTL as Phase A mins — **BUY** requires ``balance >= reserve + n`` after mins
    pass (`Phase_B_planing.md` §6). Missing snapshot / unparsable ``balance`` is fail-closed
    (``RISK_ALLOWANCE_UNAVAILABLE``). **B5** remains docs/operator matrix only.
    """

    def __init__(
        self,
        settings: RiskSettings,
        *,
        execution_reader: ExecutionStateReader | None = None,
        account_snapshot: AccountSnapshotSource | None = None,
        allowance_provider: AllowanceSnapshotSource | None = None,
        position_reader: PositionStateReader | None = None,
        portfolio_exposure: NautilusPortfolioExposureAggregator | None = None,
        token_open_authoritative_for_pending: bool = True,
    ) -> None:
        self._s = settings
        self._token_open: dict[str, float] = defaultdict(float)
        self._execution_reader = execution_reader
        self._account_snapshot = account_snapshot
        self._allowance_provider = allowance_provider
        self._position_reader = position_reader
        self._portfolio_exposure = portfolio_exposure
        self._token_open_authoritative_for_pending = token_open_authoritative_for_pending
        self._account_cache = None
        self._allowance_cache = None

    @property
    def execution_reader(self):
        """Nautilus cache-backed reader (same node's ``Cache``); never owned by strategy."""
        return self._execution_reader

    @property
    def account_snapshot_provider(self):
        """Portfolio-backed account snapshots (may be empty until exec connects)."""
        return self._account_snapshot

    @property
    def allowance_provider(self):
        """py-clob collateral snapshot owner; ``None`` in shadow mode."""
        return self._allowance_provider

    @property
    def token_open_authoritative_for_pending(self) -> bool:
        """False when guru uses Nautilus framework submit — pending exposure uses cache readers."""
        return self._token_open_authoritative_for_pending

    @property
    def portfolio_exposure(self):
        """B1 aggregator; ``None`` off framework path (B0 forbids finite portfolio cap there)."""
        return self._portfolio_exposure

    def framework_open_order_count(self) -> int:
        """Count of orders Nautilus marks open — for tests / ops; Phase B precursor."""
        if self._execution_reader is None:
            return 0
        return len(self._execution_reader.list_open_orders())

    def note_fill_assumption(self, intent: OrderIntent) -> None:
        """
        **Legacy py-clob path only:** bump session heuristic after HTTP submit success.

        **Step 4:** No-op when ``token_open_authoritative_for_pending`` is False (Nautilus submit
        path — use cache-backed pending instead).
        """
        if not self._token_open_authoritative_for_pending:
            return
        n = _estimate_notional(intent)
        if n is not None and n > 0:
            self._token_open[intent.token_id] += n

    def evaluate(self, intent: OrderIntent) -> tuple[bool, str]:
        if self._s.kill_switch:
            return False, ReasonCode.RISK_KILL_SWITCH

        if intent.quantity > self._s.max_order_quantity:
            return False, ReasonCode.RISK_ORDER_QTY_LIMIT

        n = _estimate_notional(intent)
        if n is None:
            if self._s.fail_on_missing_price_for_notional:
                return False, ReasonCode.RISK_MISSING_PRICE
        else:
            if n > self._s.max_notional_usd_per_order:
                return False, ReasonCode.RISK_NOTIONAL_PER_ORDER

        ok_cap, rc_cap = self._capital_gate_eval(_utc_now(), intent, n)
        if not ok_cap:
            return False, rc_cap

        if not math.isinf(self._s.max_token_notional_usd_open):
            if n is None:
                if self._s.fail_on_missing_price_for_notional:
                    return False, ReasonCode.RISK_MISSING_PRICE
            else:
                if self._token_open_authoritative_for_pending:
                    next_open = self._token_open[intent.token_id] + n
                else:
                    pending = self._pending_open_notional_from_cache(intent.token_id)
                    filled, ok_filled = self._filled_notional_for_token(intent)
                    if not ok_filled:
                        return False, ReasonCode.RISK_POSITION_EXPOSURE_UNRESOLVED
                    next_open = filled + pending + n
                if next_open > self._s.max_token_notional_usd_open:
                    return False, ReasonCode.RISK_TOKEN_NOTIONAL_OPEN

        if not math.isinf(self._s.max_portfolio_notional_usd_open):
            ok_pf, rc_pf = self._portfolio_wide_cap_eval(intent, n)
            if not ok_pf:
                return False, rc_pf

        if self._s.max_concurrent_guru_resting_orders is not None:
            ok_cg, rc_cg = self._guru_concurrent_resting_cap_eval(intent)
            if not ok_cg:
                return False, rc_cg

        return True, "approved"

    def _portfolio_wide_cap_eval(
        self,
        intent: OrderIntent,
        intent_notional: float | None,
    ) -> tuple[bool, str]:
        """
        Phase B B2 — §4.5: deny if ``E_portfolio + n > C`` using B1 aggregate only.

        ``intent_notional`` is the same ``n`` computed once in :meth:`evaluate`
        (``price_ref * quantity``). **Incomplete** aggregates always deny here; ``unsafe`` only
        relaxes **partial-marks** underestimation when ``complete`` and ``e_portfolio`` are valid.
        """
        pe = self._portfolio_exposure
        if pe is None:
            _LOG.info(
                "event=%s gate=portfolio reason=%s correlation_id=%s detail=no_b1_aggregator",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED,
                intent.correlation_id,
            )
            return False, ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED

        agg = pe.aggregate(
            intent,
            fail_on_unresolved=self._s.fail_on_unresolved_portfolio_exposure,
        )

        # Incomplete / no scalar: never bypass the portfolio gate (unsafe applies only to
        # complete aggregates with known partial-marks underestimation — see omissions warning below).
        if not agg.complete or agg.e_portfolio is None:
            _LOG.info(
                "event=%s gate=portfolio_unresolved reason=%s correlation_id=%s "
                "b1_pending_complete=%s b1_filled_complete=%s b1_complete=%s e_portfolio_present=%s "
                "b1_error=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED,
                intent.correlation_id,
                agg.pending_complete,
                agg.filled_complete,
                agg.complete,
                agg.e_portfolio is not None,
                _ops_snippet(agg.error) or "(none)",
            )
            return False, ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED

        ep = agg.e_portfolio

        if not self._s.fail_on_unresolved_portfolio_exposure and agg.omitted_instruments_unresolved_mark:
            _LOG.warning(
                "portfolio cap: filled leg used partial marks; omitted instruments may underestimate "
                "exposure: %s",
                agg.omitted_instruments_unresolved_mark,
            )

        if intent_notional is None:
            return False, ReasonCode.RISK_MISSING_PRICE

        cap = self._s.max_portfolio_notional_usd_open
        if ep + intent_notional > cap:
            _LOG.info(
                "event=%s gate=portfolio_cap reason=%s correlation_id=%s "
                "e_portfolio=%.6g intent_notional=%.6g cap=%.6g sum=%.6g",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED,
                intent.correlation_id,
                float(ep),
                float(intent_notional),
                float(cap),
                float(ep + intent_notional),
            )
            return False, ReasonCode.RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED

        return True, ""

    def _guru_concurrent_resting_cap_eval(self, intent: OrderIntent) -> tuple[bool, str]:
        """Phase B B3 — deny when open guru resting orders are already at ``limit`` (§5)."""
        lim = self._s.max_concurrent_guru_resting_orders
        if lim is None:
            return True, ""
        cid = intent.correlation_id
        er = self._execution_reader
        if er is None:
            _LOG.info(
                "event=%s gate=guru_concurrent reason=%s correlation_id=%s "
                "guru_resting_count=(no_reader) limit=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT,
                cid,
                lim,
            )
            return False, ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT
        try:
            cnt = er.count_guru_resting_orders_open(venue=POLYMARKET_VENUE_ID)
        except (TypeError, AttributeError):
            _LOG.info(
                "event=%s gate=guru_concurrent reason=%s correlation_id=%s "
                "guru_resting_count=(count_failed) limit=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT,
                cid,
                lim,
            )
            return False, ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT
        if cnt >= lim:
            _LOG.info(
                "event=%s gate=guru_concurrent reason=%s correlation_id=%s guru_resting_count=%s limit=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT,
                cid,
                cnt,
                lim,
            )
            return False, ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT
        return True, ""

    def _filled_notional_for_token(self, intent: OrderIntent) -> tuple[float, bool]:
        """
        Return ``(filled_usd, ok)``.

        ``ok`` is False when resolution is required but exposure cannot be computed.
        """
        if self._position_reader is None:
            if self._s.fail_on_unresolved_position_for_token_cap:
                return 0.0, False
            return 0.0, True
        exp = self._position_reader.filled_exposure_usd_best_effort(
            intent.token_id,
            float(intent.price_ref) if intent.price_ref is not None else None,
        )
        if exp is None:
            if self._s.fail_on_unresolved_position_for_token_cap:
                return 0.0, False
            return 0.0, True
        return float(exp), True

    def _capital_gate_eval(
        self,
        now: datetime,
        intent: OrderIntent,
        intent_notional: float | None,
    ) -> tuple[bool, str]:
        if not self._s.capital_gate_enabled:
            if self._s.collateral_reserve_usd > 0:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            return True, ""

        if self._account_snapshot is None:
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE

        need_acct = (
            self._account_cache is None
            or (now - self._account_cache.captured_at_utc).total_seconds()
            >= self._s.max_account_snapshot_age_seconds
        )
        if need_acct:
            self._account_cache = self._account_snapshot.snapshot()
        if self._account_cache is None or not self._account_cache.account_present:
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE

        need_py_clob_snapshot = (
            self._s.min_collateral_balance_usd is not None
            or self._s.min_allowance_usd is not None
            or self._s.collateral_reserve_usd > 0
        )
        bal: float | None = None
        allow: float | None = None

        if need_py_clob_snapshot:
            if self._allowance_provider is None:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            need_alw = (
                self._allowance_cache is None
                or (now - self._allowance_cache.captured_at_utc).total_seconds()
                >= self._s.max_allowance_snapshot_age_seconds
            )
            if need_alw:
                self._allowance_cache = self._allowance_provider.snapshot()
            if self._allowance_cache is None:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            bal, allow = _parse_clob_balance_allowance(self._allowance_cache.raw)

            if self._s.min_collateral_balance_usd is not None:
                if bal is None:
                    return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
                if bal < self._s.min_collateral_balance_usd:
                    _LOG.info(
                        "event=%s gate=min_collateral reason=%s py_clob_balance=%.6g min_required=%.6g",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_INSUFFICIENT_COLLATERAL_BALANCE,
                        float(bal),
                        float(self._s.min_collateral_balance_usd),
                    )
                    return False, ReasonCode.RISK_INSUFFICIENT_COLLATERAL_BALANCE
            if self._s.min_allowance_usd is not None:
                if allow is None:
                    return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
                if allow < self._s.min_allowance_usd:
                    _LOG.info(
                        "event=%s gate=min_allowance reason=%s py_clob_allowance=%.6g min_required=%.6g",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_INSUFFICIENT_ALLOWANCE,
                        float(allow),
                        float(self._s.min_allowance_usd),
                    )
                    return False, ReasonCode.RISK_INSUFFICIENT_ALLOWANCE

        if self._s.collateral_reserve_usd > 0:
            if bal is None:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            if intent.side.upper() == "BUY":
                if intent_notional is None:
                    return False, ReasonCode.RISK_MISSING_PRICE
                reserve = float(self._s.collateral_reserve_usd)
                need = reserve + float(intent_notional)
                if bal < need:
                    _LOG.info(
                        "event=%s gate=reserve reason=%s correlation_id=%s "
                        "py_clob_balance=%.6g reserve_usd=%.6g intent_notional=%.6g required_free=%.6g",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE,
                        intent.correlation_id,
                        float(bal),
                        reserve,
                        float(intent_notional),
                        need,
                    )
                    return False, ReasonCode.RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE

        return True, ""

    def _pending_open_notional_from_cache(self, token_id: str) -> float:
        """Resting notional from framework open orders — **leaves qty × price** per order."""
        reader = self._execution_reader
        if reader is None:
            return 0.0
        total = 0.0
        for snap in reader.list_open_orders():
            try:
                iid = InstrumentId.from_str(snap.instrument_id)
            except ValueError:
                continue
            try:
                if get_polymarket_token_id(iid) != str(token_id):
                    continue
            except ValueError:
                continue
            if snap.price is None:
                continue
            try:
                leaves = float(snap.leaves_quantity)
                px = float(snap.price)
            except ValueError:
                continue
            total += leaves * px
        return total


def _estimate_notional(intent: OrderIntent) -> float | None:
    if intent.price_ref is None:
        return None
    return float(intent.price_ref) * float(intent.quantity)
