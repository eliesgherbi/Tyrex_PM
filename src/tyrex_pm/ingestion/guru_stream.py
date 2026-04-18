"""
Guru ingest — Data API only (parity).

**Watermark** (lexicographic, monotonic):
  cursor = (ts_ms, dedup_key) where ts_ms is Unix milliseconds from `ts_venue`
  or 0 if missing; dedup_key is `GuruTradeSignal.dedup_key`.

**Dedup**: `strategy_store.guru_seen_dedup` for process lifetime; persisted with the
store so the same API id is not re-emitted after restart.

**Ordering**: sort candidate rows by (ts_ms, dedup_key) before processing.

**Gap-fill**: `poll_guru_incremental` fetches up to `max_pages` API pages per poll
so activity newer than the watermark can be recovered after lag (single-page APIs
that return newest-first still benefit from chaining `next_cursor`).
"""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.core.models import GuruTradeSignal
from tyrex_pm.state.strategy_store import GuruWatermark, StrategyStore
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient
from tyrex_pm.venue.polymarket.normalizers import normalize_data_api_activity_row


def _ts_ms(sig: GuruTradeSignal) -> int:
    if sig.ts_venue is None:
        return 0
    return int(sig.ts_venue.timestamp() * 1000)


def _after_watermark(sig: GuruTradeSignal, wm: GuruWatermark | None) -> bool:
    if wm is None:
        return True
    t = _ts_ms(sig)
    if t > wm.ts_ms:
        return True
    if t < wm.ts_ms:
        return False
    return sig.dedup_key > wm.dedup_id


def _advance_wm(store: StrategyStore, sig: GuruTradeSignal) -> None:
    store.guru_watermark = GuruWatermark(ts_ms=_ts_ms(sig), dedup_id=sig.dedup_key)


def ingest_guru_signals(store: StrategyStore, candidates: list[GuruTradeSignal]) -> list[GuruTradeSignal]:
    """Sort, dedup, watermark filter, advance cursor; return newly accepted signals."""
    rows = sorted(candidates, key=lambda s: (_ts_ms(s), s.dedup_key))
    new: list[GuruTradeSignal] = []
    for sig in rows:
        if sig.dedup_key in store.guru_seen_dedup:
            continue
        if not _after_watermark(sig, store.guru_watermark):
            continue
        store.guru_seen_dedup.add(sig.dedup_key)
        _advance_wm(store, sig)
        new.append(sig)
    return new


@dataclass
class GuruPollResult:
    new_signals: list[GuruTradeSignal]
    raw_rows: int = 0
    normalized_candidates: int = 0
    pages_fetched: int = 0


async def poll_guru_incremental(
    *,
    client: DataApiClient,
    guru_wallet: str,
    limit: int,
    max_pages: int,
    store: StrategyStore,
) -> GuruPollResult:
    raw_items: list[dict] = []
    cursor: str | None = None
    pages_fetched = 0
    for _ in range(max(1, max_pages)):
        pages_fetched += 1
        page = await client.fetch_wallet_activity(guru_wallet, limit=limit, cursor=cursor)
        for item in page.items:
            if isinstance(item, dict):
                raw_items.append(item)
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    normalized: list[GuruTradeSignal] = []
    for item in raw_items:
        sig = normalize_data_api_activity_row(item, guru_wallet)
        if sig is not None:
            normalized.append(sig)
    new = ingest_guru_signals(store, normalized)
    return GuruPollResult(
        new_signals=new,
        raw_rows=len(raw_items),
        normalized_candidates=len(normalized),
        pages_fetched=pages_fetched,
    )


def process_fixture_signals(
    signals: list[GuruTradeSignal],
    store: StrategyStore,
) -> list[GuruTradeSignal]:
    """Sync path for tests — same ordering/dedup/watermark rules."""
    return ingest_guru_signals(store, list(signals))
