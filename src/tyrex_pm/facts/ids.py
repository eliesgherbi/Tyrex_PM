"""Stable fact identifiers for strategy input contracts.

Source facts are produced by adapters. Derived facts are produced from other
facts. Strategies name facts; the runtime resolves the transitive connector set.
"""

from __future__ import annotations

from typing import Final

# Source facts
CLOCK_SYNC: Final = "clock.sync"
ACCOUNT_SNAPSHOT: Final = "account.snapshot"
POLYMARKET_BOOKS: Final = "polymarket.books"
POLYMARKET_MARKET_META: Final = "polymarket.market_meta"
CHAINLINK_TWAP: Final = "chainlink.twap"
BINANCE_SPOT_TRADES: Final = "binance.spot.trades"
BINANCE_SPOT_L2: Final = "binance.spot.l2"
BINANCE_PERP_TRADES: Final = "binance.perp.trades"
BINANCE_PERP_L2: Final = "binance.perp.l2"
BINANCE_PERP_FUNDING: Final = "binance.perp.funding"

# Derived facts (producers declare upstreams in producers.py)
BINANCE_SPOT_MID: Final = "binance.spot.mid"
BINANCE_PERP_MID: Final = "binance.perp.mid"
PTB_SEALED: Final = "ptb.sealed"
REFERENCE_ALIGNED: Final = "reference.aligned"
TAU: Final = "tau"

SOURCE_FACTS: frozenset[str] = frozenset(
    {
        CLOCK_SYNC,
        ACCOUNT_SNAPSHOT,
        POLYMARKET_BOOKS,
        POLYMARKET_MARKET_META,
        CHAINLINK_TWAP,
        BINANCE_SPOT_TRADES,
        BINANCE_SPOT_L2,
        BINANCE_PERP_TRADES,
        BINANCE_PERP_L2,
        BINANCE_PERP_FUNDING,
    }
)

DERIVED_FACTS: frozenset[str] = frozenset(
    {
        BINANCE_SPOT_MID,
        BINANCE_PERP_MID,
        PTB_SEALED,
        REFERENCE_ALIGNED,
        TAU,
    }
)

KNOWN_FACTS: frozenset[str] = SOURCE_FACTS | DERIVED_FACTS
