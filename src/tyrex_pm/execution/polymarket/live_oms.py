"""Live Polymarket OMS — R6 mutations disabled by default.

Implements the shared OMS protocol. ``mutations_enabled=False`` (R6 default)
rejects submit/cancel without calling transport mutation methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

from tyrex_pm.core.commands import CancelOrderCommand, SubmitOrderCommand
from tyrex_pm.core.execution_events import OrderCancelPending
from tyrex_pm.core.events import EventSource
from tyrex_pm.core.ids import CorrelationId, OrderId, new_event_id, new_order_id
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.order_store import OrderStore
from tyrex_pm.execution.polymarket.normalize import (
    order_accepted_event,
    order_canceled_event,
    order_rejected_event,
    order_submitted_event,
)
from tyrex_pm.execution.polymarket.readiness import ExecutionReadiness, ReadinessReason
from tyrex_pm.execution.polymarket.reconciliation import ReconciliationService
from tyrex_pm.execution.polymarket.transport import (
    PolymarketTransport,
    SubmitOrderRequest,
)
from tyrex_pm.portfolio.portfolio import Portfolio


class SubmissionState(str, Enum):
    COMMAND_CREATED = "COMMAND_CREATED"
    SUBMITTING = "SUBMITTING"
    VENUE_ACCEPTED = "VENUE_ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"


FactEmitter = Callable[[str, dict[str, Any]], None]


@dataclass
class LiveOrderTracking:
    order_id: OrderId
    submission: SubmissionState
    venue_order_id: str | None = None
    correlation_id: CorrelationId | None = None
    command: SubmitOrderCommand | None = None


@dataclass
class LiveOMS:
    """Production-shaped live OMS behind the shared protocol."""

    transport: PolymarketTransport
    dispatcher: EventDispatcher
    order_store: OrderStore
    portfolio: Portfolio
    mutations_enabled: bool = False
    emit_fact: FactEmitter | None = None
    readiness: ExecutionReadiness = field(default_factory=ExecutionReadiness)
    recon: ReconciliationService | None = None
    _tracking: dict[str, LiveOrderTracking] = field(default_factory=dict)
    _stopped: bool = False

    def __post_init__(self) -> None:
        if self.recon is None:
            self.recon = ReconciliationService(
                order_store=self.order_store, portfolio=self.portfolio
            )
        self.readiness.mutations_enabled = self.mutations_enabled
        if not self.mutations_enabled:
            self.readiness.deny(ReadinessReason.MUTATIONS_DISABLED)

    def _fact(self, fact_type: str, payload: dict[str, Any]) -> None:
        if self.emit_fact is not None:
            self.emit_fact(fact_type, payload)

    def stop(self) -> None:
        self._stopped = True
        self.transport.stop()
        self.readiness.deny(ReadinessReason.TRANSPORT_DISCONNECTED)
        self._fact("execution_readiness_changed", self.readiness.to_dict())

    def mark_user_stream(self, ready: bool) -> None:
        if ready:
            self.readiness.clear(ReadinessReason.USER_STREAM_UNREADY)
        else:
            self.readiness.deny(ReadinessReason.USER_STREAM_UNREADY)
            self.readiness.deny(ReadinessReason.RECONCILIATION_PENDING)
        self._fact("user_stream_state", {"ready": ready})
        self._fact("execution_readiness_changed", self.readiness.to_dict())

    def run_reconciliation(self, *, market_id: str | None = None) -> Any:
        assert self.recon is not None
        self._fact("reconciliation_started", {"market_id": market_id})
        report = self.recon.reconcile_from_transport(self.transport, market_id=market_id)
        self._fact(
            "reconciliation_completed",
            {
                "counts": report.counts(),
                "blocks_entry": report.blocks_entry,
                "requires_manual": report.requires_manual,
            },
        )
        if report.blocks_entry or report.requires_manual:
            self.readiness.deny(ReadinessReason.UNRESOLVED_MISMATCH)
            self.readiness.deny(ReadinessReason.RECONCILIATION_FAILED)
        else:
            self.readiness.clear(ReadinessReason.UNRESOLVED_MISMATCH)
            self.readiness.clear(ReadinessReason.RECONCILIATION_FAILED)
            self.readiness.clear(ReadinessReason.RECONCILIATION_PENDING)
        # Resolve UNKNOWN_SUBMISSION if venue now shows the order
        for track in list(self._tracking.values()):
            if track.submission is SubmissionState.UNKNOWN_SUBMISSION:
                self._try_resolve_unknown(track)
        self._fact("execution_readiness_changed", self.readiness.to_dict())
        return report

    def _try_resolve_unknown(self, track: LiveOrderTracking) -> None:
        opens = self.transport.get_open_orders()
        if len(opens) == 1 and track.command is not None:
            vo = opens[0]
            # Conservative: single open order matching token/side/size
            cmd = track.command
            if (
                vo.instrument_token_id == cmd.instrument_id.value
                and vo.side.upper() == cmd.side.value
                and vo.original_size == cmd.quantity
            ):
                track.venue_order_id = vo.venue_order_id
                track.submission = SubmissionState.VENUE_ACCEPTED
                assert self.recon is not None
                self.recon.register_owned(vo.venue_order_id)
                when = datetime.now(timezone.utc)
                self.dispatcher.publish(
                    order_accepted_event(
                        order_id=track.order_id,
                        venue_order_id=vo.venue_order_id,
                        correlation_id=cmd.correlation_id,
                        when=when,
                    )
                )
                self.readiness.clear(ReadinessReason.UNKNOWN_SUBMISSION)
                self._fact(
                    "submission_resolved",
                    {
                        "order_id": track.order_id.value,
                        "venue_order_id": vo.venue_order_id,
                    },
                )
                return
        self._fact(
            "manual_intervention_required",
            {
                "reason": "UNKNOWN_SUBMISSION_UNRESOLVED",
                "order_id": track.order_id.value,
            },
        )

    def submit(self, command: SubmitOrderCommand, *, order_id: OrderId | None = None) -> OrderId:
        if self._stopped:
            raise RuntimeError("LiveOMS stopped")
        oid = order_id or new_order_id()
        when = command.created_at
        self.order_store.create_from_command(command, order_id=oid)
        track = LiveOrderTracking(
            order_id=oid,
            submission=SubmissionState.COMMAND_CREATED,
            correlation_id=command.correlation_id,
            command=command,
        )
        self._tracking[oid.value] = track
        self.dispatcher.publish(
            order_submitted_event(
                order_id=oid,
                client_order_id=command.client_order_id,
                instrument_id=command.instrument_id,
                side=command.side,
                quantity=command.quantity,
                limit_price=command.limit_price,
                correlation_id=command.correlation_id,
                when=when,
            )
        )
        track.submission = SubmissionState.SUBMITTING
        self._fact(
            "command_submit_dispatch",
            {
                "order_id": oid.value,
                "mutations_enabled": self.mutations_enabled,
            },
        )

        if not self.mutations_enabled:
            # R6: never call transport.submit_order
            self.dispatcher.publish(
                order_rejected_event(
                    order_id=oid,
                    reason_code="MUTATIONS_DISABLED_R6",
                    correlation_id=command.correlation_id,
                    when=when,
                )
            )
            track.submission = SubmissionState.REJECTED
            self._fact(
                "submission_denied",
                {"order_id": oid.value, "reason": "MUTATIONS_DISABLED_R6"},
            )
            return oid

        req = SubmitOrderRequest(
            token_id=command.instrument_id.value,
            side=command.side.value,
            price=str(command.limit_price),
            size=str(command.quantity),
            local_order_id=oid.value,
            payload={"plan_id": command.plan_id.value},
        )
        result = self.transport.submit_order(req)
        if result.uncertain:
            track.submission = SubmissionState.UNKNOWN_SUBMISSION
            self.readiness.deny(ReadinessReason.UNKNOWN_SUBMISSION)
            self._fact(
                "submission_uncertain",
                {"order_id": oid.value, "error": result.error},
            )
            return oid
        if not result.ok or not result.venue_order_id:
            track.submission = SubmissionState.REJECTED
            self.dispatcher.publish(
                order_rejected_event(
                    order_id=oid,
                    reason_code=result.error or "SUBMIT_REJECTED",
                    correlation_id=command.correlation_id,
                    when=when,
                )
            )
            return oid
        track.venue_order_id = result.venue_order_id
        track.submission = SubmissionState.VENUE_ACCEPTED
        assert self.recon is not None
        self.recon.register_owned(result.venue_order_id)
        self.dispatcher.publish(
            order_accepted_event(
                order_id=oid,
                venue_order_id=result.venue_order_id,
                correlation_id=command.correlation_id,
                when=when,
            )
        )
        return oid

    def cancel(self, command: CancelOrderCommand) -> None:
        if self._stopped:
            return
        rec = self.order_store.get(command.order_id)
        if rec is None:
            self._fact("cancel_unknown_order", {"order_id": command.order_id.value})
            return
        if not self.mutations_enabled:
            self._fact(
                "cancel_denied",
                {"order_id": command.order_id.value, "reason": "MUTATIONS_DISABLED_R6"},
            )
            return
        when = command.created_at
        self.dispatcher.publish(
            OrderCancelPending(
                event_id=new_event_id(),
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
                ts_event=when,
                ts_received=when,
                source=EventSource.SYSTEM,
                order_id=command.order_id,
            )
        )
        if rec.venue_order_id is None:
            self._fact(
                "cancel_uncertain",
                {"order_id": command.order_id.value, "reason": "NO_VENUE_ID"},
            )
            return
        result = self.transport.cancel_order(rec.venue_order_id)
        if result.uncertain:
            self._fact(
                "cancel_uncertain",
                {"order_id": command.order_id.value, "venue_order_id": rec.venue_order_id},
            )
            return
        if result.ok:
            self.dispatcher.publish(
                order_canceled_event(
                    order_id=command.order_id,
                    reason_code=command.reason_code,
                    correlation_id=command.correlation_id,
                    when=when,
                )
            )
            return
        self._fact(
            "cancel_rejected",
            {
                "order_id": command.order_id.value,
                "error": result.error,
                "already_terminal": result.already_terminal,
            },
        )

    def has_uncertain_submission(self) -> bool:
        return any(
            t.submission is SubmissionState.UNKNOWN_SUBMISSION for t in self._tracking.values()
        )
