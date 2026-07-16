from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from tyrex_pm.core.models import Intent
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.signals.base import GuruCopySignal, Signal


@dataclass(frozen=True)
class StrategyContext:
    """Read-only context handed to a strategy for one signal (P1).

    Strategies must not mutate state or call the venue; they read from ``coord``
    (allocation ledger, wallet snapshot, market info) and the optional
    ``market_state`` (Phase 2 ``MarketStateStore``) and return intents only.
    """

    coord: RuntimeCoordinator
    #: Phase 2 ``MarketStateStore`` (typed loosely to avoid an import cycle); may
    #: be ``None`` until market data is wired/enabled.
    market_state: Any | None = None


@dataclass(frozen=True)
class StrategyResult:
    """What a strategy returns for one signal.

    * ``intents`` — zero or more intents to push through risk → planner → OMS.
    * ``skip_reason`` — set to emit a ``strategy_skip`` fact and drop the signal.
    * ``meta`` — operator evidence merged into the ``intent_created`` fact (and,
      for the reserved key ``guru_exit_health``, emitted as a ``health`` fact).
    """

    intents: list[Intent] = field(default_factory=list)
    skip_reason: str | None = None
    meta: dict[str, Any] | None = None


class Strategy(Protocol):
    """Generic strategy contract (P1 architecture_enhance).

    The single dispatch entry point is :meth:`on_signal`. ``GuruFollowStrategy``
    additionally keeps :meth:`on_guru_signal` as a backward-compatible alias.
    """

    def on_signal(self, signal: Signal, ctx: StrategyContext) -> StrategyResult: ...


class LegacyGuruStrategy(Protocol):
    """Back-compat shape kept for callers still using the tuple return."""

    def on_guru_signal(
        self,
        sig: GuruCopySignal,
        coord: RuntimeCoordinator,
    ) -> tuple[list[Intent], str | None, dict[str, Any] | None]: ...
