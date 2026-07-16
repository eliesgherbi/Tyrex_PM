from __future__ import annotations

from enum import Enum, auto


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStyle(str, Enum):
    GTC = "GTC"
    FOK = "FOK"
    FAK = "FAK"


class ExecutionMode(str, Enum):
    SHADOW = "shadow"
    LIVE = "live"


class HealthState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"


class EventSource(str, Enum):
    MARKET_WS = "market_ws"
    USER_WS = "user_ws"
    REST = "rest"
    INTERNAL = "internal"
    REPLAY = "replay"
