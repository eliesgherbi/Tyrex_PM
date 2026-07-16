"""R5.1 entry/exit retry scheduling — not risk dedup.

Ownership:
* Strategy/lifecycle decides whether an economic action is needed.
* RetryController decides when another attempt is allowed.
* Dedup registry prevents replay of the same attempt key.
* Risk evaluates safety; planner evaluates executability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from uuid import uuid4


class EntryRetryPhase(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INTENT_PENDING = "INTENT_PENDING"
    PLAN_FAILED = "PLAN_FAILED"
    RETRY_WAIT = "RETRY_WAIT"
    CAP_REACHED = "CAP_REACHED"


class ExitRetryPhase(str, Enum):
    IDLE = "IDLE"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    EXIT_PENDING = "EXIT_PENDING"
    EXIT_RETRY_WAIT = "EXIT_RETRY_WAIT"
    ESCALATED = "ESCALATED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


@dataclass(frozen=True, kw_only=True)
class RetryConfig:
    entry_cooldown: timedelta = timedelta(seconds=5)
    entry_max_attempts: int = 3
    exit_cooldown: timedelta = timedelta(seconds=3)
    exit_max_normal_retries: int = 3
    exit_escalate_after: int = 2
    material_book_bps: Decimal = Decimal("50")  # 0.50% mid move counts as material

    def __post_init__(self) -> None:
        if self.entry_cooldown <= timedelta(0):
            raise ValueError("entry_cooldown must be > 0")
        if self.exit_cooldown <= timedelta(0):
            raise ValueError("exit_cooldown must be > 0")
        if self.entry_max_attempts < 1:
            raise ValueError("entry_max_attempts must be >= 1")
        if self.exit_max_normal_retries < 1:
            raise ValueError("exit_max_normal_retries must be >= 1")


@dataclass
class AttemptRecord:
    attempt_id: str
    episode_id: str
    kind: str
    reason: str
    created_at: datetime
    previous_attempt_id: str | None = None


@dataclass
class EntryRetryState:
    phase: EntryRetryPhase = EntryRetryPhase.ELIGIBLE
    episode_id: str | None = None
    attempts: int = 0
    last_attempt_at: datetime | None = None
    last_attempt_id: str | None = None
    last_fail_reason: str | None = None
    last_book_fingerprint: str | None = None
    signal_direction: str | None = None


@dataclass
class ExitRetryState:
    phase: ExitRetryPhase = ExitRetryPhase.IDLE
    attempts: int = 0
    last_attempt_at: datetime | None = None
    last_attempt_id: str | None = None
    last_fail_reason: str | None = None
    urgency: str = "NORMAL"
    reason_code: str | None = None
    position_episode_id: str | None = None


@dataclass
class RetryController:
    config: RetryConfig = field(default_factory=RetryConfig)
    entry: EntryRetryState = field(default_factory=EntryRetryState)
    exit: ExitRetryState = field(default_factory=ExitRetryState)
    history: list[AttemptRecord] = field(default_factory=list)

    def reset_market(self) -> None:
        self.entry = EntryRetryState()
        self.exit = ExitRetryState()
        self.history.clear()

    def on_flat(self) -> None:
        """Position flat — reset exit retry; entry starts a new episode on next signal."""
        self.exit = ExitRetryState()
        if self.entry.phase is not EntryRetryPhase.ELIGIBLE:
            self.entry = EntryRetryState()

    def on_directional_transition(self, direction: str) -> None:
        """Meaningful signal episode change resets entry attempts."""
        if self.entry.signal_direction != direction:
            self.entry = EntryRetryState(signal_direction=direction, episode_id=str(uuid4()))

    # --- Entry ---

    def entry_allowed(
        self,
        *,
        now: datetime,
        book_fingerprint: str | None,
        timer_fired: bool = False,
    ) -> tuple[bool, str]:
        if self.entry.phase is EntryRetryPhase.CAP_REACHED:
            return False, "ENTRY_ATTEMPT_CAP"
        if self.entry.phase is EntryRetryPhase.ELIGIBLE:
            return True, "ELIGIBLE"
        if self.entry.phase is EntryRetryPhase.INTENT_PENDING:
            return False, "ENTRY_INTENT_PENDING"
        if self.entry.phase in {EntryRetryPhase.PLAN_FAILED, EntryRetryPhase.RETRY_WAIT}:
            if self.entry.last_attempt_at is None:
                return True, "RETRY_READY"
            cooled = now - self.entry.last_attempt_at >= self.config.entry_cooldown
            book_changed = (
                book_fingerprint is not None
                and book_fingerprint != self.entry.last_book_fingerprint
            )
            if cooled and (book_changed or timer_fired):
                return True, "RETRY_TRIGGER"
            if not cooled:
                return False, "ENTRY_COOLDOWN"
            return False, "ENTRY_WAIT_TRIGGER"
        return False, "ENTRY_NOT_ELIGIBLE"

    def note_entry_attempt(
        self,
        *,
        now: datetime,
        direction: str,
        book_fingerprint: str | None,
        reason: str,
    ) -> AttemptRecord:
        if self.entry.episode_id is None:
            self.entry.episode_id = str(uuid4())
        self.entry.signal_direction = direction
        self.entry.attempts += 1
        self.entry.last_attempt_at = now
        self.entry.last_book_fingerprint = book_fingerprint
        self.entry.phase = EntryRetryPhase.INTENT_PENDING
        attempt_id = str(uuid4())
        prev = self.entry.last_attempt_id
        self.entry.last_attempt_id = attempt_id
        rec = AttemptRecord(
            attempt_id=attempt_id,
            episode_id=self.entry.episode_id,
            kind="ENTER",
            reason=reason,
            created_at=now,
            previous_attempt_id=prev,
        )
        self.history.append(rec)
        return rec

    def note_entry_plan_failed(self, *, reason: str, now: datetime) -> None:
        self.entry.last_fail_reason = reason
        if self.entry.attempts >= self.config.entry_max_attempts:
            self.entry.phase = EntryRetryPhase.CAP_REACHED
        else:
            self.entry.phase = EntryRetryPhase.RETRY_WAIT
            self.entry.last_attempt_at = now

    def note_entry_risk_denied(self) -> None:
        # Risk denial does not consume attempt budget permanently — return to wait/eligible.
        if self.entry.phase is EntryRetryPhase.INTENT_PENDING:
            self.entry.phase = EntryRetryPhase.RETRY_WAIT

    def note_entry_submitted(self) -> None:
        self.entry.phase = EntryRetryPhase.INTENT_PENDING

    def note_entry_rejected_or_canceled_unfilled(self) -> None:
        if self.entry.attempts >= self.config.entry_max_attempts:
            self.entry.phase = EntryRetryPhase.CAP_REACHED
        else:
            self.entry.phase = EntryRetryPhase.RETRY_WAIT

    def note_entry_filled_active(self) -> None:
        # Position open — entry episode complete; no more entries until flat.
        self.entry.phase = EntryRetryPhase.CAP_REACHED

    # --- Exit ---

    def exit_outstanding(self) -> bool:
        return self.exit.phase in {
            ExitRetryPhase.EXIT_REQUESTED,
            ExitRetryPhase.EXIT_PENDING,
            ExitRetryPhase.EXIT_RETRY_WAIT,
            ExitRetryPhase.ESCALATED,
            ExitRetryPhase.MANUAL_INTERVENTION,
        }

    def exit_allowed(
        self,
        *,
        now: datetime,
        escalate: bool = False,
        timer_fired: bool = False,
        book_fingerprint: str | None = None,
    ) -> tuple[bool, str]:
        if self.exit.phase is ExitRetryPhase.MANUAL_INTERVENTION:
            return False, "MANUAL_INTERVENTION"
        if self.exit.phase is ExitRetryPhase.IDLE:
            return True, "NEW_EXIT"
        if self.exit.phase is ExitRetryPhase.EXIT_PENDING:
            if escalate and self.exit.urgency != "URGENT":
                return True, "ESCALATE_EXIT"
            return False, "EXIT_PENDING"
        if self.exit.phase is ExitRetryPhase.EXIT_REQUESTED:
            if escalate and self.exit.urgency != "URGENT":
                return True, "ESCALATE_EXIT"
            return False, "EXIT_REQUESTED"
        if self.exit.phase in {ExitRetryPhase.EXIT_RETRY_WAIT, ExitRetryPhase.ESCALATED}:
            if escalate and self.exit.urgency != "URGENT":
                return True, "ESCALATE_EXIT"
            if self.exit.last_attempt_at is None:
                return True, "EXIT_RETRY_READY"
            cooled = now - self.exit.last_attempt_at >= self.config.exit_cooldown
            if not cooled:
                return False, "EXIT_COOLDOWN"
            # Prefer timer/book change; allow cooldown-alone as bounded recovery path.
            if timer_fired or book_fingerprint is not None or cooled:
                return True, "EXIT_RETRY_TRIGGER"
            return False, "EXIT_WAIT_TRIGGER"
        return False, "EXIT_NOT_ELIGIBLE"

    def note_exit_request(
        self,
        *,
        now: datetime,
        reason: str,
        urgency: str = "NORMAL",
        escalate: bool = False,
    ) -> AttemptRecord:
        if self.exit.position_episode_id is None:
            self.exit.position_episode_id = str(uuid4())
        if escalate or urgency == "URGENT":
            self.exit.urgency = "URGENT"
            self.exit.phase = ExitRetryPhase.ESCALATED
        else:
            self.exit.phase = ExitRetryPhase.EXIT_REQUESTED
        self.exit.reason_code = reason
        self.exit.attempts += 1
        self.exit.last_attempt_at = now
        attempt_id = str(uuid4())
        prev = self.exit.last_attempt_id
        self.exit.last_attempt_id = attempt_id
        rec = AttemptRecord(
            attempt_id=attempt_id,
            episode_id=self.exit.position_episode_id,
            kind="EXIT",
            reason=reason,
            created_at=now,
            previous_attempt_id=prev,
        )
        self.history.append(rec)
        return rec

    def note_exit_submitted(self) -> None:
        self.exit.phase = ExitRetryPhase.EXIT_PENDING

    def note_exit_plan_failed(self, *, reason: str, now: datetime) -> None:
        self.exit.last_fail_reason = reason
        self.exit.last_attempt_at = now
        if self.exit.urgency == "URGENT" and self.exit.attempts >= self.config.exit_max_normal_retries:
            self.exit.phase = ExitRetryPhase.MANUAL_INTERVENTION
            return
        if self.exit.attempts >= self.config.exit_escalate_after:
            self.exit.urgency = "URGENT"
            self.exit.phase = ExitRetryPhase.ESCALATED
        else:
            self.exit.phase = ExitRetryPhase.EXIT_RETRY_WAIT

    def note_exit_flat(self) -> None:
        self.exit = ExitRetryState()

    def snapshot(self) -> dict:
        return {
            "entry_phase": self.entry.phase.value,
            "entry_attempts": self.entry.attempts,
            "entry_episode_id": self.entry.episode_id,
            "entry_last_fail": self.entry.last_fail_reason,
            "exit_phase": self.exit.phase.value,
            "exit_attempts": self.exit.attempts,
            "exit_urgency": self.exit.urgency,
            "exit_last_fail": self.exit.last_fail_reason,
            "exit_position_episode_id": self.exit.position_episode_id,
        }

    def restore(self, data: dict) -> None:
        self.entry.phase = EntryRetryPhase(data.get("entry_phase", "ELIGIBLE"))
        self.entry.attempts = int(data.get("entry_attempts") or 0)
        self.entry.episode_id = data.get("entry_episode_id")
        self.entry.last_fail_reason = data.get("entry_last_fail")
        self.exit.phase = ExitRetryPhase(data.get("exit_phase", "IDLE"))
        self.exit.attempts = int(data.get("exit_attempts") or 0)
        self.exit.urgency = str(data.get("exit_urgency") or "NORMAL")
        self.exit.last_fail_reason = data.get("exit_last_fail")
        self.exit.position_episode_id = data.get("exit_position_episode_id")
