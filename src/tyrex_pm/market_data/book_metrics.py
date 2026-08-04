"""Non-blocking book event accounting (receive → terminal outcome)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TokenBookCounters:
    """Per-token mutually exclusive accounting stages."""

    snapshots_received: int = 0
    snapshots_decoded: int = 0
    snapshots_matched: int = 0
    snapshots_applied: int = 0
    snapshots_decode_rejected: int = 0
    snapshots_identity_rejected: int = 0
    snapshots_unsupported: int = 0
    snapshots_application_rejected: int = 0
    snapshots_ignored_noop: int = 0

    deltas_received: int = 0
    deltas_decoded: int = 0
    deltas_matched: int = 0
    deltas_applied: int = 0
    deltas_decode_rejected: int = 0
    deltas_identity_rejected: int = 0
    deltas_unsupported: int = 0
    deltas_application_rejected: int = 0
    deltas_ignored_noop: int = 0

    levels_inserted: int = 0
    levels_updated: int = 0
    levels_deleted: int = 0
    unknown_token: int = 0
    old_generation: int = 0
    rest_requests: int = 0
    rest_successes: int = 0
    rest_failures: int = 0
    bootstrap_count: int = 0
    resync_count: int = 0
    reconnect_count: int = 0

    def reconcile_ok(self) -> bool:
        """Every received snapshot/delta reaches a terminal decoded branch."""
        snap_recv = self.snapshots_received
        snap_term = (
            self.snapshots_decode_rejected
            + self.snapshots_identity_rejected
            + self.snapshots_unsupported
            + self.snapshots_applied
            + self.snapshots_ignored_noop
            + self.snapshots_application_rejected
        )
        # decoded = matched + identity + unsupported; matched split into applied/noop/rejected
        delta_recv = self.deltas_received
        delta_term = (
            self.deltas_decode_rejected
            + self.deltas_identity_rejected
            + self.deltas_unsupported
            + self.deltas_applied
            + self.deltas_ignored_noop
            + self.deltas_application_rejected
        )
        return snap_recv == snap_term and delta_recv == delta_term

    def to_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.__dict__.items()}


@dataclass
class BookMetricsRegistry:
    by_token: dict[str, TokenBookCounters] = field(default_factory=dict)

    def for_token(self, token_id: str) -> TokenBookCounters:
        if token_id not in self.by_token:
            self.by_token[token_id] = TokenBookCounters()
        return self.by_token[token_id]

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {tok: c.to_dict() for tok, c in self.by_token.items()}
