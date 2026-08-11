"""Authoritative, recoverable account state for execution readiness.

Account reads are idempotent venue observations.  They are prepared in the
background for active and next markets and published as immutable snapshots.
An unavailable read is never represented as a zero balance.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Awaitable, Callable

from tyrex_pm.adapters.polymarket.sdk_errors import (
    PolymarketOperation,
    describe_error,
)


def _value(model: Any, name: str, default: Any = None) -> Any:
    return model.get(name, default) if isinstance(model, dict) else getattr(model, name, default)


class AccountSnapshotStatus(str, Enum):
    READY = "READY"
    INSUFFICIENT = "INSUFFICIENT"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"


@dataclass(frozen=True)
class TokenAccountState:
    token_id: str
    balance_shares: Decimal
    allowance_shares: Decimal | None


@dataclass(frozen=True)
class AccountStateSnapshot:
    market_id: str
    token_ids: tuple[str, ...]
    status: AccountSnapshotStatus
    observed_at: datetime
    observed_monotonic_s: float
    collateral_balance: Decimal | None
    collateral_allowance: Decimal | None
    tokens: tuple[TokenAccountState, ...]
    open_order_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    read_attempt: int
    operation_elapsed_ms: tuple[tuple[str, float], ...] = ()
    operation_queue_wait_ms: tuple[tuple[str, float], ...] = ()
    refresh_elapsed_ms: float = 0.0
    refresh_cycle_overrun: bool = False
    slow_operations: tuple[str, ...] = ()

    @property
    def read_complete(self) -> bool:
        return self.status in {
            AccountSnapshotStatus.READY,
            AccountSnapshotStatus.INSUFFICIENT,
        }

    def token(self, token_id: str) -> TokenAccountState:
        for item in self.tokens:
            if item.token_id == token_id:
                return item
        raise KeyError(token_id)


@dataclass(frozen=True)
class AccountStatePolicy:
    refresh_interval_s: float = 5.0
    snapshot_max_age_s: float = 15.0
    read_timeout_s: float = 4.0
    retry_attempts: int = 3
    retry_base_delay_s: float = 0.25

    @property
    def slow_read_threshold_s(self) -> float:
        """Diagnostic threshold; SDK transport owns actual request timeouts.

        Force-cancelling shared HTTP/2 requests from an outer asyncio timeout
        corrupted capacity-limiter state on Windows.  Account reads run in the
        background, so a slow read is reported and allowed to finish safely.
        """
        return self.read_timeout_s

    def __post_init__(self) -> None:
        if (
            min(
                self.refresh_interval_s,
                self.snapshot_max_age_s,
                self.read_timeout_s,
                self.retry_attempts,
                self.retry_base_delay_s,
            )
            <= 0
        ):
            raise ValueError("account-state policy values must be positive")


@dataclass(frozen=True)
class _AccountTarget:
    market_id: str
    token_ids: tuple[str, ...]
    required_collateral: Decimal
    active: bool = False


EvidenceSink = Callable[[str, dict[str, Any]], None]


class _PrioritySingleFlight:
    """One SDK call at a time, with active-market waiters served first."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._locked = False
        self._active_waiters = 0

    @asynccontextmanager
    async def slot(self, *, active: bool):
        async with self._condition:
            if active:
                self._active_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._locked and (active or self._active_waiters == 0)
                )
                self._locked = True
            finally:
                if active:
                    self._active_waiters -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._locked = False
                self._condition.notify_all()


