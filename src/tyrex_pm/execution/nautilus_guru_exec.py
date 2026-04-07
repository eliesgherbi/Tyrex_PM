"""
Guru-follow live execution via **Nautilus framework** ``submit_order`` → ExecEngine →
``PolymarketExecutionClient`` (**Step 4** + **Step 5** dynamic instruments).

**Package-source-confirmed:** same pattern as ``scripts/spike_nautilus_polymarket_exec.py`` /
``order_factory.limit`` + ``submit_order(..., client_id=POLYMARKET_CLIENT_ID)``.

``OrderIntent`` is translated here — **not** in
:class:`~tyrex_pm.strategy.copy_strategy.CopyStrategy` (thin strategy invariant).

**C3:** Optional **book** features (entry guard, depth clip, limit timeout) behind runtime YAML flags.
Limit price/qty are always snapped to instrument tick/size step **without** operator “alignment” policy;
see ``c3_normalize.quantize_limit_order_for_instrument``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from nautilus_trader.adapters.polymarket import POLYMARKET_CLIENT_ID
from nautilus_trader.common.component import TimeEvent
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.execution.c3_book_top import resolve_book_top
from tyrex_pm.execution.c3_depth import clip_to_book_depth
from tyrex_pm.execution.c3_entry_guard import check_entry_guard
from tyrex_pm.execution.c3_normalize import floor_quantity_to_step, quantize_limit_order_for_instrument
from tyrex_pm.reporting.correlation_registry import OrderCorrelationRegistry
from tyrex_pm.runtime.clob_factory import build_clob_client_from_env

if TYPE_CHECKING:
    from nautilus_trader.trading.strategy import Strategy

    from tyrex_pm.runtime.guru_instrument_dynamic import GuruInstrumentDynamicController

_TAG_SAFE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _client_order_id_from_guru_correlation(correlation_id: str) -> ClientOrderId:
    """
    Deterministic, short ``ClientOrderId`` from guru ``source_trade_id`` (often a tx hash).

    **Package-source-confirmed:** ``ClientOrderId`` accepts alphanumeric strings;
    keep bounded length.
    """
    digest = hashlib.sha256(correlation_id.encode("utf-8", errors="replace")).hexdigest()[:26]
    return ClientOrderId(f"TX{digest}")


def _guru_tag(correlation_id: str) -> str:
    """Nautilus order tag for grep / ops (ASCII-safe, length-bounded)."""
    s = _TAG_SAFE.sub("_", correlation_id.strip())[:120]
    return f"guru_cid={s}"


def _tick_float(inst: Any) -> float:
    inc = getattr(inst, "price_increment", None)
    if inc is None:
        return 0.01
    try:
        return float(inc)
    except (TypeError, ValueError):
        return float(getattr(inc, "raw", inc))


class NautilusGuruExecutionPort:
    """
    Live guru execution through the **framework** path (visibility in ``Cache``).

    Uses dynamic resolution when a controller is wired; optional YAML token map as overlay.
    """

    __slots__ = (
        "_strategy",
        "_runtime",
        "_token_to_instrument",
        "_dynamic",
        "_rest_clob",
        "_fact_emit",
        "_order_registry",
    )

    def __init__(
        self,
        strategy: Strategy,
        runtime: RuntimeSettings,
        *,
        dynamic: GuruInstrumentDynamicController | None = None,
        fact_emit: Callable[[str, dict[str, Any]], None] | None = None,
        order_registry: OrderCorrelationRegistry | None = None,
    ) -> None:
        self._strategy = strategy
        self._runtime = runtime
        self._token_to_instrument = dict(runtime.polymarket_token_to_instrument)
        self._dynamic = dynamic
        self._rest_clob: Any | None = None
        self._fact_emit = fact_emit
        self._order_registry = order_registry

    def _emit(self, fact_type: str, payload: dict[str, Any]) -> None:
        fe = self._fact_emit
        if fe is not None:
            fe(fact_type, payload)

    def _reconcile_submit_vs_cache(
        self,
        *,
        intent: OrderIntent,
        coid: ClientOrderId,
        coid_s: str,
        expected_qty: float,
        expected_price: float,
    ) -> None:
        """INT-RC-01: compare immediately post-submit ``Cache.order`` to submitted qty/price."""
        if self._fact_emit is None:
            return
        cached = self._strategy.cache.order(coid)
        if cached is None:
            self._emit(
                "reconciliation",
                {
                    "check_type": "submit_vs_cache",
                    "outcome": "order_missing",
                    "correlation_id": intent.correlation_id,
                    "client_order_id": coid_s,
                    "detail": "cache.order None after submit_order",
                },
            )
            return
        try:
            q_obj = getattr(cached, "quantity", None)
            if q_obj is None:
                raise ValueError("no quantity")
            if hasattr(q_obj, "as_decimal"):
                cq = float(q_obj.as_decimal())
            else:
                cq = float(q_obj)
            p_obj = getattr(cached, "price", None)
            if p_obj is None:
                self._emit(
                    "reconciliation",
                    {
                        "check_type": "submit_vs_cache",
                        "outcome": "mismatch_price",
                        "correlation_id": intent.correlation_id,
                        "client_order_id": coid_s,
                        "detail": "cached order has no price",
                        "expected_qty": expected_qty,
                        "cache_qty": cq,
                        "expected_price": expected_price,
                    },
                )
                return
            if hasattr(p_obj, "as_decimal"):
                cp = float(p_obj.as_decimal())
            else:
                cp = float(p_obj)
        except (TypeError, ValueError, ArithmeticError) as exc:
            self._emit(
                "reconciliation",
                {
                    "check_type": "submit_vs_cache",
                    "outcome": "parse_error",
                    "correlation_id": intent.correlation_id,
                    "client_order_id": coid_s,
                    "detail": str(exc),
                },
            )
            return

        tol_q = max(1e-9, abs(expected_qty) * 1e-9)
        tol_p = max(1e-9, abs(expected_price) * 1e-9)
        qty_ok = abs(cq - expected_qty) <= tol_q
        price_ok = abs(cp - expected_price) <= tol_p
        if qty_ok and price_ok:
            out = "match"
        elif not qty_ok and not price_ok:
            out = "mismatch_qty_price"
        elif not qty_ok:
            out = "mismatch_qty"
        else:
            out = "mismatch_price"
        self._emit(
            "reconciliation",
            {
                "check_type": "submit_vs_cache",
                "outcome": out,
                "correlation_id": intent.correlation_id,
                "client_order_id": coid_s,
                "expected_qty": expected_qty,
                "cache_qty": cq,
                "expected_price": expected_price,
                "cache_price": cp,
            },
        )

    def _c3_shape_prepare(
        self,
        intent: OrderIntent,
        *,
        inst: Any,
        instrument_id: InstrumentId,
        side_u: str,
        qty: float,
        price: float,
        approved_qty: float,
    ) -> tuple[float, float, bool, str | None]:
        """
        Apply optional C3 guard / depth clip (book-driven only).

        Returns ``(qty, price, skip_submitted, skip_reason_code)`` — ``skip_reason_code`` set when
        ``skip_submitted`` is true.
        """
        r = self._runtime
        need_book = r.execution_entry_guard_enabled or r.execution_book_depth_clip_enabled
        book = None
        if need_book:
            clob = None
            if r.execution_book_rest_snapshot_enabled:
                if self._rest_clob is None:
                    self._rest_clob = build_clob_client_from_env(self._runtime)
                clob = self._rest_clob
            book = resolve_book_top(
                cache=self._strategy.cache,
                instrument_id=instrument_id,
                token_id=str(intent.token_id),
                rest_enabled=r.execution_book_rest_snapshot_enabled,
                clob=clob,
            )
            self._emit(
                "book_constraint",
                {
                    "correlation_id": intent.correlation_id,
                    "book_source": book.source if book is not None else "none",
                },
            )
            if book.source == "none" and r.execution_book_strict:
                self._strategy.log.info(
                    f"event={ReasonCode.EXEC_BOOK_UNAVAILABLE_SKIP} component=nautilus_guru_exec "
                    f"correlation_id={intent.correlation_id} instrument_id={instrument_id}",
                )
                return qty, price, True, str(ReasonCode.EXEC_BOOK_UNAVAILABLE_SKIP)

        if r.execution_entry_guard_enabled and book is not None and book.source != "none":
            gr = check_entry_guard(
                side=side_u,
                reference_price=float(intent.price_ref or 0.0),
                book=book,
                max_slippage_ticks=r.execution_max_entry_slippage_ticks,
                tick_size=_tick_float(inst),
            )
            if not gr.ok:
                self._strategy.log.info(
                    f"event={ReasonCode.EXEC_ENTRY_GUARD_SKIP} component=nautilus_guru_exec "
                    f"correlation_id={intent.correlation_id} reference_price={intent.price_ref} "
                    f"detail={gr.detail}",
                )
                return qty, price, True, str(ReasonCode.EXEC_ENTRY_GUARD_SKIP)
        elif r.execution_entry_guard_enabled:
            self._strategy.log.debug(
                "event=exec_c3_guard_no_book component=nautilus_guru_exec "
                f"correlation_id={intent.correlation_id} strict={r.execution_book_strict}",
            )

        depth_applied = False
        depth_intended_qty = qty
        if r.execution_book_depth_clip_enabled and book is not None and book.source != "none":
            dr = clip_to_book_depth(
                side=side_u,
                quantity=qty,
                book=book,
                utilization_cap=r.execution_book_depth_utilization_cap,
            )
            depth_applied = True
            if dr.clipped:
                self._strategy.log.info(
                    f"event={ReasonCode.EXEC_DEPTH_CLIP_APPLIED} component=nautilus_guru_exec "
                    f"correlation_id={intent.correlation_id} intended_qty={depth_intended_qty} "
                    f"submitted_qty={dr.quantity} visible_liquidity={dr.visible_liquidity}",
                )
            qty = dr.quantity
        elif r.execution_book_depth_clip_enabled:
            self._strategy.log.debug(
                "event=exec_c3_depth_no_book component=nautilus_guru_exec "
                f"correlation_id={intent.correlation_id}",
            )

        if depth_applied:
            qty = floor_quantity_to_step(inst, qty, approved_qty)
            if qty <= 0:
                self._strategy.log.info(
                    f"event={ReasonCode.EXEC_INSTRUMENT_QUANTIZE_SKIP} component=nautilus_guru_exec "
                    f"correlation_id={intent.correlation_id} detail=qty_rounded_to_zero_after_depth",
                )
                return qty, price, True, str(ReasonCode.EXEC_INSTRUMENT_QUANTIZE_SKIP)
        return qty, price, False, None

    def _schedule_limit_cancel(self, client_order_id: ClientOrderId, correlation_id: str) -> None:
        name = f"c3guru_{client_order_id.value}"
        timeout_s = float(self._runtime.execution_limit_timeout_seconds)

        def on_time(event: TimeEvent) -> None:
            _ = event
            try:
                self._strategy.clock.cancel_timer(name)
            except Exception:  # noqa: BLE001
                pass
            cached = self._strategy.cache.order(client_order_id)
            if cached is None or cached.is_closed:
                return
            try:
                self._strategy.cancel_order(cached, client_id=POLYMARKET_CLIENT_ID)
            except Exception as exc:  # noqa: BLE001
                self._strategy.log.warning(
                    f"event={ReasonCode.LIVE_ORDER_ERROR} component=nautilus_guru_exec "
                    f"correlation_id={correlation_id} detail=limit_timeout_cancel_failed err={exc}",
                )
                return
            self._strategy.log.info(
                f"event={ReasonCode.EXEC_LIMIT_TIMEOUT_CANCEL} component=nautilus_guru_exec "
                f"correlation_id={correlation_id} client_order_id={client_order_id}",
            )

        self._strategy.clock.set_timer(
            name=name,
            interval=timedelta(seconds=timeout_s),
            callback=on_time,
        )

    def notify_order_event(self, event: Any) -> None:
        """Cancel C3 limit timer when orders complete (called from ``CopyStrategy``)."""
        if not self._runtime.execution_limit_timeout_enabled:
            return
        try:
            coid = event.client_order_id
        except AttributeError:
            return
        name = f"c3guru_{coid.value}"
        try:
            self._strategy.clock.cancel_timer(name)
        except Exception:  # noqa: BLE001
            pass

    def submit_intent(self, intent: OrderIntent, *, mode: str) -> None:
        if mode != "live":
            return
        if intent.price_ref is None:
            self._strategy.log.warning(
                f"event={ReasonCode.LIVE_ORDER_ERROR} component=nautilus_guru_exec "
                f"correlation_id={intent.correlation_id} detail=missing_price",
            )
            self._emit(
                "execution_outcome",
                {
                    "correlation_id": intent.correlation_id,
                    "outcome": "error",
                    "reason_code": str(ReasonCode.LIVE_ORDER_ERROR),
                    "instrument_id": "",
                    "submitted_qty": 0.0,
                    "submitted_price": 0.0,
                },
            )
            return

        qty = float(intent.quantity)
        price = float(intent.price_ref)
        side_u = intent.side.upper()
        approved_qty = qty

        tid = str(intent.token_id)
        instr_s = self._token_to_instrument.get(tid)
        instrument_id: InstrumentId | None = None
        inst = None
        dyn_fail: str | None = None

        if self._dynamic is not None:
            inst, dtag = self._dynamic.resolve_and_activate(tid)
            if inst is not None:
                instrument_id = inst.id
            else:
                dyn_fail = dtag

        if inst is None and instr_s is not None:
            instrument_id = InstrumentId.from_str(instr_s)
            inst = self._strategy.cache.instrument(instrument_id)

        if inst is None:
            if instr_s is not None:
                rc = str(ReasonCode.GURU_INSTRUMENT_NOT_IN_CACHE)
                self._strategy.log.error(
                    f"event={ReasonCode.GURU_INSTRUMENT_NOT_IN_CACHE} "
                    f"component=nautilus_guru_exec correlation_id={intent.correlation_id} "
                    f"detail=instrument_not_in_cache instrument_id={instrument_id}",
                )
                self._emit(
                    "execution_outcome",
                    {
                        "correlation_id": intent.correlation_id,
                        "outcome": "error",
                        "reason_code": rc,
                        "instrument_id": str(instrument_id) if instrument_id else "",
                        "submitted_qty": 0.0,
                        "submitted_price": 0.0,
                    },
                )
                return
            if self._dynamic is not None and dyn_fail is not None:
                rc_e = (
                    ReasonCode.GURU_DYNAMIC_ACTIVATION_CAP
                    if dyn_fail == "activation_cap"
                    else ReasonCode.GURU_DYNAMIC_RESOLVE_FAILED
                )
                self._strategy.log.error(
                    f"event={rc_e} component=nautilus_guru_exec correlation_id={intent.correlation_id} "
                    f"token_id={tid} detail=dynamic_path failure={dyn_fail}",
                )
                self._emit(
                    "execution_outcome",
                    {
                        "correlation_id": intent.correlation_id,
                        "outcome": "error",
                        "reason_code": str(rc_e),
                        "instrument_id": "",
                        "submitted_qty": 0.0,
                        "submitted_price": 0.0,
                    },
                )
                return
            self._strategy.log.error(
                f"event={ReasonCode.GURU_INSTRUMENT_UNMAPPED} component=nautilus_guru_exec "
                f"correlation_id={intent.correlation_id} detail=no_instrument_for_token "
                f"token_id={tid}",
            )
            self._emit(
                "execution_outcome",
                {
                    "correlation_id": intent.correlation_id,
                    "outcome": "error",
                    "reason_code": str(ReasonCode.GURU_INSTRUMENT_UNMAPPED),
                    "instrument_id": "",
                    "submitted_qty": 0.0,
                    "submitted_price": 0.0,
                },
            )
            return

        assert instrument_id is not None and inst is not None

        r = self._runtime
        c3_shape = r.execution_entry_guard_enabled or r.execution_book_depth_clip_enabled
        if c3_shape:
            qty, price, skip, skip_rc = self._c3_shape_prepare(
                intent,
                inst=inst,
                instrument_id=instrument_id,
                side_u=side_u,
                qty=qty,
                price=price,
                approved_qty=approved_qty,
            )
            if skip:
                self._emit(
                    "execution_outcome",
                    {
                        "correlation_id": intent.correlation_id,
                        "outcome": "skip",
                        "stage": "pre_submit_book",
                        "reason_code": skip_rc or "c3_skip",
                        "instrument_id": str(instrument_id),
                        "submitted_qty": float(qty),
                        "submitted_price": float(price),
                    },
                )
                self._emit(
                    "normalization",
                    {
                        "correlation_id": intent.correlation_id,
                        "skipped_submit": True,
                        "reason_code": skip_rc or "c3_skip",
                        "pre_qty": float(approved_qty),
                        "post_qty": float(qty),
                        "pre_price": float(intent.price_ref or 0.0),
                        "post_price": float(price),
                    },
                )
                return

        pre_quant_q = float(qty)
        pre_quant_p = float(price)
        qres = quantize_limit_order_for_instrument(
            inst,
            side=side_u,
            price=pre_quant_p,
            quantity=pre_quant_q,
        )
        if not qres.ok:
            self._strategy.log.info(
                f"event={ReasonCode.EXEC_INSTRUMENT_QUANTIZE_SKIP} component=nautilus_guru_exec "
                f"correlation_id={intent.correlation_id} detail={qres.detail}",
            )
            self._emit(
                "execution_outcome",
                {
                    "correlation_id": intent.correlation_id,
                    "outcome": "skip",
                    "stage": "instrument_quantize",
                    "reason_code": str(ReasonCode.EXEC_INSTRUMENT_QUANTIZE_SKIP),
                    "instrument_id": str(instrument_id),
                    "submitted_qty": float(qres.quantity),
                    "submitted_price": float(qres.price),
                    "quantize_detail": qres.detail,
                },
            )
            self._emit(
                "normalization",
                {
                    "correlation_id": intent.correlation_id,
                    "skipped_submit": True,
                    "reason_code": str(ReasonCode.EXEC_INSTRUMENT_QUANTIZE_SKIP),
                    "pre_qty": pre_quant_q,
                    "post_qty": float(qres.quantity),
                    "pre_price": pre_quant_p,
                    "post_price": float(qres.price),
                    "quantize_detail": qres.detail,
                },
            )
            return

        qty, price = qres.quantity, qres.price
        quant_changed = abs(qty - pre_quant_q) > 1e-9 or abs(price - pre_quant_p) > 1e-9

        side = OrderSide.BUY if side_u == "BUY" else OrderSide.SELL
        coid = _client_order_id_from_guru_correlation(intent.correlation_id)
        order = self._strategy.order_factory.limit(
            instrument_id=instrument_id,
            order_side=side,
            quantity=inst.make_qty(Decimal(str(qty))),
            price=inst.make_price(Decimal(str(price))),
            time_in_force=TimeInForce.GTC,
            client_order_id=coid,
            tags=[_guru_tag(intent.correlation_id)],
        )
        self._strategy.submit_order(order, client_id=POLYMARKET_CLIENT_ID)
        self._strategy.log.info(
            f"event={ReasonCode.LIVE_ORDER_SUBMIT} component=nautilus_guru_exec "
            f"correlation_id={intent.correlation_id} client_order_id={order.client_order_id} "
            f"instrument_id={instrument_id} side={side_u} qty={qty} price={price} "
            f"reference_price={intent.price_ref} approved_qty={approved_qty}",
        )
        coid_s = str(order.client_order_id)
        reg = self._order_registry
        if reg is not None:
            reg.register(coid_s, intent.correlation_id)
        self._emit(
            "order_correlation_map",
            {
                "correlation_id": intent.correlation_id,
                "client_order_id": coid_s,
                "instrument_id": str(instrument_id),
            },
        )
        self._emit(
            "execution_outcome",
            {
                "correlation_id": intent.correlation_id,
                "outcome": "submit",
                "stage": "framework_submit",
                "reason_code": str(ReasonCode.LIVE_ORDER_SUBMIT),
                "client_order_id": coid_s,
                "instrument_id": str(instrument_id),
                "submitted_qty": float(qty),
                "submitted_price": float(price),
                "approved_qty": float(approved_qty),
                "risk_approved_not_success": True,
            },
        )
        if quant_changed:
            self._emit(
                "normalization",
                {
                    "correlation_id": intent.correlation_id,
                    "skipped_submit": False,
                    "kind": "instrument_quantize",
                    "reason_code": "",
                    "pre_qty": pre_quant_q,
                    "post_qty": float(qty),
                    "pre_price": pre_quant_p,
                    "post_price": float(price),
                },
            )

        self._reconcile_submit_vs_cache(
            intent=intent,
            coid=order.client_order_id,
            coid_s=coid_s,
            expected_qty=float(qty),
            expected_price=float(price),
        )

        if r.execution_limit_timeout_enabled and r.execution_limit_timeout_seconds > 0:
            self._schedule_limit_cancel(order.client_order_id, intent.correlation_id)
