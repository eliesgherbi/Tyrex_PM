"""Authoritative market / reference state and decision-time views."""

from __future__ import annotations

from typing import Any

from tyrex_pm.market_data.binding_record import (
    BindingLifecycleRole,
    MarketBindingRecord,
    binding_record_from_discovery,
    make_binding_id,
    persist_bindings,
)
from tyrex_pm.market_data.book_health import (
    ConnectionHealth,
    FeedSyncPhase,
    SideLiquidity,
    SyncHealth,
)
from tyrex_pm.market_data.book_store import BookState, MarketStateStore
from tyrex_pm.market_data.book_view import BookView, ExecutableBookQuote, LegBookSnapshot
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote, VwapResult, book_quote, executable_vwap
from tyrex_pm.market_data.freshness import (
    FreshnessAssessment,
    FreshnessConfig,
    FreshnessReason,
    TimestampBasis,
    assess_freshness,
)
from tyrex_pm.market_data.reference_store import ReferenceDataStore, ReferenceState
from tyrex_pm.market_data.registry import InstrumentRegistry

# BindingFeed / BookFeedSupervisor are lazy-exported to avoid a circular import:
# rest_book → book_store → market_data.__init__ → book_feed → rest_book

__all__ = [
    "BindingFeed",
    "BindingLifecycleRole",
    "BookFeedSupervisor",
    "BookState",
    "BookView",
    "ConnectionHealth",
    "DecisionSnapshot",
    "ExecutableBookQuote",
    "ExecutableQuote",
    "FeedSyncPhase",
    "FreshnessAssessment",
    "FreshnessConfig",
    "FreshnessReason",
    "InstrumentRegistry",
    "LegBookSnapshot",
    "MarketBindingRecord",
    "MarketStateStore",
    "ReferenceDataStore",
    "ReferenceState",
    "SideLiquidity",
    "SyncHealth",
    "TimestampBasis",
    "VwapResult",
    "assess_freshness",
    "binding_record_from_discovery",
    "book_quote",
    "executable_vwap",
    "make_binding_id",
    "persist_bindings",
]


def __getattr__(name: str) -> Any:
    if name in {"BindingFeed", "BookFeedSupervisor"}:
        from tyrex_pm.market_data.book_feed import BindingFeed, BookFeedSupervisor

        return BindingFeed if name == "BindingFeed" else BookFeedSupervisor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
