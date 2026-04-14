"""
Producer boundary for :class:`TradableStateHealthSnapshot`.

**Spike-gated:** Live implementation must subscribe to documented Nautilus ``LiveExecEngine`` /
message-bus signals — not log scraping. See ``Docs/Implementation/refactor_lifecycle/tradable_state_health.md`` §5–§6.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tyrex_pm.runtime.tradable_state.types import TradableStateHealthSnapshot


@runtime_checkable
class TradableStateHealthSource(Protocol):
    """Latest health snapshot (caller may cache; implementations should be cheap or self-cache)."""

    def snapshot(self) -> TradableStateHealthSnapshot: ...
