"""
Copy strategy: guru bus → signal policies → sizing → execution port (shadow by default).

`OrderIntent` is translated in `execution/` (`ExecutionPort` implementations).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from nautilus_trader.model.events.order import OrderDenied, OrderEvent
from nautilus_trader.trading.config import StrategyConfig

from tyrex_pm.config.loaders import LayerAFiltersSettings, _default_layer_a_filters
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal, OrderIntent
from tyrex_pm.data.guru_monitor import GURU_TRADE_TOPIC
from tyrex_pm.execution.port import ExecutionPort, NoOpExecutionPort
from tyrex_pm.reporting.correlation_registry import OrderCorrelationRegistry
from tyrex_pm.reporting.order_events import emit_order_event_facts
from tyrex_pm.reporting.position_sample import emit_position_snapshot
from tyrex_pm.risk.policy import RiskPolicy, ShadowAllPassRisk
from tyrex_pm.signal.layer_a import build_layer_a_orchestrator
from tyrex_pm.signal.layer_a.orchestrator import LayerAOrchestrator
from tyrex_pm.signal.layer_a.types import LayerAContext, LayerAOutcome, LayerAStepRecord
from tyrex_pm.signal.sizing import SizingPolicy, build_sizing_policy
from tyrex_pm.signal.token_filter_spec import TokenFilterSpec
from tyrex_pm.strategy.base import BaseComposableStrategy

FactEmitFn = Callable[[str, dict[str, Any]], None]


class CopyStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    """
    Token gate: set ``token_filter_enabled`` and ``allowlisted_token_ids`` per strategy YAML
    ``token_filter`` block (see loaders). Risk / execution are unchanged.
    """

    token_filter_enabled: bool = False
    allowlisted_token_ids: tuple[str, ...] = ()
    execution_mode: str = "shadow"
    copy_scale: float = 1.0
    conviction_sizing_enabled: bool = False
    conviction_sizing_cap: float = 2.0
    conviction_sizing_lookback_trades: int = 20
    #: Optional Layer A filters; ``None`` → defaults (all off).
    layer_a: LayerAFiltersSettings | None = None


class CopyStrategy(BaseComposableStrategy):
    """
    Subscribes to `GURU_TRADE_TOPIC`; runs entry (BUY) / exit (SELL) branches.
    """

    def __init__(self, config: CopyStrategyConfig) -> None:
        super().__init__(config)
        self._cfg = config
        tokens = TokenFilterSpec(
            enabled=config.token_filter_enabled,
            allowlisted=frozenset(config.allowlisted_token_ids),
        )
        layer_a_cfg = config.layer_a if config.layer_a is not None else _default_layer_a_filters()
        self._orchestrator: LayerAOrchestrator = build_layer_a_orchestrator(layer_a_cfg, tokens)
        self._layer_a_ctx: LayerAContext | None = None
        self._sizing: SizingPolicy = build_sizing_policy(
            copy_scale=config.copy_scale,
            conviction_sizing_enabled=config.conviction_sizing_enabled,
            conviction_sizing_lookback_trades=config.conviction_sizing_lookback_trades,
            conviction_sizing_cap=config.conviction_sizing_cap,
        )
        self._risk: RiskPolicy = ShadowAllPassRisk()
        self._execution: ExecutionPort = NoOpExecutionPort()
        self._reporting_emit: FactEmitFn | None = None
        self._order_registry: OrderCorrelationRegistry | None = None
        self._reporting_run_id: str | None = None
        self._position_reader: Any | None = None

    def set_risk_policy(self, policy: RiskPolicy) -> None:
        """Inject real `RiskPolicy` in v1.06+ tests or runtime."""
        self._risk = policy

    def set_execution_port(self, port: ExecutionPort) -> None:
        """Tests or runtime wiring may inject a custom port."""
        self._execution = port

    def set_reporting_emit(self, emit: FactEmitFn | None) -> None:
        self._reporting_emit = emit

    def set_order_correlation_registry(self, registry: OrderCorrelationRegistry | None) -> None:
        self._order_registry = registry

    def set_reporting_run_id(self, run_id: str | None) -> None:
        self._reporting_run_id = run_id

    def set_position_reporting_reader(self, reader: Any | None) -> None:
        """Optional position reader for ``position`` facts (framework path)."""
        self._position_reader = reader

    def set_layer_a_context(self, ctx: LayerAContext | None) -> None:
        """Inject runtime follower-position reader for ``full_exit`` (live/shadow Nautilus)."""
        self._layer_a_ctx = ctx

    def on_start(self) -> None:
        super().on_start()
        self.msgbus.subscribe(topic=GURU_TRADE_TOPIC, handler=self._on_guru_trade)

    def on_order_event(self, event: OrderEvent) -> None:
        super().on_order_event(event)
        re = self._reporting_emit
        rid = self._reporting_run_id
        reg = self._order_registry
        if re is not None and rid is not None and reg is not None:

            def _lookup(coid: str) -> str | None:
                return reg.correlation_for(coid)

            emit_order_event_facts(
                event,
                run_id=rid,
                correlation_lookup=_lookup,
                emit=re,
            )
            if isinstance(event, OrderDenied):
                coid_ev = getattr(event, "client_order_id", None)
                coid_s = str(
                    getattr(coid_ev, "value", coid_ev) if coid_ev is not None else "",
                )
                corr_d = reg.correlation_for(coid_s) if reg is not None and coid_s else None
                emit_cap = getattr(self._risk, "emit_capital_observation", None)
                if callable(emit_cap):
                    emit_cap("order_denied", correlation_id=corr_d, intent=None)
        pr = self._position_reader
        if re is not None and pr is not None and rid is not None:
            ins = getattr(event, "instrument_id", None)
            inst_s = str(ins) if ins is not None else ""
            if inst_s:
                coid_ev = getattr(event, "client_order_id", None)
                coid_s = str(coid_ev) if coid_ev is not None else ""
                corr = reg.correlation_for(coid_s) if reg is not None and coid_s else None
                token_id: str | None = None
                mark: float | None = None
                try:
                    from nautilus_trader.adapters.polymarket.common.symbol import (
                        get_polymarket_token_id,
                    )
                    from nautilus_trader.model.enums import PriceType
                    from nautilus_trader.model.identifiers import InstrumentId

                    iid = InstrumentId.from_str(inst_s)
                    token_id = str(get_polymarket_token_id(iid))
                    px = self.cache.price(iid, price_type=PriceType.LAST)
                    if px is not None:
                        if hasattr(px, "as_double"):
                            mark = float(px.as_double())
                        elif hasattr(px, "as_decimal"):
                            mark = float(px.as_decimal())  # type: ignore[arg-type]
                        else:
                            mark = float(px)
                except Exception:  # noqa: BLE001
                    token_id = None
                    mark = None
                emit_position_snapshot(
                    pr,
                    instrument_id_str=inst_s,
                    token_id=token_id,
                    mark_price=mark,
                    correlation_id=corr,
                    trigger="order_event",
                    emit=re,
                )
        notify = getattr(self._execution, "notify_order_event", None)
        if callable(notify):
            notify(event)

    def _on_guru_trade(self, msg: object) -> None:
        if not isinstance(msg, GuruTradeSignal):
            return

        ts_signal_ms = int(time.time() * 1000)
        if msg.side == "BUY":
            branch = "entry"
        elif msg.side == "SELL":
            branch = "exit"
        else:
            self.log.warning(
                "event=copy_skip "
                f"component=copy_strategy correlation_id={msg.source_trade_id} "
                f"reason_code={ReasonCode.UNSUPPORTED_SIDE} side={msg.side}"
            )
            return

        outcome, steps = self._orchestrator.run(
            msg,
            branch=branch,
            ctx=self._layer_a_ctx,
        )
        self._emit_layer_a_filter_facts(msg.source_trade_id, steps)
        if not outcome.accept:
            self._log_layer_a_skip(msg.source_trade_id, outcome)
            self._emit_strategy_decision_skip(msg.source_trade_id, branch, outcome.reason_code)
            return

        self._handle_branch(
            msg,
            outcome,
            branch,
            ts_signal_ms=ts_signal_ms,
        )

    def _adjust_intent_before_risk(
        self,
        intent: OrderIntent,
        *,
        signal: GuruTradeSignal,
        branch: str,
    ) -> OrderIntent:
        """
        Hook for subclasses (e.g. Scenario A harness) to adjust ``price_ref`` before risk.
        Default: no change — production behavior unchanged.
        """
        _ = signal
        _ = branch
        return intent

    def _emit_layer_a_filter_facts(
        self,
        correlation_id: str,
        steps: list[LayerAStepRecord],
    ) -> None:
        em = self._reporting_emit
        if em is None:
            return
        for s in steps:
            em("layer_a_filter", s.as_fact_payload(correlation_id))

    def _log_layer_a_skip(self, correlation_id: str, outcome: LayerAOutcome) -> None:
        self.log.info(
            "event=copy_skip "
            "component=copy_strategy "
            f"correlation_id={correlation_id} "
            f"reason_code={outcome.reason_code} "
            f"detail={outcome.detail or ''}",
        )

    def _emit_strategy_decision_skip(
        self,
        correlation_id: str,
        branch: str,
        reason_code: str,
    ) -> None:
        em = self._reporting_emit
        if em is not None:
            em(
                "strategy_decision",
                {
                    "correlation_id": correlation_id,
                    "branch": branch,
                    "decision": "skip",
                    "reason_code": str(reason_code),
                },
            )

    def _handle_branch(
        self,
        sig: GuruTradeSignal,
        outcome: LayerAOutcome,
        kind: str,
        *,
        ts_signal_ms: int,
    ) -> None:
        em = self._reporting_emit
        cid = sig.source_trade_id
        decision_reason = outcome.reason_code

        exit_mode = str(outcome.metadata.get("exit_qty_mode", "mirror_guru"))
        if kind == "exit" and exit_mode == "full_position":
            qty = float(outcome.metadata["follower_position_qty"])
        else:
            qty = self._sizing.size(sig, branch=kind)
        if kind == "entry":
            self._sizing.record_accepted_entry_size(sig)
        if qty <= 0:
            self.log.info(
                "event=copy_skip "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"reason_code={ReasonCode.COPY_SKIP} detail=zero_qty"
            )
            if em is not None:
                em(
                    "strategy_decision",
                    {
                        "correlation_id": cid,
                        "branch": kind,
                        "decision": "skip",
                        "reason_code": str(ReasonCode.COPY_SKIP),
                    },
                )
            return

        m = self._sizing.entry_metrics_after_last_size()

        if kind == "entry" and self._cfg.conviction_sizing_enabled:
            self.log.debug(
                "event=copy_conviction_diag component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"base_scale={m.get('base_scale')} effective_scale={m.get('effective_scale')} "
                f"conviction_ratio={m.get('conviction_ratio')} "
                f"guru_size_raw={m.get('guru_size_raw')} "
                f"rolling_avg_guru_size={m.get('rolling_avg_guru_size')} qty={qty}",
            )

        if em is not None:
            em(
                "strategy_decision",
                {
                    "correlation_id": cid,
                    "branch": kind,
                    "decision": "accept",
                    "reason_code": str(decision_reason),
                },
            )
            em(
                "sizing",
                {
                    "correlation_id": cid,
                    "target_qty": float(qty),
                    "signal_branch": kind,
                    "base_scale": m.get("base_scale"),
                    "effective_scale": m.get("effective_scale"),
                    "conviction_ratio": m.get("conviction_ratio"),
                    "guru_size_raw": m.get("guru_size_raw"),
                    "rolling_avg_guru_size": m.get("rolling_avg_guru_size"),
                    "target_notional_usd": float(sig.price_raw) * float(qty)
                    if sig.price_raw is not None
                    else None,
                },
            )

        intent = OrderIntent(
            correlation_id=sig.source_trade_id,
            token_id=str(sig.token_id),
            side=sig.side,
            quantity=qty,
            signal_kind=kind,
            reason_code=str(decision_reason),
            price_ref=sig.price_raw,
        )
        intent = self._adjust_intent_before_risk(intent, signal=sig, branch=kind)
        approved, risk_rc, intent_risk = self._risk.evaluate(intent)
        if not approved or intent_risk is None:
            self.log.info(
                "event=copy_skip "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"reason_code=risk_denied risk_detail={risk_rc}"
            )
            return

        if em is not None:
            ts_risk_ms = int(time.time() * 1000)
            em(
                "execution_intent",
                {
                    "correlation_id": cid,
                    "token_id": str(sig.token_id),
                    "side": sig.side,
                    "quantity": float(intent_risk.quantity),
                    "quantity_strategy_sized": float(qty),
                    "signal_kind": kind,
                    "price_ref": intent_risk.price_ref,
                    "ts_risk_approved_ms": ts_risk_ms,
                },
            )

        self._execution.submit_intent(intent_risk, mode=self._cfg.execution_mode)
        emit_cap = getattr(self._risk, "emit_capital_observation", None)
        if em is not None and callable(emit_cap):
            emit_cap("submit", correlation_id=cid, intent=intent_risk)
        ts_submit_ms = int(time.time() * 1000)
        det_to_submit = ts_submit_ms - sig.ts_event_ms
        sig_to_submit = ts_submit_ms - ts_signal_ms
        latency_kv = (
            f" ts_event_ms={sig.ts_event_ms} ts_signal_received_ms={ts_signal_ms} "
            f"ts_submit_ms={ts_submit_ms} detection_to_submit_ms={det_to_submit} "
            f"signal_to_submit_ms={sig_to_submit}"
        )
        if self._cfg.execution_mode == "shadow":
            self.log.info(
                "event=shadow_order_intent "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"reason_code={ReasonCode.SHADOW_ORDER_INTENT} "
                f"signal_kind={kind} side={sig.side} qty={intent_risk.quantity} strategy_qty={qty}"
                f"{latency_kv}"
            )
        else:
            self.log.info(
                "event=live_order_intent "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"signal_kind={kind} side={sig.side} qty={intent_risk.quantity} strategy_qty={qty}"
                f"{latency_kv}"
            )
