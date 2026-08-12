"""One capability model for the production trading runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class RuntimeCapabilities:
    observable: bool
    decision_ready: bool
    execution_infrastructure_ready: bool
    entry_executable: bool
    exit_executable: bool
    reconciliation_ready: bool
    blockers: tuple[str, ...] = ()


@dataclass
class CapabilityController:
    """Derive permissions from facts; callers cannot set permissions directly."""

    live_requested: bool = False
    public_feeds_ready: bool = False
    active_market_ready: bool = False
    books_ready: bool = False
    model_ready: bool = False
    account_reads_ready: bool = False
    user_stream_ready: bool = False
    collateral_ready: bool = False
    entry_allowance_ready: bool = False
    order_metadata_ready: bool = False
    selected_token_sellable: bool = False
    prior_scope_clear: bool = False
    entry_window_open: bool = False
    kill_switch_active: bool = False
    stream_gap_active: bool = False
    book_desync_active: bool = False
    extra_blockers: dict[str, str | None] = field(default_factory=dict)

    def set_blocker(self, code: str, detail: str | None = None) -> None:
        self.extra_blockers[code] = detail

    def clear_blocker(self, code: str) -> None:
        self.extra_blockers.pop(code, None)

    def snapshot(self, *, exposed: bool = False) -> RuntimeCapabilities:
        blockers: list[str] = []
        facts: Mapping[str, bool] = {
            "LIVE_NOT_REQUESTED": self.live_requested,
            "PUBLIC_FEEDS_NOT_READY": self.public_feeds_ready,
            "ACTIVE_MARKET_NOT_READY": self.active_market_ready,
            "BOOKS_NOT_READY": self.books_ready,
            "MODEL_NOT_READY": self.model_ready,
            "ACCOUNT_READS_NOT_READY": self.account_reads_ready,
            "USER_STREAM_NOT_READY": self.user_stream_ready,
            "COLLATERAL_NOT_READY": self.collateral_ready,
            "ENTRY_ALLOWANCE_NOT_READY": self.entry_allowance_ready,
            "ORDER_METADATA_NOT_READY": self.order_metadata_ready,
            "PRIOR_SCOPE_NOT_CLEAR": self.prior_scope_clear,
            "ENTRY_WINDOW_CLOSED": self.entry_window_open,
        }
        for code, ready in facts.items():
            if not ready:
                blockers.append(code)
        if self.kill_switch_active:
            blockers.append("KILL_SWITCH_ACTIVE")
        if self.stream_gap_active:
            blockers.append("USER_STREAM_GAP")
        if self.book_desync_active:
            blockers.append("BOOK_DESYNC")
        blockers.extend(sorted(self.extra_blockers))

        observable = self.public_feeds_ready and self.active_market_ready
        # Infrastructure is the venue/account machinery that can submit safely.
        # Strategy model readiness is intentionally excluded so a vol/input
        # lockout is not mislabeled as an infrastructure failure.
        execution_infrastructure_ready = (
            self.live_requested
            and self.public_feeds_ready
            and self.active_market_ready
            and self.books_ready
            and self.account_reads_ready
            and self.user_stream_ready
            and self.collateral_ready
            and self.entry_allowance_ready
            and self.order_metadata_ready
            and self.prior_scope_clear
            and self.entry_window_open
            and not self.kill_switch_active
            and not self.stream_gap_active
            and not self.book_desync_active
            and not self.extra_blockers
        )
        decision_ready = observable and self.books_ready and self.model_ready
        entry_executable = execution_infrastructure_ready and self.model_ready
        # Exits remain available when entry-only facts fail.  This coarse
        # capability only requires known, sellable inventory on the active
        # market.  Book synchronization/freshness is checked from a newly
        # captured BookView by FinalExecutionGate on every retry, so a cached
        # DESYNC flag cannot strand an otherwise recoverable position.
        exit_executable = (
            exposed
            and self.live_requested
            and self.account_reads_ready
            and self.active_market_ready
            and self.selected_token_sellable
        )
        reconciliation_ready = self.live_requested and self.account_reads_ready
        return RuntimeCapabilities(
            observable=observable,
            decision_ready=decision_ready,
            execution_infrastructure_ready=execution_infrastructure_ready,
            entry_executable=entry_executable,
            exit_executable=exit_executable,
            reconciliation_ready=reconciliation_ready,
            blockers=tuple(blockers),
        )

    def user_stream_gap(self) -> None:
        self.user_stream_ready = False
        self.stream_gap_active = True

    def user_stream_recovered(self) -> None:
        self.user_stream_ready = True
        self.stream_gap_active = False

    def book_desync(self) -> None:
        self.books_ready = False
        self.book_desync_active = True

    def book_snapshot_restored(self) -> None:
        self.books_ready = True
        self.book_desync_active = False
