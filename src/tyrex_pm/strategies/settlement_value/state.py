"""Private per-window episode state. Not monetary authority: positions come from context.

One *window* can hold several *episodes* (one intended position lifecycle each) and each
episode several *attempts* (one authorized entry intent each):

    IDLE ──enter──► PENDING ──fill──────────► POSITION ──hold──► HOLD_COMMITTED
      ▲               │  ├─not sent/zero fill─► REARMABLE ─(retry)─► PENDING
      │               │  └─unknown───────────► UNCERTAIN ─(resolved)─► …
      │               │                         POSITION ──exit──► EXITING ──flat──► IDLE
      │               └─(no retry / budget)───► CLOSED  (no more entries this window)
      └──────────────────── re-entry (new episode) from IDLE after a complete exit

PENDING starts when the intent is created, never later: a queued, signing or in-flight
order already blocks another BUY. With retry and re-entry disabled the machine
reproduces the original single-attempt behaviour exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any


class EpisodePhase(str, Enum):
    IDLE = "IDLE"
    PENDING = "PENDING"
    UNCERTAIN = "UNCERTAIN"
    REARMABLE = "REARMABLE"
    POSITION = "POSITION"
    EXITING = "EXITING"
    HOLD_COMMITTED = "HOLD_COMMITTED"
    CLOSED = "CLOSED"


P = EpisodePhase


@dataclass
class EpisodeState:
    window_id: str
    phase: EpisodePhase = P.IDLE
    # Episodes started in this window (1-based once the first attempt is created).
    episode_seq: int = 0
    # Attempts in the current episode, and window-wide counters across episodes.
    attempt_seq: int = 0
    window_attempts: int = 0
    episode_posts: int = 0
    window_posts: int = 0
    episode_prepare_failures: int = 0
    consecutive_service_failures: int = 0
    # Episodes that acquired confirmed exposure (the re-entry "trade" count).
    filled_episodes: int = 0
    episode_filled: bool = False
    window_debit: str = "0"
    episode_started_at: str | None = None
    next_attempt_not_before: str | None = None
    entry_side: str | None = None
    entry_intent_id: str | None = None
    last_outcome: str | None = None
    last_outcome_reason: str | None = None
    closed_reason: str | None = None
    hold_requested: bool = False
    exit_intent_id: str | None = None
    exit_attempts: int = 0
    last_exit_reason: str | None = None
    last_close_at: str | None = None
    last_close_reason: str | None = None
    last_closed_side: str | None = None
    # signal_reset re-arm: the previously traded side must first become ineligible.
    rearm_reset_seen: bool = False
    decision_epoch: int = 0

    # -- compatibility --------------------------------------------------------------

    @property
    def entry_emitted(self) -> bool:
        """True once the window's current episode has spent an attempt (legacy view)."""
        return self.phase is not P.IDLE

    @property
    def attempt_id(self) -> str:
        return f"e{self.episode_seq}a{self.attempt_seq}"

    def as_slice(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.value if isinstance(value, Enum) else value
        out["entry_emitted"] = self.entry_emitted
        return out

    def restore(self, data: dict[str, Any]) -> None:
        if data.get("window_id") not in (None, self.window_id):
            return
        names = {f.name for f in fields(self)}
        for key, value in data.items():
            if key not in names or key == "window_id":
                continue
            if key == "phase":
                value = EpisodePhase(value)
            setattr(self, key, value)
        if "phase" not in data and data.get("entry_emitted"):
            # Slice written before episodes existed: the single attempt was spent.
            self.phase = P.POSITION if data.get("hold_requested") else P.CLOSED
            self.episode_seq = self.attempt_seq = self.window_attempts = 1
