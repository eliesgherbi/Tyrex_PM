"""Synchronous in-process event dispatcher.

Semantics (R2)
--------------
* **Ordering:** higher priority first; equal priority in subscription order.
* **Routing:** exact event type only (subclasses are not delivered to base
  subscribers). Simpler until a base-type consumer exists.
* **Failures:** fail-fast — wrap in ``DispatchError``, stop remaining handlers.
* **Reentrant publish:** queued until the current publication finishes (BFS
  per nesting root), avoiding deep recursive handler stacks.
* **Subscribe/unsubscribe during publish:** handler list is snapshotted at the
  start of each publication; mutations apply to subsequent events.
* **Duplicates:** the same callable may not be registered twice for the same
  event type (raises ``ValueError``).
* **Idempotency:** the dispatcher does *not* deduplicate events. Venue
  adapters / stores own venue-id idempotency later (R5–R6).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, TypeVar
from uuid import uuid4

from tyrex_pm.core.events import Event
from tyrex_pm.core.ids import EventId

E = TypeVar("E", bound=Event)
EventHandler = Callable[[Event], None]


@dataclass(frozen=True, slots=True)
class Subscription:
    subscription_id: str
    event_type: type[Event]
    handler: EventHandler
    priority: int


@dataclass(frozen=True, slots=True)
class DispatchResult:
    event_id: EventId
    event_type: str
    handler_count: int
    delivered: int
    queued_followups: int = 0


class DispatchError(RuntimeError):
    """Handler failure during publish; subsequent handlers were not invoked."""

    def __init__(
        self,
        *,
        event_id: EventId,
        handler: EventHandler,
        cause: BaseException,
    ) -> None:
        self.event_id = event_id
        self.handler = handler
        self.cause = cause
        handler_name = getattr(handler, "__qualname__", repr(handler))
        super().__init__(
            f"handler {handler_name!r} failed for event_id={event_id.value}: {cause!r}"
        )


@dataclass(slots=True)
class _HandlerEntry:
    subscription_id: str
    handler: EventHandler
    priority: int
    order: int


@dataclass
class EventDispatcher:
    _handlers: dict[type[Event], list[_HandlerEntry]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _order_counter: int = 0
    _publishing: bool = False
    _queue: Deque[Event] = field(default_factory=deque)

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], None],
        *,
        priority: int = 0,
    ) -> Subscription:
        if not isinstance(event_type, type) or not issubclass(event_type, Event):
            raise TypeError("event_type must be a subclass of Event")
        if not callable(handler):
            raise TypeError("handler must be callable")

        entries = self._handlers[event_type]
        for entry in entries:
            if entry.handler is handler:
                raise ValueError(f"duplicate subscription of {handler!r} for {event_type.__name__}")

        self._order_counter += 1
        sub_id = str(uuid4())
        entries.append(
            _HandlerEntry(
                subscription_id=sub_id,
                handler=handler,  # type: ignore[arg-type]
                priority=priority,
                order=self._order_counter,
            )
        )
        return Subscription(
            subscription_id=sub_id,
            event_type=event_type,
            handler=handler,  # type: ignore[arg-type]
            priority=priority,
        )

    def unsubscribe(self, subscription: Subscription) -> None:
        entries = self._handlers.get(subscription.event_type)
        if not entries:
            return
        self._handlers[subscription.event_type] = [
            entry for entry in entries if entry.subscription_id != subscription.subscription_id
        ]
        if not self._handlers[subscription.event_type]:
            del self._handlers[subscription.event_type]

    def publish(self, event: Event) -> DispatchResult:
        if not isinstance(event, Event):
            raise TypeError("publish expects an Event instance")

        if self._publishing:
            self._queue.append(event)
            return DispatchResult(
                event_id=event.event_id,
                event_type=type(event).__name__,
                handler_count=self._handler_count(type(event)),
                delivered=0,
                queued_followups=1,
            )

        self._publishing = True
        try:
            primary = self._deliver(event)
            followups = 0
            while self._queue:
                nested = self._queue.popleft()
                self._deliver(nested)
                followups += 1
            return DispatchResult(
                event_id=primary.event_id,
                event_type=primary.event_type,
                handler_count=primary.handler_count,
                delivered=primary.delivered,
                queued_followups=followups,
            )
        finally:
            self._publishing = False
            self._queue.clear()

    def _handler_count(self, event_type: type[Event]) -> int:
        return len(self._handlers.get(event_type, ()))

    def _deliver(self, event: Event) -> DispatchResult:
        event_type = type(event)
        # Snapshot at publication start so subscribe/unsubscribe apply next time.
        snapshot = list(self._handlers.get(event_type, ()))
        ordered = sorted(snapshot, key=lambda entry: (-entry.priority, entry.order))
        delivered = 0
        for entry in ordered:
            try:
                entry.handler(event)
            except DispatchError:
                raise
            except Exception as exc:  # noqa: BLE001 - rewrap as DispatchError
                raise DispatchError(
                    event_id=event.event_id,
                    handler=entry.handler,
                    cause=exc,
                ) from exc
            delivered += 1
        return DispatchResult(
            event_id=event.event_id,
            event_type=event_type.__name__,
            handler_count=len(ordered),
            delivered=delivered,
            queued_followups=0,
        )
