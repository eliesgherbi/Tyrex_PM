"""Compose live adapters from an InputContract source-fact closure."""

from __future__ import annotations

from tyrex_pm.facts import ids as F
from tyrex_pm.facts.contract import InputContract

# Catalog entries that exist as live adapters today. L2/perp/funding are
# registered facts; adapters are added when a strategy that needs them is built.
LIVE_ADAPTERS: frozenset[str] = frozenset(
    {
        F.CLOCK_SYNC,
        F.CHAINLINK_TWAP,
        F.BINANCE_SPOT_TRADES,
        F.POLYMARKET_BOOKS,
        F.POLYMARKET_MARKET_META,
        F.ACCOUNT_SNAPSHOT,
    }
)

UNIMPLEMENTED_ADAPTERS: frozenset[str] = frozenset(
    {
        F.BINANCE_SPOT_L2,
        F.BINANCE_PERP_TRADES,
        F.BINANCE_PERP_L2,
        F.BINANCE_PERP_FUNDING,
    }
)


def adapters_to_start(contract: InputContract) -> frozenset[str]:
    """Source facts the host should start for this contract."""
    return contract.source_facts()


def missing_live_adapters(contract: InputContract) -> frozenset[str]:
    """Facts in the closure that have no live adapter yet (q-edge L2/perp)."""
    return contract.source_facts() & UNIMPLEMENTED_ADAPTERS


def starts_adapter(contract: InputContract, fact: str) -> bool:
    return fact in contract.source_facts()
