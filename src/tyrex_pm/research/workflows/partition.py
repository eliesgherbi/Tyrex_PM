"""Markets a caller explicitly reserves for a request.

A window set has no development/holdout partition (research_IHM_enhancment.md, 6.1).
The notebook-era split file is history and gates nothing here. Only Tune will introduce
a holdout, as one of its own stated parameters.
"""

from __future__ import annotations


def reserved_market_ids(request: dict) -> set[str]:
    return {str(item) for item in request.get("reserved_market_ids") or []}
