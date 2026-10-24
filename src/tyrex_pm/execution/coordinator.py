"""Serialized account execution authority."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol, Sequence
from uuid import uuid4

from tyrex_pm.adapters.polymarket.sdk_errors import PolymarketOperation, describe_error
from tyrex_pm.execution.cancel import CancelResult
from tyrex_pm.execution.evidence import (
    CancelObserved,
    CancelRequested,
    DispatchAuthorized,
    ExecutionEvidence,
    ExecutionRole,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    PreDispatchStage,
    SessionOperationalClearanceRecorded,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
    new_envelope,
)
from tyrex_pm.execution.orders import MarketBuyOrderSpec, OrderSide, OrderSpec, order_spec_to_dict
from tyrex_pm.execution.reducer import (
    ReducerEffect,
    assert_clearance_appendable,
    reduce_execution_event,
)
from tyrex_pm.execution.session_state import ExecutionSessionState
from tyrex_pm.persistence.execution_journal import ExecutionJournal


class PreDispatchFailure(RuntimeError):
    """Known local failure before a venue mutation could begin."""


class OrderPreparationFailed(PreDispatchFailure):
    pass


class DispatchBlocked(PreDispatchFailure):
    pass


@dataclass(frozen=True)
class PreparedOrder:
    local_order_id: str
    digest: str
    payload: Any
    dispatch_spec: OrderSpec | None = None
    requested_protection_price: Decimal | None = None
    effective_protection_price: Decimal | None = None
    tick_size: Decimal | None = None
    prepare_queue_wait_ms: float | None = None
    tick_resolve_ms: float | None = None
    create_market_order_ms: float | None = None
    refresh_wait_ms: float | None = None
    refresh_elapsed_ms: float | None = None
    order_metadata_cache_miss: bool = False


def _prepare_timing_kwargs(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    def getter(key: str, default: Any = None) -> Any:
        if isinstance(source, dict):
            return source.get(key, default)
        return getattr(source, key, default)
    miss = getter("order_metadata_cache_miss", None)
    return {
        "prepare_queue_wait_ms": getter("prepare_queue_wait_ms"),
        "tick_resolve_ms": getter("tick_resolve_ms"),
        "create_market_order_ms": getter("create_market_order_ms"),
        "refresh_wait_ms": getter("refresh_wait_ms"),
        "refresh_elapsed_ms": getter("refresh_elapsed_ms"),
        "order_metadata_cache_miss": None if miss is None else bool(miss),
    }


@dataclass(frozen=True)
class GatewaySubmissionResult:
    accepted: bool
    venue_order_id: str | None
    status: str
    cumulative_matched_shares: Any | None = None
    trade_ids: tuple[str, ...] = ()
    error_code: str | None = None
    message: str | None = None


class ExecutionGateway(Protocol):
    async def prepare_order(self, spec: OrderSpec) -> PreparedOrder: ...
    async def discard_prepared(self, prepared: PreparedOrder) -> None: ...
    async def post_order(self, prepared: PreparedOrder) -> GatewaySubmissionResult: ...
    async def close(self) -> None: ...


class _ReentrantEvidenceGate:
    """One task may nest applies. Every other task waits until it finishes."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def __aenter__(self) -> "_ReentrantEvidenceGate":
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, *_args: object) -> bool:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()
        return False


class CancelUnsupported(RuntimeError):
    """Gateway has no official cancel surface."""


FinalDispatchGate = Callable[[OrderSpec], Awaitable[tuple[bool, str | None]]]
StateListener = Callable[[ExecutionSessionState, tuple[ReducerEffect, ...]], None]


