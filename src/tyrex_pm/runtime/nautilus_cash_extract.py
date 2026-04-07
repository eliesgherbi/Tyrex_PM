"""
Extract spendable USDC-style cash from Nautilus Polymarket ``Portfolio.account`` ``to_dict()`` output.

**Framework-submit canonical:** when present, ``free`` on USDC / USDC.e legs matches venue
order denial text more closely than raw CLOB integer strings before normalization.
"""

from __future__ import annotations

from typing import Any


def extract_nautilus_cash_free_usd(balances: Any) -> tuple[float | None, str]:
    """
    Best-effort sum of ``free`` fields on balance rows whose ``currency`` contains ``USDC``.

    Returns ``(usd_total_or_none, extract_note)``.
    """
    if balances is None:
        return None, "missing_balances"
    if not isinstance(balances, dict):
        return None, "balances_not_dict"

    found: list[float] = []
    stack: list[Any] = [balances]

    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            ccy = cur.get("currency")
            fr = cur.get("free")
            if ccy is not None and fr is not None:
                ccy_s = str(ccy).upper()
                if "USDC" in ccy_s:
                    try:
                        found.append(float(str(fr).strip()))
                    except ValueError:
                        pass
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)

    if not found:
        return None, "no_usdc_free_found"
    if len(found) == 1:
        return found[0], "single_usdc_free"
    return float(sum(found)), "multiple_usdc_free_summed"
