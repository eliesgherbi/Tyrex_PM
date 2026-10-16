"""Execution outcome contract delivered by a host (replay or live) to a strategy driver.

The strategy never reads venue state. After every entry or exit intent the host tells
the driver, through these immutable records, what is *known* about it:

* ``NOT_SENT``  — authoritatively never reached the venue (host/gate/preparation refusal);
* ``ZERO_FILL`` — reached the venue and is authoritatively terminal with nothing filled;
* ``PARTIAL`` / ``FILLED`` — confirmed exposure (partial fills are positions, not retries);
* ``UNCERTAIN`` — acceptance or fills unknown; blocks another order until reconciled.

``retry_class`` comes from the shared reason table (``execution.outcome_reasons``) so the
same reason means the same thing in replay and live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class RetryClass(str, Enum):
    # A transient pre-send refusal: retry once the blocker is gone (still bounded).
    RETRY_WHEN_CLEARED = "RETRY_WHEN_CLEARED"
    # Reached the venue, nothing filled: a fresh, re-checked attempt may follow.
    RETRY_AFTER_ZERO_FILL = "RETRY_AFTER_ZERO_FILL"
    # Outcome unknown: no further BUY until authoritative evidence resolves it.
    BLOCK_UNTIL_RECONCILED = "BLOCK_UNTIL_RECONCILED"
    # The episode cannot continue in this window (cutoff, window budget, identity).
    END_EPISODE = "END_EPISODE"
    # Structural/account problem: stop entries with a visible reason; never fast-retry.
    PAUSE_ENTRIES = "PAUSE_ENTRIES"
    # Not a failure (fill, position); kept for completeness of the table.
    NONE = "NONE"


def is_retryable(retry_class: RetryClass) -> bool:
    """Whether an outcome of this class may be followed by a fresh, bounded attempt."""
    return retry_class in (RetryClass.RETRY_WHEN_CLEARED, RetryClass.RETRY_AFTER_ZERO_FILL)


class EntryOutcomeKind(str, Enum):
    NOT_SENT = "NOT_SENT"
    ZERO_FILL = "ZERO_FILL"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    UNCERTAIN = "UNCERTAIN"


class ExitOutcomeKind(str, Enum):
    NOT_SENT = "NOT_SENT"
    NO_FILL = "NO_FILL"
    PARTIAL = "PARTIAL"
    FLAT = "FLAT"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class EntryOutcome:
    kind: EntryOutcomeKind
    intent_id: str
    at: datetime
    reason: str | None = None
    retry_class: RetryClass = RetryClass.NONE
    confirmed_shares: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    # Whether this attempt reached a POST (venue mutation attempt) and/or failed during
    # local preparation; counted separately against their own ceilings.
    posted: bool = False
    prepare_failed: bool = False
    # True when this outcome resolves an earlier UNCERTAIN one for the same intent.
    resolved_from_uncertain: bool = False

    @property
    def exposure(self) -> bool:
        return self.kind in (EntryOutcomeKind.PARTIAL, EntryOutcomeKind.FILLED)


@dataclass(frozen=True)
class ExitOutcome:
    kind: ExitOutcomeKind
    intent_id: str
    at: datetime
    reason: str | None = None
    remaining_shares: Decimal = Decimal("0")
    sold_shares: Decimal = Decimal("0")
    proceeds: Decimal = Decimal("0")
    posted: bool = False
