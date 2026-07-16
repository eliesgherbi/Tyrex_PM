from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import GuruTradeSignal


@runtime_checkable
class Signal(Protocol):
    """Generic, venue-independent strategy input (P1 architecture_enhance).

    Guru copy is one signal source among many. Every signal exposes:

    * ``source`` — origin tag (e.g. ``"guru"``, ``"simple_signal_test"``); drives
      which ingress fact is written (``guru_signal`` vs ``signal_received``).
    * ``token_id`` — canonical CLOB outcome token id.
    * ``dedup_key`` — correlation id used for facts and idempotency.
    """

    @property
    def source(self) -> str: ...

    @property
    def token_id(self) -> TokenId: ...

    @property
    def dedup_key(self) -> str: ...


SIGNAL_SOURCE_GURU = "guru"


@dataclass(frozen=True)
class GuruCopySignal:
    """Enriched guru signal for strategy (parity: same as row).

    Implements the generic :class:`Signal` protocol via read-only properties so
    the guru path can flow through the same ``process_signals`` dispatch as any
    other source without changing its on-the-wire ``guru_signal`` fact.
    """

    trade: GuruTradeSignal

    @property
    def source(self) -> str:
        return SIGNAL_SOURCE_GURU

    @property
    def token_id(self) -> TokenId:
        return self.trade.token_id

    @property
    def dedup_key(self) -> str:
        return self.trade.dedup_key
