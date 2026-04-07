"""
Polymarket CLOB collateral balance/allowance → interpretable USD.

**API shape (Polymarket CLOB HTTP):** ``get_balance_allowance`` returns string fields.
Integer strings (no decimal point) are **USDC in 1e-6 atomic units** (on-chain style).
Strings with an explicit decimal point are treated as **human USD** (tests, tooling, or
mixed clients).

See production validation against Nautilus ``CashAccount`` free balance + venue DENIED text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ClobCollateralUsdParse:
    balance_usd: float | None
    allowance_usd: float | None
    balance_raw: str | None
    allowance_raw: str | None
    balance_parse_note: str
    allowance_parse_note: str


def _scalar_usd_from_clob_string(raw_val: Any, *, field: str) -> tuple[float | None, str, str | None]:
    """
    Return ``(usd, parse_note, raw_str)`` for one CLOB scalar.

    ``parse_note``: ``missing`` | ``empty`` | ``decimal_string`` |
    ``polymarket_atomic_usdc_6`` | ``unparseable``
    """
    if raw_val is None:
        return None, "missing", None
    raw_s = str(raw_val).strip()
    if not raw_s:
        return None, "empty", raw_s
    if "." in raw_s or "E" in raw_s.upper():
        try:
            return float(raw_s), "decimal_string", raw_s
        except ValueError:
            return None, "unparseable", raw_s
    try:
        atoms = int(raw_s, 10)
    except ValueError:
        try:
            return float(raw_s), "decimal_string", raw_s
        except ValueError:
            return None, "unparseable", raw_s
    # Integer string: Polymarket CLOB collateral uses 6-decimal fixed point.
    return atoms / 1_000_000.0, "polymarket_atomic_usdc_6", raw_s


def parse_clob_collateral_usd(raw: dict[str, Any]) -> ClobCollateralUsdParse:
    """Normalize ``balance`` / ``allowance`` from a py-clob ``get_balance_allowance`` dict."""
    b_u, b_note, b_raw = _scalar_usd_from_clob_string(raw.get("balance"), field="balance")
    a_u, a_note, a_raw = _scalar_usd_from_clob_string(raw.get("allowance"), field="allowance")
    return ClobCollateralUsdParse(
        balance_usd=b_u,
        allowance_usd=a_u,
        balance_raw=b_raw,
        allowance_raw=a_raw,
        balance_parse_note=b_note,
        allowance_parse_note=a_note,
    )
