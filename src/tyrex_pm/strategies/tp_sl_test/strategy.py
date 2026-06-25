"""Standalone strategy validating the P6 TP/SL overlay path (deterministic fixtures)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.runtime.config import (
    SELL_TEST_PRICING_AUTO,
    TP_SL_PRICE_SOURCE_BEST_BID,
    TP_SL_PRICE_SOURCE_FIXTURE,
    TP_SL_SIZE_MODE_FIXED,
    TP_SL_SIZE_MODE_FULL,
    TP_SL_SIZE_MODE_PERCENT,
    TP_SL_TRIGGER_REFERENCE_ENTRY,
    TpSlTestMonitorConfig,
    TpSlTestStrategyConfig,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.allocation_ids import TP_SL_TEST_INTENT_SOURCE
from tyrex_pm.runtime.exit_lifecycle import (
    ArmSource,
    emit_arm_attempt,
    inventory_snapshot,
    parse_taking_amount,
)
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.strategies.sell_test.pricing import (
    ResolvedPrice,
    best_levels_from_book,
    resolve_marketable_price_via_client,
)

# Lifecycle phases (operator-visible).
PHASE_ENTRY_PENDING = "entry_pending"
PHASE_POSITION_ACTIVE = "position_active"
PHASE_TRIGGERED_EXIT = "triggered_exit"

# Terminal outcomes that allow run_once completion.
_DONE_OUTCOMES = frozenset(
    {
        "sell_submitted",
        "sell_risk_denied",
        "sell_oms_reject",
        "inventory_timeout",
        "trigger_timeout",
        "position_gone",
    }
)

# Tiny epsilon for float-noise only (e.g. 5.309999999 vs 5.31), not intent-vs-fill slack.
_QTY_EPS = Decimal("0.000001")


def compute_tp_sl_trigger_thresholds(
    monitor: TpSlTestMonitorConfig,
    entry_price: Decimal,
) -> tuple[Decimal | None, Decimal | None, dict[str, object]]:
    """Resolve absolute trigger prices and operator-visible threshold evidence."""
    evidence: dict[str, object] = {}
    tp_trigger: Decimal | None = None
    sl_trigger: Decimal | None = None

    if monitor.take_profit_pct is not None or monitor.stop_loss_pct is not None:
        evidence["trigger_reference"] = monitor.trigger_reference or TP_SL_TRIGGER_REFERENCE_ENTRY
        evidence["reference_price"] = str(entry_price)
        if monitor.take_profit_pct is not None:
            evidence["take_profit_pct"] = str(monitor.take_profit_pct)
            tp_trigger = entry_price * (Decimal("1") + monitor.take_profit_pct)
        if monitor.stop_loss_pct is not None:
            evidence["stop_loss_pct"] = str(monitor.stop_loss_pct)
            sl_trigger = entry_price * (Decimal("1") - monitor.stop_loss_pct)
    if monitor.take_profit_price is not None:
        evidence["take_profit_price"] = str(monitor.take_profit_price)
        tp_trigger = monitor.take_profit_price
    if monitor.stop_loss_price is not None:
        evidence["stop_loss_price"] = str(monitor.stop_loss_price)
        sl_trigger = monitor.stop_loss_price
    if tp_trigger is not None:
        evidence["take_profit_trigger_price"] = str(tp_trigger.normalize())
    if sl_trigger is not None:
        evidence["stop_loss_trigger_price"] = str(sl_trigger.normalize())
    return tp_trigger, sl_trigger, evidence


def _qty_positive(q: Decimal) -> bool:
    return q > _QTY_EPS


def _allocated_available(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
) -> Decimal:
    ledger = coord.allocation_ledger
    if ledger is None:
        return Decimal("0")
    return ledger.get_available_allocated(owner_id, token_id)


def _position_snapshot(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
) -> tuple[Decimal, Decimal, Decimal, dict[str, str]]:
    allocated = _allocated_available(coord, owner_id=owner_id, token_id=token_id)
    snap = inventory_snapshot(coord, token_id)
    avail = Decimal(snap["available_to_sell"])
    protected = min(allocated, avail)
    return allocated, avail, protected, snap


def _compute_final_exit_size(
    planned_exit_size: Decimal,
    allocated_available: Decimal,
    available_to_sell: Decimal,
) -> Decimal:
    return min(planned_exit_size, allocated_available, available_to_sell)


def resolve_entry_price(
    coord: RuntimeCoordinator,
    token_id: TokenId,
    *,
    limit_price: Decimal,
    match_evidence: dict | None = None,
) -> tuple[Decimal, str]:
    """Prefer wallet avg fill, then match evidence price, then submitted limit."""
    pos = coord.wallet.positions.get(token_id)
    if pos is not None and pos.qty > _QTY_EPS and pos.avg_price_usd is not None and pos.avg_price_usd > 0:
        return pos.avg_price_usd, "avg_fill_price"
    ev = match_evidence or {}
    for key in ("fill_price", "avg_price", "price", "match_price"):
        raw = ev.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            price = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if price > 0:
            return price, "match_evidence_price"
    return limit_price, "buy_limit_price"


@dataclass
class _PendingMonitor:
    token_id: TokenId
    intended_buy_size: Decimal
    limit_price: Decimal
    entry_price: Decimal
    entry_price_source: str
    parent_correlation_id: str
    parent_buy_intent_id: str
    parent_client_order_id: str
    match_evidence: dict = field(default_factory=dict)


@dataclass
class _ActiveMonitor:
    token_id: TokenId
    intended_buy_size: Decimal
    protected_qty: Decimal
    entry_price_source: str
    parent_correlation_id: str
    parent_buy_intent_id: str
    parent_client_order_id: str
    entry_price: Decimal
    take_profit_trigger_price: Decimal | None = None
    stop_loss_trigger_price: Decimal | None = None
    threshold_evidence: dict[str, object] = field(default_factory=dict)
    fixture_index: int = 0
    triggered: bool = False
    trigger_kind: str | None = None
    observed_price: Decimal | None = None
    planned_before_clamp: Decimal | None = None
    allocated_available: Decimal | None = None
    available_to_sell: Decimal | None = None
    final_size: Decimal | None = None
    sizing_evidence: dict[str, object] | None = None


@dataclass
class TpSlTestState:
    cfg: TpSlTestStrategyConfig
    _pending_inventory: list[_PendingMonitor] = field(default_factory=list)
    _monitoring: _ActiveMonitor | None = None
    _exit_in_flight: bool = False
    _terminal: bool = False
    _outcome: str | None = None
    _monitor_started_mono: float | None = None
    _allocation_wait_emitted: bool = False
    _position_active_wait_emitted: bool = False

    @property
    def exit_enabled(self) -> bool:
        return self.cfg.enabled and self.cfg.exit.enabled and self.cfg.monitor.enabled

    @property
    def lifecycle_phase(self) -> str | None:
        if self._exit_in_flight:
            return PHASE_TRIGGERED_EXIT
        if self._monitoring is not None:
            if self._monitoring.triggered:
                return PHASE_TRIGGERED_EXIT
            return PHASE_POSITION_ACTIVE
        if self._pending_inventory:
            return PHASE_ENTRY_PENDING
        return None

    @property
    def is_terminal(self) -> bool:
        return self._terminal

    @property
    def outcome(self) -> str | None:
        return self._outcome

    @property
    def has_open_work(self) -> bool:
        return bool(self._pending_inventory or self._monitoring is not None or self._exit_in_flight)

    def mark_exit_terminal(self, outcome: str) -> None:
        self._terminal = True
        self._outcome = outcome
        self._exit_in_flight = False
        self._monitoring = None

    def mark_exit_in_flight(self) -> None:
        self._exit_in_flight = True

    def emit_tp_sl(
        self,
        coord: RuntimeCoordinator,
        event: str,
        correlation_id: str,
        **payload: object,
    ) -> None:
        if coord.exit_lifecycle_sink is None or coord.exit_lifecycle_run_id is None:
            return
        body: dict[str, object] = {"event": event, **payload}
        coord.exit_lifecycle_sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                coord.exit_lifecycle_run_id,
                body,
                correlation_id=correlation_id,
            )
        )

    def _position_fact_fields(
        self,
        *,
        intended_buy_size: Decimal,
        allocated: Decimal,
        avail: Decimal,
        protected: Decimal,
        entry_price: Decimal,
        entry_price_source: str,
        lifecycle_phase: str,
    ) -> dict[str, str]:
        return {
            "lifecycle_phase": lifecycle_phase,
            "intended_buy_size": str(intended_buy_size),
            "actual_allocated_qty": str(allocated),
            "available_to_sell": str(avail),
            "protected_qty": str(protected),
            "entry_price": str(entry_price),
            "entry_price_source": entry_price_source,
        }

    def _start_monitoring(
        self,
        *,
        token_id: TokenId,
        intended_buy_size: Decimal,
        protected_qty: Decimal,
        entry_price_source: str,
        parent_correlation_id: str,
        parent_buy_intent_id: str,
        parent_client_order_id: str,
        entry_price: Decimal,
    ) -> _ActiveMonitor:
        if entry_price <= 0:
            raise ValueError("entry_price must be positive for TP/SL monitor")
        mon = self.cfg.monitor
        if (
            mon.trigger_reference == TP_SL_TRIGGER_REFERENCE_ENTRY
            or mon.take_profit_pct is not None
            or mon.stop_loss_pct is not None
        ) and entry_price <= 0:
            raise ValueError("trigger_reference entry_price requires a known positive entry price")
        tp_trigger, sl_trigger, evidence = compute_tp_sl_trigger_thresholds(mon, entry_price)
        return _ActiveMonitor(
            token_id=token_id,
            intended_buy_size=intended_buy_size,
            protected_qty=protected_qty,
            entry_price_source=entry_price_source,
            parent_correlation_id=parent_correlation_id,
            parent_buy_intent_id=parent_buy_intent_id,
            parent_client_order_id=parent_client_order_id,
            entry_price=entry_price,
            take_profit_trigger_price=tp_trigger,
            stop_loss_trigger_price=sl_trigger,
            threshold_evidence=evidence,
        )

    def _pending_from_row(self, row: _PendingMonitor) -> _PendingMonitor:
        return _PendingMonitor(
            token_id=row.token_id,
            intended_buy_size=row.intended_buy_size,
            limit_price=row.limit_price,
            entry_price=row.entry_price,
            entry_price_source=row.entry_price_source,
            parent_correlation_id=row.parent_correlation_id,
            parent_buy_intent_id=row.parent_buy_intent_id,
            parent_client_order_id=row.parent_client_order_id,
            match_evidence=dict(row.match_evidence),
        )

    def _demote_monitor_to_pending(self, row: _ActiveMonitor) -> _PendingMonitor:
        return _PendingMonitor(
            token_id=row.token_id,
            intended_buy_size=row.intended_buy_size,
            limit_price=row.entry_price,
            entry_price=row.entry_price,
            entry_price_source=row.entry_price_source,
            parent_correlation_id=row.parent_correlation_id,
            parent_buy_intent_id=row.parent_buy_intent_id,
            parent_client_order_id=row.parent_client_order_id,
        )

    def register_after_successful_buy(
        self,
        ap: ApprovedIntent,
        coord: RuntimeCoordinator,
        *,
        parent_correlation_id: str,
        entry_price: Decimal,
        entry_price_source: str,
        execution_mode: ExecutionMode,
        apply_shadow_fill: bool,
        match_evidence: dict | None = None,
    ) -> None:
        if not self.exit_enabled or self._terminal:
            return
        intent = ap.intent
        if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
            return
        if str(intent.token_id) != self.cfg.token_id:
            return
        ev = match_evidence or {}
        resolved_price, resolved_source = resolve_entry_price(
            coord,
            intent.token_id,
            limit_price=entry_price,
            match_evidence=ev,
        )
        allocated, avail, protected, snap = _position_snapshot(
            coord,
            owner_id=self.cfg.owner_id,
            token_id=intent.token_id,
        )
        pos_fields = self._position_fact_fields(
            intended_buy_size=intent.size,
            allocated=allocated,
            avail=avail,
            protected=protected,
            entry_price=resolved_price,
            entry_price_source=resolved_source,
            lifecycle_phase=PHASE_ENTRY_PENDING,
        )
        self.emit_tp_sl(
            coord,
            "tp_sl_entry_pending",
            parent_correlation_id,
            token_id=str(intent.token_id),
            owner_id=self.cfg.owner_id,
            wallet_position_qty=snap["wallet_position_qty"],
            in_flight_qty=snap["in_flight_qty"],
            **pos_fields,
        )
        self._pending_inventory.append(
            _PendingMonitor(
                token_id=intent.token_id,
                intended_buy_size=intent.size,
                limit_price=entry_price,
                entry_price=resolved_price,
                entry_price_source=resolved_source,
                parent_correlation_id=parent_correlation_id,
                parent_buy_intent_id=str(intent.intent_id),
                parent_client_order_id=str(ap.client_order_id),
                match_evidence=dict(ev),
            )
        )
        _ = parse_taking_amount(ev)
        _ = execution_mode
        _ = apply_shadow_fill
        self.try_promote_inventory(coord, source="post_buy_ack")

    def try_promote_inventory(
        self,
        coord: RuntimeCoordinator,
        *,
        source: ArmSource = "post_buy_ack",
    ) -> None:
        if not self.exit_enabled or not self._pending_inventory or self._terminal:
            return
        still_pending: list[_PendingMonitor] = []
        for row in self._pending_inventory:
            allocated, avail, protected, snap = _position_snapshot(
                coord,
                owner_id=self.cfg.owner_id,
                token_id=row.token_id,
            )
            entry_price, entry_source = resolve_entry_price(
                coord,
                row.token_id,
                limit_price=row.limit_price,
                match_evidence=row.match_evidence,
            )
            row.entry_price = entry_price
            row.entry_price_source = entry_source
            pos_fields = self._position_fact_fields(
                intended_buy_size=row.intended_buy_size,
                allocated=allocated,
                avail=avail,
                protected=protected,
                entry_price=entry_price,
                entry_price_source=entry_source,
                lifecycle_phase=PHASE_ENTRY_PENDING,
            )
            if not _qty_positive(allocated):
                emit_arm_attempt(
                    coord,
                    event="arm_attempt",
                    token_id=row.token_id,
                    parent_correlation_id=row.parent_correlation_id,
                    planned_sell_size=protected,
                    required_qty=allocated,
                    source=source,
                    armed=False,
                    snap=snap,
                    reason="waiting_for_allocation",
                )
                if not self._allocation_wait_emitted:
                    self.emit_tp_sl(
                        coord,
                        "tp_sl_waiting_for_allocation",
                        row.parent_correlation_id,
                        token_id=str(row.token_id),
                        wallet_position_qty=snap["wallet_position_qty"],
                        in_flight_qty=snap["in_flight_qty"],
                        **pos_fields,
                    )
                    self._allocation_wait_emitted = True
                still_pending.append(row)
                continue
            if not _qty_positive(avail) or not _qty_positive(protected):
                emit_arm_attempt(
                    coord,
                    event="arm_attempt",
                    token_id=row.token_id,
                    parent_correlation_id=row.parent_correlation_id,
                    planned_sell_size=protected,
                    required_qty=allocated,
                    source=source,
                    armed=False,
                    snap=snap,
                    reason="waiting_for_sellable_inventory",
                )
                if not self._position_active_wait_emitted:
                    self.emit_tp_sl(
                        coord,
                        "tp_sl_waiting_for_position_active",
                        row.parent_correlation_id,
                        token_id=str(row.token_id),
                        wallet_position_qty=snap["wallet_position_qty"],
                        in_flight_qty=snap["in_flight_qty"],
                        **pos_fields,
                    )
                    self._position_active_wait_emitted = True
                still_pending.append(row)
                continue
            emit_arm_attempt(
                coord,
                event="arm_granted",
                token_id=row.token_id,
                parent_correlation_id=row.parent_correlation_id,
                planned_sell_size=protected,
                required_qty=protected,
                source=source,
                armed=True,
                snap=snap,
            )
            monitor_row = self._start_monitoring(
                token_id=row.token_id,
                intended_buy_size=row.intended_buy_size,
                protected_qty=protected,
                entry_price_source=entry_source,
                parent_correlation_id=row.parent_correlation_id,
                parent_buy_intent_id=row.parent_buy_intent_id,
                parent_client_order_id=row.parent_client_order_id,
                entry_price=entry_price,
            )
            active_fields = self._position_fact_fields(
                intended_buy_size=row.intended_buy_size,
                allocated=allocated,
                avail=avail,
                protected=protected,
                entry_price=entry_price,
                entry_price_source=entry_source,
                lifecycle_phase=PHASE_POSITION_ACTIVE,
            )
            self.emit_tp_sl(
                coord,
                "tp_sl_position_active",
                row.parent_correlation_id,
                token_id=str(row.token_id),
                owner_id=self.cfg.owner_id,
                price_source=self.cfg.monitor.price_source,
                **active_fields,
                **monitor_row.threshold_evidence,
            )
            self.emit_tp_sl(
                coord,
                "tp_sl_registered",
                row.parent_correlation_id,
                token_id=str(row.token_id),
                owner_id=self.cfg.owner_id,
                price_source=self.cfg.monitor.price_source,
                **active_fields,
                **monitor_row.threshold_evidence,
            )
            self._monitoring = monitor_row
            self._monitor_started_mono = monotonic_s()
        self._pending_inventory = still_pending

    def _planned_exit_from_allocation(
        self,
        coord: RuntimeCoordinator,
        token_id: TokenId,
    ) -> Decimal:
        ledger = coord.allocation_ledger
        allocated = (
            ledger.get_available_allocated(self.cfg.owner_id, token_id)
            if ledger is not None
            else Decimal("0")
        )
        exit_cfg = self.cfg.exit
        if exit_cfg.size_mode == TP_SL_SIZE_MODE_FULL:
            return allocated
        if exit_cfg.size_mode == TP_SL_SIZE_MODE_PERCENT:
            return allocated * exit_cfg.percent
        if exit_cfg.size_mode == TP_SL_SIZE_MODE_FIXED:
            return exit_cfg.fixed_size or Decimal("0")
        return Decimal("0")

    def _check_trigger(
        self,
        price: Decimal,
        *,
        take_profit_trigger_price: Decimal | None,
        stop_loss_trigger_price: Decimal | None,
    ) -> str | None:
        if take_profit_trigger_price is not None and price >= take_profit_trigger_price:
            return "take_profit"
        if stop_loss_trigger_price is not None and price <= stop_loss_trigger_price:
            return "stop_loss"
        return None

    async def _observe_price(
        self,
        coord: RuntimeCoordinator,
        row: _ActiveMonitor,
        *,
        live_clob_client: object | None,
    ) -> Decimal | None:
        mon = self.cfg.monitor
        if mon.price_source == TP_SL_PRICE_SOURCE_FIXTURE:
            prices = mon.fixture_prices
            if row.fixture_index >= len(prices):
                return None
            price = prices[row.fixture_index]
            row.fixture_index += 1
            return price
        if mon.price_source == TP_SL_PRICE_SOURCE_BEST_BID and live_clob_client is not None:
            market_info_cache = getattr(coord, "market_info_cache", None)
            market_info = None
            if market_info_cache is not None:
                try:
                    market_info = await market_info_cache.get(row.token_id)
                except Exception:  # noqa: BLE001
                    market_info = None
            from tyrex_pm.strategies.sell_test.pricing import fetch_order_book

            try:
                book = await fetch_order_book(live_clob_client, str(row.token_id))
            except Exception:  # noqa: BLE001
                return None
            best_bid, _ = best_levels_from_book(book)
            return best_bid
        return None

    async def tick_monitor(
        self,
        coord: RuntimeCoordinator,
        *,
        live_clob_client: object | None = None,
    ) -> None:
        if not self.exit_enabled or self._terminal or self._exit_in_flight:
            return
        row = self._monitoring
        if row is None or row.triggered:
            return
        allocated, avail, protected, _snap = _position_snapshot(
            coord,
            owner_id=self.cfg.owner_id,
            token_id=row.token_id,
        )
        if not _qty_positive(protected):
            self._pending_inventory.append(self._demote_monitor_to_pending(row))
            self._monitoring = None
            self._monitor_started_mono = None
            self.try_promote_inventory(coord, source="periodic_refresh")
            return
        price = await self._observe_price(coord, row, live_clob_client=live_clob_client)
        if price is not None:
            self.emit_tp_sl(
                coord,
                "tp_sl_monitor_tick",
                row.parent_correlation_id,
                token_id=str(row.token_id),
                lifecycle_phase=PHASE_POSITION_ACTIVE,
                observed_price=str(price),
                fixture_index=row.fixture_index,
                protected_qty=str(protected),
                **row.threshold_evidence,
            )
        trigger = (
            self._check_trigger(
                price,
                take_profit_trigger_price=row.take_profit_trigger_price,
                stop_loss_trigger_price=row.stop_loss_trigger_price,
            )
            if price is not None
            else None
        )
        if trigger is None:
            return
        row.triggered = True
        row.trigger_kind = trigger
        row.observed_price = price
        self.emit_tp_sl(
            coord,
            "tp_sl_triggered",
            row.parent_correlation_id,
            token_id=str(row.token_id),
            lifecycle_phase=PHASE_TRIGGERED_EXIT,
            trigger=trigger,
            observed_price=str(price),
            **row.threshold_evidence,
        )
        planned_before = self._planned_exit_from_allocation(coord, row.token_id)
        row.planned_before_clamp = planned_before
        row.allocated_available = allocated
        row.available_to_sell = avail
        final_size = _compute_final_exit_size(planned_before, allocated, avail)
        row.final_size = final_size
        row.sizing_evidence = {
            **row.threshold_evidence,
            "lifecycle_phase": PHASE_TRIGGERED_EXIT,
            "owner_id": self.cfg.owner_id,
            "intended_buy_size": str(row.intended_buy_size),
            "protected_qty": str(row.protected_qty),
            "entry_price": str(row.entry_price),
            "entry_price_source": row.entry_price_source,
            "trigger": trigger,
            "observed_price": str(price),
            "planned_before_clamp": str(planned_before),
            "allocated_available": str(allocated),
            "available_to_sell": str(avail),
            "final_size": str(final_size),
        }
        self.emit_tp_sl(
            coord,
            "tp_sl_exit_sizing",
            row.parent_correlation_id,
            token_id=str(row.token_id),
            tp_sl_sizing=row.sizing_evidence,
        )

    def _handle_trigger_blocked_no_inventory(
        self,
        coord: RuntimeCoordinator,
        row: _ActiveMonitor,
    ) -> None:
        allocated, avail, protected, snap = _position_snapshot(
            coord,
            owner_id=self.cfg.owner_id,
            token_id=row.token_id,
        )
        self.emit_tp_sl(
            coord,
            "tp_sl_trigger_blocked_no_sellable_inventory",
            row.parent_correlation_id,
            token_id=str(row.token_id),
            trigger=row.trigger_kind,
            observed_price=str(row.observed_price) if row.observed_price is not None else None,
            wallet_position_qty=snap["wallet_position_qty"],
            in_flight_qty=snap["in_flight_qty"],
            **self._position_fact_fields(
                intended_buy_size=row.intended_buy_size,
                allocated=allocated,
                avail=avail,
                protected=protected,
                entry_price=row.entry_price,
                entry_price_source=row.entry_price_source,
                lifecycle_phase=PHASE_ENTRY_PENDING,
            ),
        )
        self._pending_inventory.append(self._demote_monitor_to_pending(row))
        self._monitoring = None
        self._monitor_started_mono = None

    async def resolve_triggered_work_units(
        self,
        *,
        coord: RuntimeCoordinator,
        live_clob_client: object | None,
    ) -> list[IntentWorkUnit]:
        if not self.exit_enabled or self._terminal or self._exit_in_flight:
            return []
        row = self._monitoring
        if row is None or not row.triggered:
            return []
        planned_before = self._planned_exit_from_allocation(coord, row.token_id)
        allocated, avail, protected, _snap = _position_snapshot(
            coord,
            owner_id=self.cfg.owner_id,
            token_id=row.token_id,
        )
        row.planned_before_clamp = planned_before
        row.allocated_available = allocated
        row.available_to_sell = avail
        final_size = _compute_final_exit_size(planned_before, allocated, avail)
        row.final_size = final_size
        row.sizing_evidence = {
            **row.threshold_evidence,
            "lifecycle_phase": PHASE_TRIGGERED_EXIT,
            "owner_id": self.cfg.owner_id,
            "intended_buy_size": str(row.intended_buy_size),
            "protected_qty": str(row.protected_qty),
            "entry_price": str(row.entry_price),
            "entry_price_source": row.entry_price_source,
            "trigger": row.trigger_kind,
            "observed_price": str(row.observed_price) if row.observed_price is not None else None,
            "planned_before_clamp": str(planned_before),
            "allocated_available": str(allocated),
            "available_to_sell": str(avail),
            "final_size": str(final_size),
        }
        if row.final_size <= 0:
            self._handle_trigger_blocked_no_inventory(coord, row)
            return []
        exit_cfg = self.cfg.exit
        fallback_price = exit_cfg.limit_price
        if fallback_price is None and self.cfg.buy.limit_price is not None:
            fallback_price = self.cfg.buy.limit_price
        override_price: Decimal | None = None
        pricing_evidence: dict[str, object] | None = None
        if exit_cfg.pricing_mode == SELL_TEST_PRICING_AUTO and live_clob_client is not None:
            market_info_cache = getattr(coord, "market_info_cache", None)
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
                aggression_ticks=exit_cfg.aggression_ticks,
                fallback_price=fallback_price,
                min_price=exit_cfg.min_price,
            )
            pricing_evidence = resolved.to_evidence()
            if resolved.source == "auto_book":
                override_price = resolved.price
        chosen_price = override_price if override_price is not None else fallback_price
        exit_int = ExitIntent(
            token_id=row.token_id,
            side=Side.SELL,
            size=row.final_size,
            limit_price=chosen_price,
            order_style=exit_cfg.order_style,
        )
        prov: dict[str, object] = {
            "source": TP_SL_TEST_INTENT_SOURCE,
            "allocation_owner_id": self.cfg.owner_id,
            "parent_correlation_id": row.parent_correlation_id,
            "parent_buy_intent_id": row.parent_buy_intent_id,
            "parent_client_order_id": row.parent_client_order_id,
        }
        if row.sizing_evidence:
            prov["tp_sl_sizing"] = row.sizing_evidence
        if pricing_evidence:
            prov["tp_sl_exit_pricing"] = pricing_evidence
        self.emit_tp_sl(
            coord,
            "tp_sl_exit_intent_emitted",
            row.parent_correlation_id,
            token_id=str(row.token_id),
            lifecycle_phase=PHASE_TRIGGERED_EXIT,
            sell_size=str(row.final_size),
            limit_price=str(chosen_price) if chosen_price is not None else None,
        )
        self.mark_exit_in_flight()
        self._monitoring = None
        return [
            IntentWorkUnit(
                intent=exit_int,
                correlation_id=row.parent_correlation_id,
                intent_fact_extensions=prov,
            )
        ]

    def emit_timeout_waiting_for_inventory(self, coord: RuntimeCoordinator) -> None:
        if self._terminal:
            return
        corr = "tp_sl_inventory_timeout"
        if self._pending_inventory:
            corr = self._pending_inventory[0].parent_correlation_id
        elif self._monitoring is not None:
            corr = self._monitoring.parent_correlation_id
        self.emit_tp_sl(coord, "tp_sl_timeout_waiting_for_inventory", corr)
        self.mark_exit_terminal("inventory_timeout")

    def emit_timeout_waiting_for_trigger(self, coord: RuntimeCoordinator) -> None:
        if self._terminal or self._monitoring is None:
            return
        self.emit_tp_sl(
            coord,
            "tp_sl_timeout_waiting_for_trigger",
            self._monitoring.parent_correlation_id,
        )
        self.mark_exit_terminal("trigger_timeout")


class TpSlTestStrategy:
    """One BUY, TP/SL monitor, one allocation-aware SELL on trigger."""

    def __init__(self, cfg: TpSlTestStrategyConfig) -> None:
        self._cfg = cfg
        self.tp_sl_state = TpSlTestState(cfg)
        self._buy_work_issued = False
        self._buy_submit_succeeded = False
        self._buy_correlation_id = "tp_sl_test_buy"
        self._resolved_buy_price: Decimal | None = None
        self._buy_pricing_evidence: dict[str, object] | None = None

    @property
    def cfg(self) -> TpSlTestStrategyConfig:
        return self._cfg

    @property
    def buy_submit_succeeded(self) -> bool:
        return self._buy_submit_succeeded

    @property
    def effective_buy_price(self) -> Decimal | None:
        if self._resolved_buy_price is not None:
            return self._resolved_buy_price
        return self._cfg.buy.limit_price

    def set_resolved_buy_price(self, price: Decimal, *, evidence: dict[str, object]) -> None:
        self._resolved_buy_price = price
        self._buy_pricing_evidence = evidence

    @property
    def entry_price_source(self) -> str:
        if self._buy_pricing_evidence is not None:
            src = self._buy_pricing_evidence.get("source")
            if src is not None and str(src).strip():
                return str(src)
        if self._cfg.buy.pricing_mode == SELL_TEST_PRICING_AUTO:
            return "auto_resolved"
        return "buy_intent_limit"

    def initial_buy_work_units(self) -> list[IntentWorkUnit]:
        if not self._cfg.enabled or not self._cfg.buy.enabled:
            return []
        if self._buy_work_issued and self._cfg.run_once:
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
            "source": TP_SL_TEST_INTENT_SOURCE,
            "allocation_owner_id": self._cfg.owner_id,
            "tp_sl_test_token_id": self._cfg.token_id,
        }
        if self._buy_pricing_evidence is not None:
            ext["tp_sl_test_buy_pricing"] = self._buy_pricing_evidence
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
        intent = ap.intent
        entry_price = intent.limit_price if isinstance(intent, EnterIntent) else None
        if entry_price is None or entry_price <= 0:
            entry_price = self.effective_buy_price
        if entry_price is None or entry_price <= 0:
            return
        self.tp_sl_state.register_after_successful_buy(
            ap,
            coord,
            parent_correlation_id=parent_correlation_id,
            entry_price=entry_price,
            entry_price_source=self.entry_price_source,
            execution_mode=execution_mode,
            apply_shadow_fill=apply_local_shadow_fill,
            match_evidence=match_evidence,
        )
        self.tp_sl_state.try_promote_inventory(coord, source="post_buy_ack")
        if coord.scheduled_exit_demo_try_arm is not None:
            coord.scheduled_exit_demo_try_arm(source="post_buy_ack")

    def is_done(self) -> bool:
        if not self._cfg.run_once:
            return False
        if self._cfg.buy.enabled and not self._buy_submit_succeeded:
            return False
        if self._cfg.buy.enabled and not self._cfg.exit.enabled:
            return True
        if not self._cfg.exit.enabled:
            return True
        st = self.tp_sl_state
        if not st.is_terminal:
            return False
        if st.outcome not in _DONE_OUTCOMES:
            return False
        return not st.has_open_work

    def has_pending_inventory_wait(self) -> bool:
        st = self.tp_sl_state
        return bool(st._pending_inventory) and not st.is_terminal


def try_arm_tp_sl_pending(
    strat: object,
    coord: RuntimeCoordinator,
    *,
    source: ArmSource = "post_buy_ack",
) -> None:
    if isinstance(strat, TpSlTestStrategy):
        strat.tp_sl_state.try_promote_inventory(coord, source=source)
