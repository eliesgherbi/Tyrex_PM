"""Immutable capital snapshot for risk, readiness (later), and reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class CapitalStateSource(str, Enum):
    """How the snapshot was composed (framework-first vs supplemental HTTP)."""

    ADAPTER_ACCOUNT = "adapter_account"
    EXPLICIT_REFRESH = "explicit_refresh"


@dataclass(frozen=True, slots=True)
class CapitalState:
    """
    Single Tyrex view of collateral / allowance for gates and observability.

    ``free_collateral_usd`` prefers Nautilus USDC ``free`` when extractable; otherwise
    normalized py-clob balance when a CLOB snapshot was merged in.
    """

    free_collateral_usd: float | None
    allowance_usd: float | None
    captured_at_utc: datetime
    source: CapitalStateSource
    stale_after_seconds: float
    ok: bool
    error: str | None
    account_present: bool
    venue: str
    nautilus_balances: dict[str, Any] | None
    nautilus_cash_free_usd: float | None
    nautilus_cash_extract_note: str
    py_clob_balance_usd: float | None
    py_clob_allowance_usd: float | None
    py_clob_balance_raw: str | None
    py_clob_allowance_raw: str | None
    py_clob_balance_parse_note: str
    py_clob_allowance_parse_note: str
    merged_clob: bool
