"""One exit owner per position: admission and precedence of competing exit requests.

Several sources can want to sell the same inventory: the host's mandatory close, a
protective stop (SL / trailing), the strategy's thesis stop, a take-profit and the
strategy's economic exit. Only one SELL may be in flight at a time and every retry is
sized from confirmed quantities by the lifecycle, never from the first request.
A committed hold is never sold by any of them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Lower value wins when several exits are requested at the same instant.
EXIT_PRECEDENCE: dict[str, int] = {
    "MANDATORY_FLATTEN": 0,
    "PROTECTION_SL": 1,
    "PROTECTION_TRAIL": 1,
    "THESIS_STOP": 2,
    "PROTECTION_TP": 3,
    "VALUE_EXIT": 3,
}
_DEFAULT_PRECEDENCE = 4


@dataclass(frozen=True)
class ExitAdmission:
    admitted: bool
    reason: str


def precedence(reason_code: str) -> int:
    return EXIT_PRECEDENCE.get(reason_code, _DEFAULT_PRECEDENCE)


def admit_exit(*, in_flight_reason: str | None, committed_hold: bool) -> ExitAdmission:
    if committed_hold:
        return ExitAdmission(False, "HOLD_COMMITTED_NEVER_SOLD")
    if in_flight_reason:
        return ExitAdmission(False, f"EXIT_ALREADY_IN_FLIGHT:{in_flight_reason}")
    return ExitAdmission(True, "ADMITTED")


def pick_exit(candidates: Iterable[Any]) -> Any | None:
    """The highest-precedence exit intent among simultaneous requests (stable order)."""
    ranked = sorted(
        enumerate(candidates), key=lambda item: (precedence(item[1].reason_code), item[0])
    )
    return ranked[0][1] if ranked else None
