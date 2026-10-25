"""Recurring up/down window market families, defined in configuration.

A family is one kind of recurring Polymarket market: an asset, a window length and a slug
``<prefix>-<window start epoch>``, resolved by a price stream. Discovery, the trading
runtime, the data warehouse and replay all read the window length, slug and reference
symbols from here, so a new family (BTC 15-minute, for example) is a file in
``config/markets/``, not code.

Only BTC reference feeds are wired into the collector and the runtime today; a family
for another asset is refused (``supported``) rather than silently recording BTC data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

MARKETS = Path(__file__).resolve().parents[4] / "config" / "markets"
DEFAULT_FAMILY = "btc_updown_5m"
SUPPORTED_ASSETS = frozenset({"BTC"})


@dataclass(frozen=True)
class WindowFamily:
    family_id: str
    asset: str
    slug_prefix: str
    window_duration_s: int
    # Price stream the markets resolve against (checked against Gamma's resolutionSource).
    resolution_stream: str
    binance_symbol: str
    outcomes: tuple[str, str] = ("UP", "DOWN")
    # Discovered by live runs and recordings by default. A disabled family is valid
    # (replay of its recordings, explicit runs) but never chosen implicitly.
    enabled: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", self.family_id):
            raise ValueError(f"invalid family id {self.family_id!r}")
        if not re.fullmatch(r"[a-z0-9-]+", self.slug_prefix):
            raise ValueError(f"invalid slug prefix {self.slug_prefix!r}")
        if self.window_duration_s <= 0 or 86400 % self.window_duration_s:
            raise ValueError("window_duration_s must divide a day")
        if tuple(o.upper() for o in self.outcomes) != ("UP", "DOWN"):
            raise ValueError("recurring window families are UP/DOWN markets")

    @property
    def supported(self) -> bool:
        """Whether the collector and the runtime have reference feeds for this asset."""
        return self.asset in SUPPORTED_ASSETS

    @property
    def slug_regex(self) -> re.Pattern[str]:
        return re.compile(rf"^{re.escape(self.slug_prefix)}-(\d{{10,}})$", re.IGNORECASE)

    def slug(self, start_epoch_s: int) -> str:
        return f"{self.slug_prefix}-{start_epoch_s}"

    def window_start(self, epoch_s: int) -> int:
        """Start (epoch seconds) of the window containing ``epoch_s``."""
        return epoch_s // self.window_duration_s * self.window_duration_s

    def current_slug(self, now: datetime | None = None) -> str:
        return self.slug(self.window_start(_epoch(now)))

    def next_slug(self, now: datetime | None = None) -> str:
        return self.slug(self.window_start(_epoch(now)) + self.window_duration_s)

    def seconds_to_next_window(self, now: datetime | None = None) -> float:
        epoch = _epoch(now)
        return float(self.window_start(epoch) + self.window_duration_s - epoch)

    def slug_epoch(self, slug: str) -> int | None:
        match = self.slug_regex.match(slug.strip())
        return None if match is None else int(match.group(1))

    def window_bounds(self, slug: str) -> tuple[datetime, datetime]:
        epoch = self.slug_epoch(slug)
        if epoch is None:
            raise ValueError(f"not a {self.slug_prefix} slug: {slug!r}")
        start = datetime.fromtimestamp(epoch, tz=timezone.utc)
        return start, start + timedelta(seconds=self.window_duration_s)


def _epoch(now: datetime | None) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return int(now.timestamp())


_FAMILIES: dict[str, WindowFamily] | None = None


def _load(folder: Path) -> dict[str, WindowFamily]:
    families = {}
    for path in sorted(folder.glob("*.yaml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if body.get("schema") != "window_family_v1":
            raise ValueError(f"{path.name}: schema must be window_family_v1")
        family = WindowFamily(
            family_id=str(body["family_id"]),
            asset=str(body["asset"]).upper(),
            slug_prefix=str(body["slug_prefix"]),
            window_duration_s=int(body["window_duration_s"]),
            resolution_stream=str(body["resolution_stream"]),
            binance_symbol=str(body["binance_symbol"]).upper(),
            outcomes=tuple(body.get("outcomes") or ("UP", "DOWN")),
            enabled=bool(body.get("enabled", True)),
        )
        if family.family_id != path.stem:
            raise ValueError(f"{path.name}: family_id must equal the file name")
        families[family.family_id] = family
    if DEFAULT_FAMILY not in families:
        raise ValueError(f"{folder}: the {DEFAULT_FAMILY} family definition is missing")
    return families


def window_families(folder: Path | None = None) -> dict[str, WindowFamily]:
    global _FAMILIES
    if folder is not None:
        return _load(folder)
    if _FAMILIES is None:
        _FAMILIES = _load(MARKETS)
    return _FAMILIES


def window_family(family_id: str = DEFAULT_FAMILY) -> WindowFamily:
    families = window_families()
    if family_id not in families:
        known = ", ".join(sorted(families))
        raise ValueError(f"unknown market family {family_id!r}; known: {known}")
    return families[family_id]


def family_for_slug(slug: str) -> WindowFamily | None:
    """The family whose slug pattern matches, or None."""
    for family in window_families().values():
        if family.slug_epoch(slug) is not None:
            return family
    return None


def family_for_duration(asset: str, duration_s: int) -> WindowFamily | None:
    for family in window_families().values():
        if family.asset == asset.upper() and family.window_duration_s == int(duration_s):
            return family
    return None
