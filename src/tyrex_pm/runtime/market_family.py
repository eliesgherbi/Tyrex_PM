"""Market family registry — discovery and window model, independent of strategy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tyrex_pm.runtime.config_error import ConfigError

_REGISTRY: dict[str, MarketFamily] = {}


@dataclass(frozen=True)
class MarketFamily:
    family_id: str
    window_duration_s: int
    resolve_active: Callable[..., Awaitable[Any]]
    prepare_next: Callable[..., Awaitable[Any]]


def register_market_family(family: MarketFamily) -> None:
    if not family.family_id.strip():
        raise ValueError("market family id must be non-empty")
    if family.window_duration_s <= 0:
        raise ValueError("window_duration_s must be > 0")
    existing = _REGISTRY.get(family.family_id)
    if existing is not None and existing is not family:
        raise ValueError(f"market family {family.family_id!r} is already registered")
    _REGISTRY[family.family_id] = family


def ensure_market_families_registered() -> None:
    if _REGISTRY:
        return
    from tyrex_pm.adapters.polymarket.discovery import (
        GammaMarketDiscovery,
        current_btc_updown_slug,
    )
    from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole

    async def resolve_active(discovery: GammaMarketDiscovery | None = None) -> Any:
        client = discovery or GammaMarketDiscovery()
        return await client.resolve_btc_5m_window(
            slug=current_btc_updown_slug(),
            session_role=DiscoverySessionRole.ACTIVE,
        )

    async def prepare_next(discovery: GammaMarketDiscovery | None = None) -> Any:
        client = discovery or GammaMarketDiscovery()
        return await client.prepare_next_btc_5m()

    register_market_family(
        MarketFamily(
            family_id="btc_updown_5m",
            window_duration_s=300,
            resolve_active=resolve_active,
            prepare_next=prepare_next,
        )
    )


def get_market_family(family_id: str) -> MarketFamily:
    ensure_market_families_registered()
    family = _REGISTRY.get(family_id)
    if family is None:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ConfigError(
            f"market family {family_id!r} is not registered; available: {available}"
        )
    return family


def registered_market_families() -> tuple[str, ...]:
    ensure_market_families_registered()
    return tuple(sorted(_REGISTRY))
