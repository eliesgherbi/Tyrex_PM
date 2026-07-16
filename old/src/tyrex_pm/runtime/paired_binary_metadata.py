"""Apply Polymarket event metadata overrides onto paired-binary app config."""

from __future__ import annotations

import re
from dataclasses import replace

from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig
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


def is_paired_binary_metadata_placeholder(field: str, value: object) -> bool:
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


def merge_paired_binary_metadata(
    cfg: PairedBinaryStrategyConfig,
    meta: PairedBinaryEventMetadata,
) -> PairedBinaryStrategyConfig:
    """Fill missing/placeholder paired-binary fields from resolved metadata."""
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
        if is_paired_binary_metadata_placeholder(field, current):
            updates[field] = meta_value
    if not updates:
        return cfg
    return replace(cfg, **updates)


def apply_event_metadata_to_app(app: AppConfig, meta: PairedBinaryEventMetadata) -> AppConfig:
    if app.paired_binary is None:
        raise EventMetadataError("paired_binary strategy config missing")
    merged = merge_paired_binary_metadata(app.paired_binary, meta)
    return replace(app, paired_binary=merged)


def resolve_and_apply_paired_binary_metadata(
    app: AppConfig,
    *,
    event_url: str | None = None,
) -> tuple[AppConfig, PairedBinaryEventMetadata | None]:
    """Resolve metadata from --event-url or real yes/no tokens, then merge into app."""
    if app.paired_binary is None:
        raise EventMetadataError("paired_binary strategy config missing")

    meta: PairedBinaryEventMetadata | None = None
    if event_url:
        meta = resolve_paired_binary_event_metadata(event_url)
    else:
        pb = app.paired_binary
        tokens_real = not is_paired_binary_metadata_placeholder(
            "yes_token_id", pb.yes_token_id
        ) and not is_paired_binary_metadata_placeholder("no_token_id", pb.no_token_id)
        needs_metadata = any(
            is_paired_binary_metadata_placeholder(field, getattr(pb, field))
            for field in (
                "market_id",
                "condition_id",
                "event_start_ts",
                "event_end_ts",
            )
        )
        if tokens_real and needs_metadata:
            meta = resolve_paired_binary_from_tokens(pb.yes_token_id, pb.no_token_id)

    if meta is None:
        return app, None
    return apply_event_metadata_to_app(app, meta), meta
