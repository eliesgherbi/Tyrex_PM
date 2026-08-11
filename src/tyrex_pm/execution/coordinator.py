"""Serialized account execution authority."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from tyrex_pm.adapters.polymarket.sdk_errors import PolymarketOperation, describe_error
from tyrex_pm.execution.evidence import (
    DispatchAuthorized,
    ExecutionEvidence,
    ExecutionRole,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    PreDispatchStage,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
    new_envelope,
)
from tyrex_pm.execution.orders import OrderSpec, order_spec_to_dict
from tyrex_pm.execution.reducer import ReducerEffect, reduce_execution_event
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

    async def start(self) -> None:
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer(), name="execution-coordinator")

    async def restore(self, session_id: str) -> ExecutionSessionState:
        if self._writer_task is not None:
            raise RuntimeError("restore must happen before coordinator start")
        state = ExecutionSessionState()
        for event in self.journal.load(session_id):
            state, _ = reduce_execution_event(state, event)
        self.states[session_id] = state
        return state

    async def apply(self, event: ExecutionEvidence) -> tuple[ReducerEffect, ...]:
        if self._closed:
            raise RuntimeError("execution coordinator is closed")
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
                inserted = self.journal.append(event)
                state = self.states.setdefault(event.session_id, ExecutionSessionState())
                if inserted:
                    state, effects = reduce_execution_event(state, event)
                    self.states[event.session_id] = state
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
    ) -> GatewaySubmissionResult:
        """Prepare/sign, revalidate, durably authorize, then POST."""
        await self.apply(
            OrderRequested(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"order-requested:{role.value}:{spec.order_id}",
                ),
                role=role,
                order=spec,
            )
        )
        try:
            prepared = await self.gateway.prepare_order(spec)
        except Exception as exc:
            classified = describe_error(
                getattr(exc, "original_error", None) or exc,
                operation=PolymarketOperation.PREPARE,
            )
            details = getattr(exc, "preparation_details", {})
            error_code = str(getattr(exc, "error_code", None) or classified["category"])
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
                )
            )
            raise OrderPreparationFailed(
                f"{error_code}:{classified['original_type']}:{classified['message']}"
            ) from exc

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
            )
        )
        dispatch_spec = prepared.dispatch_spec or spec
        try:
            allowed, reason = await self.final_gate(dispatch_spec)
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
                )
            )
            await self.gateway.discard_prepared(prepared)
            raise DispatchBlocked(blocked_reason)
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
        try:
            result = await self.gateway.post_order(prepared)
        except (Exception, asyncio.CancelledError) as exc:
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