class AccountStateAuthority:
    """Single owner of account-read health and last-known-good snapshots."""

    def __init__(
        self,
        gateway: Any,
        *,
        policy: AccountStatePolicy,
        evidence_sink: EvidenceSink | None = None,
    ) -> None:
        self._gateway = gateway
        self.policy = policy
        self._evidence_sink = evidence_sink
        self._targets: dict[str, _AccountTarget] = {}
        self._snapshots: dict[str, AccountStateSnapshot] = {}
        self._latest_failures: dict[str, tuple[str, ...]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        # One async SDK client is shared by active + prepared-market refreshes.
        # Serialize private reads so cancellation/HTTP2 pool accounting cannot
        # race, while keeping the whole refresh off the trading hot path.
        self._single_flight = _PrioritySingleFlight()
        self._closed = False

    def prepare(
        self,
        *,
        market_id: str,
        token_ids: tuple[str, ...],
        required_collateral: Decimal,
        active: bool = False,
    ) -> None:
        """Register a market and start refreshing it without blocking promotion."""
        if self._closed:
            raise RuntimeError("account-state authority is closed")
        existing = self._targets.get(market_id)
        target = _AccountTarget(
            market_id=market_id,
            token_ids=tuple(token_ids),
            required_collateral=required_collateral,
            active=active or bool(existing and existing.active),
        )
        self._targets[market_id] = target
        wakeup = self._wakeups.setdefault(market_id, asyncio.Event())
        wakeup.set()
        task = self._tasks.get(market_id)
        if task is None or task.done():
            self._tasks[market_id] = asyncio.create_task(
                self._refresh_loop(market_id),
                name=f"account-state:{market_id}",
            )
        self._record(
            "ACCOUNT_STATE_PREPARATION_STARTED",
            {
                "market_id": market_id,
                "token_ids": list(token_ids),
                "priority": "ACTIVE" if target.active else "PREPARED",
            },
        )

    def retire(self, market_id: str) -> None:
        self._targets.pop(market_id, None)
        wakeup = self._wakeups.get(market_id)
        if wakeup is not None:
            wakeup.set()
        self._snapshots.pop(market_id, None)
        self._latest_failures.pop(market_id, None)

    def current(self, market_id: str) -> AccountStateSnapshot:
        snapshot = self._snapshots.get(market_id)
        if snapshot is None:
            return self._unavailable_snapshot(
                market_id,
                self._latest_failures.get(market_id, ("ACCOUNT_STATE_NOT_OBSERVED",)),
            )
        age_s = max(0.0, time.monotonic() - snapshot.observed_monotonic_s)
        if age_s <= self.policy.snapshot_max_age_s:
            return snapshot
        return replace(
            snapshot,
            status=AccountSnapshotStatus.STALE,
            blockers=tuple(dict.fromkeys((*snapshot.blockers, "ACCOUNT_SNAPSHOT_STALE"))),
        )

    async def refresh_now(self, market_id: str) -> AccountStateSnapshot:
        target = self._targets[market_id]
        await self._refresh_with_retries(target)
        return self.current(market_id)

    async def _refresh_loop(self, market_id: str) -> None:
        try:
            while not self._closed:
                target = self._targets.get(market_id)
                if target is None:
                    return
                wakeup = self._wakeups[market_id]
                wakeup.clear()
                await self._refresh_with_retries(target)
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=self.policy.refresh_interval_s)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            if self._tasks.get(market_id) is asyncio.current_task():
                self._tasks.pop(market_id, None)
                if not self._closed and market_id in self._targets:
                    self._tasks[market_id] = asyncio.create_task(
                        self._refresh_loop(market_id),
                        name=f"account-state:{market_id}",
                    )
                else:
                    self._wakeups.pop(market_id, None)

    async def _refresh_with_retries(self, target: _AccountTarget) -> None:
        for attempt in range(1, self.policy.retry_attempts + 1):
            started = time.monotonic()
            snapshot, failures, retryable, diagnostics = await self._capture_once(
                target, attempt=attempt
            )
            elapsed_ms = round((time.monotonic() - started) * 1_000, 3)
            if target.market_id not in self._targets:
                return
            if snapshot is not None:
                snapshot = replace(
                    snapshot,
                    refresh_elapsed_ms=elapsed_ms,
                    refresh_cycle_overrun=elapsed_ms > self.policy.refresh_interval_s * 1_000,
                )
                self._snapshots[target.market_id] = snapshot
                self._latest_failures.pop(target.market_id, None)
                self._record(
                    "ACCOUNT_STATE_UPDATED",
                    {
                        "market_id": target.market_id,
                        "status": snapshot.status.value,
                        "attempt": attempt,
                        "elapsed_ms": elapsed_ms,
                        "collateral_balance": snapshot.collateral_balance,
                        "collateral_allowance": snapshot.collateral_allowance,
                        "blockers": list(snapshot.blockers),
                        "operation_elapsed_ms": dict(snapshot.operation_elapsed_ms),
                        "operation_queue_wait_ms": dict(snapshot.operation_queue_wait_ms),
                        "refresh_cycle_overrun": snapshot.refresh_cycle_overrun,
                        "slow_operations": list(snapshot.slow_operations),
                    },
                )
                return
            self._latest_failures[target.market_id] = failures
            self._record(
                "ACCOUNT_STATE_READ_FAILED",
                {
                    "market_id": target.market_id,
                    "attempt": attempt,
                    "elapsed_ms": elapsed_ms,
                    "failures": list(failures),
                    **diagnostics,
                    "retry_scheduled": retryable and attempt < self.policy.retry_attempts,
                },
            )
            if not retryable:
                return
            if attempt < self.policy.retry_attempts:
                await asyncio.sleep(self.policy.retry_base_delay_s * (2 ** (attempt - 1)))

    async def _capture_once(
        self,
        target: _AccountTarget,
        *,
        attempt: int,
    ) -> tuple[AccountStateSnapshot | None, tuple[str, ...], bool, dict[str, Any]]:
        operations: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
            ("COLLATERAL", self._gateway.collateral_balance),
        ]
        operations.extend(
            (
                f"TOKEN:{token_id}",
                lambda token_id=token_id: self._gateway.conditional_balance(token_id),
            )
            for token_id in target.token_ids
        )
        operations.append(
            (
                "OPEN_ORDERS",
                lambda: self._gateway.list_open_orders(market_id=target.market_id),
            )
        )

        values: dict[str, Any] = {}
        elapsed_by_operation: dict[str, float] = {}
        queue_wait_by_operation: dict[str, float] = {}
        error_details: list[dict[str, Any]] = []
        # Do not wrap these SDK calls in asyncio.timeout. The official SDK owns
        # connect/read/write/pool timeouts; outer cancellation was the source of
        # the observed "semaphore released too many times" failure. A single
        # flight also prevents active and prepared refreshes racing one client.
        for operation, invoke in operations:
            queue_started = time.monotonic()
            current = self._targets.get(target.market_id)
            active = target.active if current is None else current.active
            async with self._single_flight.slot(active=active):
                queue_wait_by_operation[operation] = round(
                    (time.monotonic() - queue_started) * 1_000, 3
                )
                started = time.monotonic()
                try:
                    values[operation] = await invoke()
                except Exception as exc:  # classified below; never fake zero
                    values[operation] = exc
                    error_details.append(_exception_detail(operation, exc))
                elapsed_by_operation[operation] = round((time.monotonic() - started) * 1_000, 3)

        slow_operations = tuple(
            operation
            for operation, elapsed_ms in elapsed_by_operation.items()
            if elapsed_ms > self.policy.slow_read_threshold_s * 1_000
        )
        failures: list[str] = []
        retryable = True
        for operation, result in values.items():
            if isinstance(result, BaseException):
                detail = describe_error(result, operation=PolymarketOperation.READ)
                retryable = retryable and bool(detail["retryable"])
                failures.append(
                    f"{operation}:{detail['category']}:{detail['original_type']}:{detail['message']}"
                )
        if failures:
            return (
                None,
                tuple(failures),
                retryable,
                {
                    "operation_elapsed_ms": elapsed_by_operation,
                    "operation_queue_wait_ms": queue_wait_by_operation,
                    "slow_operations": list(slow_operations),
                    "error_details": error_details,
                    "sdk_cancellation_policy": "SDK_TRANSPORT_TIMEOUTS_NO_OUTER_CANCELLATION",
                },
            )

        collateral, collateral_allowance = values["COLLATERAL"]
        tokens = tuple(
            TokenAccountState(
                token_id=token_id,
                balance_shares=values[f"TOKEN:{token_id}"][0],
                allowance_shares=values[f"TOKEN:{token_id}"][1],
            )
            for token_id in target.token_ids
        )
        blockers: list[str] = []
        if collateral < target.required_collateral:
            blockers.append("INSUFFICIENT_COLLATERAL")
        if collateral_allowance is None or collateral_allowance < target.required_collateral:
            blockers.append("INSUFFICIENT_COLLATERAL_ALLOWANCE")
        open_ids: list[str] = []
        for order in values["OPEN_ORDERS"]:
            order_id = str(_value(order, "id", "") or "")
            token_id = str(_value(order, "token_id", "") or "")
            if order_id and token_id in target.token_ids:
                open_ids.append(order_id)
                blockers.append(f"PRIOR_OPEN_ORDER:{order_id}")
        for token in tokens:
            if token.balance_shares > 0:
                blockers.append(f"PRIOR_POSITION:{token.token_id}:{token.balance_shares}")
        insufficient = any(value.startswith("INSUFFICIENT_") for value in blockers)
        now = datetime.now(timezone.utc)
        return (
            AccountStateSnapshot(
                market_id=target.market_id,
                token_ids=target.token_ids,
                status=(
                    AccountSnapshotStatus.INSUFFICIENT
                    if insufficient
                    else AccountSnapshotStatus.READY
                ),
                observed_at=now,
                observed_monotonic_s=time.monotonic(),
                collateral_balance=collateral,
                collateral_allowance=collateral_allowance,
                tokens=tokens,
                open_order_ids=tuple(sorted(open_ids)),
                blockers=tuple(blockers),
                read_attempt=attempt,
                operation_elapsed_ms=tuple(elapsed_by_operation.items()),
                operation_queue_wait_ms=tuple(queue_wait_by_operation.items()),
                slow_operations=slow_operations,
            ),
            (),
            False,
            {},
        )

    def _unavailable_snapshot(
        self,
        market_id: str,
        blockers: tuple[str, ...],
    ) -> AccountStateSnapshot:
        target = self._targets.get(market_id)
        return AccountStateSnapshot(
            market_id=market_id,
            token_ids=() if target is None else target.token_ids,
            status=AccountSnapshotStatus.UNAVAILABLE,
            observed_at=datetime.now(timezone.utc),
            observed_monotonic_s=time.monotonic(),
            collateral_balance=None,
            collateral_allowance=None,
            tokens=(),
            open_order_ids=(),
            blockers=blockers,
            read_attempt=0,
        )

    def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._evidence_sink is not None:
            self._evidence_sink(event_type, payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        self._targets.clear()
        self._wakeups.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _exception_detail(operation: str, exc: BaseException) -> dict[str, Any]:
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(
            {
                "error_type": type(current).__name__,
                "message": str(current),
            }
        )
        current = current.__cause__ or current.__context__
    return {"operation": operation, "exception_chain": chain}
