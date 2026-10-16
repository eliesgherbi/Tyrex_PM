from __future__ import annotations

import heapq
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from dataclasses import fields
from typing import Any, Callable, Iterable, Iterator

from ..capture.files import read_json, within
from ..contracts import QUALITY_VERSION, decode, exact, identity
from ..query.reader import BINANCE, Reader, order
from ..reconstruction.books import OUTCOME_PREFIX, Books
from ..reconstruction.boundaries import boundary_candidates, resolution_assessment
from ..reconstruction.policy import load_policy, semantic_status


class CoverageBooks(Books):
    """Coverage needs epoch provenance, not copies of every full depth ladder."""

    def invalidate(self, book, at, reason):
        if book.valid:
            self.epochs.append(
                {
                    **{
                        f.name: getattr(book, f.name)
                        for f in fields(book)
                        if f.name not in ("bids", "asks")
                    },
                    "invalidated_at": at,
                    "reason": reason,
                    "reconstruction_version": "3",
                }
            )
        book.valid, book.reason = False, reason


def union(intervals: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for left, right in sorted((max(start, a), min(end, b)) for a, b in intervals):
        if left >= right:
            continue
        if result and left <= result[-1][1]:
            result[-1] = result[-1][0], max(right, result[-1][1])
        else:
            result.append((left, right))
    return result


def interval_report(
    intervals: list[tuple[int, int]], start: int, end: int, *, assurance: str
) -> dict:
    merged = union(intervals, start, end)
    usable = sum(b - a for a, b in merged)
    required = max(0, end - start)
    return {
        "required_from": start,
        "required_until": end,
        "required_duration_ns": required,
        "usable_duration_ns": usable,
        "unusable_or_missing_duration_ns": required - usable,
        "unknown_continuity_duration_ns": usable if assurance == "UNKNOWN_CONTINUITY" else 0,
        "recorded_duration_ratio": usable / required if required else None,
        "status": "NOT_YET_EXPECTED"
        if not required
        else (assurance if usable == required else "GAP"),
        "intervals": merged,
        "ratio_is_provider_event_completeness": False,
        "staleness_is_coverage_gap": False,
    }


def transport_intervals(event_rows: list[dict], source: str, horizon: int) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    connected: dict[tuple, tuple[int, int]] = {}
    for event_row in event_rows:
        event = event_row["_event"] if "_event" in event_row else decode(event_row["details"])
        at = event_row["received_at"]
        session = event_row.get("session_id")
        if event.get("type") in ("session_interrupted", "session_started", "session_stopped"):
            previous = event.get("previous_session")
            for key, (start, last) in list(connected.items()):
                affected = (
                    key[0] == previous
                    if previous
                    else key[0] == session
                    if event.get("type") == "session_stopped"
                    else key[0] != session
                )
                if affected:
                    # Never bridge an unobserved process outage to restart wall time.
                    bound = event.get("last_confirmed_at", last)
                    intervals.append((start, max(start, min(at, bound))))
                    del connected[key]
            continue
        if event.get("type") == "heartbeat":
            for feed in event.get("status", {}).get("feeds", {}).values():
                key = (session, feed.get("connection_id"))
                if feed.get("source") == source and feed.get("connected") and key in connected:
                    start, _ = connected[key]
                    connected[key] = (start, at)
            continue
        if event.get("source") != source:
            continue
        key = (session, event.get("connection_id"))
        if event.get("type") == "connected":
            if key in connected:
                start, last = connected.pop(key)
                intervals.append((start, last))
            connected[key] = (at, at)
        elif event.get("type") in ("disconnected", "sequence_gap") and key in connected:
            start, last = connected.pop(key)
            intervals.append((start, at if event["type"] == "disconnected" else last))
        elif event.get("type") == "feed_evidence":
            if key in connected:
                start, _ = connected[key]
                connected[key] = (start, at)
            else:
                connected[key] = (at, at)
    for start, last in connected.values():
        intervals.append((start, min(horizon, last)))
    return intervals


def reference_intervals(
    connections: list[tuple[int, int]], observations: list[dict]
) -> list[tuple[int, int]]:
    """Connection span is the coverage interval. A tick is not a 5-second blob."""
    if connections:
        return connections
    return [
        (int(row["received_at"]), int(row["received_at"]) + 1)
        for row in observations
        if row.get("delivery_kind") == "live"
    ]


def stale_references(rows: list[dict], freshness_ns: int) -> list[dict]:
    return [
        row
        for row in rows
        if row.get("delivery_kind") == "live"
        and row.get("source_time") is not None
        and abs(row["received_at"] - row["source_time"]) > freshness_ns
    ]


LIFECYCLE = frozenset(
    (
        "connected",
        "disconnected",
        "sequence_gap",
        "feed_evidence",
        "session_interrupted",
        "session_started",
        "session_stopped",
        "subscription",
    )
)


def _memo(reader: Reader, key: Any, build: Callable[[], Any]) -> Any:
    """Per-reader cache. A reader's manifests are fixed, so derived rows are too."""
    memo = reader.__dict__.setdefault("_coverage_memo", {})
    if key not in memo:
        memo[key] = build()
    return memo[key]


def reader_horizon(reader: Reader) -> int:
    return max((b["last_received_at"] for b in reader.manifests), default=0)


def _timed(rows: list[dict]) -> tuple[list[dict], list[int]]:
    return rows, [r["received_at"] for r in rows]


def _operational(reader: Reader):
    def build():
        rows = []
        for row in reader.iter_rows("operational_events"):
            rows.append({**row, "_event": decode(row["details"])})
        lifecycle = [r for r in rows if r["_event"].get("type") in LIFECYCLE]
        return _timed(rows), _timed(lifecycle)

    return _memo(reader, "operational", build)


def operational_slice(reader: Reader, start: int | None, until: int) -> list[dict]:
    (rows, times), _ = _operational(reader)
    left = 0 if start is None else bisect_left(times, start)
    return rows[left : bisect_right(times, until)]


def transport_events(reader: Reader, lower: int, until: int) -> list[dict]:
    """Lifecycle history plus every event from ``lower``.

    Heartbeats only extend an already-open connection, so dropping those before the
    clipped coverage interval cannot change it; connection/session transitions can.
    """
    _, (lifecycle, times) = _operational(reader)
    return lifecycle[: bisect_left(times, lower)] + operational_slice(reader, lower, until)


def _references(reader: Reader):
    def build():
        rows = list(reader.iter_rows("reference_observations"))
        by_source = sorted(
            (r["source_time"], n) for n, r in enumerate(rows) if r.get("source_time") is not None
        )
        return _timed(rows), by_source

    return _memo(reader, "references", build)


def references_until(reader: Reader, horizon: int) -> list[dict]:
    (rows, times), _ = _references(reader)
    return rows[: bisect_right(times, horizon)]


def nearby_references(
    reader: Reader, targets: Iterable[int], horizon: int | None = None, window: int = 5 * 10**9
) -> list[dict]:
    """References whose source time is within ``window`` of any target, in archive order."""
    (rows, times), by_source = _references(reader)
    limit = len(rows) if horizon is None else bisect_right(times, horizon)
    keys = [t for t, _ in by_source]
    chosen = set()
    for target in targets:
        for _, n in by_source[
            bisect_left(keys, target - window) : bisect_right(keys, target + window)
        ]:
            if n < limit:
                chosen.add(n)
    return [rows[n] for n in sorted(chosen)]


def _grouped(reader: Reader, dataset: str, field: str):
    def build():
        groups: dict[str, list[dict]] = {}
        for row in reader.iter_rows(dataset):
            groups.setdefault(row.get(field) or "", []).append(row)
        return groups

    return _memo(reader, ("grouped", dataset), build)


def market_rows(reader: Reader, dataset: str, field: str, market: str, horizon: int) -> list:
    return [
        r for r in _grouped(reader, dataset, field).get(market, []) if r["received_at"] <= horizon
    ]


def market_bounds(metadata: list[dict], horizon: int) -> dict | None:
    scheduled = [r for r in metadata if r.get("window_start") is not None]
    if not scheduled:
        return None
    latest = scheduled[-1]
    closed = None
    for row in metadata:
        if row.get("closed") is True:
            closed = closed or row["received_at"]
        elif row.get("closed") is False:
            closed = None
    start, scheduled_end = latest["window_start"], latest["window_end"]
    # Feed coverage concerns the trading window; later settlement evidence must not
    # extend its required recording interval beyond expiry.
    end = min(horizon, scheduled_end, closed if closed is not None else scheduled_end)
    return {
        "latest": latest,
        "start": start,
        "scheduled_end": scheduled_end,
        "closed": closed,
        "end": end,
    }


def merged(streams: list[tuple[str, Iterable[dict]]]) -> Iterator[tuple[str, dict]]:
    def tagged(name: str, rows: Iterable[dict]) -> Iterator:
        for row in rows:
            yield name, row

    yield from heapq.merge(*(tagged(n, r) for n, r in streams), key=lambda item: order(item[1]))


def open_epoch(book, at: int) -> dict:
    return {
        **{f.name: getattr(book, f.name) for f in fields(book) if f.name not in ("bids", "asks")},
        "invalidated_at": at,
        "reason": book.reason,
    }


def books_at(books: Books, at: int, instruments: set[str] | None = None) -> list[dict]:
    """Epochs exactly as a replay that stopped after the last event at or before ``at``."""
    rows = [e for e in books.epochs if instruments is None or e["instrument_id"] in instruments]
    for book in books.physical():
        if book.valid and book.usable_from is not None:
            if instruments is None or book.instrument_id in instruments:
                rows.append(open_epoch(book, at))
    return rows


def binance_epochs(reader: Reader, end: int, horizon: int) -> list[dict]:
    """One causal Binance replay serves every market end of this reader.

    Binance is seeded only at (re)connection, so a per-market replay would reread the
    whole connection for each window. State is captured as of each requested end.
    """
    memo = _memo(reader, ("binance", horizon), dict)
    if end not in memo:
        ends = {end}
        for history in _grouped(reader, "metadata_versions", "entity_id").values():
            bounds = market_bounds([r for r in history if r["received_at"] <= horizon], horizon)
            if bounds is not None and bounds["start"] <= horizon:
                ends.add(bounds["end"])
        pending = sorted(ends - memo.keys())
        last = pending[-1]
        books = CoverageBooks()
        events = merged(
            [
                (
                    "book_snapshots",
                    reader.iter_rows("book_snapshots", until=last, entities=[BINANCE]),
                ),
                ("book_updates", reader.iter_rows("book_updates", until=last, entities=[BINANCE])),
                ("operational_events", operational_slice(reader, None, last)),
            ]
        )
        index = 0
        for dataset, row in events:
            while index < len(pending) and row["received_at"] > pending[index]:
                memo[pending[index]] = books_at(books, pending[index])
                index += 1
            books.apply(dataset, row)
        for at in pending[index:]:
            memo[at] = books_at(books, at)
    return memo[end]


def clob_replay_start(reader: Reader, instruments: set[str], lower: int) -> int:
    """Earliest real seed before warm-up of any socket stream that can still be usable.

    Each (session, connection) stream is rebuilt only from its own snapshot. A stream
    closed before ``lower`` (disconnect, sequence gap, unsubscribe, earlier session)
    cannot contribute coverage after it, so its older seed need not be replayed.
    Without a live seed, no earlier update can make a book usable: ``lower`` is exact.
    """
    _, (lifecycle, times) = _operational(reader)
    before = lifecycle[: bisect_left(times, lower)]
    session = before[-1]["session_id"] if before else None
    closed: dict[tuple, int] = {}
    for row in before:
        event = row["_event"]
        if event.get("type") in ("disconnected", "sequence_gap"):
            closed[(row["session_id"], event.get("connection_id"), None)] = row["received_at"]
        elif event.get("type") == "subscription" and event.get("operation") == "unsubscribe":
            for token in event.get("tokens", []):
                key = (row["session_id"], event.get("connection_id"), f"{OUTCOME_PREFIX}{token}")
                closed[key] = row["received_at"]
    start = lower
    for instrument in instruments:
        latest: dict[tuple, int] = {}
        for row in reader.iter_rows("book_snapshots", until=lower - 1, entities=[instrument]):
            if row["snapshot_origin"] != "rest_audit":
                latest[(row["session_id"], row["connection_id"])] = row["received_at"]
        for (stream_session, connection), seeded in latest.items():
            if session is not None and stream_session != session:
                continue
            ends = [
                closed.get((stream_session, connection, None)),
                closed.get((stream_session, connection, instrument)),
            ]
            if any(at is not None and at >= seeded for at in ends):
                continue
            start = min(start, seeded)
    return start


@contextmanager
def coverage_source(reader: Reader) -> Iterator[Reader]:
    """Use a current coverage index read-only; otherwise read the archive directly.

    Verification and export never build, lock, or write the index.
    """
    from .index import CoverageIndex

    if isinstance(reader, CoverageIndex):
        yield reader
        return
    indexed = CoverageIndex.open_current(reader)
    if indexed is None:
        yield reader
        return
    try:
        yield indexed
    finally:
        indexed.close()


def coverage(
    reader: Reader, market: str, *, warmup_s: int | None = None, as_of: int | None = None
) -> dict[str, Any]:
    with coverage_source(reader) as source:
        return _coverage(source, market, warmup_s=warmup_s, as_of=as_of)


def _coverage(
    reader: Reader, market: str, *, warmup_s: int | None = None, as_of: int | None = None
) -> dict[str, Any]:
    horizon = as_of if as_of is not None else reader_horizon(reader)
    metadata = market_rows(reader, "metadata_versions", "entity_id", market, horizon)
    bounds = market_bounds(metadata, horizon)
    if bounds is None:
        return {
            "market_id": market,
            "policy_version": QUALITY_VERSION,
            "semantic_readiness": "UNAVAILABLE",
            "reason": "no_window_metadata",
        }
    latest = bounds["latest"]
    collection = latest["collection_id"]
    path = within(reader.root, f"collections/{collection}/collection.json")
    profile = read_json(path).get("profile", {}) if path.exists() else {}
    if warmup_s is None:
        warmup_s = profile.get("coverage", {}).get("external_warmup_s", 900)
    freshness = profile.get("reference", {}).get("observation_warn_s", 5) * 10**9
    policy = load_policy(reader.root, collection)
    start, scheduled_end = bounds["start"], bounds["scheduled_end"]
    if horizon < start:
        return {
            "market_id": market,
            "policy_version": QUALITY_VERSION,
            "assessed_at": horizon,
            "semantic_readiness": "UNAVAILABLE",
            "reason": "window_not_started",
            "scheduled_end": scheduled_end,
            "feeds": {},
            "boundaries": [],
            "book_epochs": [],
        }
    closed, end = bounds["closed"], bounds["end"]
    lower = start - warmup_s * 10**9
    instrument_history = market_rows(reader, "instruments", "market_id", market, horizon)
    instruments = {r["instrument_id"] for r in instrument_history}
    clob_start = clob_replay_start(reader, instruments, lower)
    books = CoverageBooks()
    for dataset, row in merged(
        [
            (
                "book_snapshots",
                reader.iter_rows("book_snapshots", clob_start, end, entities=instruments),
            ),
            (
                "book_updates",
                reader.iter_rows("book_updates", clob_start, end, entities=instruments),
            ),
            ("operational_events", operational_slice(reader, clob_start, end)),
        ]
    ):
        books.apply(dataset, row)
    epochs = books_at(books, end, instruments) + binance_epochs(reader, end, horizon)
    feeds = {}
    for instrument in instruments:
        intervals = [
            (e["usable_from"], e["invalidated_at"])
            for e in epochs
            if e["instrument_id"] == instrument and e["usable_from"] is not None
        ]
        feeds[instrument] = interval_report(intervals, start, end, assurance="UNKNOWN_CONTINUITY")
    binance = [
        (e["usable_from"], e["invalidated_at"])
        for e in epochs
        if e["instrument_id"] == BINANCE and e["usable_from"] is not None
    ]
    feeds["binance_depth"] = interval_report(
        binance, lower, end, assurance="CAPTURED_NO_DETECTED_GAP"
    )
    references = references_until(reader, horizon)
    operational = transport_events(reader, lower, end)
    live_references = [row for row in references if row.get("delivery_kind") == "live"]
    reference_evidence = [
        {
            "received_at": r["received_at"],
            "session_id": r["session_id"],
            "_event": {
                "type": "feed_evidence",
                "source": "polybolt",
                "connection_id": r["connection_id"],
            },
        }
        for r in live_references
    ]
    reference_span = reference_intervals(
        transport_intervals(
            sorted(operational + reference_evidence, key=lambda r: r["received_at"]),
            "polybolt",
            horizon,
        ),
        live_references,
    )
    feeds["official_reference"] = interval_report(
        reference_span, lower, end, assurance="UNKNOWN_CONTINUITY"
    )
    feeds["official_reference"]["staleness_warn_ns"] = freshness
    stale = stale_references(
        [r for r in live_references if lower <= r["received_at"] <= end],
        freshness,
    )
    feeds["official_reference"]["stale_observations"] = len(stale)
    # Trade observations have no universal sequence: expose recording intervals, not completeness.
    trades = [
        r for r in reader.iter_rows("trade_prints", lower, end) if r["source_id"] == "binance"
    ]
    trade_evidence = [
        {
            "received_at": r["received_at"],
            "session_id": r["session_id"],
            "_event": {
                "type": "feed_evidence",
                "source": "binance",
                "connection_id": r["connection_id"],
            },
        }
        for r in trades
    ]
    trade_intervals = transport_intervals(
        sorted(operational + trade_evidence, key=lambda r: r["received_at"]), "binance", horizon
    )
    if not trade_intervals and trades:
        # Without transport evidence, don't bridge silent gaps using first/last prints.
        trade_intervals = [(r["received_at"], r["received_at"] + 1) for r in trades]
    feeds["binance_trades"] = interval_report(
        trade_intervals, lower, end, assurance="UNKNOWN_CONTINUITY"
    )
    failures = [
        r
        for r in reader.iter_rows("processing_results", lower, end)
        if r["status"] in ("REJECTED", "UNKNOWN_TYPE")
    ]
    interruptions = [
        r["_event"]
        for r in operational_slice(reader, start, end)
        if r["event_type"] in ("disconnected", "sequence_gap", "session_interrupted")
    ]
    semantic, semantic_reason = semantic_status(policy, latest.get("rule_fingerprint"))
    settlement = resolution_assessment(
        market_rows(reader, "resolution_observations", "market_id", market, horizon),
        supported_statuses=tuple(policy.get("terminal_statuses", [])),
        payout_unit=policy.get("payout_unit"),
    )
    for name, report in feeds.items():
        if name == "official_reference":
            report["required"] = profile.get("reference", {}).get("required", True)
        elif name.startswith("binance_"):
            external = profile.get("external", {}).get("binance_spot", {})
            report["required"] = external.get("required", True)
        else:
            report["required"] = True
    targets = [
        row[field]
        for row in metadata
        if row.get("rule_fingerprint")
        for field in ("window_start", "window_end")
        if row.get(field) is not None
    ]
    return {
        "market_id": market,
        "policy_version": QUALITY_VERSION,
        "assessed_at": horizon,
        "capture_integrity": "MANIFEST_VERIFICATION_REQUIRED",
        "normalization": "INCOMPLETE" if failures else "ACCOUNTED",
        "reconstruction_usability": (
            "GAP"
            if len(instruments) != 2
            or any(
                feed["required"] and feed["status"] in ("GAP", "NOT_YET_EXPECTED")
                for feed in feeds.values()
            )
            else "NO_DETECTED_GAP_WITH_STATED_ASSURANCE"
        ),
        "semantic_readiness": semantic,
        "semantic_reason": semantic_reason,
        "settlement_readiness": settlement["status"],
        "settlement": settlement,
        "boundaries": boundary_candidates(
            market, metadata, nearby_references(reader, targets, horizon), horizon
        ),
        "book_epochs": [
            r
            for r in records(epochs)
            if r["instrument_id"] in instruments | {BINANCE}
            and (r["invalidated_at"] or end) >= lower
        ],
        "scoring_labels": scoring_labels(instrument_history, settlement, policy),
        "provider_policy": policy,
        "scheduled_end": scheduled_end,
        "observed_close": closed,
        "closure_status": "OBSERVED" if closed is not None else "UNKNOWN",
        "feeds": feeds,
        "processing_failures": len(failures),
        "interruptions": interruptions,
        "cross_session_ordering": reader.merge_policy,
        "cross_session_clock_uncertainty": "NOT_ELIMINATED",
    }


def instrument_rows(reader: Reader, market: str, horizon: int) -> list[dict]:
    return [
        row
        for row in reader.iter_rows("instruments", until=horizon)
        if row.get("market_id") == market
    ]


def epoch_records(books: Books, horizon: int) -> list[dict]:
    return records(books_at(books, horizon))


def records(seen: list[dict]) -> list[dict]:
    rows = []
    for epoch in seen:
        rows.append(
            {
                "epoch_id": epoch.get("epoch_id")
                or identity(epoch["instrument_id"], epoch.get("seed_id"), epoch.get("usable_from")),
                "instrument_id": epoch["instrument_id"],
                "connection_id": epoch.get("connection_id"),
                "session_id": epoch.get("session_id"),
                "seed_id": epoch.get("seed_id"),
                "usable_from": epoch.get("usable_from"),
                "invalidated_at": epoch.get("invalidated_at"),
                "reason": epoch.get("reason"),
                "assurance": epoch.get("assurance"),
                "reconstruction_version": epoch.get("reconstruction_version") or "2",
            }
        )
    return sorted(
        rows, key=lambda row: (row["instrument_id"], row["usable_from"] or 0, row["epoch_id"])
    )


def scoring_labels(instruments: list[dict], settlement: dict, policy: dict) -> list[dict]:
    """Map a terminal payout vector only when the policy names the index basis."""
    payouts = settlement.get("payouts")
    if settlement.get("status") != "PROVIDER_TERMINAL" or not payouts:
        return []
    if policy.get("payout_index_basis") != "outcome_index":
        return []
    latest = {}
    for row in sorted(
        instruments,
        key=lambda r: (
            r.get("received_at", 0),
            r.get("ingest_sequence", 0),
            r.get("record_id", ""),
        ),
    ):
        latest[row["instrument_id"]] = row
    ordered = sorted(
        latest.values(),
        key=lambda row: (
            row.get("outcome_index") is None,
            row.get("outcome_index") if row.get("outcome_index") is not None else 0,
            row["instrument_id"],
        ),
    )
    if len(ordered) != len(payouts) or [r.get("outcome_index") for r in ordered] != list(
        range(len(payouts))
    ):
        return []
    available = settlement.get("available_at")
    return [
        {
            "instrument_id": row["instrument_id"],
            "outcome_index": row["outcome_index"],
            "status": "PROVIDER_TERMINAL",
            "payout": format(exact(payout), "f"),
            "available_at": available,
        }
        for row, payout in zip(ordered, payouts)
    ]
