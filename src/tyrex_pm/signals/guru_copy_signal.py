from __future__ import annotations

from tyrex_pm.core.models import GuruTradeSignal
from tyrex_pm.signals.base import GuruCopySignal


def to_copy_signal(t: GuruTradeSignal) -> GuruCopySignal:
    return GuruCopySignal(trade=t)
