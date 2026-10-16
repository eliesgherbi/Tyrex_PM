"""Private per-window episode state. Not monetary authority: positions come from context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EpisodeState:
    window_id: str
    # Set when an entry intent is emitted: the order counts as pending from then on,
    # so no second entry can be produced in this window (attempt budget = 1).
    entry_emitted: bool = False
    entry_side: str | None = None
    entry_intent_id: str | None = None
    hold_requested: bool = False
    decision_epoch: int = 0

    def as_slice(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "entry_emitted": self.entry_emitted,
            "entry_side": self.entry_side,
            "entry_intent_id": self.entry_intent_id,
            "hold_requested": self.hold_requested,
            "decision_epoch": self.decision_epoch,
        }

    def restore(self, data: dict[str, Any]) -> None:
        if data.get("window_id") not in (None, self.window_id):
            return
        self.entry_emitted = bool(data.get("entry_emitted", False))
        self.entry_side = data.get("entry_side")
        self.entry_intent_id = data.get("entry_intent_id")
        self.hold_requested = bool(data.get("hold_requested", False))
        self.decision_epoch = int(data.get("decision_epoch") or 0)
