"""R7 mutation lifecycle state machine (entry + exit authorization separated)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MutationPhase(str, Enum):
    MUTATIONS_DISABLED = "MUTATIONS_DISABLED"
    APPROVAL_PREPARED = "APPROVAL_PREPARED"
    APPROVAL_ACCEPTED = "APPROVAL_ACCEPTED"
    ARMED = "ARMED"
    ENTRY_SUBMITTING = "ENTRY_SUBMITTING"
    ENTRY_ACCEPTED = "ENTRY_ACCEPTED"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    ENTRY_UNKNOWN = "ENTRY_UNKNOWN"
    ENTRY_FILLED = "ENTRY_FILLED"
    ENTRY_PARTIAL = "ENTRY_PARTIAL"
    ENTRY_CANCELED = "ENTRY_CANCELED"
    POSITION_ACTIVE = "POSITION_ACTIVE"
    EXIT_SUBMITTING = "EXIT_SUBMITTING"
    EXIT_FILLED = "EXIT_FILLED"
    EXIT_PARTIAL = "EXIT_PARTIAL"
    EXIT_UNKNOWN = "EXIT_UNKNOWN"
    RECONCILING = "RECONCILING"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    BLOCKED = "BLOCKED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"
    UNKNOWN_CANCEL = "UNKNOWN_CANCEL"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"


_ENTRY_ALLOWED_FROM = frozenset(
    {
        MutationPhase.ARMED,
    }
)

_FAILURE = frozenset(
    {
        MutationPhase.BLOCKED,
        MutationPhase.MANUAL_INTERVENTION,
        MutationPhase.UNKNOWN_SUBMISSION,
        MutationPhase.UNKNOWN_CANCEL,
        MutationPhase.POSITION_MISMATCH,
        MutationPhase.RECONCILIATION_FAILED,
        MutationPhase.ENTRY_UNKNOWN,
        MutationPhase.EXIT_UNKNOWN,
    }
)


@dataclass
class MutationLifecycle:
    """Narrow one-shot mutation enablement with separate exit authorization."""

    phase: MutationPhase = MutationPhase.MUTATIONS_DISABLED
    entry_mutations_enabled: bool = False
    exit_mutations_enabled: bool = False
    approval_artifact_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def _set(self, phase: MutationPhase, *, reason: str | None = None) -> None:
        self.history.append(
            {
                "from": self.phase.value,
                "to": phase.value,
                "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.phase = phase

    def prepare_approval(self, artifact_id: str) -> None:
        if self.phase not in {
            MutationPhase.MUTATIONS_DISABLED,
            MutationPhase.APPROVAL_PREPARED,
            MutationPhase.FLAT_CONFIRMED,
        }:
            self._set(MutationPhase.BLOCKED, reason="INVALID_PREPARE")
            return
        self.approval_artifact_id = artifact_id
        self.entry_mutations_enabled = False
        self.exit_mutations_enabled = False
        self._set(MutationPhase.APPROVAL_PREPARED)

    def accept_approval(self) -> None:
        if self.phase is not MutationPhase.APPROVAL_PREPARED:
            self._set(MutationPhase.BLOCKED, reason="ACCEPT_WITHOUT_PREPARE")
            return
        self._set(MutationPhase.APPROVAL_ACCEPTED)

    def arm(self) -> None:
        """Enable one-shot entry mutation after explicit R7B authorization."""
        if self.phase is not MutationPhase.APPROVAL_ACCEPTED:
            self._set(MutationPhase.BLOCKED, reason="ARM_WITHOUT_ACCEPT")
            return
        self.entry_mutations_enabled = True
        self.exit_mutations_enabled = True  # exit authorized with same artifact
        self._set(MutationPhase.ARMED)

    def can_submit_entry(self) -> bool:
        return self.entry_mutations_enabled and self.phase in _ENTRY_ALLOWED_FROM

    def can_submit_exit(self) -> bool:
        return self.exit_mutations_enabled and self.phase in {
            MutationPhase.POSITION_ACTIVE,
            MutationPhase.ENTRY_FILLED,
            MutationPhase.ENTRY_PARTIAL,
            MutationPhase.EXIT_PARTIAL,
        }

    def note_entry_submitting(self) -> None:
        if not self.can_submit_entry():
            self._set(MutationPhase.BLOCKED, reason="ENTRY_NOT_ARMED")
            return
        self.entry_mutations_enabled = False  # one-shot: disable further entry
        self._set(MutationPhase.ENTRY_SUBMITTING)

    def note_entry_accepted(self) -> None:
        self._set(MutationPhase.ENTRY_ACCEPTED)

    def note_entry_rejected(self) -> None:
        self.entry_mutations_enabled = False
        self._set(MutationPhase.ENTRY_REJECTED)
        self.disable_all_mutations()

    def note_entry_unknown(self) -> None:
        self.entry_mutations_enabled = False
        self._set(MutationPhase.ENTRY_UNKNOWN)
        self._set(MutationPhase.UNKNOWN_SUBMISSION)

    def note_entry_filled(self, *, partial: bool = False) -> None:
        self.entry_mutations_enabled = False
        self._set(MutationPhase.ENTRY_PARTIAL if partial else MutationPhase.ENTRY_FILLED)
        self._set(MutationPhase.POSITION_ACTIVE)

    def note_entry_canceled(self) -> None:
        self._set(MutationPhase.ENTRY_CANCELED)

    def note_exit_submitting(self) -> None:
        if not self.can_submit_exit():
            self._set(MutationPhase.BLOCKED, reason="EXIT_NOT_AUTHORIZED")
            return
        self._set(MutationPhase.EXIT_SUBMITTING)

    def note_exit_filled(self, *, partial: bool = False) -> None:
        if partial:
            self._set(MutationPhase.EXIT_PARTIAL)
            self._set(MutationPhase.POSITION_ACTIVE)
        else:
            self._set(MutationPhase.EXIT_FILLED)

    def note_exit_unknown(self) -> None:
        self._set(MutationPhase.EXIT_UNKNOWN)

    def note_reconciling(self) -> None:
        self._set(MutationPhase.RECONCILING)

    def note_flat_confirmed(self) -> None:
        self.disable_all_mutations()
        self._set(MutationPhase.FLAT_CONFIRMED)
        self._set(MutationPhase.MUTATIONS_DISABLED)

    def note_manual_intervention(self) -> None:
        self.entry_mutations_enabled = False
        # Keep exit enabled only if still needed — operator may flatten manually
        self._set(MutationPhase.MANUAL_INTERVENTION)

    def note_position_mismatch(self) -> None:
        self.entry_mutations_enabled = False
        self._set(MutationPhase.POSITION_MISMATCH)

    def disable_all_mutations(self) -> None:
        self.entry_mutations_enabled = False
        self.exit_mutations_enabled = False

    def disable_entry_only(self) -> None:
        self.entry_mutations_enabled = False

    def is_failure(self) -> bool:
        return self.phase in _FAILURE

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "entry_mutations_enabled": self.entry_mutations_enabled,
            "exit_mutations_enabled": self.exit_mutations_enabled,
            "approval_artifact_id": self.approval_artifact_id,
            "history_len": len(self.history),
        }
