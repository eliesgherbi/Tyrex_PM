"""Framework intent duplicate guard (single owner — used by risk).

Semantic key (not random intent_id) prevents reprocessing of equivalent
requests from retries, re-entry, or replayed inputs.

Strategy transition logic separately prevents spam on identical market signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tyrex_pm.core.clock import require_utc


@dataclass(frozen=True, kw_only=True)
class DedupRecord:
    semantic_key: str
    first_intent_id: str
    first_seen_at: datetime


class IntentDedupRegistry:
    def __init__(self, *, lifetime: timedelta) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("dedup lifetime must be > 0")
        self._lifetime = lifetime
        self._by_key: dict[str, DedupRecord] = {}

    def reset_market(self) -> None:
        self._by_key.clear()

    def purge_expired(self, now: datetime) -> None:
        now = require_utc(now, field_name="now")
        expired = [
            key
            for key, rec in self._by_key.items()
            if now - rec.first_seen_at > self._lifetime
        ]
        for key in expired:
            del self._by_key[key]

    def lookup(self, semantic_key: str, *, now: datetime) -> DedupRecord | None:
        self.purge_expired(now)
        return self._by_key.get(semantic_key)

    def register(self, semantic_key: str, *, intent_id: str, now: datetime) -> DedupRecord:
        self.purge_expired(now)
        existing = self._by_key.get(semantic_key)
        if existing is not None:
            return existing
        rec = DedupRecord(
            semantic_key=semantic_key,
            first_intent_id=intent_id,
            first_seen_at=require_utc(now, field_name="now"),
        )
        self._by_key[semantic_key] = rec
        return rec
