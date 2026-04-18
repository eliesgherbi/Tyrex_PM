from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tyrex_pm.core.ids import TokenId


@dataclass
class MarketStore:
    best_bid: dict[TokenId, Decimal] = field(default_factory=dict)
    best_ask: dict[TokenId, Decimal] = field(default_factory=dict)
