"""Standalone strategy for validating the V2 SELL / exit path end-to-end."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.runtime.config import (
    SELL_TEST_PRICING_AUTO,
    SellTestStrategyConfig,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.allocation_ids import OWNER_SELL_TEST
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
from tyrex_pm.strategies.sell_test.pricing import (
    ResolvedPrice,
    resolve_marketable_price_via_client,
)


SELL_TEST_FACT_SOURCE = "sell_test_strategy"


@dataclass
class _PendingSellArm:
    token_id: TokenId
    planned_sell_size: Decimal
    sell_limit_price: Decimal | None
    sell_order_style: OrderStyle
    parent_correlation_id: str
    parent_buy_intent_id: str
    parent_client_order_id: str
    match_taking_amount: Decimal | None = None


@dataclass
class _ArmedSellExit:
    due_mono: float
    token_id: TokenId
    sell_size: Decimal
    sell_limit_price: Decimal | None
    sell_order_style: OrderStyle
    parent_correlation_id: str
    parent_buy_intent_id: str
    parent_client_order_id: str


@dataclass
class SellTestState:
    """Per-strategy-instance pending/armed SELL bookkeeping (in-memory)."""

    cfg: SellTestStrategyConfig
    _pending_live: list[_PendingSellArm] = field(default_factory=list)
    _armed: list[_ArmedSellExit] = field(default_factory=list)
    _sell_terminal: bool = False
    _sell_outcome: str | None = None
    _sell_in_flight: bool = False

    @property
    def sell_enabled(self) -> bool:
        return self.cfg.enabled and self.cfg.sell.enabled

    @property
    def has_open_work(self) -> bool:
        return bool(self._pending_live or self._armed or self._sell_in_flight)

    @property
    def is_terminal(self) -> bool:
        return self._sell_terminal

    @property
    def sell_outcome(self) -> str | None:
        return self._sell_outcome

    def mark_sell_terminal(self, outcome: str) -> None:
        self._sell_terminal = True
        self._sell_outcome = outcome
        self._sell_in_flight = False

    def mark_sell_in_flight(self) -> None:
        self._sell_in_flight = True

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
        if not self.sell_enabled or self._sell_terminal:
            return
        intent = ap.intent
        if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
            return
        if str(intent.token_id) != self.cfg.token_id:
            return
        match_taking = parse_taking_amount(match_evidence or {})
        planned = clamp_planned_sell_size(
            intent.size,
            match_taking_amount=match_taking,
        )
        planned = clamp_planned_to_allocated(
            coord,
            owner_id=OWNER_SELL_TEST,
            token_id=intent.token_id,
            planned=planned,
        )
        sell_price = (
            self.cfg.sell.limit_price
            if self.cfg.sell.limit_price is not None
            else intent.limit_price
        )
        sell_style = self.cfg.sell.order_style
        delay = float(self.cfg.sell.delay_s)
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
                _ArmedSellExit(
                    due_mono=now + delay,
                    token_id=intent.token_id,
                    sell_size=planned,
                    sell_limit_price=sell_price,
                    sell_order_style=sell_style,
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
            _PendingSellArm(
                token_id=intent.token_id,
                planned_sell_size=planned,
                sell_limit_price=sell_price,
                sell_order_style=sell_style,
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
        if not self.sell_enabled or not self._pending_live or self._sell_terminal:
            return
        delay = float(self.cfg.sell.delay_s)
        now = monotonic_s()
        still_pending: list[_PendingSellArm] = []
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
                    owner_id=OWNER_SELL_TEST,
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
                    _ArmedSellExit(
                        due_mono=now + delay,
                        token_id=row.token_id,
                        sell_size=sell_size,
                        sell_limit_price=row.sell_limit_price,
                        sell_order_style=row.sell_order_style,
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

    def pop_due_rows(
        self,
        coord: RuntimeCoordinator,
        now_mono: float | None = None,
    ) -> list[_ArmedSellExit]:
        if not self.sell_enabled or not self._armed or self._sell_terminal:
            return []
        now = monotonic_s() if now_mono is None else now_mono
        due: list[_ArmedSellExit] = []
        rest: list[_ArmedSellExit] = []
        for row in self._armed:
            if row.due_mono <= now:
                due.append(row)
            else:
                rest.append(row)
        self._armed = rest
        for row in due:
            emit_exit_lifecycle(
                coord,
                "sell_due",
                row.parent_correlation_id,
                token_id=str(row.token_id),
                sell_size=str(row.sell_size),
            )
        return due

    def _build_work_unit(
        self,
        row: _ArmedSellExit,
        coord: RuntimeCoordinator,
        *,
        override_price: Decimal | None = None,
        pricing_evidence: dict[str, object] | None = None,
    ) -> IntentWorkUnit:
        chosen_price = override_price if override_price is not None else row.sell_limit_price
        exit_int = ExitIntent(
            token_id=row.token_id,
            side=Side.SELL,
            size=row.sell_size,
            limit_price=chosen_price,
            order_style=row.sell_order_style,
        )
        prov: dict[str, object] = {
            "source": SELL_TEST_FACT_SOURCE,
            "parent_correlation_id": row.parent_correlation_id,
            "parent_buy_intent_id": row.parent_buy_intent_id,
            "parent_client_order_id": row.parent_client_order_id,
            "sell_test_delay_s": str(self.cfg.sell.delay_s),
        }
        if pricing_evidence:
            prov["sell_test_pricing"] = pricing_evidence
        emit_exit_lifecycle(
            coord,
            "sell_intent_emitted",
            row.parent_correlation_id,
            token_id=str(row.token_id),
            sell_size=str(row.sell_size),
        )
        self.mark_sell_in_flight()
        return IntentWorkUnit(
            intent=exit_int,
            correlation_id=row.parent_correlation_id,
            intent_fact_extensions=prov,
        )

    def pop_due_work_units(
        self,
        coord: RuntimeCoordinator,
        now_mono: float | None = None,
    ) -> list[IntentWorkUnit]:
        return [
            self._build_work_unit(row, coord)
            for row in self.pop_due_rows(coord, now_mono)
        ]

    def emit_timeout_waiting_for_inventory(self, coord: RuntimeCoordinator) -> None:
        if self._sell_terminal or not self._pending_live:
            return
        row = self._pending_live[0]
        snap = inventory_snapshot(coord, row.token_id)
        req = required_sell_qty(row.planned_sell_size, row.match_taking_amount)
        emit_exit_lifecycle(
            coord,
            "timeout_waiting_for_sellable_inventory",
            row.parent_correlation_id,
            token_id=str(row.token_id),
            planned_sell_size=str(row.planned_sell_size),
            required_qty=str(req),
            **snap,
        )
        self.mark_sell_terminal("timeout_waiting_for_sellable_inventory")
        self._pending_live.clear()
        self._armed.clear()


class SellTestStrategy:
    """Validation strategy: emit one BUY then one SELL after sellable inventory."""

    def __init__(self, cfg: SellTestStrategyConfig) -> None:
        self._cfg = cfg
        self.sell_test_state = SellTestState(cfg)
        self._buy_submit_succeeded: bool = False
        self._buy_work_issued: bool = False
        self._buy_correlation_id: str = f"sell_test:{cfg.token_id}"
        self._resolved_buy_price: Decimal | None = None
        self._buy_pricing_evidence: dict[str, object] | None = None

    @property
    def cfg(self) -> SellTestStrategyConfig:
        return self._cfg

    @property
    def buy_correlation_id(self) -> str:
        return self._buy_correlation_id

    @property
    def buy_submit_succeeded(self) -> bool:
        return self._buy_submit_succeeded

    @property
    def effective_buy_price(self) -> Decimal | None:
        return (
            self._resolved_buy_price
            if self._resolved_buy_price is not None
            else self._cfg.buy.limit_price
        )

    def set_resolved_buy_price(
        self,
        price: Decimal,
        *,
        evidence: dict[str, object] | None = None,
    ) -> None:
        if price <= 0:
            raise ValueError(f"resolved buy price must be positive, got {price!r}")
        self._resolved_buy_price = price
        self._buy_pricing_evidence = evidence

    def initial_buy_work_units(self) -> list[IntentWorkUnit]:
        if not self._cfg.enabled or not self._cfg.buy.enabled:
            return []
        if self._cfg.run_once and (self._buy_submit_succeeded or self._buy_work_issued):
            return []
        price = self.effective_buy_price
        if price is None or price <= 0:
            return []
        size = self._cfg.buy.notional_usd / price
        intent = EnterIntent(
            token_id=TokenId(self._cfg.token_id),
            side=Side.BUY,
            size=size,
            limit_price=price,
            order_style=self._cfg.buy.order_style,
        )
        ext: dict[str, object] = {
            "source": SELL_TEST_FACT_SOURCE,
            "sell_test_token_id": self._cfg.token_id,
            "sell_test_buy_notional_usd": str(self._cfg.buy.notional_usd),
            "sell_test_buy_pricing_mode": self._cfg.buy.pricing_mode,
        }
        if self._buy_pricing_evidence is not None:
            ext["sell_test_buy_pricing"] = self._buy_pricing_evidence
        self._buy_work_issued = True
        return [
            IntentWorkUnit(
                intent=intent,
                correlation_id=self._buy_correlation_id,
                intent_fact_extensions=ext,
            )
        ]

    def notify_buy_not_submitted(self) -> None:
        self._buy_work_issued = False

    def notify_buy_submitted(self) -> None:
        self._buy_submit_succeeded = True

    async def resolve_due_work_units(
        self,
        *,
        coord: RuntimeCoordinator,
        live_clob_client: object | None,
    ) -> list[IntentWorkUnit]:
        rows = self.sell_test_state.pop_due_rows(coord)
        if not rows:
            return []
        out: list[IntentWorkUnit] = []
        sell_cfg = self._cfg.sell
        market_info_cache = getattr(coord, "market_info_cache", None)
        for row in rows:
            override_price: Decimal | None = None
            evidence: dict[str, object] | None = None
            if sell_cfg.pricing_mode == SELL_TEST_PRICING_AUTO and live_clob_client is not None:
                market_info = None
                if market_info_cache is not None:
                    try:
                        market_info = await market_info_cache.get(row.token_id)
                    except Exception:  # noqa: BLE001
                        market_info = None
                resolved: ResolvedPrice = await resolve_marketable_price_via_client(
                    client=live_clob_client,
                    market_info=market_info,
                    token_id=str(row.token_id),
                    side="SELL",
                    aggression_ticks=sell_cfg.aggression_ticks,
                    fallback_price=row.sell_limit_price,
                    min_price=sell_cfg.min_price,
                )
                evidence = resolved.to_evidence()
                if resolved.source == "auto_book":
                    override_price = resolved.price
            out.append(
                self.sell_test_state._build_work_unit(
                    row,
                    coord,
                    override_price=override_price,
                    pricing_evidence=evidence,
                )
            )
        return out

    def on_buy_submit_ack(
        self,
        *,
        ap: ApprovedIntent,
        parent_correlation_id: str,
        coord: RuntimeCoordinator,
        execution_mode: ExecutionMode,
        apply_local_shadow_fill: bool,
        match_evidence: dict | None = None,
    ) -> None:
        self.notify_buy_submitted()
        self.sell_test_state.register_after_successful_buy(
            ap,
            coord,
            parent_correlation_id=parent_correlation_id,
            execution_mode=execution_mode,
            apply_shadow_fill=apply_local_shadow_fill,
            match_evidence=match_evidence,
        )
        if coord.scheduled_exit_demo_try_arm is not None:
            coord.scheduled_exit_demo_try_arm(source="post_buy_ack")

    def is_done(self) -> bool:
        if not self._cfg.run_once:
            return False
        if self._cfg.buy.enabled and not self._buy_submit_succeeded:
            return False
        if self._cfg.buy.enabled and not self._cfg.sell.enabled:
            return True
        if not self._cfg.sell.enabled:
            return True
        return self.sell_test_state.is_terminal and not self.sell_test_state.has_open_work

    def has_pending_inventory_wait(self) -> bool:
        st = self.sell_test_state
        return bool(st._pending_live) and not st.is_terminal


def try_arm_sell_test_pending(
    strat: object,
    coord: RuntimeCoordinator,
    *,
    source: ArmSource = "post_buy_ack",
) -> None:
    state = getattr(strat, "sell_test_state", None)
    if isinstance(state, SellTestState):
        state.try_arm_live_pending(coord, source=source)
