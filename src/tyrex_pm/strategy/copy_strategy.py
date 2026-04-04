"""
Copy strategy: guru bus → signal policies → sizing → execution port (shadow by default).

No Nautilus `Order` construction here — v1.08 will translate `OrderIntent` via execution policy.
"""

from __future__ import annotations

from nautilus_trader.trading.config import StrategyConfig

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal, OrderIntent
from tyrex_pm.data.guru_monitor import GURU_TRADE_TOPIC
from tyrex_pm.execution.port import ExecutionPort, NoOpExecutionPort
from tyrex_pm.risk.policy import RiskPolicy, ShadowAllPassRisk
from tyrex_pm.signal.entry import GuruFollowEntryPolicy, GuruMirrorExitPolicy, SignalDecision
from tyrex_pm.signal.sizing import ProportionalSizingPolicy
from tyrex_pm.signal.token_filter_spec import TokenFilterSpec
from tyrex_pm.strategy.base import BaseComposableStrategy


class CopyStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    """
    Token gate: set ``token_filter_enabled`` and ``allowlisted_token_ids`` per strategy YAML
    ``token_filter`` block (see loaders). Risk / execution are unchanged.
    """

    token_filter_enabled: bool = False
    allowlisted_token_ids: tuple[str, ...] = ()
    execution_mode: str = "shadow"
    copy_scale: float = 1.0


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
        self._sizing = ProportionalSizingPolicy(config.copy_scale)
        self._risk: RiskPolicy = ShadowAllPassRisk()
        self._execution: ExecutionPort = NoOpExecutionPort()

    def set_risk_policy(self, policy: RiskPolicy) -> None:
        """Inject real `RiskPolicy` in v1.06+ tests or runtime."""
        self._risk = policy

    def set_execution_port(self, port: ExecutionPort) -> None:
        """Tests or runtime wiring may inject a custom port."""
        self._execution = port

    def on_start(self) -> None:
        super().on_start()
        self.msgbus.subscribe(topic=GURU_TRADE_TOPIC, handler=self._on_guru_trade)

    def _on_guru_trade(self, msg: object) -> None:
        if not isinstance(msg, GuruTradeSignal):
            return

        if msg.side == "BUY":
            self._handle_branch(msg, self._entry.evaluate(msg), "entry")
        elif msg.side == "SELL":
            self._handle_branch(msg, self._exit.evaluate(msg), "exit")
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
    ) -> None:
        if not decision.accept:
            self.log.info(
                "event=copy_skip "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"reason_code={decision.reason_code} "
                f"detail={decision.detail or ''}"
            )
            return

        qty = self._sizing.size(sig)
        if qty <= 0:
            self.log.info(
                "event=copy_skip "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"reason_code={ReasonCode.COPY_SKIP} detail=zero_qty"
            )
            return

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

        self._execution.submit_intent(intent, mode=self._cfg.execution_mode)
        if self._cfg.execution_mode == "shadow":
            self.log.info(
                "event=shadow_order_intent "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"reason_code={ReasonCode.SHADOW_ORDER_INTENT} "
                f"signal_kind={kind} side={sig.side} qty={qty}"
            )
        else:
            self.log.info(
                "event=live_order_intent "
                "component=copy_strategy "
                f"correlation_id={sig.source_trade_id} "
                f"signal_kind={kind} side={sig.side} qty={qty}"
            )