@dataclass
class AccountExecutionCoordinator:
    """The only component allowed to mutate execution-session state."""

    journal: ExecutionJournal
    gateway: ExecutionGateway
    final_gate: FinalDispatchGate
    states: dict[str, ExecutionSessionState] = field(default_factory=dict)
    listeners: list[StateListener] = field(default_factory=list)
    _queue: asyncio.Queue[
        tuple[ExecutionEvidence, asyncio.Future[tuple[ReducerEffect, ...]]] | None
    ] = field(default_factory=asyncio.Queue, init=False)
    _writer_task: asyncio.Task[None] | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)
    _mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    evidence_gate: _ReentrantEvidenceGate = field(
        default_factory=_ReentrantEvidenceGate, init=False
    )
    recovery_generation: str | None = None
    applied_event_ids: dict[str, str] = field(default_factory=dict)
    recovery_now: Callable[[], datetime] | None = None
    # Latency of each submit stage by order id (prepare/sign, final gate, durable
    # authorization, POST), for run evidence. Bounded; never part of the journal.
    submit_timings: dict[str, dict[str, float | None]] = field(default_factory=dict)

    def _note_timing(self, order_id: str, stage: str, started: float) -> None:
        timings = self.submit_timings.setdefault(order_id, {})
        timings[stage] = round((time.monotonic() - started) * 1_000, 3)
        while len(self.submit_timings) > 64:
            self.submit_timings.pop(next(iter(self.submit_timings)))

    def _recovery_limit_block(
        self,
        session_id: str,
        spec: OrderSpec,
        *,
        historical: bool = False,
    ) -> str | None:
        """Recheck an approved recovery immediately before POST."""
        state = self.states.get(session_id)
        auth = None if state is None else state.recovery_authorization
        if auth is None:
            return "historical_recovery_not_authorized" if historical else None
        if isinstance(spec, MarketBuyOrderSpec) or getattr(spec, "side", None) is OrderSide.BUY:
            return "recovery_action_type_mismatch"
        if auth.action not in {"cancel_and_close", "close"}:
            return "recovery_action_type_mismatch"
        now = (
            datetime.now(timezone.utc)
            if self.recovery_now is None
            else self.recovery_now()
        )
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if auth.generation != (self.recovery_generation or ""):
            return "recovery_authorization_generation_mismatch"
        if now >= auth.expires_at:
            return "recovery_authorization_expired"
        if spec.token_id != auth.token_id or spec.market_id != auth.condition_id:
            return "recovery_authorization_identity_mismatch"
        shares = getattr(spec, "shares", None)
        if shares is not None and Decimal(str(shares)) > auth.max_shares:
            return "recovery_quantity_above_ceiling"
        price = getattr(spec, "minimum_price", None)
        if price is None:
            price = getattr(spec, "limit_price", None)
        if price is not None and Decimal(str(price)) < auth.min_price:
            return "recovery_price_below_floor"
        return None

    async def start(self) -> None:
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer(), name="execution-coordinator")

    async def restore(self, session_id: str) -> ExecutionSessionState:
        if self._writer_task is not None:
            raise RuntimeError("restore must happen before coordinator start")
        state = ExecutionSessionState()
        for event in self.journal.load(session_id):
            state, _ = reduce_execution_event(state, event)
            self.applied_event_ids[session_id] = event.event_id
        self.states[session_id] = state
        return state

    async def apply(self, event: ExecutionEvidence) -> tuple[ReducerEffect, ...]:
        if self._closed:
            raise RuntimeError("execution coordinator is closed")
        async with self.evidence_gate:
            await self.start()
            loop = asyncio.get_running_loop()
            future: asyncio.Future[tuple[ReducerEffect, ...]] = loop.create_future()
            await self._queue.put((event, future))
            return await future

    async def _writer(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            event, future = item
            try:
                if isinstance(event, SessionOperationalClearanceRecorded):
                    current = self.states.get(event.session_id) or ExecutionSessionState()
                    assert_clearance_appendable(current, event)
                inserted = self.journal.append(event)
                state = self.states.setdefault(event.session_id, ExecutionSessionState())
                if inserted:
                    state, effects = reduce_execution_event(state, event)
                    self.states[event.session_id] = state
                    self.applied_event_ids[event.session_id] = event.event_id
                    for listener in tuple(self.listeners):
                        listener(state, effects)
                else:
                    effects = ()
                if not future.done():
                    future.set_result(effects)
            except Exception as exc:  # noqa: BLE001 - deliver to event producer
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def submit(
        self,
        *,
        session_id: str,
        role: ExecutionRole,
        spec: OrderSpec,
        recovery: bool = False,
        historical_recovery: bool = False,
    ) -> GatewaySubmissionResult:
        """Prepare/sign, revalidate, durably authorize, then POST."""
        if (recovery or historical_recovery) and role is ExecutionRole.ENTRY:
            raise DispatchBlocked("recovery cannot request a BUY")
        async with self._mutation_lock:
            return await self._submit_locked(
                session_id=session_id,
                role=role,
                spec=spec,
                recovery=recovery or historical_recovery,
                historical_recovery=historical_recovery,
            )

    async def _submit_locked(
        self,
        *,
        session_id: str,
        role: ExecutionRole,
        spec: OrderSpec,
        recovery: bool = False,
        historical_recovery: bool = False,
    ) -> GatewaySubmissionResult:
        await self.apply(
            OrderRequested(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"order-requested:{role.value}:{spec.order_id}",
                ),
                role=role,
                order=spec,
                recovery=recovery,
            )
        )
        prepare_error: BaseException | None = None
        stage_started = time.monotonic()
        try:
            prepared = await self.gateway.prepare_order(spec)
        except Exception as exc:
            prepare_error = exc
        finally:
            self._note_timing(spec.order_id, "prepare_ms", stage_started)
        if prepare_error is not None:
            classified = describe_error(
                getattr(prepare_error, "original_error", None) or prepare_error,
                operation=PolymarketOperation.PREPARE,
            )
            details = getattr(prepare_error, "preparation_details", {})
            error_code = str(getattr(prepare_error, "error_code", None) or classified["category"])
            await self.apply(
                OrderPreDispatchFailed(
                    **new_envelope(
                        session_id=session_id,
                        dedupe_key=f"pre-dispatch-failed:PREPARATION:{spec.order_id}",
                    ),
                    role=role,
                    order_id=spec.order_id,
                    stage=PreDispatchStage.PREPARATION,
                    error_class=str(classified["original_type"]),
                    error_code=error_code,
                    message=str(classified["message"]),
                    requested_protection_price=details.get("requested_protection_price"),
                    effective_protection_price=details.get("effective_protection_price"),
                    tick_size=details.get("tick_size"),
                    **_prepare_timing_kwargs(details),
                )
            )
            raise OrderPreparationFailed(
                f"{error_code}:{classified['original_type']}:{classified['message']}"
            ) from prepare_error

        digest = (
            prepared.digest
            or hashlib.sha256(repr(order_spec_to_dict(spec)).encode("utf-8")).hexdigest()
        )
        await self.apply(
            OrderPrepared(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"order-prepared:{spec.order_id}:{digest}",
                ),
                role=role,
                order_id=spec.order_id,
                prepared_order_digest=digest,
                requested_protection_price=prepared.requested_protection_price,
                effective_protection_price=prepared.effective_protection_price,
                tick_size=prepared.tick_size,
                **_prepare_timing_kwargs(prepared),
            )
        )
        dispatch_spec = prepared.dispatch_spec or spec
        stage_started = time.monotonic()
        try:
            allowed, reason = await self.final_gate(dispatch_spec)
            self._note_timing(spec.order_id, "final_gate_ms", stage_started)
        except Exception as exc:
            classified = describe_error(exc, operation=PolymarketOperation.PREPARE)
            await self.apply(
                OrderPreDispatchFailed(
                    **new_envelope(
                        session_id=session_id,
                        dedupe_key=f"pre-dispatch-failed:FINAL_GATE:{spec.order_id}",
                    ),
                    role=role,
                    order_id=spec.order_id,
                    stage=PreDispatchStage.FINAL_GATE,
                    error_class=str(classified["original_type"]),
                    error_code=str(classified["category"]),
                    message=str(classified["message"]),
                    requested_protection_price=prepared.requested_protection_price,
                    effective_protection_price=prepared.effective_protection_price,
                    tick_size=prepared.tick_size,
                    **_prepare_timing_kwargs(prepared),
                )
            )
            await self.gateway.discard_prepared(prepared)
            raise DispatchBlocked(
                f"final_dispatch_gate_error:{classified['original_type']}:{classified['message']}"
            ) from exc
        if not allowed:
            blocked_reason = reason or "final dispatch gate rejected order"
            await self.apply(
                OrderPreDispatchFailed(
                    **new_envelope(
                        session_id=session_id,
                        dedupe_key=f"pre-dispatch-failed:FINAL_GATE:{spec.order_id}",
                    ),
                    role=role,
                    order_id=spec.order_id,
                    stage=PreDispatchStage.FINAL_GATE,
                    error_class="DispatchBlocked",
                    error_code="FINAL_GATE_REJECTED",
                    message=blocked_reason,
                    requested_protection_price=prepared.requested_protection_price,
                    effective_protection_price=prepared.effective_protection_price,
                    tick_size=prepared.tick_size,
                    **_prepare_timing_kwargs(prepared),
                )
            )
            await self.gateway.discard_prepared(prepared)
            raise DispatchBlocked(blocked_reason)
        if recovery or historical_recovery:
            blocked = self._recovery_limit_block(
                session_id, dispatch_spec, historical=historical_recovery
            )
            if blocked is not None:
                await self.gateway.discard_prepared(prepared)
                raise DispatchBlocked(blocked)
        stage_started = time.monotonic()
        await self.apply(
            DispatchAuthorized(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"dispatch-authorized:{spec.order_id}:{digest}",
                ),
                role=role,
                order_id=spec.order_id,
                prepared_order_digest=digest,
            )
        )
        attempt_id = str(uuid4())
        await self.apply(
            SubmissionAttempted(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"submission-attempt:{attempt_id}",
                ),
                role=role,
                order_id=spec.order_id,
                attempt_id=attempt_id,
            )
        )
        self._note_timing(spec.order_id, "authorize_ms", stage_started)
        stage_started = time.monotonic()
        try:
            result = await self.gateway.post_order(prepared)
            self._note_timing(spec.order_id, "post_ms", stage_started)
        except (Exception, asyncio.CancelledError) as exc:
            self._note_timing(spec.order_id, "post_ms", stage_started)
            classified = describe_error(exc, operation=PolymarketOperation.DISPATCH)
            await self.apply(
                SubmissionFailed(
                    **new_envelope(
                        session_id=session_id,
                        dedupe_key=f"submission-failed:{attempt_id}",
                    ),
                    role=role,
                    order_id=spec.order_id,
                    attempt_id=attempt_id,
                    error_class=str(classified["category"]),
                    message=(f"{classified['original_type']}:{classified['message']}"),
                    ambiguous=True,
                )
            )
            raise
        await self.apply(
            SubmissionResponseObserved(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"submission-response:{attempt_id}",
                ),
                role=role,
                order_id=spec.order_id,
                attempt_id=attempt_id,
                accepted=result.accepted,
                venue_order_id=result.venue_order_id,
                status=result.status,
                cumulative_matched_shares=result.cumulative_matched_shares,
                trade_ids=result.trade_ids,
                error_code=result.error_code,
                message=result.message,
            )
        )
        return result

    async def cancel(
        self,
        *,
        session_id: str,
        venue_order_ids: Sequence[str],
        reason: str,
        token_id: str | None = None,
    ) -> CancelResult:
        """Cancel session-owned live orders. Does not increment mutation_attempts."""
        async with self._mutation_lock:
            return await self._cancel_locked(
                session_id=session_id,
                venue_order_ids=venue_order_ids,
                reason=reason,
                token_id=token_id,
            )

    async def _cancel_locked(
        self,
        *,
        session_id: str,
        venue_order_ids: Sequence[str],
        reason: str,
        token_id: str | None = None,
    ) -> CancelResult:
        ids = tuple(str(item) for item in venue_order_ids if str(item).strip())
        if not ids:
            return CancelResult(canceled=(), not_canceled=())
        state = self.states.get(session_id)
        auth = None if state is None else state.recovery_authorization
        if auth is not None:
            if auth.action not in {"cancel", "cancel_and_close"}:
                raise DispatchBlocked("recovery_action_type_mismatch")
            allowed = set(auth.venue_order_ids)
            if not allowed and state is not None:
                allowed = {
                    order.venue_order_id
                    for order in state.orders.values()
                    if order.venue_order_id
                }
                allowed.update(state.open_venue_order_ids)
            if any(item not in allowed for item in ids):
                raise DispatchBlocked("cancel_order_not_owned")
        cancel_orders = getattr(self.gateway, "cancel_orders", None)
        if not callable(cancel_orders):
            raise CancelUnsupported("gateway has no cancel_orders")
        await self.apply(
            CancelRequested(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"cancel-requested:{'|'.join(ids)}:{reason}",
                ),
                venue_order_ids=ids,
                reason=reason,
                token_id=token_id,
            )
        )
        raw = await cancel_orders(ids)
        result = raw if isinstance(raw, CancelResult) else CancelResult(
            canceled=tuple(getattr(raw, "canceled", ()) or ()),
            not_canceled=tuple((getattr(raw, "not_canceled", {}) or {}).items())
            if not isinstance(getattr(raw, "not_canceled", ()), tuple)
            else tuple(getattr(raw, "not_canceled", ())),
        )
        await self.apply(
            CancelObserved(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"cancel-observed:{'|'.join(ids)}:{reason}",
                ),
                canceled=result.canceled,
                not_canceled=result.not_canceled,
                matched_instead=result.matched_instead,
                delay_pending=result.delay_pending,
            )
        )
        return result

    def state(self, session_id: str) -> ExecutionSessionState:
        return self.states[session_id]

    def mutation_attempt_count(self, session_id: str | None = None) -> int:
        return self.journal.mutation_attempt_count(session_id)

    async def close(self) -> None:
        if self._closed:
            return
        stop_stream = getattr(self.gateway, "stop_user_stream", None)
        if callable(stop_stream):
            await stop_stream()
        self._closed = True
        if self._writer_task is not None:
            await self._queue.join()
            await self._queue.put(None)
            await self._writer_task
            self._writer_task = None
        await self.gateway.close()
        self.journal.close()
