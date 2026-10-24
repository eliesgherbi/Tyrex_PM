"""Offline comparison on captured CLOB messages. Never connects or submits orders."""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.adapters.polymarket.normalize import normalize_market_ws_message
from tyrex_pm.core.book_events import BookDeltaReceived, BookSnapshotReceived, TickSizeChanged
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.recording.records import iter_records
from tyrex_pm.research.strategy_replay.capture_replay import sdk_to_venue_message


def audit(capture):
    stores, counts, costs = {}, {p: Counter() for p in ("legacy", "venue_top_v1")}, {}
    for index, record in enumerate(iter_records(Path(capture) / "recording", "raw")):
        if index and index % 100000 == 0:
            print(json.dumps({"capture": str(capture), "records_checked": index}), flush=True)
        if record.get("input_class") != "clob_market":
            continue
        payload = sdk_to_venue_message(record.get("body_json") or {})
        if payload is None:
            continue
        received = datetime.fromtimestamp(record["received_at"] / 1e9, timezone.utc)
        try:
            event = normalize_market_ws_message(payload, ts_received=received)
        except ValueError:
            for policy in counts:
                counts[policy]["ingress_rejected"] += 1
            continue
        if event is None:
            continue
        for policy in counts:
            key = (policy, record.get("connection_id"))
            if key not in stores:
                stores[key] = MarketStateStore(reconstruction=policy)
            store = stores[key]
            start = time.perf_counter_ns()
            if isinstance(event, BookSnapshotReceived):
                store.on_snapshot(event)
            elif isinstance(event, BookDeltaReceived):
                store.on_delta(event)
            elif isinstance(event, TickSizeChanged):
                store.on_tick_size(event)
            costs.setdefault(policy, []).append((time.perf_counter_ns() - start) / 1000)
            counts[policy]["messages"] += 1
            if isinstance(event, BookDeltaReceived):
                last = {c.instrument_id: c for c in event.changes}
                for iid, change in last.items():
                    if store.sync_health(iid) is not SyncHealth.READY:
                        counts[policy]["unavailable_after_delta"] += 1
                        continue
                    counts[policy]["ready_token_messages"] += 1
                    quote = store.quote_for(iid)
                    bid = Decimal(0) if quote.best_bid is None else quote.best_bid
                    ask = Decimal(1) if quote.best_ask is None else quote.best_ask
                    if (change.reported_best_bid is not None
                            and change.reported_best_ask is not None):
                        counts[policy]["reported_top_checks"] += 1
                        counts[policy]["ready_top_mismatch"] += int(
                            (bid, ask) != (change.reported_best_bid, change.reported_best_ask))
    result = {"capture": str(capture), "policies": {},
              "limits": ["Top consistency does not prove depth quantities or continuity.",
                         "Mutation timings exclude depth reads, transport and order submission.",
                         "Legacy uses the optimized representation too; this is not a "
                         "benchmark of the original pre-change implementation."]}
    for policy in counts:
        values = sorted(costs.get(policy, []))
        result["policies"][policy] = {**counts[policy], "mutation_us": {
            str(p): values[min(len(values)-1, int(p * len(values)))] if values else None
            for p in (.5, .95, .99)}}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    reports = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for path in args.captures:
        reports.append(audit(path))
        args.out.write_text(json.dumps(reports, indent=2), "utf-8")
    print(json.dumps(reports, indent=2))
