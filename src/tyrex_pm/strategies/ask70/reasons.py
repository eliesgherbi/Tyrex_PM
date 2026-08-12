"""ask70 reason codes."""

from __future__ import annotations

from enum import Enum


class Ask70Reason(str, Enum):
    ENTRY_ASK_HIT = "ENTRY_ASK_HIT"
    HOLD_POSITION = "HOLD_POSITION"
    WAIT_NO_ASK_HIT = "WAIT_NO_ASK_HIT"
    BLOCKED = "BLOCKED"
    SKIP_NO_BOOK = "SKIP_NO_BOOK"
