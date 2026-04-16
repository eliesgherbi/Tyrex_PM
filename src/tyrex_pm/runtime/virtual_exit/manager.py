"""Virtual TP/SL manager — Tier A/B reconciliation, triggers, risk+execution path."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from nautilus_trader.model.events.order import (
    OrderCanceled,
    OrderDenied,
    OrderEvent,
    OrderFilled,
    OrderRejected,
)
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.config.loaders import (
    RiskSettings,
    RuntimeSettings,
    VirtualExitRuntimeSettings,
    VirtualExitStrategySettings,
)
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.execution.nautilus_guru_exec import virtual_exit_client_order_id_value
from tyrex_pm.runtime.virtual_exit.lot import LOT_TERMINAL_STATES, ProtectedLot
from tyrex_pm.runtime.virtual_exit.store import VirtualExitStore

if TYPE_CHECKING:
    from tyrex_pm.risk.policy import RiskPolicy
    from tyrex_pm.strategy.copy_strategy import CopyStrategy

_LOG = logging.getLogger(__name__)
_QTY_EPS = 1e-9


def _coid_str(event: OrderEvent) -> str:
    c = getattr(event, "client_order_id", None)
    if c is None:
        return ""
    return str(getattr(c, "value", c))


def _fill_qty_px(event: OrderFilled) -> tuple[float, float]:
    lq = getattr(event, "last_qty", None)
    lp = getattr(event, "last_px", None)
    q = float(lq.as_decimal()) if lq is not None and hasattr(lq, "as_decimal") else float(lq or 0)
    p = float(lp.as_decimal()) if lp is not None and hasattr(lp, "as_decimal") else float(lp or 0)
    return q, p


class VirtualExitManager:
    """
    Post-fill monitor: arms on guru BUY fills, evaluates TP/SL, submits SELL via
    :meth:`tyrex_pm.execution.nautilus_guru_exec.NautilusGuruExecutionPort.submit_virtual_exit_intent`
    after :meth:`tyrex_pm.risk.configured.ConfiguredRiskPolicy.evaluate`.
    """

    __slots__ = (
        "_strategy",
        "_ve_st",
        "_ve_rt",
        "_store",
        "_venue_state",
        "_risk",
        "_execution",
        "_emit",
        "_wallet_sync_ok",
        "_venue_cash_ok",
        "_lifecycle",
        "_risk_settings",
        "_lots",
        "_timer_name",
        "_loaded",
        "_runtime",
    )

    def __init__(
        self,
        strategy: CopyStrategy,
        *,
        ve_strategy: VirtualExitStrategySettings,
        ve_runtime: VirtualExitRuntimeSettings,
        runtime: RuntimeSettings,
        store: VirtualExitStore,
        venue_state: Any | None,
        risk: RiskPolicy,
        execution: Any,
        emit: Callable[[str, dict[str, Any]], None] | None,
        wallet_sync_ready: Callable[[], bool],
        venue_cash_ready: Callable[[], bool],
        lifecycle: Any,
        risk_settings: RiskSettings,
    ) -> None:
        self._strategy = strategy
        self._ve_st = ve_strategy
        self._ve_rt = ve_runtime
        self._runtime = runtime
        self._store = store
        self._venue_state = venue_state
        self._risk = risk
        self._execution = execution
        self._emit = emit
        self._wallet_sync_ok = wallet_sync_ready
        self._venue_cash_ok = venue_cash_ready
        self._lifecycle = lifecycle
        self._risk_settings = risk_settings
        self._lots: list[ProtectedLot] = []
        self._timer_name = "tyrex_virtual_exit_eval"
        self._loaded = False

    @property
    def enabled(self) -> bool:
        return bool(self._ve_st.enabled)

    def on_strategy_start(self) -> None:
        if not self.enabled:
            return
        self._load_once()
        try:
            self._strategy.clock.set_timer(
                name=self._timer_name,
                interval=timedelta(seconds=float(self._ve_rt.evaluate_interval_seconds)),
                callback=self._on_timer,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("event=virtual_exit_timer_failed err=%s", exc)

    def on_strategy_stop(self) -> None:
        try:
            self._strategy.clock.cancel_timer(self._timer_name)
        except Exception:  # noqa: BLE001
            pass
        self._persist()

    def _load_once(self) -> None:
        if self._loaded:
            return
        self._lots = self._store.load_lots()
        self._recover_inflight_exits()
        for lot in self._lots:
            if lot.state in ("ARMED", "EXIT_PARTIAL") and lot.armed_at_ts_ms is None:
                base = lot.updated_ts_ms or lot.created_ts_ms
                if base > 0:
                    lot.armed_at_ts_ms = base
        self._loaded = True
        self._emit_fact(
            "virtual_exit_recovery",
            {"action": "load_store", "lot_count": len(self._lots)},
        )

    def _recover_inflight_exits(self) -> None:
        """Tier B: reconcile persisted exit COIDs against Cache (restart safety)."""
        for lot in self._lots:
            if lot.state in LOT_TERMINAL_STATES:
                continue
            coid = lot.exit_client_order_id
            if not coid:
                continue
            try:
                from nautilus_trader.model.identifiers import ClientOrderId

                o = self._strategy.cache.order(ClientOrderId(coid))
            except Exception:  # noqa: BLE001
                o = None
            if o is None or o.is_closed:
                lot.exit_client_order_id = None
                lot.exit_kind = None
                if lot.qty_open <= _QTY_EPS:
                    lot.state = "COMPLETED"
                elif lot.state in ("EXIT_SUBMITTED", "TRIGGERED_TP", "TRIGGERED_SL", "EXIT_PARTIAL"):
                    lot.state = "ARMED"
                lot.updated_ts_ms = int(time.time() * 1000)
                self._emit_fact(
                    "virtual_exit_recovery",
                    {
                        "lot_id": lot.lot_id,
                        "action": "clear_stale_exit_coid",
                        "detail": "cache_closed_or_missing",
                    },
                )
            else:
                lot.recovery_seen_open_exit = True

    def _persist(self) -> None:
        try:
            self._store.save_lots(self._lots)
        except OSError as exc:
            _LOG.error("event=virtual_exit_persist_failed err=%s", exc)

    def _emit_fact(self, fact_type: str, payload: dict[str, Any]) -> None:
        fe = self._emit
        if fe is not None:
            fe(fact_type, payload)

    def _execution_mode(self) -> str:
        return str(getattr(self._strategy, "_cfg").execution_mode)

    def register_pending_entry(
        self,
        *,
        client_order_id: str,
        guru_correlation_id: str,
        token_id: str,
    ) -> None:
        if not self.enabled or self._execution_mode() != "live":
            return
        if not self._ve_st.adopt_existing_positions:
            pass  # v1 default path
        now = int(time.time() * 1000)
        lot = ProtectedLot(
            lot_id=str(uuid.uuid4()),
            instrument_id="",
            token_id=str(token_id),
            entry_guru_correlation_id=guru_correlation_id,
            entry_client_order_id=client_order_id,
            state="PENDING_ENTRY",
            created_ts_ms=now,
            updated_ts_ms=now,
        )
        self._lots.append(lot)
        self._persist()
        self._emit_fact(
            "virtual_exit_arm",
            {
                "phase": "pending_entry",
                "lot_id": lot.lot_id,
                "entry_client_order_id": client_order_id,
                "token_id": token_id,
                "guru_correlation_id": guru_correlation_id,
            },
        )

    def _find_by_entry_coid(self, coid: str) -> ProtectedLot | None:
        for lot in self._lots:
            if lot.entry_client_order_id == coid and lot.state not in LOT_TERMINAL_STATES:
                return lot
        return None

    def _find_by_exit_coid(self, coid: str) -> ProtectedLot | None:
        for lot in self._lots:
            if lot.exit_client_order_id == coid and lot.state not in LOT_TERMINAL_STATES:
                return lot
        return None

    def _ready_to_trade_sell(self) -> bool:
        if self._execution_mode() != "live":
            return False
        lc = self._lifecycle
        rs = self._risk_settings
        if lc is None or rs is None:
            return False
        br = lc.block_reason_for_side("SELL", risk=rs)
        if br is not None:
            return False
        if self._venue_state is not None:
            if not self._wallet_sync_ok():
                return False
            if not self._venue_cash_ok():
                return False
        return True

    def _venue_stale_hold(self) -> bool:
        vs = self._venue_state
        if vs is None:
            return False
        if self._ve_rt.max_venue_staleness_seconds <= 0:
            return False
        try:
            if vs.is_stale():
                return True
        except Exception:  # noqa: BLE001
            return True
        return False

    def _tier_a_long_qty(self, instrument_id: InstrumentId) -> float | None:
        vs = self._venue_state
        if vs is None:
            return None
        try:
            sz = vs.position_size(instrument_id)
            if sz is None:
                return 0.0
            return max(0.0, float(sz))
        except Exception:  # noqa: BLE001
            return None

    def _apply_drift(self, lot: ProtectedLot, iid: InstrumentId) -> bool:
        """Returns True if lot was disarmed/terminated."""
        tier = self._tier_a_long_qty(iid)
        if tier is None:
            return False
        if tier <= _QTY_EPS and lot.qty_open > _QTY_EPS:
            grace_s = float(self._ve_rt.tier_a_flat_disarm_grace_seconds)
            if grace_s > 0 and lot.armed_at_ts_ms is not None and lot.armed_at_ts_ms > 0:
                now_ms = int(time.time() * 1000)
                if now_ms - lot.armed_at_ts_ms < int(grace_s * 1000):
                    return False
            lot.state = "DISARMED_EXTERNAL_FLAT"
            lot.updated_ts_ms = int(time.time() * 1000)
            self._emit_fact(
                "virtual_exit_disarm",
                {"lot_id": lot.lot_id, "reason": "tier_a_flat", "token_id": lot.token_id},
            )
            return True
        if lot.qty_open > tier + _QTY_EPS:
            if self._ve_rt.drift_policy == "disarm":
                lot.state = "DISARMED_DRIFT"
                lot.updated_ts_ms = int(time.time() * 1000)
                self._emit_fact(
                    "virtual_exit_disarm",
                    {"lot_id": lot.lot_id, "reason": "drift_disarm", "tier_a_qty": tier},
                )
                return True
            lot.qty_open = max(0.0, tier)
            lot.updated_ts_ms = int(time.time() * 1000)
            self._emit_fact(
                "virtual_exit_reconcile",
                {
                    "lot_id": lot.lot_id,
                    "reason": "clamp_to_venue",
                    "after_qty": lot.qty_open,
                    "tier_a_qty": tier,
                },
            )
        return False

    def _fix_clob_for_price(self) -> Any:
        execp = self._execution
        if hasattr(execp, "_rest_clob") and execp._rest_clob is not None:
            return execp._rest_clob
        if self._ve_rt.execution_book_rest_for_triggers:
            try:
                from tyrex_pm.runtime.clob_factory import build_clob_client_from_env

                clob = build_clob_client_from_env(self._runtime)
                if hasattr(execp, "_rest_clob"):
                    setattr(execp, "_rest_clob", clob)
                return clob
            except Exception:  # noqa: BLE001
                return None
        return None

    def _executable_price_resolved(self, iid: InstrumentId, token_id: str) -> float | None:
        from tyrex_pm.execution.c3_book_top import resolve_book_top

        src = self._ve_rt.trigger_price_source
        clob2 = self._fix_clob_for_price()
        use_rest = bool(self._ve_rt.execution_book_rest_for_triggers and clob2 is not None)
        if src == "book_bid":
            book = resolve_book_top(
                cache=self._strategy.cache,
                instrument_id=iid,
                token_id=token_id,
                rest_enabled=use_rest,
                clob=clob2,
            )
            if book is not None and book.best_bid is not None:
                return float(book.best_bid)
        from nautilus_trader.model.enums import PriceType

        px = self._strategy.cache.price(iid, price_type=PriceType.LAST)
        if px is None:
            return None
        if hasattr(px, "as_double"):
            return float(px.as_double())
        if hasattr(px, "as_decimal"):
            return float(px.as_decimal())
        return float(px)

    def _try_arm_if_ready(self, lot: ProtectedLot) -> None:
        if lot.state != "PENDING_ENTRY":
            return
        if lot.entry_qty_filled < self._ve_rt.min_entry_qty_to_arm - _QTY_EPS:
            return
        if not self._ready_to_trade_sell():
            return
        lot.tp_pct = float(self._ve_st.take_profit_pct)
        lot.sl_pct = float(self._ve_st.stop_loss_pct)
        vwap = lot.entry_vwap
        lot.tp_trigger_price = vwap * (1.0 + lot.tp_pct / 100.0)
        lot.sl_trigger_price = vwap * (1.0 - lot.sl_pct / 100.0)
        lot.tp_armed = True
        lot.sl_armed = True
        lot.state = "ARMED"
        now_ms = int(time.time() * 1000)
        lot.armed_at_ts_ms = now_ms
        lot.updated_ts_ms = now_ms
        self._emit_fact(
            "virtual_exit_arm",
            {
                "lot_id": lot.lot_id,
                "instrument_id": lot.instrument_id,
                "token_id": lot.token_id,
                "entry_qty_filled": lot.entry_qty_filled,
                "entry_vwap": lot.entry_vwap,
                "tp_trigger_price": lot.tp_trigger_price,
                "sl_trigger_price": lot.sl_trigger_price,
                "guru_correlation_id": lot.entry_guru_correlation_id,
            },
        )

    def on_order_event(self, event: OrderEvent) -> None:
        if not self.enabled:
            return
        self._load_once()
        coid = _coid_str(event)
        if not coid:
            return

        if isinstance(event, OrderFilled):
            self._on_filled(event, coid)
            return

        if isinstance(event, (OrderCanceled, OrderRejected, OrderDenied)):
            lot = self._find_by_exit_coid(coid)
            if lot is None:
                return
            prev_kind = lot.exit_kind
            lot.exit_client_order_id = None
            lot.exit_kind = None
            if lot.state not in ("COMPLETED",):
                lot.state = "ARMED"
            lot.exit_attempts += 1
            lot.updated_ts_ms = int(time.time() * 1000)
            self._emit_fact(
                "virtual_exit_retry",
                {
                    "lot_id": lot.lot_id,
                    "reason": type(event).__name__,
                    "attempt": lot.exit_attempts,
                },
            )
            self._persist()
            if (
                isinstance(event, OrderRejected)
                and prev_kind == "sl"
                and lot.last_exit_was_market
                and self._ve_rt.market_sl_fallback_to_limit
                and not lot.sl_limit_fallback_used
            ):
                lot.sl_limit_fallback_used = True
                self._submit_exit(lot, "sl", force_style="aggressive_limit")
                self._persist()

    def _on_filled(self, event: OrderFilled, coid: str) -> None:
        lot_e = self._find_by_exit_coid(coid)
        if lot_e is not None:
            lq, lpx = _fill_qty_px(event)
            lot_e.qty_open = max(0.0, lot_e.qty_open - lq)
            lot_e.updated_ts_ms = int(time.time() * 1000)
            if lot_e.qty_open <= _QTY_EPS:
                lot_e.state = "COMPLETED"
                lot_e.exit_client_order_id = None
                lot_e.exit_kind = None
            else:
                lot_e.state = "EXIT_PARTIAL"
                lot_e.exit_client_order_id = None
                lot_e.exit_kind = None
                lot_e.tp_armed = True
                lot_e.sl_armed = True
            self._emit_fact(
                "virtual_exit_reconcile",
                {
                    "lot_id": lot_e.lot_id,
                    "reason": "exit_fill",
                    "qty_open": lot_e.qty_open,
                    "last_qty": lq,
                },
            )
            self._persist()
            return

        lot_p = self._find_by_entry_coid(coid)
        if lot_p is None:
            return
        inst = getattr(event, "instrument_id", None)
        if inst is None:
            return
        iid_s = str(inst)
        lot_p.instrument_id = iid_s
        lq, lpx = _fill_qty_px(event)
        if lq <= 0:
            return
        old_q = lot_p.entry_qty_filled
        old_v = lot_p.entry_vwap
        new_q = old_q + lq
        if new_q > _QTY_EPS:
            lot_p.entry_vwap = (old_v * old_q + lpx * lq) / new_q if old_q > _QTY_EPS else lpx
        lot_p.entry_qty_filled = new_q
        lot_p.qty_open = new_q
        lot_p.updated_ts_ms = int(time.time() * 1000)

        self._try_arm_if_ready(lot_p)
        self._persist()

    def _on_timer(self, event: Any) -> None:
        _ = event
        if not self.enabled:
            return
        self._load_once()
        if not self._ready_to_trade_sell():
            return
        if self._venue_stale_hold():
            self._emit_fact("virtual_exit_hold", {"reason": "venue_stale"})
            return

        for lot in list(self._lots):
            if lot.state == "PENDING_ENTRY":
                self._try_arm_if_ready(lot)
        self._persist()

        for lot in list(self._lots):
            if lot.state in LOT_TERMINAL_STATES:
                continue
            if lot.state != "ARMED" and lot.state != "EXIT_PARTIAL":
                continue
            if lot.exit_client_order_id:
                continue
            if not lot.instrument_id:
                continue
            try:
                iid = InstrumentId.from_str(lot.instrument_id)
            except ValueError:
                continue

            if self._apply_drift(lot, iid):
                self._persist()
                continue

            if lot.qty_open <= _QTY_EPS:
                lot.state = "COMPLETED"
                self._persist()
                continue

            px = self._executable_price_resolved(iid, lot.token_id)
            if px is None:
                self._emit_fact("virtual_exit_hold", {"lot_id": lot.lot_id, "reason": "no_price"})
                continue

            fired: str | None = None
            if lot.tp_armed and lot.tp_trigger_price is not None and px >= lot.tp_trigger_price - _QTY_EPS:
                fired = "tp"
            elif lot.sl_armed and lot.sl_trigger_price is not None and px <= lot.sl_trigger_price + _QTY_EPS:
                fired = "sl"

            if fired is None:
                continue

            now_ms = int(time.time() * 1000)
            if lot.last_trigger_ts_ms is not None and now_ms - lot.last_trigger_ts_ms < int(
                self._ve_rt.exit_retry_cooldown_seconds * 1000,
            ):
                continue
            lot.last_trigger_ts_ms = now_ms

            lot.state = "TRIGGERED_TP" if fired == "tp" else "TRIGGERED_SL"
            lot.updated_ts_ms = now_ms
            self._emit_fact(
                "virtual_exit_trigger",
                {
                    "lot_id": lot.lot_id,
                    "kind": fired,
                    "executable_price": px,
                    "trigger_basis": self._ve_rt.trigger_price_source,
                },
            )
            self._submit_exit(lot, fired)
            self._persist()

    def _submit_exit(
        self,
        lot: ProtectedLot,
        kind: str,
        *,
        force_style: str | None = None,
    ) -> None:
        if lot.exit_attempts > self._ve_rt.exit_retry_max:
            lot.state = "FAILED"
            self._emit_fact("virtual_exit_disarm", {"lot_id": lot.lot_id, "reason": "max_retries"})
            return

        try:
            iid = InstrumentId.from_str(lot.instrument_id)
        except ValueError:
            return

        tier = self._tier_a_long_qty(iid)
        sell_qty = lot.qty_open
        if tier is not None:
            sell_qty = min(sell_qty, max(0.0, tier))
        if sell_qty <= _QTY_EPS:
            lot.state = "DISARMED_EXTERNAL_FLAT"
            return

        px_ref = self._executable_price_resolved(iid, lot.token_id)
        if px_ref is None:
            lot.state = "ARMED"
            return

        origin = "virtual_tp" if kind == "tp" else "virtual_sl"
        rc = ReasonCode.VIRTUAL_EXIT_TP if kind == "tp" else ReasonCode.VIRTUAL_EXIT_SL
        lot.exit_nonce += 1
        corr = f"ve:{lot.lot_id}:{kind}:n{lot.exit_nonce}"

        intent = OrderIntent(
            correlation_id=corr,
            token_id=lot.token_id,
            side="SELL",
            quantity=float(sell_qty),
            signal_kind="exit",
            reason_code=str(rc),
            price_ref=float(px_ref),
            intent_origin=origin,
            virtual_lot_id=lot.lot_id,
            virtual_exit_kind=kind,
        )

        approved, risk_rc, final = self._risk.evaluate(intent)
        if not approved or final is None:
            _LOG.info(
                "event=virtual_exit_risk_denied lot_id=%s detail=%s",
                lot.lot_id,
                risk_rc,
            )
            self._emit_fact(
                "virtual_exit_retry",
                {"lot_id": lot.lot_id, "reason": f"risk:{risk_rc}", "attempt": lot.exit_attempts},
            )
            lot.state = "ARMED"
            lot.exit_attempts += 1
            return

        style_tp = self._ve_rt.exit_take_profit_style
        style_sl = self._ve_rt.exit_stop_loss_style
        primary = (force_style or (style_tp if kind == "tp" else style_sl)).strip().lower()
        if primary not in ("market", "aggressive_limit"):
            primary = "aggressive_limit"

        fn = getattr(self._execution, "submit_virtual_exit_intent", None)
        if not callable(fn):
            lot.state = "FAILED"
            return

        lot.last_exit_was_market = primary == "market"
        fn(
            final,
            mode=self._execution_mode(),
            order_style=primary,
            aggression_ticks=int(self._ve_rt.aggressive_limit_ticks),
            use_rest_book=bool(self._ve_rt.execution_book_rest_for_triggers),
        )

        if kind == "tp":
            lot.sl_armed = False
        else:
            lot.tp_armed = False

        lot.exit_client_order_id = virtual_exit_client_order_id_value(corr)
        lot.exit_kind = kind
        lot.state = "EXIT_SUBMITTED"
        self._emit_fact(
            "virtual_exit_submit",
            {
                "lot_id": lot.lot_id,
                "kind": kind,
                "order_style": primary,
                "qty": float(final.quantity),
                "correlation_id": corr,
                "intent_origin": origin,
            },
        )
