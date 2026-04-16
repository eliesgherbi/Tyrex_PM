"""
Venue Sync Truth — ``WalletSyncActor`` and supporting types.

Continuously discovers all Polymarket markets the wallet has exposure on
(positions or resting orders), ensures they are in Nautilus ``Cache``, and
lets the engine's continuous reconciliation loop handle order/position/WS
subscription from there.

See ``docs/implementation/venue_sync_truth/02_components.md`` for the full spec.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket.common.symbol import (
    get_polymarket_condition_id,
    get_polymarket_token_id,
)
from nautilus_trader.common.actor import Actor
from nautilus_trader.core.message import Event
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import (
    ContingencyType,
    LiquiditySide,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.model.events import OrderAccepted, OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    VenueOrderId,
    Venue,
)
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.orders.unpacker import OrderUnpacker

from tyrex_pm.runtime.guru_instrument_dynamic import (
    GuruInstrumentDynamicController,
)

_LOG = logging.getLogger(__name__)
_POLYMARKET_VENUE = Venue(POLYMARKET)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnresolvableEntry:
    """Tracks a ``condition_id`` that has failed resolution across cycles."""

    condition_id: str
    token_ids: tuple[str, ...]
    last_detail: str
    retry_count: int
    terminal: bool


@dataclass(frozen=True, slots=True)
class WalletSyncConfig:
    poll_interval_seconds: float = 15.0
    startup_deadline_seconds: float = 120.0
    per_instrument_max_retries: int = 3
    data_api_base_url: str = "https://data-api.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    gamma_http_timeout_seconds: float = 15.0
    clob_host: str = "https://clob.polymarket.com"
    position_reconciliation_enabled: bool = False
    position_reconciliation_shadow_mode: bool = True
    data_api_lag_tolerance_seconds: float = 60.0
    position_reconciliation_deferral_max: int = 5
    recently_reconciled_ttl_seconds: float = 60.0
    reconcile_venue_has_more: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    instrument_id: InstrumentId
    venue_qty: Decimal
    cache_qty: Decimal
    diff_direction: str  # "close" | "partial_reduce" | "deferred" | "skipped_ttl" | "venue_has_more"
    deferred: bool
    defer_count: int
    strategy_id: StrategyId | None = None  # original position's strategy; None when deferred


@dataclass(frozen=True, slots=True)
class WalletSyncResult:
    cycle_number: int
    positions_fetched: int
    orders_fetched: int
    condition_ids_on_wallet: int
    condition_ids_in_cache: int
    instruments_newly_added: int
    resolution_failures: int
    unresolvable_retrying: int
    unresolvable_terminal: int
    http_positions_ok: bool
    http_orders_ok: bool
    first_sync_complete: bool
    elapsed_seconds: float
    failure_details: dict[str, int]
    reconciliation_actions: list[ReconciliationAction] = field(default_factory=list)
    reconciliation_sent_count: int = 0
    reconciliation_deferred_count: int = 0
    reconciliation_skipped_recently_reconciled: int = 0


@runtime_checkable
class WalletSyncHealthAdapter(Protocol):
    """Read-only view consumed by ``NautilusLiveExecutionHealthSource``."""

    @property
    def first_sync_complete(self) -> bool: ...

    @property
    def startup_deadline_exceeded(self) -> bool: ...

    @property
    def last_successful_cycle_utc(self) -> datetime | None: ...

    @property
    def consecutive_failure_count(self) -> int: ...

    @property
    def terminally_unresolvable_count(self) -> int: ...

    @property
    def poll_interval_seconds(self) -> float: ...

    @property
    def stuck_deferral_count(self) -> int: ...


# ---------------------------------------------------------------------------
# Actor
# ---------------------------------------------------------------------------


class WalletSyncActor(Actor):
    """
    Continuously discovers all markets the wallet has exposure on and ensures
    they are in Cache.

    Manages three categories of sync state:

    - ``_first_sync_complete``: flipped to True only when at least one HTTP
      source returned and every wallet condition_id is either cached or
      terminally unresolvable.
    - ``_unresolvable_condition_ids``: tracks per-instrument resolution failures
      with retry counts.  After ``per_instrument_max_retries`` cycles a
      condition_id is marked terminal and excluded from the completeness check.
    - ``_start_mono``: monotonic timestamp from ``on_start``, used to enforce
      ``startup_deadline_seconds``.

    The actor has no persistent state of its own and no cleanup obligation on
    restart.
    """

    def __init__(
        self,
        config: WalletSyncConfig,
        clob_client: Any,
        dynamic_controller: GuruInstrumentDynamicController,
        *,
        fact_emit: Callable[[str, dict[str, Any]], None] | None = None,
        positions_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
        venue_state: Any | None = None,
    ) -> None:
        super().__init__()
        self._wsconfig = config
        self._clob = clob_client
        self._dynamic_ctrl = dynamic_controller
        self._venue_state = venue_state
        self._known_condition_ids: set[str] = set()
        self._first_sync_complete: bool = False
        self._unresolvable_condition_ids: dict[str, UnresolvableEntry] = {}
        self._start_mono: float = 0.0
        self._fact_emit = fact_emit
        self._sync_count: int = 0
        self._instruments_discovered: int = 0
        self._last_successful_cycle_utc: datetime | None = None
        self._consecutive_failure_count: int = 0
        self._positions_fetcher = positions_fetcher
        # Position reconciliation state
        self._recently_reconciled: dict[InstrumentId, float] = {}
        self._deferred_reconciliations: dict[InstrumentId, int] = {}
        self._reconciliation_count: int = 0
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._cycle_in_progress: bool = False

    # -- Actor lifecycle (all synchronous — actor.pxd:93-94) ----------------

    def on_start(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._start_mono = time.monotonic()
        self.run_in_executor(self._sync_cycle_wrapper)
        self.clock.set_timer(
            name="wallet_sync",
            interval=timedelta(seconds=self._wsconfig.poll_interval_seconds),
            callback=self.on_timer,
        )

    def on_stop(self) -> None:
        self.clock.cancel_timer("wallet_sync")
        self.cancel_all_tasks()

    def on_timer(self, event: Event) -> None:  # noqa: ARG002
        if self._cycle_in_progress:
            return
        self._cycle_in_progress = True
        self.run_in_executor(self._sync_cycle_wrapper)

    # -- Properties for readiness gate / health source ----------------------

    @property
    def first_sync_complete(self) -> bool:
        return self._first_sync_complete

    @property
    def startup_deadline_exceeded(self) -> bool:
        if self._first_sync_complete:
            return False
        if self._start_mono == 0.0:
            return False
        return (
            time.monotonic() - self._start_mono
            > self._wsconfig.startup_deadline_seconds
        )

    @property
    def last_successful_cycle_utc(self) -> datetime | None:
        return self._last_successful_cycle_utc

    @property
    def consecutive_failure_count(self) -> int:
        return self._consecutive_failure_count

    @property
    def terminally_unresolvable_count(self) -> int:
        return sum(
            1 for e in self._unresolvable_condition_ids.values() if e.terminal
        )

    @property
    def poll_interval_seconds(self) -> float:
        return self._wsconfig.poll_interval_seconds

    @property
    def sync_count(self) -> int:
        return self._sync_count

    @property
    def instruments_discovered(self) -> int:
        return self._instruments_discovered

    @property
    def stuck_deferral_count(self) -> int:
        return sum(
            1 for count in self._deferred_reconciliations.values()
            if count >= self._wsconfig.position_reconciliation_deferral_max
        )

    @property
    def reconciliation_count(self) -> int:
        return self._reconciliation_count

    # -- Sync cycle wrapper (executor thread entry point) -------------------

    def _sync_cycle_wrapper(self) -> None:
        try:
            result = self._sync_cycle()
            self._emit_sync_fact(result)
            if result.reconciliation_actions and self._event_loop is not None:
                actionable = [a for a in result.reconciliation_actions if a.strategy_id is not None]
                if actionable:
                    self._event_loop.call_soon_threadsafe(
                        self._apply_reconciliation_actions,
                        actionable,
                    )
        except Exception:
            _LOG.exception("event=wallet_sync_cycle_error component=wallet_sync")
        finally:
            self._cycle_in_progress = False

    # -- Core sync logic (runs in executor thread) --------------------------

    def _sync_cycle(self) -> WalletSyncResult:
        t0 = time.monotonic()

        self._known_condition_ids = {
            str(get_polymarket_condition_id(inst.id))
            for inst in self.cache.instruments(venue=_POLYMARKET_VENUE)
        }

        # --- HTTP fetches ---
        positions: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        http_positions_ok = True
        http_orders_ok = True

        try:
            positions = self._fetch_wallet_positions()
        except Exception:
            http_positions_ok = False
            _LOG.warning(
                "event=wallet_sync_positions_fetch_fail component=wallet_sync",
                exc_info=True,
            )

        try:
            orders = self._fetch_wallet_orders()
        except Exception:
            http_orders_ok = False
            _LOG.warning(
                "event=wallet_sync_orders_fetch_fail component=wallet_sync",
                exc_info=True,
            )

        failure_details: dict[str, int] = {}

        if not http_positions_ok and not http_orders_ok:
            self._consecutive_failure_count += 1
            self._sync_count += 1
            result = WalletSyncResult(
                cycle_number=self._sync_count,
                positions_fetched=0,
                orders_fetched=0,
                condition_ids_on_wallet=0,
                condition_ids_in_cache=len(self._known_condition_ids),
                instruments_newly_added=0,
                resolution_failures=0,
                unresolvable_retrying=sum(
                    1
                    for e in self._unresolvable_condition_ids.values()
                    if not e.terminal
                ),
                unresolvable_terminal=self.terminally_unresolvable_count,
                http_positions_ok=False,
                http_orders_ok=False,
                first_sync_complete=self._first_sync_complete,
                elapsed_seconds=time.monotonic() - t0,
                failure_details=failure_details,
            )
            self._maybe_emit_timeout_fact()
            return result

        # --- Build condition_id → token_ids map ---
        wallet_map: dict[str, set[str]] = {}
        for row in positions:
            cid = str(row.get("conditionId") or row.get("condition_id") or "").strip()
            asset = str(row.get("asset") or row.get("token_id") or "").strip()
            if cid and asset:
                wallet_map.setdefault(cid, set()).add(asset)

        for order in orders:
            asset = str(order.get("asset_id") or order.get("token_id") or "").strip()
            cid = str(order.get("condition_id") or "").strip()
            if not cid and asset:
                for cached in self.cache.instruments(venue=_POLYMARKET_VENUE):
                    if str(get_polymarket_token_id(cached.id)) == asset:
                        cid = str(get_polymarket_condition_id(cached.id))
                        break
            if cid and asset:
                wallet_map.setdefault(cid, set()).add(asset)

        # --- Resolve missing instruments ---
        newly_added = 0
        resolution_failures = 0

        terminal_cids = {
            cid
            for cid, entry in self._unresolvable_condition_ids.items()
            if entry.terminal
        }

        for cid, token_ids in wallet_map.items():
            if cid in self._known_condition_ids:
                continue
            if cid in terminal_cids:
                continue

            resolved_any = False
            for tid in token_ids:
                outcome = self._dynamic_ctrl.resolve_and_activate_by_condition_and_token(
                    cid, tid,
                )
                if outcome.instrument is not None:
                    newly_added += 1
                    self._instruments_discovered += 1
                    resolved_any = True
                    _LOG.info(
                        "event=wallet_sync_instrument_added component=wallet_sync "
                        "condition_id=%s token_id=%s instrument_id=%s",
                        cid[:24], tid[:24], outcome.instrument.id,
                    )
                else:
                    resolution_failures += 1
                    detail = outcome.detail or "unknown"
                    failure_details[detail] = failure_details.get(detail, 0) + 1

            if resolved_any:
                self._known_condition_ids.add(cid)
                if cid in self._unresolvable_condition_ids:
                    del self._unresolvable_condition_ids[cid]
            else:
                prev = self._unresolvable_condition_ids.get(cid)
                new_count = (prev.retry_count + 1) if prev else 1
                is_terminal = new_count >= self._wsconfig.per_instrument_max_retries
                last_detail = next(iter(failure_details), "unknown") if failure_details else "unknown"
                self._unresolvable_condition_ids[cid] = UnresolvableEntry(
                    condition_id=cid,
                    token_ids=tuple(sorted(token_ids)),
                    last_detail=last_detail,
                    retry_count=new_count,
                    terminal=is_terminal,
                )
                if is_terminal:
                    _LOG.warning(
                        "event=wallet_sync_unresolvable component=wallet_sync "
                        "condition_id=%s retry_count=%d detail=%s",
                        cid[:24], new_count, last_detail,
                    )
                    if self._fact_emit is not None:
                        self._fact_emit("wallet_sync_unresolvable", {
                            "condition_id": cid,
                            "token_ids": list(token_ids),
                            "retry_count": new_count,
                            "detail": last_detail,
                        })

        # --- Evaluate completeness ---
        all_wallet_cids = set(wallet_map.keys())
        non_terminal_unresolved = {
            cid
            for cid in all_wallet_cids
            if cid not in self._known_condition_ids
            and not self._unresolvable_condition_ids.get(cid, UnresolvableEntry("", (), "", 0, False)).terminal
        }
        completeness_ok = len(non_terminal_unresolved) == 0

        if completeness_ok:
            self._first_sync_complete = True
            self._consecutive_failure_count = 0
            self._last_successful_cycle_utc = datetime.now(tz=UTC)
        elif self._first_sync_complete:
            self._consecutive_failure_count += 1
            self._last_successful_cycle_utc = datetime.now(tz=UTC)

        self._sync_count += 1

        self._maybe_emit_timeout_fact()

        # --- VenueState snapshot (same executor thread; no duplicate HTTP) ---
        if self._venue_state is not None:
            self._venue_state.apply_positions_and_orders_rows(
                position_rows=positions,
                orders_raw=orders,
                ts_utc=datetime.now(tz=UTC),
            )
            self._venue_state.maybe_poll_clob_balance(self._clob)

        # --- Position reconciliation pass ---
        recon_actions: list[ReconciliationAction] = []
        recon_sent = 0
        recon_deferred = 0
        recon_skipped_ttl = 0
        if (
            self._wsconfig.position_reconciliation_enabled
            and self._first_sync_complete
            and http_positions_ok
        ):
            recon_actions = self._reconciliation_pass(positions)
            for a in recon_actions:
                self._emit_reconciliation_fact(a, self._sync_count)
                if a.deferred:
                    recon_deferred += 1
                elif a.diff_direction == "skipped_ttl":
                    recon_skipped_ttl += 1
                elif a.strategy_id is not None:
                    recon_sent += 1

        result = WalletSyncResult(
            cycle_number=self._sync_count,
            positions_fetched=len(positions),
            orders_fetched=len(orders),
            condition_ids_on_wallet=len(all_wallet_cids),
            condition_ids_in_cache=len(self._known_condition_ids),
            instruments_newly_added=newly_added,
            resolution_failures=resolution_failures,
            unresolvable_retrying=sum(
                1
                for e in self._unresolvable_condition_ids.values()
                if not e.terminal
            ),
            unresolvable_terminal=self.terminally_unresolvable_count,
            http_positions_ok=http_positions_ok,
            http_orders_ok=http_orders_ok,
            first_sync_complete=self._first_sync_complete,
            elapsed_seconds=time.monotonic() - t0,
            failure_details=failure_details,
            reconciliation_actions=recon_actions,
            reconciliation_sent_count=recon_sent,
            reconciliation_deferred_count=recon_deferred,
            reconciliation_skipped_recently_reconciled=recon_skipped_ttl,
        )

        return result

    # -- HTTP helpers -------------------------------------------------------

    def _fetch_wallet_positions(self) -> list[dict[str, Any]]:
        if self._positions_fetcher is not None:
            return self._positions_fetcher()
        from tyrex_pm.runtime.guru_cache_warmup import fetch_wallet_position_rows

        return fetch_wallet_position_rows(
            data_api_base_url=self._wsconfig.data_api_base_url,
            user_address=self._resolve_user_address(),
        )

    def _fetch_wallet_orders(self) -> list[dict[str, Any]]:
        raw = self._clob.get_orders()
        if isinstance(raw, list):
            return raw
        return []

    def _resolve_user_address(self) -> str:
        funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
        if funder:
            return funder
        addr = self._clob.get_address()
        if not addr:
            raise RuntimeError("Cannot resolve wallet address for wallet sync")
        return str(addr)

    # -- Position reconciliation (executor thread) --------------------------

    def _build_venue_position_map(
        self,
        position_rows: list[dict[str, Any]],
    ) -> dict[InstrumentId, Decimal]:
        venue_map: dict[InstrumentId, Decimal] = {}
        for row in position_rows:
            token_id = str(row.get("asset") or row.get("token_id") or "").strip()
            size_raw = str(row.get("size") or "0").strip()
            if not token_id:
                continue
            try:
                size = Decimal(size_raw)
            except InvalidOperation:
                size = Decimal(0)
            if size <= 0:
                size = Decimal(0)
            for cached in self.cache.instruments(venue=_POLYMARKET_VENUE):
                try:
                    if str(get_polymarket_token_id(cached.id)) == token_id:
                        venue_map[cached.id] = venue_map.get(cached.id, Decimal(0)) + size
                        break
                except ValueError:
                    continue
        return venue_map

    def _build_cache_position_map(self) -> dict[InstrumentId, Decimal]:
        cache_map: dict[InstrumentId, Decimal] = {}
        for pos in self.cache.positions_open(venue=_POLYMARKET_VENUE):
            cache_map[pos.instrument_id] = pos.signed_decimal_qty()
        return cache_map

    def _reconciliation_pass(
        self,
        position_rows: list[dict[str, Any]],
    ) -> list[ReconciliationAction]:
        venue_map = self._build_venue_position_map(position_rows)
        cache_map = self._build_cache_position_map()

        all_ids = set(venue_map.keys()) | set(cache_map.keys())
        actions: list[ReconciliationAction] = []

        now_ns = self.clock.timestamp_ns()
        tolerance_ns = int(self._wsconfig.data_api_lag_tolerance_seconds * 1e9)

        for iid in all_ids:
            venue_qty = venue_map.get(iid, Decimal(0))
            cache_qty = cache_map.get(iid, Decimal(0))

            if venue_qty == cache_qty:
                continue

            if venue_qty > cache_qty:
                if not self._wsconfig.reconcile_venue_has_more:
                    continue
                diff_direction = "venue_has_more"
            elif venue_qty == 0:
                diff_direction = "close"
            else:
                diff_direction = "partial_reduce"

            # Race E: recently-reconciled TTL
            last_recon = self._recently_reconciled.get(iid)
            if last_recon is not None:
                if (time.monotonic() - last_recon) < self._wsconfig.recently_reconciled_ttl_seconds:
                    actions.append(ReconciliationAction(
                        instrument_id=iid,
                        venue_qty=venue_qty,
                        cache_qty=cache_qty,
                        diff_direction="skipped_ttl",
                        deferred=False,
                        defer_count=self._deferred_reconciliations.get(iid, 0),
                    ))
                    _LOG.debug(
                        "event=position_reconciliation_skipped_ttl component=wallet_sync "
                        "instrument_id=%s ttl_remaining_s=%.1f",
                        iid,
                        self._wsconfig.recently_reconciled_ttl_seconds - (time.monotonic() - last_recon),
                    )
                    continue

            # Race B: Data API lag — ts_last debounce (ignore freshness from engine
            # reconciliation fills; those update ts_last but are not venue truth lag).
            defer_count = self._deferred_reconciliations.get(iid, 0)
            ts_last_deferred = False
            if diff_direction in ("close", "partial_reduce"):
                for pos in self.cache.positions_open(instrument_id=iid):
                    if (now_ns - pos.ts_last) < tolerance_ns:
                        # ``last_event`` None → cannot treat as reconciliation skip; defer below.
                        last_evt = pos.last_event
                        if last_evt is not None and getattr(
                            last_evt,
                            "reconciliation",
                            False,
                        ):
                            _LOG.info(
                                "event=position_reconciliation_ts_last_skipped "
                                "component=wallet_sync reason=reconciliation_origin "
                                "instrument_id=%s",
                                iid,
                            )
                            continue
                        ts_last_deferred = True
                        break

            if ts_last_deferred and defer_count < self._wsconfig.position_reconciliation_deferral_max:
                self._deferred_reconciliations[iid] = defer_count + 1
                actions.append(ReconciliationAction(
                    instrument_id=iid,
                    venue_qty=venue_qty,
                    cache_qty=cache_qty,
                    diff_direction="deferred",
                    deferred=True,
                    defer_count=defer_count + 1,
                ))
                _LOG.info(
                    "event=position_reconciliation_deferred component=wallet_sync "
                    "instrument_id=%s venue_qty=%s cache_qty=%s defer_count=%d reason=position_recently_modified",
                    iid, venue_qty, cache_qty, defer_count + 1,
                )
                continue

            # Race C: In-flight SELL orders covering delta
            if diff_direction in ("close", "partial_reduce"):
                delta = abs(cache_qty - venue_qty)
                inflight_sell_qty = Decimal(0)
                for order in self.cache.orders_open(instrument_id=iid):
                    if order.side == OrderSide.SELL:
                        inflight_sell_qty += order.leaves_qty.as_decimal()
                for order in self.cache.orders_inflight(instrument_id=iid):
                    if order.side == OrderSide.SELL:
                        inflight_sell_qty += order.leaves_qty.as_decimal()

                if inflight_sell_qty >= delta and defer_count < self._wsconfig.position_reconciliation_deferral_max:
                    self._deferred_reconciliations[iid] = defer_count + 1
                    actions.append(ReconciliationAction(
                        instrument_id=iid,
                        venue_qty=venue_qty,
                        cache_qty=cache_qty,
                        diff_direction="deferred",
                        deferred=True,
                        defer_count=defer_count + 1,
                    ))
                    _LOG.info(
                        "event=position_reconciliation_deferred component=wallet_sync "
                        "instrument_id=%s venue_qty=%s cache_qty=%s defer_count=%d "
                        "inflight_sell_qty=%s reason=inflight_sell_covers_delta",
                        iid, venue_qty, cache_qty, defer_count + 1, inflight_sell_qty,
                    )
                    continue

            # Max deferrals reached — proceed anyway
            if defer_count >= self._wsconfig.position_reconciliation_deferral_max:
                _LOG.warning(
                    "event=position_reconciliation_stuck component=wallet_sync "
                    "instrument_id=%s defer_count=%d proceeding=true",
                    iid, defer_count,
                )

            # Look up original strategy_id from open position
            open_positions = self.cache.positions_open(instrument_id=iid)
            if not open_positions:
                _LOG.warning(
                    "event=position_reconciliation_no_position component=wallet_sync "
                    "instrument_id=%s reason=position_closed_between_diff_and_build",
                    iid,
                )
                continue

            original_strategy_id = open_positions[0].strategy_id

            actions.append(ReconciliationAction(
                instrument_id=iid,
                venue_qty=venue_qty,
                cache_qty=cache_qty,
                diff_direction=diff_direction,
                deferred=False,
                defer_count=defer_count,
                strategy_id=original_strategy_id,
            ))
            _LOG.info(
                "event=position_reconciliation_action_queued component=wallet_sync "
                "instrument_id=%s venue_qty=%s cache_qty=%s direction=%s "
                "note=awaiting_apply_on_event_loop_shadow_mode_may_skip_engine",
                iid, venue_qty, cache_qty, diff_direction,
            )

        return actions

    def _apply_reconciliation_actions(
        self,
        actions: list[ReconciliationAction],
    ) -> None:
        _LOG.info(
            "event=position_reconciliation_apply_begin component=wallet_sync "
            "action_count=%d shadow_mode=%s",
            len(actions),
            self._wsconfig.position_reconciliation_shadow_mode,
        )
        for action in actions:
            if action.strategy_id is None:
                continue
            if self._wsconfig.position_reconciliation_shadow_mode:
                _LOG.info(
                    "event=position_reconciliation_shadow_skip component=wallet_sync "
                    "instrument_id=%s venue_qty=%s cache_qty=%s direction=%s "
                    "note=synthetic_close_not_invoked",
                    action.instrument_id,
                    action.venue_qty,
                    action.cache_qty,
                    action.diff_direction,
                )
                self._emit_reconciliation_fact(action, self._sync_count, sent=False)
                continue

            positions = self.cache.positions_open(instrument_id=action.instrument_id)
            if not positions:
                _LOG.info(
                    "event=position_reconciliation_skip component=wallet_sync "
                    "instrument_id=%s reason=position_closed_before_application",
                    action.instrument_id,
                )
                continue

            instrument = self.cache.instrument(action.instrument_id)
            if instrument is None:
                continue

            account = self.cache.account_for_venue(_POLYMARKET_VENUE)
            if account is None:
                _LOG.warning(
                    "event=position_reconciliation_no_account component=wallet_sync "
                    "instrument_id=%s",
                    action.instrument_id,
                )
                continue

            delta = abs(action.cache_qty - action.venue_qty)
            try:
                self._send_synthetic_close(
                    instrument=instrument,
                    strategy_id=action.strategy_id,
                    delta_qty=float(delta),
                    account_id=account.id,
                )
            except Exception:
                _LOG.exception(
                    "event=position_reconciliation_send_error component=wallet_sync "
                    "instrument_id=%s",
                    action.instrument_id,
                )
                continue

            self._recently_reconciled[action.instrument_id] = time.monotonic()
            self._reconciliation_count += 1
            self._deferred_reconciliations.pop(action.instrument_id, None)
            self._emit_reconciliation_fact(action, self._sync_count, sent=True)

    def _send_synthetic_close(
        self,
        *,
        instrument: Any,
        strategy_id: StrategyId,
        delta_qty: float,
        account_id: Any,
    ) -> None:
        """Construct a synthetic order + fill and send via ExecEngine.process."""
        _LOG.info(
            "event=synthetic_close_begin component=wallet_sync "
            "instrument_id=%s strategy_id=%s delta_qty=%s account_id=%s",
            instrument.id,
            strategy_id,
            delta_qty,
            account_id,
        )
        ts_now = self.clock.timestamp_ns()
        client_order_id = ClientOrderId(UUID4().value)
        venue_order_id = VenueOrderId(UUID4().value)

        initialized = OrderInitialized(
            trader_id=self.trader_id,
            strategy_id=strategy_id,
            instrument_id=instrument.id,
            client_order_id=client_order_id,
            order_side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(delta_qty, instrument.size_precision),
            time_in_force=TimeInForce.GTC,
            post_only=False,
            reduce_only=True,
            quote_quantity=False,
            options={},
            emulation_trigger=TriggerType.NO_TRIGGER,
            trigger_instrument_id=None,
            contingency_type=ContingencyType.NO_CONTINGENCY,
            order_list_id=None,
            linked_order_ids=None,
            parent_order_id=None,
            exec_algorithm_id=None,
            exec_algorithm_params=None,
            exec_spawn_id=None,
            tags=["RECONCILIATION"],
            event_id=UUID4(),
            ts_init=ts_now,
            reconciliation=True,
        )
        order = OrderUnpacker.from_init(initialized)
        self.cache.add_order(order)

        accepted = OrderAccepted(
            trader_id=self.trader_id,
            strategy_id=strategy_id,
            instrument_id=instrument.id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            account_id=account_id,
            event_id=UUID4(),
            ts_event=ts_now,
            ts_init=ts_now,
            reconciliation=True,
        )
        order.apply(accepted)
        self.cache.update_order(order)

        position_id = PositionId(f"{instrument.id}-{strategy_id}")
        fill = OrderFilled(
            self.trader_id,
            strategy_id,
            instrument.id,
            client_order_id,
            venue_order_id,
            account_id,
            TradeId(UUID4().value),
            position_id,
            OrderSide.SELL,
            OrderType.MARKET,
            Quantity(delta_qty, instrument.size_precision),
            Price(0.50, instrument.price_precision),
            instrument.quote_currency,
            Money(0, instrument.quote_currency),
            LiquiditySide.TAKER,
            UUID4(),
            ts_now,
            ts_now,
            reconciliation=True,
        )
        self.msgbus.send("ExecEngine.process", fill)

        _LOG.info(
            "event=position_reconciliation_fill_sent component=wallet_sync "
            "instrument_id=%s strategy_id=%s delta_qty=%s position_id=%s",
            instrument.id, strategy_id, delta_qty, position_id,
        )

    # -- Fact emission ------------------------------------------------------

    def _emit_reconciliation_fact(
        self,
        action: ReconciliationAction,
        cycle: int,
        *,
        sent: bool | None = None,
    ) -> None:
        if self._fact_emit is None:
            return
        reconciliation_sent = sent if sent is not None else (action.strategy_id is not None and not action.deferred)
        self._fact_emit("position_reconciliation", {
            "cycle": cycle,
            "instrument_id": str(action.instrument_id),
            "venue_qty": str(action.venue_qty),
            "cache_qty": str(action.cache_qty),
            "diff_direction": action.diff_direction,
            "deferred": action.deferred,
            "defer_count": action.defer_count,
            "reconciliation_sent": reconciliation_sent,
        })

    def _emit_sync_fact(self, result: WalletSyncResult) -> None:
        if self._fact_emit is None:
            return
        self._fact_emit("wallet_sync", {
            "cycle": result.cycle_number,
            "positions_fetched": result.positions_fetched,
            "orders_fetched": result.orders_fetched,
            "condition_ids_wallet": result.condition_ids_on_wallet,
            "condition_ids_cache": result.condition_ids_in_cache,
            "newly_added": result.instruments_newly_added,
            "resolution_failures": result.resolution_failures,
            "unresolvable_retrying": result.unresolvable_retrying,
            "unresolvable_terminal": result.unresolvable_terminal,
            "http_positions_ok": result.http_positions_ok,
            "http_orders_ok": result.http_orders_ok,
            "first_sync_complete": result.first_sync_complete,
            "elapsed_ms": round(result.elapsed_seconds * 1000, 2),
            "failure_details": result.failure_details,
        })

    def _maybe_emit_timeout_fact(self) -> None:
        if not self._first_sync_complete and self.startup_deadline_exceeded:
            if self._fact_emit is not None:
                self._fact_emit("wallet_sync_startup_timeout", {
                    "cycle": self._sync_count,
                    "elapsed_since_start_s": round(
                        time.monotonic() - self._start_mono, 1,
                    ),
                    "deadline_s": self._wsconfig.startup_deadline_seconds,
                })


__all__ = [
    "ReconciliationAction",
    "UnresolvableEntry",
    "WalletSyncActor",
    "WalletSyncConfig",
    "WalletSyncHealthAdapter",
    "WalletSyncResult",
]
