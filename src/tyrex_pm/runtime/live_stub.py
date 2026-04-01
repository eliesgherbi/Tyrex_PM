"""
Live runtime stub (milestone v1.03).

Full `TradingNode` smoke requires credentials and network; keep import-light default.
Set `TYREX_LIVE_NODE_SMOKE=1` and Nautilus Polymarket env to attempt a short connect (optional).
"""

from __future__ import annotations

import os


def live_node_smoke_waiver_text() -> str:
    return (
        "TradingNode live smoke is deferred unless TYREX_LIVE_NODE_SMOKE=1. "
        "Operators should follow examples/live/polymarket in NautilusTrader after v1.00 auth. "
        "See Docs/Runbooks/live_stub_v1_03.md."
    )


def maybe_run_live_node_smoke() -> str:
    """
    Returns 'skipped' | 'ok' | error message.
    Does not submit orders.
    """
    if os.environ.get("TYREX_LIVE_NODE_SMOKE") != "1":
        return "skipped"
    try:
        from nautilus_trader.live.node import TradingNode
    except ImportError as exc:
        return f"import_error:{exc}"

    # Import-only gate: constructing a full Polymarket node requires factories + config objects.
    _ = TradingNode
    return "ok_import"
