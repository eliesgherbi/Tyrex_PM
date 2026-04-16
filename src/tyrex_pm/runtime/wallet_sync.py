"""
Venue Sync Truth — ``WalletSyncActor`` and supporting types.

Continuously discovers all Polymarket markets the wallet has exposure on
(positions or resting orders), ensures they are in Nautilus ``Cache``, and
lets the engine's continuous reconciliation loop handle order/position/WS
subscription from there.

See ``docs/implementation/venue_sync_truth/02_components.md`` for the full spec.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket.common.symbol import (
    get_polymarket_condition_id,
    get_polymarket_token_id,
)
from nautilus_trader.common.actor import Actor
from nautilus_trader.core.message import Event
from nautilus_trader.model.identifiers import Venue

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
        self._cycle_in_progress: bool = False

    # -- Actor lifecycle (all synchronous — actor.pxd:93-94) ----------------

    def on_start(self) -> None:
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

    # -- Sync cycle wrapper (executor thread entry point) -------------------

    def _sync_cycle_wrapper(self) -> None:
        try:
            result = self._sync_cycle()
            self._emit_sync_fact(result)
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

    # -- Fact emission ------------------------------------------------------

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
    "UnresolvableEntry",
    "WalletSyncActor",
    "WalletSyncConfig",
    "WalletSyncHealthAdapter",
    "WalletSyncResult",
]
