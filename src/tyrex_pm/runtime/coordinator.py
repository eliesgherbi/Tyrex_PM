from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import RiskContext
from tyrex_pm.risk.in_flight import derive_in_flight_buy_reservations
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.venue.polymarket.market_info import MarketInfoCache


@dataclass
class RuntimeCoordinator:
    """Store-backed truth for risk + strategy holdings."""

    wallet: WalletStore
    orders: OrderStore
    health: HealthRuntime = field(default_factory=HealthRuntime)
    #: Provisional repair window (s): age below this is non-blocking ``provisional_pending_venue``.
    submit_grace_s: float = 15.0
    #: Age (s) after which a provisional row absent from a fresh merged book is auto-resolved
    #: as ``UNKNOWN_TERMINAL`` (non-blocking) — only when WS is fresh AND no venue restart suspected.
    provisional_unknown_terminal_timeout_s: float = 60.0
    #: Back-compat alias kept so external operators / scripts can keep using the old name.
    venue_confirm_provisional_timeout_s: float = 60.0
    #: Adoption window (s) for the venue-truth mirror race: when REST sees an order id we don't
    #: yet track locally, look for a no-vid provisional row submitted within this window. Adopt
    #: on strong match (token+side+size+price), otherwise stay non-blocking briefly, otherwise block.
    adoption_grace_s: float = 5.0
    #: Last reconcile-state signature emitted as a fact. The reconcile pipeline computes a
    #: deterministic tuple of operator-relevant fields and skips writing a new fact when it
    #: matches this value, collapsing tight bursts of unchanged reconciles. ``None`` = no
    #: fact emitted yet (always emit on the first reconcile of the run).
    last_reconcile_signature: tuple | None = None
    #: Last ``wallet_sync`` fact signature: ``(usdc_balance, usdc_allowance, last_sync_ts,
    #: last_positions_sync_ts, position_count, open_order_count, mark_count)`` so refresh
    #: ticks that didn't move any of those numbers do not flood the report.
    last_wallet_sync_signature: tuple | None = None
    #: Live mode only: per-token venue-truth metadata cache (tick_size,
    #: min_order_size, neg_risk, fee_rate_bps, outcomes). Resolved on demand
    #: by the live pipeline before risk evaluation; ``build_risk_context``
    #: snapshots it into ``RiskContext.market_info`` so the venue-min-size
    #: gate uses *venue truth* instead of the YAML default. Shadow mode and
    #: unit tests pass ``None`` and the gate falls back to the YAML default
    #: (see :mod:`tyrex_pm.risk.venue_min_size`).
    market_info_cache: MarketInfoCache | None = None
    #: When set, called after user-WS or REST updates that may change positions — used to arm
    #: live scheduled demo exits once sellable inventory is visible (see ``scheduled_exit_demo``).
    scheduled_exit_demo_try_arm: Callable[[], None] | None = None

    def holdings(self) -> dict[TokenId, Decimal]:
        return {tid: p.qty for tid, p in self.wallet.positions.items()}

    def marks_for_risk(self) -> dict[TokenId, Decimal]:
        m: dict[TokenId, Decimal] = {}
        for tid, p in self.wallet.positions.items():
            if p.avg_price_usd is not None:
                m[tid] = p.avg_price_usd
        return m

    def build_risk_context(self, app: AppConfig) -> RiskContext:
        # Derive in-flight BUY reservations from the OrderStore so deployment + capital gates
        # see the orders the venue may have already locked collateral for, but which the
        # merged wallet view (REST/WS) has not mirrored yet. See risk.in_flight for lifecycle.
        in_flight = derive_in_flight_buy_reservations(self.orders, self.wallet)
        return RiskContext(
            execution_mode=app.runtime.execution_mode,
            wallet_positions=tuple(self.wallet.positions.values()),
            open_orders=self.wallet.open_orders,
            usdc_balance=self.wallet.usdc_balance,
            usdc_allowance=self.wallet.usdc_allowance,
            last_wallet_sync_ts=self.wallet.last_sync_ts,
            mark_prices=dict(self.marks_for_risk()),
            kill_switch=False,
            health_ok=not self.health.reconcile_drift,
            heartbeat_ok=(
                True
                if app.runtime.execution_mode == ExecutionMode.SHADOW
                else self.health.heartbeat_ok
            ),
            clob_session_ok=(
                True
                if app.runtime.execution_mode == ExecutionMode.SHADOW
                else self.health.clob_session_ok
            ),
            in_flight_order_count=self.orders.in_flight_order_count,
            orders_in_flight_by_token=dict(self.orders.in_flight_by_token),
            reconcile_drift=self.health.reconcile_drift,
            venue_truth_stale=(
                False
                if app.runtime.execution_mode == ExecutionMode.SHADOW or self.health.user_ws_rest_only
                else self.health.venue_truth_stale
            ),
            in_flight_buy_reservations=in_flight.reservations,
            first_v2_sync_complete=(
                True
                if app.runtime.execution_mode == ExecutionMode.SHADOW
                else self.health.first_v2_sync_complete
            ),
            market_info=(
                self.market_info_cache.snapshot()
                if self.market_info_cache is not None
                else {}
            ),
        )
