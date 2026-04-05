"""
Copy strategy: guru bus → signal policies → sizing → execution port (shadow by default).

`OrderIntent` is translated in `execution/` (`ExecutionPort` implementations).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from nautilus_trader.model.events.order import OrderEvent
from nautilus_trader.trading.config import StrategyConfig

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal, OrderIntent
from tyrex_pm.data.guru_monitor import GURU_TRADE_TOPIC
from tyrex_pm.execution.port import ExecutionPort, NoOpExecutionPort
from tyrex_pm.risk.policy import RiskPolicy, ShadowAllPassRisk
from tyrex_pm.signal.entry import GuruFollowEntryPolicy, GuruMirrorExitPolicy, SignalDecision
from tyrex_pm.signal.follow_worthiness import FollowWorthinessGate
from tyrex_pm.signal.sizing import SizingPolicy, build_sizing_policy
from tyrex_pm.reporting.correlation_registry import OrderCorrelationRegistry
from tyrex_pm.reporting.order_events import emit_order_event_facts
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
    min_follow_notional_usd: float = 0.0


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
        self._entry = GuruFollowEntryPolicy(tokens)
        self._exit = GuruMirrorExitPolicy(tokens)
        self._sizing: SizingPolicy = build_sizing_policy(
            copy_scale=config.copy_scale,
            conviction_sizing_enabled=config.conviction_sizing_enabled,
            conviction_sizing_lookback_trades=config.conviction_sizing_lookback_trades,
            conviction_sizing_cap=config.conviction_sizing_cap,
        )
        self._worthiness = FollowWorthinessGate(config.min_follow_notional_usd)
        self._risk: RiskPolicy = ShadowAllPassRisk()
        self._execution: ExecutionPort = NoOpExecutionPort()
        self._reporting_emit: FactEmitFn | None = None
        self._order_registry: OrderCorrelationRegistry | None = None
        self._reporting_run_id: str | None = None

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
        notify = getattr(self._execution, "notify_order_event", None)
        if callable(notify):
            notify(event)

    def _on_guru_trade(self, msg: object) -> None:
        if not isinstance(msg, GuruTradeSignal):
            return

        ts_signal_ms = int(time.time() * 1000)
        if msg.side == "BUY":
            self._handle_branch(
                msg,
                self._entry.evaluate(msg),
                "entry",
                ts_signal_ms=ts_signal_ms,
            )
        elif msg.side == "SELL":
            self._handle_branch(
                msg,
                self._exit.evaluate(msg),
                "exit",
                ts_signal_ms=ts_signal_ms,
            )
        else:
            self.log.warning(
                "event=copy_skip "
                f"component=copy_strategy correlation_id={msg.source_trade_id} "
                f"reason_code={ReasonCode.UNSUPPORTED_SIDE} side={msg.side}"
            )

    def _handle_branch(
        self,
        sig: GuruTradeSignal,
        decision: SignalDecision,
        kind: str,
        *,
        ts_signal_ms: int,
    ) -> None:
        em = self._reporting_emit
        cid = sig.source_trade_id
        if not decision.accept:
            self.log.info(
                "event=copy_skip "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"reason_code={decision.reason_code} "
                f"detail={decision.detail or ''}"
            )
            if em is not None:
                em(
                    "strategy_decision",
                    {
                        "correlation_id": cid,
                        "branch": kind,
                        "decision": "skip",
                        "reason_code": str(decision.reason_code),
                    },
                )
            return

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
        ok_w, w_rc = self._worthiness.evaluate(price_ref=sig.price_raw, qty=qty)
        if not ok_w:
            est = (
                float(sig.price_raw) * float(qty)
                if sig.price_raw is not None
                else None
            )
            self.log.info(
                "event=copy_skip component=copy_strategy "
                f"correlation_id={sig.source_trade_id} reason_code={w_rc} "
                f"base_scale={m.get('base_scale')} effective_scale={m.get('effective_scale')} "
                f"guru_size_raw={m.get('guru_size_raw')} "
                f"rolling_avg_guru_size={m.get('rolling_avg_guru_size')} "
                f"estimated_notional_usd={est}"
            )
            if em is not None:
                em(
                    "strategy_decision",
                    {
                        "correlation_id": cid,
                        "branch": kind,
                        "decision": "skip",
                        "reason_code": str(w_rc),
                    },
                )
            return

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
                    "reason_code": str(decision.reason_code),
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
            reason_code=str(decision.reason_code),
            price_ref=sig.price_raw,
        )
        approved, risk_rc = self._risk.evaluate(intent)
        if not approved:
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
                    "quantity": float(qty),
                    "signal_kind": kind,
                    "price_ref": sig.price_raw,
                    "ts_risk_approved_ms": ts_risk_ms,
                },
            )

        self._execution.submit_intent(intent, mode=self._cfg.execution_mode)
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
                f"signal_kind={kind} side={sig.side} qty={qty}"
                f"{latency_kv}"
            )
        else:
            self.log.info(
                "event=live_order_intent "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"signal_kind={kind} side={sig.side} qty={qty}"
                f"{latency_kv}"
            )
