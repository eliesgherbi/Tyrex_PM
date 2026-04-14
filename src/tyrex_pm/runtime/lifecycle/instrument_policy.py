"""§8.2.5 static instrument cache presence — no venue polling."""

from __future__ import annotations

from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.model.identifiers import InstrumentId


def static_instruments_in_cache(cache, instrument_id_strs: tuple[str, ...]) -> tuple[bool, str | None]:
    """
    Return ``(True, None)`` when policy passes.

    Empty ``instrument_id_strs`` (dynamic-only live) → **waived** (true); activation still
    occurs at submit time per execution truth alignment doc.
    """
    if not instrument_id_strs:
        return True, None
    for s in instrument_id_strs:
        try:
            iid = InstrumentId.from_str(s)
            _ = get_polymarket_token_id(iid)
        except ValueError:
            return False, "startup_instrument_invalid_config_id"
        if cache.instrument(iid) is None:
            return False, "startup_instrument_missing_from_cache"
    return True, None
