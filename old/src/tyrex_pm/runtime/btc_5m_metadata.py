"""Generic BTC 5m market metadata resolution for Z-Gap and other strategies."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from tyrex_pm.runtime.config import AppConfig, ConfigError, ZGapStrategyConfig
from tyrex_pm.venue.polymarket.event_metadata import (
    EventMetadataError,
    PairedBinaryEventMetadata,
    resolve_paired_binary_event_metadata,
    resolve_paired_binary_from_tokens,
)

_PLACEHOLDER_MARKERS = (
    "<required",
    "<required_",
    "placeholder",
    "todo",
    "fixme",
    "changeme",
    "xxx",
    "yyyymmdd",
)
_SHADOW_MARKET_IDS = frozenset({"shadow_test_market", "test_market", "fixture_market"})
_BTC_MARKET_RE = re.compile(r"^btc_5m_\d{8}_\d{4}$", re.IGNORECASE)


@dataclass(frozen=True)
class Btc5mMarketMetadata:
    """Resolved BTC 5m window metadata for Z-Gap and related strategies."""

    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    event_start_ts: float
    event_end_ts: float
    event_slug: str
    event_url: str | None = None
    event_title: str = ""
    yes_outcome_label: str = ""
    no_outcome_label: str = ""
    market_slug: str = ""

    @property
    def up_token_id(self) -> str:
        return self.yes_token_id

    @property
    def down_token_id(self) -> str:
        return self.no_token_id


def from_paired_binary_metadata(meta: PairedBinaryEventMetadata) -> Btc5mMarketMetadata:
    return Btc5mMarketMetadata(
        market_id=meta.market_id,
        condition_id=meta.condition_id,
        yes_token_id=meta.yes_token_id,
        no_token_id=meta.no_token_id,
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        event_slug=meta.event_slug,
        event_title=meta.event_title,
        yes_outcome_label=meta.yes_outcome_label,
        no_outcome_label=meta.no_outcome_label,
        market_slug=meta.market_slug,
    )


def is_btc_5m_metadata_placeholder(field: str, value: object) -> bool:
    if field in {"event_start_ts", "event_end_ts"}:
        return value is None
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    lower = text.lower()
    if field == "market_id":
        if text in _SHADOW_MARKET_IDS:
            return True
        if any(marker in lower for marker in _PLACEHOLDER_MARKERS):
            return True
        if "<" in text or ">" in text:
            return True
        if lower.startswith("btc_5m_") and not _BTC_MARKET_RE.match(text):
            return True
        return False
    return any(marker in lower for marker in _PLACEHOLDER_MARKERS)


def validate_btc_5m_metadata(meta: Btc5mMarketMetadata) -> None:
    """Fail closed on incomplete or invalid BTC 5m metadata."""
    if not meta.market_id or is_btc_5m_metadata_placeholder("market_id", meta.market_id):
        raise EventMetadataError("btc_5m market_id missing or placeholder")
    if not meta.condition_id or is_btc_5m_metadata_placeholder("condition_id", meta.condition_id):
        raise EventMetadataError("btc_5m condition_id missing or placeholder")
    if not meta.yes_token_id or is_btc_5m_metadata_placeholder("yes_token_id", meta.yes_token_id):
        raise EventMetadataError("btc_5m yes_token_id missing or placeholder")
    if not meta.no_token_id or is_btc_5m_metadata_placeholder("no_token_id", meta.no_token_id):
        raise EventMetadataError("btc_5m no_token_id missing or placeholder")
    if meta.event_end_ts <= meta.event_start_ts:
        raise EventMetadataError(
            f"event_end_ts ({meta.event_end_ts}) must be > event_start_ts ({meta.event_start_ts})"
        )


def merge_z_gap_metadata(cfg: ZGapStrategyConfig, meta: Btc5mMarketMetadata) -> ZGapStrategyConfig:
    updates: dict[str, object] = {}
    for field, meta_value in (
        ("market_id", meta.market_id),
        ("condition_id", meta.condition_id),
        ("yes_token_id", meta.yes_token_id),
        ("no_token_id", meta.no_token_id),
        ("event_start_ts", meta.event_start_ts),
        ("event_end_ts", meta.event_end_ts),
    ):
        current = getattr(cfg, field)
        if is_btc_5m_metadata_placeholder(field, current):
            updates[field] = meta_value
    if not updates:
        return cfg
    return replace(cfg, **updates)


def apply_btc_5m_metadata_to_app(app: AppConfig, meta: Btc5mMarketMetadata) -> AppConfig:
    if app.z_gap is None:
        raise EventMetadataError("z_gap strategy config missing")
    merged = merge_z_gap_metadata(app.z_gap, meta)
    return replace(app, z_gap=merged)


def resolve_btc_5m_event_metadata(event_url: str) -> Btc5mMarketMetadata:
    meta = resolve_paired_binary_event_metadata(event_url)
    out = from_paired_binary_metadata(meta)
    validate_btc_5m_metadata(out)
    return out


def resolve_btc_5m_from_tokens(yes_token_id: str, no_token_id: str) -> Btc5mMarketMetadata:
    meta = resolve_paired_binary_from_tokens(yes_token_id, no_token_id)
    out = from_paired_binary_metadata(meta)
    validate_btc_5m_metadata(out)
    return out


def resolve_and_apply_btc_5m_metadata(
    app: AppConfig,
    *,
    event_url: str | None = None,
) -> tuple[AppConfig, Btc5mMarketMetadata | None]:
    """Resolve metadata from --event-url or real yes/no tokens, then merge into app."""
    if app.z_gap is None:
        raise EventMetadataError("z_gap strategy config missing")

    meta: Btc5mMarketMetadata | None = None
    if event_url:
        meta = resolve_btc_5m_event_metadata(event_url)
    else:
        zg = app.z_gap
        tokens_real = not is_btc_5m_metadata_placeholder(
            "yes_token_id", zg.yes_token_id
        ) and not is_btc_5m_metadata_placeholder("no_token_id", zg.no_token_id)
        needs_metadata = any(
            is_btc_5m_metadata_placeholder(field, getattr(zg, field))
            for field in (
                "market_id",
                "condition_id",
                "event_start_ts",
                "event_end_ts",
            )
        )
        if tokens_real and needs_metadata:
            meta = resolve_btc_5m_from_tokens(zg.yes_token_id, zg.no_token_id)

    if meta is None:
        return app, None
    validate_btc_5m_metadata(meta)
    return apply_btc_5m_metadata_to_app(app, meta), meta


def require_resolved_z_gap_metadata(app: AppConfig) -> Btc5mMarketMetadata:
    if app.z_gap is None:
        raise ConfigError("z_gap strategy config missing")
    zg = app.z_gap
    try:
        meta = Btc5mMarketMetadata(
            market_id=zg.market_id,
            condition_id=str(zg.condition_id or ""),
            yes_token_id=zg.yes_token_id,
            no_token_id=zg.no_token_id,
            event_start_ts=float(zg.event_start_ts or 0),
            event_end_ts=float(zg.event_end_ts or 0),
            event_slug="",
        )
        validate_btc_5m_metadata(meta)
    except EventMetadataError as exc:
        raise ConfigError(str(exc)) from exc
    return meta
