"""In-process event engine primitives."""

from tyrex_pm.engine.dispatcher import (
    DispatchError,
    DispatchResult,
    EventDispatcher,
    Subscription,
)

__all__ = [
    "DispatchError",
    "DispatchResult",
    "EventDispatcher",
    "Subscription",
]
