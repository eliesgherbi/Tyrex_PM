"""
**Step 5 self-bootstrap:** preload ``Cache`` from recent guru Data API ``/activity`` rows.

**Repo-confirmed:** uses the same ``asset`` field as guru parse
(:func:`~tyrex_pm.data.guru_parse.trade_row_to_signal`).
**Not** a second source of trading truth — only opportunistic resolution into Nautilus ``Cache``.
"""

from __future__ import annotations

import logging

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.data.data_api_client import PolymarketDataApiClient
from tyrex_pm.runtime.guru_instrument_dynamic import GuruInstrumentDynamicController

_LOG = logging.getLogger(__name__)


def warm_polymarket_cache_from_guru_activity(
    controller: GuruInstrumentDynamicController,
    *,
    guru_wallet_address: str,
    runtime: RuntimeSettings,
) -> int:
    """
    Resolve up to ``runtime.polymarket_startup_token_warmup_max`` distinct outcome tokens from
    the latest guru TRADE activity. Returns count successfully present in ``Cache`` afterward.
    """
    cap = int(runtime.polymarket_startup_token_warmup_max)
    if cap <= 0:
        return 0

    client = PolymarketDataApiClient(runtime.data_api_base_url, log_backoff=None)
    limit = max(1, min(500, int(runtime.guru_activity_limit)))

    try:
        rows = client.get_user_trade_activity(
            user=guru_wallet_address,
            limit=limit,
            offset=0,
            sort_direction="DESC",
            sort_by="TIMESTAMP",
        )
    except Exception:
        _LOG.warning(
            "event=guru_cache_warmup_failed component=guru_cache_warmup detail=data_api",
            exc_info=True,
        )
        return 0

    seen: set[str] = set()
    warmed = 0
    for row in rows:
        if warmed >= cap:
            break
        asset = row.get("asset")
        if asset is None:
            continue
        tid = str(asset).strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        inst, _fail = controller.resolve_and_activate(tid)
        if inst is not None:
            warmed += 1

    _LOG.info(
        "event=guru_cache_warmup_done component=guru_cache_warmup distinct=%s warmed=%s cap=%s",
        len(seen),
        warmed,
        cap,
    )
    return warmed
