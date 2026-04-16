"""
Bot-originated sell harness (Scenario A): after a full guru-entry BUY fill, schedules a SELL
through the same risk + :class:`~tyrex_pm.execution.nautilus_guru_exec.NautilusGuruExecutionPort`
path as normal copy traffic.

Production guru-follow uses :class:`~tyrex_pm.strategy.copy_strategy.CopyStrategy` only; this
strategy is selected when strategy YAML includes a ``bot_sell_validate`` block.

**Validation aggressive limits:** optional marketable limit prices (top-of-book + tick bump +
    slippage cap vs guru/entry reference) to reduce time-at-rest and pending-deployment ambiguity —
    see :mod:`tyrex_pm.strategy.validation_limit_pricing`.

**Terminal-state dust (validation only):** Polymarket + Nautilus can leave ``is_closed == false``
while fills/UI imply no meaningful remainder. Instruments often report ``size_increment`` as ``1e-6``
while share prints use a coarser step; the harness uses ``max(reported, 0.01)`` **only for this dust
decision** (not for order quantization — see :mod:`tyrex_pm.execution.c3_normalize`). See
:func:`_validation_dust_size_step` and :func:`_validation_order_effectively_complete`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from nautilus_trader.common.component import TimeEvent
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events.order import OrderEvent, OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal, OrderIntent
from tyrex_pm.execution.c3_book_top import BookTop, resolve_book_top
from tyrex_pm.execution.c3_normalize import floor_quantity_to_step
from tyrex_pm.runtime.state_readers import instrument_id_for_outcome_token
from tyrex_pm.strategy.copy_strategy import CopyStrategy, CopyStrategyConfig
from tyrex_pm.strategy.validation_constants import DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS
from tyrex_pm.strategy.validation_limit_pricing import (
    ValidationAggressiveQuote,
    aggressive_validation_buy_limit,
    aggressive_validation_sell_limit,
)

if TYPE_CHECKING:
    from tyrex_pm.config.loaders import RuntimeSettings

# Float slack for quantity compares (Nautilus ``Quantity`` → float).
_QTY_FLOAT_EPS = 1e-9

# Validation dust: remainder must be strictly below one venue size step; use epsilon so
# ``rem < step`` is stable when ``step`` comes from the same grid Tyrex uses to quantize submits
# (:mod:`tyrex_pm.execution.c3_normalize`).
_DUST_SLOP = 1e-9

# Nautilus Polymarket instruments often expose ``size_increment=1e-6``; CLOB fills/UI use coarser
# share granularity. Without a floor, ``remainder < size_increment`` almost never holds for
# operator-visible dust (e.g. 0.0009 shares left). Validation-only; does not change submit quantization.
_VALIDATION_DUST_SHARE_STEP_FLOOR = 0.01


@dataclass(frozen=True, slots=True)
class ValidationOrderEffectiveness:
    """Validation-harness judgment only — not Nautilus canonical order terminality."""

    effective_complete: bool
    reason: str
    is_closed: bool
    order_status: str | None
    instrument_id: str | None
    size_step: float
    quantity: float
    filled_qty: float
    leaves_qty: float | None
    remainder: float
    tolerance_rule: str


class BotSellValidateStrategyConfig(CopyStrategyConfig, frozen=True, kw_only=True):
    """Extends copy config with deterministic sell-after-buy timing (validation only)."""

    sell_delay_seconds: float = 5.0
    max_cycles: int = 1
    validation_aggressive_limits: bool = True
    validation_buy_aggression_ticks: int = 2
    validation_sell_aggression_ticks: int = 2
    validation_max_slippage_fraction: float = 0.08
    validation_rest_book_for_pricing: bool = True
    #: Shave ``bps / 10_000`` off **long** ``Portfolio.net_position`` before ``min(.., buy fill)`` and
    #: step floor (Scenario A only). Default ~2% — venue atomic balance may trail optimistic floats.
    validation_sell_inventory_haircut_bps: float = DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS


def _client_order_id_str(event: OrderEvent) -> str:
    coid = getattr(event, "client_order_id", None)
    if coid is None:
        return ""
    return str(getattr(coid, "value", coid))


def _qty_float_from_obj(qty_obj: Any) -> float:
    if qty_obj is None:
        return 0.0
    if hasattr(qty_obj, "as_decimal"):
        return float(qty_obj.as_decimal())
    if hasattr(qty_obj, "as_double"):
        return float(qty_obj.as_double())
    return float(qty_obj)


def _px_float_from_obj(px_obj: Any) -> float | None:
    if px_obj is None:
        return None
    if hasattr(px_obj, "as_double"):
        return float(px_obj.as_double())
    if hasattr(px_obj, "as_decimal"):
        return float(px_obj.as_decimal())
    try:
        return float(px_obj)
    except (TypeError, ValueError):
        return None


def _tick_size_for_instrument(inst: Any) -> float:
    inc = getattr(inst, "price_increment", None)
    if inc is None:
        return 0.01
    try:
        return float(inc)
    except (TypeError, ValueError):
        try:
            return float(getattr(inc, "raw", inc))
        except (TypeError, ValueError):
            return 0.01


def _validation_size_step_from_instrument(inst: Any | None) -> float:
    """
    Venue quantity increment for Polymarket outcome instruments (same field as
    :func:`tyrex_pm.execution.c3_normalize.quantize_limit_order_for_instrument`).

    When the instrument is missing, ``0.01`` is a conservative Polymarket-common step for validation
    dust only; incomplete cache should be rare at order-fill time.
    """
    if inst is None:
        return 0.01
    inc = getattr(inst, "size_increment", None)
    if inc is None:
        return 0.01
    try:
        return max(float(inc), 1e-12)
    except (TypeError, ValueError):
        try:
            return max(float(getattr(inc, "raw", inc)), 1e-12)
        except (TypeError, ValueError):
            return 0.01


def _validation_dust_size_step(inst: Any | None) -> tuple[float, float]:
    """
    Return ``(effective_step, reported_step)`` for Scenario A dust only.

    ``effective_step = max(reported, _VALIDATION_DUST_SHARE_STEP_FLOOR)`` so micro-instrument
    increments do not block the harness when Polymarket economics match the common ~0.01 share grid.
    """
    reported = _validation_size_step_from_instrument(inst)
    return max(reported, _VALIDATION_DUST_SHARE_STEP_FLOOR), reported


def _order_status_str(cached: Any) -> str | None:
    st = getattr(cached, "status", None)
    if st is None:
        return None
    return str(getattr(st, "name", st))


def _instrument_id_str(cached: Any) -> str | None:
    iid = getattr(cached, "instrument_id", None)
    if iid is None:
        return None
    return str(iid)


def _float_portfolio_net(net: Any) -> float:
    if net is None:
        return 0.0
    try:
        return float(net)
    except (TypeError, ValueError):
        return 0.0


def resolve_validation_sell_quantity(
    *,
    portfolio: Any | None,
    cache: Any | None,
    instrument_id: InstrumentId,
    quantity_from_buy_fill: float,
    haircut_bps: float,
    venue_state: Any | None = None,
    venue_state_reads_enabled: bool = False,
) -> tuple[float, dict[str, Any]]:
    """
    Scenario A **validation SELL** size: ``min(BUY filled, long inventory after optional haircut)``,
    then floor to :func:`tyrex_pm.execution.c3_normalize.floor_quantity_to_step` so submitted qty never
    exceeds grid-fittable size (NautilusTrader / instrument ``size_increment`` — same path as live
    limit normalization).

    **Haircut base:** long inventory ``max(0, Portfolio.net_position(instrument_id))``. That is the
    same Nautilus source as the optimistic cap, but the venue may authorize sells on **strictly less**
    token float than ``net_position`` displays (atomic ledger vs UI float). Multiplying long inventory
    by ``(1 - haircut_bps/10_000)`` before ``min(buy fill, .)`` is validation-only slack.

    When ``portfolio`` or ``cache`` is unavailable (e.g. unit tests without a trader), returns
    ``quantity_from_buy_fill`` unchanged and **does not** apply the haircut (no portfolio long to shave).

    Does **not** query Polymarket REST for balances; avoids a second OMS.
    """
    hb_in = float(haircut_bps)
    out: dict[str, Any] = {
        "quantity_from_buy_fill": float(quantity_from_buy_fill),
        "portfolio_net_long": None,
        "inventory_long_before_haircut": None,
        "raw_cap_before_haircut": None,
        "inventory_after_haircut": None,
        "raw_cap": None,
        "quantity_after_step": None,
        "final_submit_qty": None,
        "haircut_bps": hb_in,
        "validation_only_inventory_haircut": False,
        "validation_inventory_haircut_note": None,
    }
    q_in = float(quantity_from_buy_fill)
    if q_in <= 0:
        return 0.0, out | {"resolution_note": "non_positive_buy_fill"}

    if cache is None:
        return q_in, out | {
            "resolution_note": "cache_unavailable",
            "validation_inventory_haircut_note": "haircut_skipped_no_cache",
        }

    inst = cache.instrument(instrument_id)
    if inst is None:
        return q_in, out | {
            "resolution_note": "instrument_not_in_cache",
            "validation_inventory_haircut_note": "haircut_skipped_no_instrument",
        }

    if venue_state_reads_enabled and venue_state is not None:
        sz = venue_state.position_size(instrument_id)
        inv_long = max(0.0, float(sz)) if sz is not None else 0.0
    else:
        if portfolio is None:
            return q_in, out | {
                "resolution_note": "portfolio_or_cache_unavailable",
                "validation_inventory_haircut_note": "haircut_skipped_no_portfolio_cache",
            }
        net = portfolio.net_position(instrument_id)
        inv_long = max(0.0, _float_portfolio_net(net))
    out["portfolio_net_long"] = inv_long
    out["inventory_long_before_haircut"] = inv_long
    raw_before = min(q_in, inv_long)
    out["raw_cap_before_haircut"] = raw_before

    hb = max(0.0, min(hb_in, 10_000.0)) / 10_000.0
    inv_adj = inv_long * (1.0 - hb)
    out["inventory_after_haircut"] = inv_adj
    out["validation_only_inventory_haircut"] = True
    out["validation_inventory_haircut_note"] = (
        "Scenario A only: long net_position * (1 - bps/10000), then min(buy_fill), "
        "then floor to size_increment; not CopyStrategy"
    )

    raw = min(q_in, inv_adj)
    out["raw_cap"] = raw

    qty = floor_quantity_to_step(inst, raw, raw)
    out["quantity_after_step"] = qty
    out["final_submit_qty"] = qty

    tags: list[str] = []
    if hb > 1e-15:
        tags.append("inventory_haircut_applied")
    if inv_adj + 1e-12 < q_in:
        tags.append("capped_vs_buy_fill")
    if qty + 1e-12 < raw - 1e-15:
        tags.append("size_step_floor")
    if not tags:
        tags.append("uncapped")
    out["resolution_note"] = "+".join(tags)

    return qty, out


def _validation_order_effectively_complete(
    cached: Any,
    *,
    size_step: float,
) -> ValidationOrderEffectiveness:
    """
    Whether this **validation harness** may treat the order as done for Scenario A.

    **Does not** alter or replace Nautilus ``is_closed`` / OMS reconciliation — it only decides
    whether we arm a validation SELL timer (BUY leg) or clear ``_validate_sell_pending`` (SELL leg).

    **Rule (venue-aware):** after sync math below, if the conservative remainder is strictly below
    one ``size_increment`` (plus float slop), no further whole step can trade on the venue grid we
    use for quantization, matching the “economically done but locally open” Polymarket reports.
    """
    is_closed = bool(getattr(cached, "is_closed", False))
    iid_s = _instrument_id_str(cached)
    step = max(float(size_step), 1e-12)
    qty_total = _qty_float_from_obj(getattr(cached, "quantity", None))
    qty_filled = _qty_float_from_obj(getattr(cached, "filled_qty", None))
    leaves_attr = getattr(cached, "leaves_qty", None)
    leaves_f: float | None
    if leaves_attr is None:
        leaves_f = None
    else:
        leaves_f = _qty_float_from_obj(leaves_attr)

    rem_from_qty = max(0.0, qty_total - qty_filled)
    if leaves_f is not None and leaves_f > 0.0:
        remainder = max(rem_from_qty, leaves_f)
    else:
        remainder = rem_from_qty

    tol_desc = (
        f"validation_only: remainder+slop < size_increment "
        f"(remainder={remainder}, size_increment={step}, "
        f"slop={_DUST_SLOP}) — does not force Nautilus is_closed"
    )

    if is_closed:
        return ValidationOrderEffectiveness(
            effective_complete=True,
            reason="is_closed",
            is_closed=True,
            order_status=_order_status_str(cached),
            instrument_id=iid_s,
            size_step=step,
            quantity=qty_total,
            filled_qty=qty_filled,
            leaves_qty=leaves_f,
            remainder=remainder,
            tolerance_rule=tol_desc,
        )

    if qty_total <= 0.0:
        return ValidationOrderEffectiveness(
            effective_complete=False,
            reason="invalid_zero_quantity",
            is_closed=False,
            order_status=_order_status_str(cached),
            instrument_id=iid_s,
            size_step=step,
            quantity=qty_total,
            filled_qty=qty_filled,
            leaves_qty=leaves_f,
            remainder=remainder,
            tolerance_rule=tol_desc,
        )

    if qty_filled + _QTY_FLOAT_EPS >= qty_total:
        return ValidationOrderEffectiveness(
            effective_complete=True,
            reason="filled_matches_order_quantity",
            is_closed=False,
            order_status=_order_status_str(cached),
            instrument_id=iid_s,
            size_step=step,
            quantity=qty_total,
            filled_qty=qty_filled,
            leaves_qty=leaves_f,
            remainder=remainder,
            tolerance_rule=tol_desc,
        )

    if (
        leaves_f is not None
        and leaves_f <= _QTY_FLOAT_EPS
        and qty_filled > _QTY_FLOAT_EPS
        and (
            rem_from_qty <= _QTY_FLOAT_EPS
            or (rem_from_qty + _DUST_SLOP) < step
        )
    ):
        return ValidationOrderEffectiveness(
            effective_complete=True,
            reason="leaves_empty_remainder_negligible",
            is_closed=False,
            order_status=_order_status_str(cached),
            instrument_id=iid_s,
            size_step=step,
            quantity=qty_total,
            filled_qty=qty_filled,
            leaves_qty=leaves_f,
            remainder=remainder,
            tolerance_rule=tol_desc,
        )

    if remainder <= _QTY_FLOAT_EPS:
        return ValidationOrderEffectiveness(
            effective_complete=True,
            reason="remainder_zero",
            is_closed=False,
            order_status=_order_status_str(cached),
            instrument_id=iid_s,
            size_step=step,
            quantity=qty_total,
            filled_qty=qty_filled,
            leaves_qty=leaves_f,
            remainder=remainder,
            tolerance_rule=tol_desc,
        )

    if (remainder + _DUST_SLOP) < step:
        return ValidationOrderEffectiveness(
            effective_complete=True,
            reason="remainder_below_size_increment",
            is_closed=False,
            order_status=_order_status_str(cached),
            instrument_id=iid_s,
            size_step=step,
            quantity=qty_total,
            filled_qty=qty_filled,
            leaves_qty=leaves_f,
            remainder=remainder,
            tolerance_rule=tol_desc,
        )

    return ValidationOrderEffectiveness(
        effective_complete=False,
        reason="incomplete_remainder_ge_size_increment",
        is_closed=False,
        order_status=_order_status_str(cached),
        instrument_id=iid_s,
        size_step=step,
        quantity=qty_total,
        filled_qty=qty_filled,
        leaves_qty=leaves_f,
        remainder=remainder,
        tolerance_rule=tol_desc,
    )


def _effectiveness_fact_payload(ev: ValidationOrderEffectiveness, *, leg: str) -> dict[str, Any]:
    return {
        "kind": "order_completion_eval",
        "leg": leg,
        "effective_complete": ev.effective_complete,
        "reason": ev.reason,
        "is_closed": ev.is_closed,
        "order_status": ev.order_status,
        "instrument_id": ev.instrument_id,
        "size_increment": float(ev.size_step),
        "quantity": float(ev.quantity),
        "filled_qty": float(ev.filled_qty),
        "leaves_qty": ev.leaves_qty,
        "remainder": float(ev.remainder),
        "tolerance_rule": ev.tolerance_rule,
    }


class BotSellValidateStrategy(CopyStrategy):
    """
    Same as :class:`~tyrex_pm.strategy.copy_strategy.CopyStrategy`, plus an internal SELL after
    each qualifying guru BUY fill (see YAML ``bot_sell_validate``).
    """

    def __init__(self, config: BotSellValidateStrategyConfig) -> None:
        super().__init__(config)
        self._vcfg = config
        self._validate_sell_pending = False
        self._validation_sells_filled = 0
        self._bot_sell_round = 0
        self._pricing_runtime: RuntimeSettings | None = None
        self._pricing_clob: Any | None = None

    def set_pricing_runtime(self, runtime: RuntimeSettings | None) -> None:
        """Set by :func:`~tyrex_pm.runtime.guru_compose.build_guru_trading_node` for REST book."""
        self._pricing_runtime = runtime
        self._pricing_clob = None

    def _lazy_rest_clob(self) -> Any | None:
        if self._pricing_clob is not None:
            return self._pricing_clob
        rt = self._pricing_runtime
        if rt is None:
            return None
        from tyrex_pm.runtime.clob_factory import build_clob_client_from_env

        self._pricing_clob = build_clob_client_from_env(rt)
        return self._pricing_clob

    def _instrument_tick_for_token(self, token_id: str) -> tuple[InstrumentId, float] | None:
        rt = self._pricing_runtime
        static = dict(rt.polymarket_token_to_instrument) if rt else {}
        iid = instrument_id_for_outcome_token(
            self.cache,
            str(token_id),
            static_token_to_instrument=static,
        )
        if iid is None:
            return None
        inst = self.cache.instrument(iid)
        tick = _tick_size_for_instrument(inst) if inst is not None else 0.01
        return iid, tick

    def _book_top_for_validation(self, token_id: str, instrument_id: InstrumentId) -> BookTop:
        rt = self._pricing_runtime
        rest_ok = bool(
            rt
            and self._vcfg.validation_rest_book_for_pricing
            and rt.execution_book_rest_snapshot_enabled
        )
        clob = self._lazy_rest_clob() if rest_ok else None
        return resolve_book_top(
            cache=self.cache,
            instrument_id=instrument_id,
            token_id=str(token_id),
            rest_enabled=rest_ok,
            clob=clob,
        )

    def _validation_effectiveness_for_cached_order(self, cached: Any) -> ValidationOrderEffectiveness:
        inst = self.cache.instrument(getattr(cached, "instrument_id", None))
        eff_step, reported = _validation_dust_size_step(inst)
        ev = _validation_order_effectively_complete(cached, size_step=eff_step)
        if reported + 1e-15 < eff_step:
            return replace(
                ev,
                tolerance_rule=(
                    f"{ev.tolerance_rule} | reported_size_increment={reported}; "
                    f"effective_dust_step={eff_step} (Scenario A floor {_VALIDATION_DUST_SHARE_STEP_FLOOR})"
                ),
            )
        return ev

    def _emit_aggressive_fact(self, kind: str, correlation_id: str, quote: ValidationAggressiveQuote) -> None:
        em = self._reporting_emit
        if em is None:
            return
        pl = quote.as_fact_payload()
        pl["kind"] = kind
        pl["correlation_id"] = correlation_id
        em("bot_sell_validate", pl)

    def _adjust_intent_before_risk(
        self,
        intent: OrderIntent,
        *,
        signal: GuruTradeSignal,
        branch: str,
    ) -> OrderIntent:
        if (
            not self._vcfg.validation_aggressive_limits
            or branch != "entry"
            or intent.side.upper() != "BUY"
        ):
            return intent
        if intent.price_ref is None or signal.price_raw is None:
            return intent

        hit = self._instrument_tick_for_token(str(intent.token_id))
        if hit is None:
            self.log.warning(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=aggressive_limit_buy_skip correlation_id={intent.correlation_id} "
                "reason=no_instrument_in_cache"
            )
            return intent

        iid, tick = hit
        book = self._book_top_for_validation(str(intent.token_id), iid)
        ref = float(signal.price_raw)
        quote = aggressive_validation_buy_limit(
            reference_price=ref,
            tick_size=tick,
            book=book,
            aggression_ticks=self._vcfg.validation_buy_aggression_ticks,
            max_slippage_fraction=self._vcfg.validation_max_slippage_fraction,
        )
        self._emit_aggressive_fact("aggressive_limit_buy", intent.correlation_id, quote)
        self.log.info(
            "event=bot_sell_validate component=bot_sell_validate_strategy "
            f"kind=aggressive_limit_buy correlation_id={intent.correlation_id} "
            f"reference_price={quote.reference_price} limit_price={quote.limit_price} "
            f"book_source={quote.book_source} anchor={quote.anchor_description} "
            f"aggression_ticks={quote.aggression_ticks} clamp={quote.clamp_note}"
        )
        return OrderIntent(
            correlation_id=intent.correlation_id,
            token_id=intent.token_id,
            side=intent.side,
            quantity=intent.quantity,
            signal_kind=intent.signal_kind,
            reason_code=intent.reason_code,
            price_ref=quote.limit_price,
        )

    def on_order_event(self, event: OrderEvent) -> None:
        if getattr(event, "reconciliation", False):
            return
        super().on_order_event(event)
        if self._vcfg.execution_mode != "live":
            return
        if not isinstance(event, OrderFilled):
            return

        coid_s = _client_order_id_str(event)
        if not coid_s:
            return

        reg = self._order_registry
        corr = reg.correlation_for(coid_s) if reg is not None else None
        if corr is None:
            return

        if corr.startswith("bot_sell_validate:"):
            self._on_validation_sell_filled(coid_s, corr)
            return

        self._maybe_schedule_validate_sell_after_guru_buy(coid_s, corr, event)

    def _on_validation_sell_filled(self, coid_s: str, corr: str) -> None:
        try:
            cached = self.cache.order(ClientOrderId(coid_s))
        except (TypeError, ValueError):
            cached = None
        if cached is None:
            return

        ev = self._validation_effectiveness_for_cached_order(cached)
        em = self._reporting_emit
        if em is not None:
            pl = _effectiveness_fact_payload(ev, leg="validation_sell")
            pl["client_order_id"] = coid_s
            pl["correlation_id"] = corr
            em("bot_sell_validate", pl)

        self.log.info(
            "event=bot_sell_validate component=bot_sell_validate_strategy "
            f"kind=validation_sell_completion_eval client_order_id={coid_s} correlation_id={corr} "
            f"effective_complete={ev.effective_complete} completion_reason={ev.reason} "
            f"is_closed={ev.is_closed} order_status={ev.order_status} "
            f"qty={ev.quantity} filled={ev.filled_qty} leaves={ev.leaves_qty} "
            f"remainder={ev.remainder} size_increment={ev.size_step} "
            f"pending_before={self._validate_sell_pending}"
        )

        if not ev.effective_complete:
            return

        self._validate_sell_pending = False
        self._validation_sells_filled += 1
        if em is not None:
            em(
                "bot_sell_validate",
                {
                    "kind": "validation_sell_filled",
                    "correlation_id": corr,
                    "client_order_id": coid_s,
                    "cycles_completed": self._validation_sells_filled,
                    "max_cycles": int(self._vcfg.max_cycles),
                    "completion_reason": ev.reason,
                    "remainder": float(ev.remainder),
                    "size_increment": float(ev.size_step),
                    "is_closed": ev.is_closed,
                },
            )
        self.log.info(
            "event=bot_sell_validate component=bot_sell_validate_strategy "
            f"kind=validation_sell_filled correlation_id={corr} "
            f"cycles_completed={self._validation_sells_filled} max_cycles={self._vcfg.max_cycles} "
            f"completion_reason={ev.reason} is_closed={ev.is_closed} remainder={ev.remainder}"
        )

    def _maybe_schedule_validate_sell_after_guru_buy(
        self,
        coid_s: str,
        entry_corr: str,
        event: OrderFilled,
    ) -> None:
        if self._validation_sells_filled >= self._vcfg.max_cycles:
            self.log.info(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=skip_schedule_max_cycles entry_correlation_id={entry_corr} "
                f"validation_sells_filled={self._validation_sells_filled} "
                f"max_cycles={self._vcfg.max_cycles}"
            )
            return
        if self._validate_sell_pending:
            self.log.info(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=skip_schedule_sell_pending exit_in_flight=true "
                f"entry_correlation_id={entry_corr} client_order_id={coid_s}"
            )
            return

        try:
            cached = self.cache.order(ClientOrderId(coid_s))
        except (TypeError, ValueError):
            cached = None
        if cached is None:
            return

        ev = self._validation_effectiveness_for_cached_order(cached)
        em = self._reporting_emit
        if em is not None:
            pl = _effectiveness_fact_payload(ev, leg="buy_schedule")
            pl["client_order_id"] = coid_s
            pl["entry_correlation_id"] = entry_corr
            em("bot_sell_validate", pl)

        self.log.info(
            "event=bot_sell_validate component=bot_sell_validate_strategy "
            f"kind=buy_schedule_completion_eval entry_correlation_id={entry_corr} "
            f"client_order_id={coid_s} effective_complete={ev.effective_complete} "
            f"completion_reason={ev.reason} is_closed={ev.is_closed} order_status={ev.order_status} "
            f"qty={ev.quantity} filled={ev.filled_qty} leaves={ev.leaves_qty} "
            f"remainder={ev.remainder} size_increment={ev.size_step}"
        )

        if not ev.effective_complete:
            self.log.info(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=buy_schedule_skip entry_correlation_id={entry_corr} "
                f"client_order_id={coid_s} reason={ev.reason}"
            )
            return
        if cached.side != OrderSide.BUY:
            return

        qty = _qty_float_from_obj(getattr(cached, "filled_qty", None))
        if qty <= 0:
            qty = _qty_float_from_obj(getattr(cached, "quantity", None))
        if qty <= 0:
            return

        price_ref = _px_float_from_obj(getattr(cached, "price", None))

        if price_ref is None or price_ref <= 0:
            last_px = getattr(event, "last_px", None)
            if last_px is not None:
                price_ref = _px_float_from_obj(last_px)
        if price_ref is None or price_ref <= 0:
            self.log.warning(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=skip_no_price entry_correlation_id={entry_corr} client_order_id={coid_s}"
            )
            return

        try:
            from nautilus_trader.adapters.polymarket.common.symbol import (
                get_polymarket_token_id,
            )

            token_id = str(get_polymarket_token_id(cached.instrument_id))
        except Exception:  # noqa: BLE001
            self.log.warning(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=skip_token_resolve entry_correlation_id={entry_corr}"
            )
            return

        self._bot_sell_round += 1
        rid = self._bot_sell_round
        sell_cid = f"bot_sell_validate:r{rid}:{entry_corr}"
        name = f"tyrex_bot_sell_validate_{rid}"
        delay = timedelta(seconds=float(self._vcfg.sell_delay_seconds))

        self._validate_sell_pending = True
        if em is not None:
            em(
                "bot_sell_validate",
                {
                    "kind": "scheduled",
                    "entry_correlation_id": entry_corr,
                    "sell_correlation_id": sell_cid,
                    "delay_seconds": float(self._vcfg.sell_delay_seconds),
                    "quantity": float(qty),
                    "token_id": token_id,
                    "max_cycles": int(self._vcfg.max_cycles),
                    "validation_sells_filled_before": int(self._validation_sells_filled),
                    "buy_round": int(rid),
                    "trigger": "buy_order_effectively_complete",
                    "completion_reason": ev.reason,
                    "remainder": float(ev.remainder),
                    "size_increment": float(ev.size_step),
                    "is_closed": ev.is_closed,
                },
            )
        self.log.info(
            "event=bot_sell_validate component=bot_sell_validate_strategy "
            f"kind=scheduled entry_correlation_id={entry_corr} sell_correlation_id={sell_cid} "
            f"delay_s={self._vcfg.sell_delay_seconds} qty={qty} max_cycles={self._vcfg.max_cycles} "
            f"validation_sells_filled={self._validation_sells_filled} buy_round={rid} "
            f"trigger=buy_order_effectively_complete completion_reason={ev.reason} "
            f"is_closed={ev.is_closed} remainder={ev.remainder}"
        )

        buy_iid = cached.instrument_id

        def on_time(t_evt: TimeEvent) -> None:
            _ = t_evt
            try:
                self.clock.cancel_timer(name)
            except Exception:  # noqa: BLE001
                pass
            self._submit_validated_sell(
                instrument_id=buy_iid,
                token_id=token_id,
                quantity_from_buy_fill=qty,
                price_ref=price_ref,
                sell_correlation_id=sell_cid,
                entry_correlation_id=entry_corr,
            )

        self.clock.set_timer(name=name, interval=delay, callback=on_time)

    def _submit_validated_sell(
        self,
        *,
        instrument_id: InstrumentId,
        token_id: str,
        quantity_from_buy_fill: float,
        price_ref: float,
        sell_correlation_id: str,
        entry_correlation_id: str,
    ) -> None:
        qty_sell, inv_meta = resolve_validation_sell_quantity(
            portfolio=getattr(self, "portfolio", None),
            cache=getattr(self, "cache", None),
            instrument_id=instrument_id,
            quantity_from_buy_fill=float(quantity_from_buy_fill),
            haircut_bps=float(self._vcfg.validation_sell_inventory_haircut_bps),
            venue_state=getattr(self, "_tyrex_venue_state", None),
            venue_state_reads_enabled=bool(
                getattr(self, "_tyrex_venue_state_reads_enabled", False),
            ),
        )

        em0 = self._reporting_emit
        if em0 is not None:
            em0(
                "bot_sell_validate",
                {
                    "kind": "validation_sell_qty_resolved",
                    "entry_correlation_id": entry_correlation_id,
                    "sell_correlation_id": sell_correlation_id,
                    "instrument_id": str(instrument_id),
                    "qty_from_buy_fill": float(quantity_from_buy_fill),
                    "qty_submit_final": float(qty_sell),
                    **{k: v for k, v in inv_meta.items() if v is not None},
                },
            )

        self.log.info(
            "event=bot_sell_validate component=bot_sell_validate_strategy "
            f"kind=validation_sell_qty_resolved entry_correlation_id={entry_correlation_id} "
            f"sell_correlation_id={sell_correlation_id} instrument_id={instrument_id} "
            f"qty_from_buy_fill={quantity_from_buy_fill} "
            f"inventory_long_before_haircut={inv_meta.get('inventory_long_before_haircut')} "
            f"raw_cap_before_haircut={inv_meta.get('raw_cap_before_haircut')} "
            f"haircut_bps={inv_meta.get('haircut_bps')} "
            f"inventory_after_haircut={inv_meta.get('inventory_after_haircut')} "
            f"raw_cap_after_haircut={inv_meta.get('raw_cap')} "
            f"qty_submit_final={qty_sell} "
            f"validation_only_inventory_haircut={inv_meta.get('validation_only_inventory_haircut')} "
            f"resolution_note={inv_meta.get('resolution_note')}"
        )

        if qty_sell <= 0:
            self._validate_sell_pending = False
            self.log.warning(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=validation_sell_skip_zero_qty entry_correlation_id={entry_correlation_id} "
                f"sell_correlation_id={sell_correlation_id} meta={inv_meta}"
            )
            return

        px_in = float(price_ref)
        if self._vcfg.validation_aggressive_limits:
            hit = self._instrument_tick_for_token(token_id)
            if hit is not None:
                iid, tick = hit
                book = self._book_top_for_validation(token_id, iid)
                quote = aggressive_validation_sell_limit(
                    reference_price=px_in,
                    tick_size=tick,
                    book=book,
                    aggression_ticks=self._vcfg.validation_sell_aggression_ticks,
                    max_slippage_fraction=self._vcfg.validation_max_slippage_fraction,
                )
                px_in = quote.limit_price
                self._emit_aggressive_fact("aggressive_limit_sell", sell_correlation_id, quote)
                self.log.info(
                    "event=bot_sell_validate component=bot_sell_validate_strategy "
                    f"kind=aggressive_limit_sell sell_correlation_id={sell_correlation_id} "
                    f"entry_correlation_id={entry_correlation_id} "
                    f"reference_price={quote.reference_price} limit_price={quote.limit_price} "
                    f"book_source={quote.book_source} anchor={quote.anchor_description} "
                    f"aggression_ticks={quote.aggression_ticks} clamp={quote.clamp_note}"
                )
            else:
                self.log.warning(
                    "event=bot_sell_validate component=bot_sell_validate_strategy "
                    f"kind=aggressive_limit_sell_skip sell_correlation_id={sell_correlation_id} "
                    "reason=no_instrument_in_cache"
                )

        intent = OrderIntent(
            correlation_id=sell_correlation_id,
            token_id=token_id,
            side="SELL",
            quantity=qty_sell,
            signal_kind="exit",
            reason_code=str(ReasonCode.BOT_SELL_VALIDATE),
            price_ref=px_in,
        )
        su = self._startup_block_reason("SELL")
        if su is not None:
            self._validate_sell_pending = False
            self.log.info(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=startup_block sell_correlation_id={sell_correlation_id} "
                f"entry_correlation_id={entry_correlation_id} reason_code={su}"
            )
            return

        approved, risk_rc, intent_risk = self._risk.evaluate(intent)
        if not approved or intent_risk is None:
            self._validate_sell_pending = False
            self.log.info(
                "event=bot_sell_validate component=bot_sell_validate_strategy "
                f"kind=risk_denied sell_correlation_id={sell_correlation_id} "
                f"entry_correlation_id={entry_correlation_id} risk_detail={risk_rc}"
            )
            return

        em = self._reporting_emit
        if em is not None:
            em(
                "execution_intent",
                {
                    "correlation_id": sell_correlation_id,
                    "token_id": token_id,
                    "side": "SELL",
                    "quantity": float(intent_risk.quantity),
                    "quantity_strategy_sized": float(qty_sell),
                    "quantity_from_buy_fill": float(quantity_from_buy_fill),
                    "validation_sell_inventory_haircut_bps": inv_meta.get("haircut_bps"),
                    "validation_raw_cap_before_haircut": inv_meta.get("raw_cap_before_haircut"),
                    "validation_final_submit_qty": float(qty_sell),
                    "validation_only_inventory_haircut": inv_meta.get(
                        "validation_only_inventory_haircut"
                    ),
                    "signal_kind": "exit",
                    "price_ref": intent_risk.price_ref,
                    "ts_risk_approved_ms": int(time.time() * 1000),
                },
            )
            em(
                "bot_sell_validate",
                {
                    "kind": "submit_sell",
                    "entry_correlation_id": entry_correlation_id,
                    "sell_correlation_id": sell_correlation_id,
                    "quantity": float(intent_risk.quantity),
                    "quantity_from_buy_fill": float(quantity_from_buy_fill),
                    "quantity_inventory_resolved": float(qty_sell),
                    "validation_sell_inventory_haircut_bps": inv_meta.get("haircut_bps"),
                    "inventory_long_before_haircut": inv_meta.get("inventory_long_before_haircut"),
                    "raw_cap_before_haircut": inv_meta.get("raw_cap_before_haircut"),
                    "inventory_after_haircut": inv_meta.get("inventory_after_haircut"),
                    "validation_only_note": inv_meta.get("validation_inventory_haircut_note"),
                },
            )

        self._execution.submit_intent(intent_risk, mode=self._vcfg.execution_mode)
        emit_cap = getattr(self._risk, "emit_capital_observation", None)
        if em is not None and callable(emit_cap):
            emit_cap("submit", correlation_id=sell_correlation_id, intent=intent_risk)

        self.log.info(
            "event=live_order_intent component=bot_sell_validate_strategy "
            f"correlation_id={sell_correlation_id} signal_kind=exit side=SELL "
            f"qty={intent_risk.quantity} entry_correlation_id={entry_correlation_id}"
        )
