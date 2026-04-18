"""Canonical identifiers — token_id is the strategy/risk/OMS key."""

from __future__ import annotations

from typing import NewType

# Outcome / CLOB token id (Polymarket asset id for the tradable leg)
TokenId = NewType("TokenId", str)
RunId = NewType("RunId", str)
IntentId = NewType("IntentId", str)
ClientOrderId = NewType("ClientOrderId", str)
VenueOrderId = NewType("VenueOrderId", str)
SyncId = NewType("SyncId", str)
