"""Validation: schedule a demo SELL a few seconds after a copied guru BUY.

* **Shadow:** arm the timer as soon as the BUY completes the success path (instant fill updates
  positions immediately).
* **Live:** register a pending exit; arm the timer only when venue-truth shows **sellable**
  inventory ≥ planned exit size (same formula as :func:`tyrex_pm.risk.inventory.available_to_sell`).
  That aligns the 3-second window with data-api/positions REST and/or user-WS CONFIRMED fills
  updating :class:`~tyrex_pm.state.wallet_store.WalletStore` — not merely HTTP submit ack.

``try_arm_live_pending`` is invoked from :meth:`~tyrex_pm.runtime.coordinator.RuntimeCoordinator`
hooks after wallet/position updates (venue refresh loop, user channel).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.risk.inventory import available_to_sell
from tyrex_pm.runtime.config import ExitsConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit


DEMO_EXIT_FACT_SOURCE = "scheduled_exit_demo"


@dataclass
class _PendingLiveArm:
    token_id: TokenId
    planned_sell_size: Decimal
    limit_price: Decimal | None
    parent_correlation_id: str
    parent_buy_intent_id: str
    parent_client_order_id: str


@dataclass
class _ArmedDemoExit:
    due_mono: float
    token_id: TokenId
    sell_size: Decimal
    limit_price: Decimal | None
    parent_correlation_id: str
    parent_buy_intent_id: str
    parent_client_order_id: str


@dataclass
class ScheduledExitDemoState:
    """Per-strategy-instance demo state (in-memory)."""

    cfg: ExitsConfig
    _pending_live: list[_PendingLiveArm] = field(default_factory=list)
    _armed: list[_ArmedDemoExit] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.cfg.demo_forced_exit_enabled

    def register_after_successful_buy(
        self,
        ap: ApprovedIntent,
        *,
        parent_correlation_id: str,
        execution_mode: ExecutionMode,
        apply_shadow_fill: bool,
    ) -> None:
        if not self.enabled:
            return
        intent = ap.intent
        if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
            return
        delay = float(self.cfg.demo_forced_exit_delay_s)
        now = monotonic_s()
        if execution_mode == ExecutionMode.SHADOW and apply_shadow_fill:
            self._armed.append(
                _ArmedDemoExit(
                    due_mono=now + delay,
                    token_id=intent.token_id,
                    sell_size=intent.size,
                    limit_price=intent.limit_price,
                    parent_correlation_id=parent_correlation_id,
                    parent_buy_intent_id=str(intent.intent_id),
                    parent_client_order_id=str(ap.client_order_id),
                )
            )
            return
        # Live: arm the delay only once venue-truth shows sellable inventory (see try_arm_live_pending).
        self._pending_live.append(
            _PendingLiveArm(
                token_id=intent.token_id,
                planned_sell_size=intent.size,
                limit_price=intent.limit_price,
                parent_correlation_id=parent_correlation_id,
                parent_buy_intent_id=str(intent.intent_id),
                parent_client_order_id=str(ap.client_order_id),
            )
        )

    def try_arm_live_pending(self, coord: RuntimeCoordinator) -> None:
        """Move pending live rows to armed when ``available_to_sell >= planned_sell_size``."""
        if not self.enabled or not self._pending_live:
            return
        positions = {p.token_id: p for p in coord.wallet.positions.values()}
        in_flight = dict(coord.orders.in_flight_by_token)
        delay = float(self.cfg.demo_forced_exit_delay_s)
        now = monotonic_s()
        still_pending: list[_PendingLiveArm] = []
        for row in self._pending_live:
            avail = available_to_sell(
                token_id=row.token_id,
                positions=positions,
                in_flight=in_flight,
            )
            if avail >= row.planned_sell_size:
                self._armed.append(
                    _ArmedDemoExit(
                        due_mono=now + delay,
                        token_id=row.token_id,
                        sell_size=row.planned_sell_size,
                        limit_price=row.limit_price,
                        parent_correlation_id=row.parent_correlation_id,
                        parent_buy_intent_id=row.parent_buy_intent_id,
                        parent_client_order_id=row.parent_client_order_id,
                    )
                )
            else:
                still_pending.append(row)
        self._pending_live = still_pending

    def pop_due_work_units(self, now_mono: float | None = None) -> list[IntentWorkUnit]:
        """Return work units for demo exits whose timer has fired (due_mono <= now)."""
        if not self.enabled or not self._armed:
            return []
        now = monotonic_s() if now_mono is None else now_mono
        due: list[_ArmedDemoExit] = []
        rest: list[_ArmedDemoExit] = []
        for row in self._armed:
            if row.due_mono <= now:
                due.append(row)
            else:
                rest.append(row)
        self._armed = rest
        out: list[IntentWorkUnit] = []
        for row in due:
            exit_int = ExitIntent(
                token_id=row.token_id,
                side=Side.SELL,
                size=row.sell_size,
                limit_price=row.limit_price,
                order_style=OrderStyle.GTC,
            )
            prov = {
                "source": DEMO_EXIT_FACT_SOURCE,
                "parent_correlation_id": row.parent_correlation_id,
                "parent_buy_intent_id": row.parent_buy_intent_id,
                "parent_client_order_id": row.parent_client_order_id,
                "demo_exit_delay_s": str(self.cfg.demo_forced_exit_delay_s),
            }
            out.append(
                IntentWorkUnit(
                    intent=exit_int,
                    correlation_id=row.parent_correlation_id,
                    intent_fact_extensions=prov,
                )
            )
        return out


def try_arm_scheduled_exit_demos(strat: object, coord: RuntimeCoordinator) -> None:
    """Invoke from coordinator hooks when wallet/positions may have changed."""
    demo = getattr(strat, "scheduled_exit_demo", None)
    if demo is None:
        return
    if isinstance(demo, ScheduledExitDemoState):
        demo.try_arm_live_pending(coord)
