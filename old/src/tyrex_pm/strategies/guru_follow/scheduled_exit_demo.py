"""Validation: schedule a demo SELL a few seconds after a copied guru BUY."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.runtime.config import ExitsConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.allocation_ids import OWNER_GURU_FOLLOW
from tyrex_pm.runtime.allocation_runtime import clamp_planned_to_allocated
from tyrex_pm.runtime.exit_lifecycle import (
    ArmSource,
    clamp_planned_sell_size,
    emit_arm_attempt,
    emit_exit_lifecycle,
    inventory_snapshot,
    parse_taking_amount,
    required_sell_qty,
)
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
    match_taking_amount: Decimal | None = None


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
        coord: RuntimeCoordinator,
        *,
        parent_correlation_id: str,
        execution_mode: ExecutionMode,
        apply_shadow_fill: bool,
        match_evidence: dict | None = None,
    ) -> None:
        if not self.enabled:
            return
        intent = ap.intent
        if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
            return
        match_taking = parse_taking_amount(match_evidence or {})
        planned = clamp_planned_sell_size(
            intent.size,
            match_taking_amount=match_taking,
        )
        planned = clamp_planned_to_allocated(
            coord,
            owner_id=OWNER_GURU_FOLLOW,
            token_id=intent.token_id,
            planned=planned,
        )
        delay = float(self.cfg.demo_forced_exit_delay_s)
        now = monotonic_s()
        emit_exit_lifecycle(
            coord,
            "pending_registered",
            parent_correlation_id,
            token_id=str(intent.token_id),
            planned_sell_size=str(planned),
            parent_buy_intent_id=str(intent.intent_id),
            parent_client_order_id=str(ap.client_order_id),
            match_taking_amount=str(match_taking) if match_taking is not None else None,
        )
        if execution_mode == ExecutionMode.SHADOW and apply_shadow_fill:
            self._armed.append(
                _ArmedDemoExit(
                    due_mono=now + delay,
                    token_id=intent.token_id,
                    sell_size=planned,
                    limit_price=intent.limit_price,
                    parent_correlation_id=parent_correlation_id,
                    parent_buy_intent_id=str(intent.intent_id),
                    parent_client_order_id=str(ap.client_order_id),
                )
            )
            emit_exit_lifecycle(
                coord,
                "arm_granted",
                parent_correlation_id,
                token_id=str(intent.token_id),
                planned_sell_size=str(planned),
                required_qty=str(planned),
                source="post_buy_ack",
                armed=True,
                sell_size=str(planned),
            )
            return
        self._pending_live.append(
            _PendingLiveArm(
                token_id=intent.token_id,
                planned_sell_size=planned,
                limit_price=intent.limit_price,
                parent_correlation_id=parent_correlation_id,
                parent_buy_intent_id=str(intent.intent_id),
                parent_client_order_id=str(ap.client_order_id),
                match_taking_amount=match_taking,
            )
        )

    def try_arm_live_pending(
        self,
        coord: RuntimeCoordinator,
        *,
        source: ArmSource = "post_buy_ack",
    ) -> None:
        if not self.enabled or not self._pending_live:
            return
        delay = float(self.cfg.demo_forced_exit_delay_s)
        now = monotonic_s()
        still_pending: list[_PendingLiveArm] = []
        for row in self._pending_live:
            snap = inventory_snapshot(coord, row.token_id)
            avail = Decimal(snap["available_to_sell"])
            req = required_sell_qty(row.planned_sell_size, row.match_taking_amount)
            if avail >= req:
                sell_size = clamp_planned_sell_size(
                    row.planned_sell_size,
                    match_taking_amount=row.match_taking_amount,
                    available=avail,
                )
                sell_size = clamp_planned_to_allocated(
                    coord,
                    owner_id=OWNER_GURU_FOLLOW,
                    token_id=row.token_id,
                    planned=sell_size,
                )
                emit_arm_attempt(
                    coord,
                    event="arm_granted",
                    token_id=row.token_id,
                    parent_correlation_id=row.parent_correlation_id,
                    planned_sell_size=row.planned_sell_size,
                    required_qty=req,
                    source=source,
                    armed=True,
                    snap=snap,
                )
                self._armed.append(
                    _ArmedDemoExit(
                        due_mono=now + delay,
                        token_id=row.token_id,
                        sell_size=sell_size,
                        limit_price=row.limit_price,
                        parent_correlation_id=row.parent_correlation_id,
                        parent_buy_intent_id=row.parent_buy_intent_id,
                        parent_client_order_id=row.parent_client_order_id,
                    )
                )
            else:
                emit_arm_attempt(
                    coord,
                    event="arm_attempt",
                    token_id=row.token_id,
                    parent_correlation_id=row.parent_correlation_id,
                    planned_sell_size=row.planned_sell_size,
                    required_qty=req,
                    source=source,
                    armed=False,
                    snap=snap,
                    reason="waiting_for_inventory",
                )
                emit_exit_lifecycle(
                    coord,
                    "waiting_for_inventory",
                    row.parent_correlation_id,
                    token_id=str(row.token_id),
                    planned_sell_size=str(row.planned_sell_size),
                    required_qty=str(req),
                    source=source,
                    reason="insufficient_inventory",
                    **snap,
                )
                still_pending.append(row)
        self._pending_live = still_pending

    def pop_due_work_units(
        self,
        coord: RuntimeCoordinator,
        now_mono: float | None = None,
    ) -> list[IntentWorkUnit]:
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
            emit_exit_lifecycle(
                coord,
                "sell_due",
                row.parent_correlation_id,
                token_id=str(row.token_id),
                sell_size=str(row.sell_size),
            )
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
            emit_exit_lifecycle(
                coord,
                "sell_intent_emitted",
                row.parent_correlation_id,
                token_id=str(row.token_id),
                sell_size=str(row.sell_size),
            )
            out.append(
                IntentWorkUnit(
                    intent=exit_int,
                    correlation_id=row.parent_correlation_id,
                    intent_fact_extensions=prov,
                )
            )
        return out


def try_arm_scheduled_exit_demos(
    strat: object,
    coord: RuntimeCoordinator,
    *,
    source: ArmSource = "post_buy_ack",
) -> None:
    demo = getattr(strat, "scheduled_exit_demo", None)
    if isinstance(demo, ScheduledExitDemoState):
        demo.try_arm_live_pending(coord, source=source)
