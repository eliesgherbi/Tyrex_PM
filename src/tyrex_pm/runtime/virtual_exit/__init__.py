"""Tyrex-owned virtual take-profit / stop-loss (Polymarket; no native OCO)."""

from __future__ import annotations

from tyrex_pm.runtime.virtual_exit.lot import LOT_TERMINAL_STATES, ProtectedLot
from tyrex_pm.runtime.virtual_exit.manager import VirtualExitManager
from tyrex_pm.runtime.virtual_exit.store import VirtualExitStore

__all__ = [
    "LOT_TERMINAL_STATES",
    "ProtectedLot",
    "VirtualExitManager",
    "VirtualExitStore",
]
