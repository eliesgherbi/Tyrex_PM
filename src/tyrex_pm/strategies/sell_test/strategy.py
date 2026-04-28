"""Standalone strategy for validating the V2 SELL / exit path end-to-end.

Goal: exercise BUY → sellable-inventory detection → SELL ExitIntent → risk →
OMS → venue truth without any guru-copy logic. Designed so a SELL test does
not require tweaking the guru-follow strategy.

State machine (per ``token_id``):

* ``initial_buy_work_units()`` returns one ``IntentWorkUnit`` carrying the
  configured BUY ``EnterIntent`` the first time it is called (when ``run_once``
  is true; otherwise on every call).
* After that BUY clears the OMS submit ack path, the runtime calls
  :meth:`SellTestStrategy.on_buy_submit_ack` which:

  * **Shadow** + ``apply_local_shadow_fill=True`` → arms the SELL timer
    immediately (synthetic instant fill mirrors live sellable inventory).
  * **Live** → registers a pending row; the SELL timer arms only when
    :meth:`SellTestState.try_arm_live_pending` observes
    ``available_to_sell >= planned_sell_size`` (same formula as
    :func:`tyrex_pm.risk.inventory.available_to_sell`). The runtime invokes
    ``try_arm_live_pending`` after every wallet/positions update.
* :meth:`SellTestState.pop_due_work_units` returns the SELL
  ``IntentWorkUnit`` once the configured ``sell.delay_s`` has elapsed since
  arming.

The strategy stops producing further intents for the token once the SELL has
been emitted (when ``run_once`` is true). Reset by restarting the process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.risk.inventory import available_to_sell
from tyrex_pm.runtime.config import (
    SELL_TEST_PRICING_AUTO,
    SellTestStrategyConfig,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
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
    _sell_emitted: bool = False

    @property
    def sell_enabled(self) -> bool:
        return self.cfg.enabled and self.cfg.sell.enabled

    @property
    def has_open_work(self) -> bool:
        """True while a pending or armed sell row still exists for this state."""
        return bool(self._pending_live or self._armed)

    def register_after_successful_buy(
        self,
        ap: ApprovedIntent,
        *,
        parent_correlation_id: str,
        execution_mode: ExecutionMode,
        apply_shadow_fill: bool,
    ) -> None:
        """Called from the pipeline once a BUY clears OMS submit ack.

        Shadow + instant fill: arm the timer immediately. Live: register a
        pending row to be armed by :meth:`try_arm_live_pending` once
        sellable inventory is observed.
        """
        if not self.sell_enabled:
            return
        if self.cfg.run_once and self._sell_emitted:
            return
        intent = ap.intent
        if not isinstance(intent, EnterIntent) or intent.side != Side.BUY:
            return
        if str(intent.token_id) != self.cfg.token_id:
            return
        sell_price = self.cfg.sell.limit_price if self.cfg.sell.limit_price is not None else intent.limit_price
        sell_style = self.cfg.sell.order_style
        delay = float(self.cfg.sell.delay_s)
        now = monotonic_s()
        if execution_mode == ExecutionMode.SHADOW and apply_shadow_fill:
            self._armed.append(
                _ArmedSellExit(
                    due_mono=now + delay,
                    token_id=intent.token_id,
                    sell_size=intent.size,
                    sell_limit_price=sell_price,
                    sell_order_style=sell_style,
                    parent_correlation_id=parent_correlation_id,
                    parent_buy_intent_id=str(intent.intent_id),
                    parent_client_order_id=str(ap.client_order_id),
                )
            )
            return
        self._pending_live.append(
            _PendingSellArm(
                token_id=intent.token_id,
                planned_sell_size=intent.size,
                sell_limit_price=sell_price,
                sell_order_style=sell_style,
                parent_correlation_id=parent_correlation_id,
                parent_buy_intent_id=str(intent.intent_id),
                parent_client_order_id=str(ap.client_order_id),
            )
        )

    def try_arm_live_pending(self, coord: RuntimeCoordinator) -> None:
        """Promote pending live rows to armed once the venue shows sellable inventory."""
        if not self.sell_enabled or not self._pending_live:
            return
        positions = {p.token_id: p for p in coord.wallet.positions.values()}
        in_flight = dict(coord.orders.in_flight_by_token)
        delay = float(self.cfg.sell.delay_s)
        now = monotonic_s()
        still_pending: list[_PendingSellArm] = []
        for row in self._pending_live:
            avail = available_to_sell(
                token_id=row.token_id,
                positions=positions,
                in_flight=in_flight,
            )
            if avail >= row.planned_sell_size:
                self._armed.append(
                    _ArmedSellExit(
                        due_mono=now + delay,
                        token_id=row.token_id,
                        sell_size=row.planned_sell_size,
                        sell_limit_price=row.sell_limit_price,
                        sell_order_style=row.sell_order_style,
                        parent_correlation_id=row.parent_correlation_id,
                        parent_buy_intent_id=row.parent_buy_intent_id,
                        parent_client_order_id=row.parent_client_order_id,
                    )
                )
            else:
                still_pending.append(row)
        self._pending_live = still_pending

    def pop_due_rows(self, now_mono: float | None = None) -> list[_ArmedSellExit]:
        """Drain (and remove) any armed rows whose timer has fired.

        Split from work-unit construction so the auto-pricing path can
        override ``sell_limit_price`` per row before a frozen ``ExitIntent``
        is built. Synchronous and side-effect-free aside from removing the
        returned rows from ``self._armed``.
        """
        if not self.sell_enabled or not self._armed:
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
        return due

    def _build_work_unit(
        self,
        row: _ArmedSellExit,
        *,
        override_price: Decimal | None = None,
        pricing_evidence: dict[str, object] | None = None,
    ) -> IntentWorkUnit:
        """Materialize an ``IntentWorkUnit`` for one drained row.

        ``override_price`` wins over ``row.sell_limit_price`` when present so
        the auto-pricing path can substitute the venue-derived price without
        mutating the frozen ``_ArmedSellExit`` dataclass. ``pricing_evidence``
        is folded into ``intent_fact_extensions`` for forensic transparency.
        """
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
        if self.cfg.run_once:
            self._sell_emitted = True
        return IntentWorkUnit(
            intent=exit_int,
            correlation_id=row.parent_correlation_id,
            intent_fact_extensions=prov,
        )

    def pop_due_work_units(self, now_mono: float | None = None) -> list[IntentWorkUnit]:
        """Return SELL work units whose timer has fired (``due_mono <= now``).

        Synchronous fast path used when no auto-pricing is required (shadow
        mode or ``sell.pricing_mode == "fixed"``). Async callers that want
        venue-derived prices should use
        :meth:`SellTestStrategy.resolve_due_work_units` instead.
        """
        return [self._build_work_unit(row) for row in self.pop_due_rows(now_mono)]


class SellTestStrategy:
    """Validation strategy: emit one BUY then one SELL after sellable inventory."""

    def __init__(self, cfg: SellTestStrategyConfig) -> None:
        self._cfg = cfg
        self.sell_test_state = SellTestState(cfg)
        self._buy_emitted: bool = False
        self._buy_correlation_id: str = f"sell_test:{cfg.token_id}"
        #: Set by :meth:`set_resolved_buy_price` when the runtime auto-prices
        #: the BUY. ``None`` means "use ``cfg.buy.limit_price`` verbatim".
        self._resolved_buy_price: Decimal | None = None
        #: Optional evidence dict captured alongside ``_resolved_buy_price``
        #: for the BUY intent fact (best_bid/best_ask/tick/etc.).
        self._buy_pricing_evidence: dict[str, object] | None = None

    @property
    def cfg(self) -> SellTestStrategyConfig:
        return self._cfg

    @property
    def buy_correlation_id(self) -> str:
        """Stable correlation id used for the initial BUY (sell rows reuse it as parent)."""
        return self._buy_correlation_id

    @property
    def buy_already_emitted(self) -> bool:
        return self._buy_emitted

    @property
    def effective_buy_price(self) -> Decimal | None:
        """The price the next BUY will use (auto-resolved if set, else config)."""
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
        """Override the BUY ``limit_price`` for the next ``initial_buy_work_units`` call.

        Called by :func:`tyrex_pm.runtime.app._run_sell_test_loop` in live mode
        when ``buy.pricing_mode == "auto"``. ``evidence`` is folded into the
        BUY intent's ``intent_fact_extensions`` so the operator can see exactly
        which book levels the price came from.
        """
        if price <= 0:
            raise ValueError(f"resolved buy price must be positive, got {price!r}")
        self._resolved_buy_price = price
        self._buy_pricing_evidence = evidence

    def initial_buy_work_units(self) -> list[IntentWorkUnit]:
        """Build the one-shot BUY ``IntentWorkUnit`` (or ``[]`` when nothing to do).

        ``buy.notional_usd`` and the effective BUY price (resolved by
        :meth:`set_resolved_buy_price` when auto-pricing is enabled, else
        ``cfg.buy.limit_price``) together determine the BUY size:
        ``size = notional_usd / price``. Size is left unrounded here; the
        venue-min-size gate and tick quantizer downstream decide what's
        actually submittable.
        """
        if not self._cfg.enabled or not self._cfg.buy.enabled:
            return []
        if self._cfg.run_once and self._buy_emitted:
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
        wu = IntentWorkUnit(
            intent=intent,
            correlation_id=self._buy_correlation_id,
            intent_fact_extensions=ext,
        )
        if self._cfg.run_once:
            self._buy_emitted = True
        return [wu]

    async def resolve_due_work_units(
        self,
        *,
        coord: RuntimeCoordinator,
        live_clob_client: object | None,
    ) -> list[IntentWorkUnit]:
        """Async equivalent of :meth:`SellTestState.pop_due_work_units` with auto-pricing.

        For each armed-and-due row:

        * If ``sell.pricing_mode == "auto"`` and a live CLOB client is
          available, fetch the venue book and pick a marketable SELL price
          (``best_bid - aggression_ticks * tick_size``, with optional
          ``min_price`` floor). On any failure the strategy falls back to the
          row's configured ``sell_limit_price``.
        * Otherwise, behaves exactly like the sync ``pop_due_work_units``.

        Returns the list of work units to be processed by the pipeline.
        Pricing evidence is captured in each work unit's
        ``intent_fact_extensions["sell_test_pricing"]`` so the operator can
        audit the chosen price after the run.
        """
        rows = self.sell_test_state.pop_due_rows()
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
                    except Exception:  # noqa: BLE001 — pricing falls back regardless
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
    ) -> None:
        """Hook invoked by the pipeline after a successful BUY OMS submit ack."""
        self.sell_test_state.register_after_successful_buy(
            ap,
            parent_correlation_id=parent_correlation_id,
            execution_mode=execution_mode,
            apply_shadow_fill=apply_local_shadow_fill,
        )
        if coord.scheduled_exit_demo_try_arm is not None:
            coord.scheduled_exit_demo_try_arm()

    def is_done(self) -> bool:
        """True when the strategy has nothing left to do (run_once + sell emitted + drained)."""
        if not self._cfg.run_once:
            return False
        if not self._buy_emitted:
            return False
        if self._cfg.buy.enabled and not self._cfg.sell.enabled:
            return True
        if not self._cfg.sell.enabled:
            return True
        return self.sell_test_state._sell_emitted and not self.sell_test_state.has_open_work


def try_arm_sell_test_pending(strat: object, coord: RuntimeCoordinator) -> None:
    """Helper installed as ``coord.scheduled_exit_demo_try_arm`` for sell_test runs.

    Mirrors :func:`tyrex_pm.strategies.guru_follow.scheduled_exit_demo.try_arm_scheduled_exit_demos`
    but routes to the sell_test state. The two helpers are kept separate so the guru
    strategy is unaware of the test strategy and vice versa.
    """
    state = getattr(strat, "sell_test_state", None)
    if isinstance(state, SellTestState):
        state.try_arm_live_pending(coord)
