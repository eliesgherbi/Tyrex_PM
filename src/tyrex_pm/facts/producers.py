"""Upstream requirements for derived facts (transitive closure)."""

from __future__ import annotations

from tyrex_pm.facts import ids as F

# A derived fact pulls these facts (which may themselves be derived).
DERIVED_UPSTREAMS: dict[str, frozenset[str]] = {
    F.BINANCE_SPOT_MID: frozenset({F.BINANCE_SPOT_L2}),
    F.BINANCE_PERP_MID: frozenset({F.BINANCE_PERP_L2}),
    F.PTB_SEALED: frozenset(
        {F.CHAINLINK_TWAP, F.CLOCK_SYNC, F.POLYMARKET_MARKET_META, F.BINANCE_SPOT_TRADES}
    ),
    F.REFERENCE_ALIGNED: frozenset({F.BINANCE_SPOT_TRADES, F.CHAINLINK_TWAP}),
    F.TAU: frozenset({F.POLYMARKET_MARKET_META, F.CLOCK_SYNC}),
}


def expand_facts(facts: frozenset[str]) -> frozenset[str]:
    """Close ``facts`` under derived-upstream edges."""
    pending = set(facts)
    seen: set[str] = set()
    while pending:
        fact = pending.pop()
        if fact in seen:
            continue
        seen.add(fact)
        pending.update(DERIVED_UPSTREAMS.get(fact, ()))
    return frozenset(seen)
