"""Multi-dimensional book health (sync ≠ connection ≠ liquidity ≠ depth)."""

from __future__ import annotations

from enum import Enum


class SyncHealth(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    SYNCING = "SYNCING"
    READY = "READY"
    STALE = "STALE"
    DESYNCED = "DESYNCED"


class ConnectionHealth(str, Enum):
    CONNECTING = "CONNECTING"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    CLOSED = "CLOSED"


class SideLiquidity(str, Enum):
    AVAILABLE = "AVAILABLE"
    EXPLICITLY_EMPTY = "EXPLICITLY_EMPTY"
    UNKNOWN = "UNKNOWN"


class FeedSyncPhase(str, Enum):
    """Per-BindingFeed synchronization barrier phases."""

    UNINITIALIZED = "UNINITIALIZED"
    WS_CONNECTING = "WS_CONNECTING"
    WS_BUFFERING = "WS_BUFFERING"
    SNAPSHOT_ACQUIRING = "SNAPSHOT_ACQUIRING"
    RECONCILING = "RECONCILING"
    READY = "READY"
    DESYNCED = "DESYNCED"
