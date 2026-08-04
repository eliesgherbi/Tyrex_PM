"""Immutable market binding identity for book feeds (BS-1).

``binding_id`` is permanent for a discovered market+token map.
``role_epoch`` changes on PREPARED↔ACTIVE promotion only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import InstrumentId, TokenId
from tyrex_pm.domain.polymarket.discovery_binding import (
    DiscoveredMarketBinding,
    DiscoverySessionRole,
)


class BindingLifecycleRole(str, Enum):
    ACTIVE = "active"
    PREPARED_NEXT = "prepared_next"
    EXPIRED = "expired"


def make_binding_id(*, window_slug: str, condition_id: str) -> str:
    """Stable permanent identity for one market window + condition."""
    cid = str(condition_id).strip().lower()
    slug = str(window_slug).strip()
    if not slug or not cid:
        raise ValueError("window_slug and condition_id required for binding_id")
    return f"{slug}|{cid}"


@dataclass(frozen=True, kw_only=True)
class MarketBindingRecord:
    """Persisted binding identity used by BookFeedSupervisor."""

    binding_id: str
    window_slug: str
    condition_id: str
    market_id: str
    up_token_id: str
    down_token_id: str
    event_start: datetime | None
    event_end: datetime | None
    role: BindingLifecycleRole
    role_epoch: int
    outcome_semantics: str
    resolved_at: datetime

    def __post_init__(self) -> None:
        if self.binding_id.strip() == "":
            raise ValueError("binding_id must be non-empty")
        if not self.up_token_id or not self.down_token_id:
            raise ValueError("up_token_id and down_token_id required")
        if self.up_token_id == self.down_token_id:
            raise ValueError("up_token_id and down_token_id must be distinct")
        if self.role_epoch < 0:
            raise ValueError("role_epoch must be >= 0")
        object.__setattr__(
            self, "resolved_at", require_utc(self.resolved_at, field_name="resolved_at")
        )
        if self.event_start is not None:
            object.__setattr__(
                self,
                "event_start",
                require_utc(self.event_start, field_name="event_start"),
            )
        if self.event_end is not None:
            object.__setattr__(
                self,
                "event_end",
                require_utc(self.event_end, field_name="event_end"),
            )

    @property
    def up_instrument_id(self) -> InstrumentId:
        return InstrumentId(self.up_token_id)

    @property
    def down_instrument_id(self) -> InstrumentId:
        return InstrumentId(self.down_token_id)

    @property
    def asset_ids(self) -> tuple[str, str]:
        return (self.up_token_id, self.down_token_id)

    def with_role(
        self, role: BindingLifecycleRole, *, role_epoch: int | None = None
    ) -> MarketBindingRecord:
        """Return same binding_id/tokens with updated lifecycle role only."""
        return MarketBindingRecord(
            binding_id=self.binding_id,
            window_slug=self.window_slug,
            condition_id=self.condition_id,
            market_id=self.market_id,
            up_token_id=self.up_token_id,
            down_token_id=self.down_token_id,
            event_start=self.event_start,
            event_end=self.event_end,
            role=role,
            role_epoch=self.role_epoch if role_epoch is None else role_epoch,
            outcome_semantics=self.outcome_semantics,
            resolved_at=self.resolved_at,
        )

    def owns_token(self, token_id: str | TokenId | InstrumentId) -> bool:
        value = token_id if isinstance(token_id, str) else token_id.value
        return value in self.asset_ids

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["role"] = self.role.value
        for key in ("event_start", "event_end", "resolved_at"):
            ts = getattr(self, key)
            raw[key] = None if ts is None else ts.isoformat()
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MarketBindingRecord:
        def _ts(value: Any) -> datetime | None:
            if value is None or value == "":
                return None
            text = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(text)

        return cls(
            binding_id=str(raw["binding_id"]),
            window_slug=str(raw["window_slug"]),
            condition_id=str(raw["condition_id"]),
            market_id=str(raw["market_id"]),
            up_token_id=str(raw["up_token_id"]),
            down_token_id=str(raw["down_token_id"]),
            event_start=_ts(raw.get("event_start")),
            event_end=_ts(raw.get("event_end")),
            role=BindingLifecycleRole(str(raw["role"])),
            role_epoch=int(raw["role_epoch"]),
            outcome_semantics=str(raw.get("outcome_semantics") or "UNKNOWN"),
            resolved_at=_ts(raw["resolved_at"]) or datetime.now(timezone.utc),
        )


def binding_record_from_discovery(
    binding: DiscoveredMarketBinding,
    *,
    role: BindingLifecycleRole | None = None,
    role_epoch: int = 0,
    resolved_at: datetime | None = None,
) -> MarketBindingRecord:
    """Build a persisted record from a validated discovery binding."""
    up, down = binding.require_up_down_tokens()
    market = binding.market
    condition = str(market.condition_id or market.market_id.value)
    if role is None:
        if binding.session_role is DiscoverySessionRole.ACTIVE:
            role = BindingLifecycleRole.ACTIVE
        elif binding.session_role is DiscoverySessionRole.PREPARED_NEXT:
            role = BindingLifecycleRole.PREPARED_NEXT
        else:
            role = BindingLifecycleRole.PREPARED_NEXT
    return MarketBindingRecord(
        binding_id=make_binding_id(
            window_slug=binding.window_slug, condition_id=condition
        ),
        window_slug=binding.window_slug,
        condition_id=condition,
        market_id=str(market.market_id.value),
        up_token_id=str(up.value),
        down_token_id=str(down.value),
        event_start=market.event_start,
        event_end=market.event_end,
        role=role,
        role_epoch=role_epoch,
        outcome_semantics=binding.outcome_semantics,
        resolved_at=resolved_at or datetime.now(timezone.utc),
    )


def persist_bindings(path: Path, records: list[MarketBindingRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "market_binding_records_v1",
        "bindings": [r.to_dict() for r in records],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_bindings(path: Path) -> list[MarketBindingRecord]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("bindings") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("bindings artifact must contain a list")
    return [MarketBindingRecord.from_dict(row) for row in rows]
