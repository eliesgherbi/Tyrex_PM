"""Record the existing PolyBolt collector and RTDS live adapter side by side.

    .venv/Scripts/python.exe scripts/compare_twap_channels.py --seconds 1200
    .venv/Scripts/python.exe scripts/compare_twap_channels.py --analyze OUTPUT_FOLDER

No exchange execution client, wallet signing, collector directory or experiment is used.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import time
from concurrent.futures import Future
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

from tyrex_pm.adapters.polymarket.rtds_adapter import RtdsChainlinkAdapter
from tyrex_pm.core.events import SettlementReferenceUpdated
from tyrex_pm.data_warehouse.config import load_credentials, load_profile
from tyrex_pm.data_warehouse.connectors.streams import record_stream
from tyrex_pm.data_warehouse.contracts import RawEnvelope, decode
from tyrex_pm.data_warehouse.normalize.registry import normalize

NS = 1_000_000_000
ROOT = Path(__file__).resolve().parents[1]


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS, timezone.utc).isoformat()


def distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {
        "n": len(values),
        "median_ms": statistics.median(ordered) if ordered else None,
        "p95_ms": ordered[max(0, math.ceil(.95 * len(ordered)) - 1)] if ordered else None,
        "min_ms": ordered[0] if ordered else None,
        "max_ms": ordered[-1] if ordered else None,
    }


class Capture:
    """Small sink implementing the production collector's capture/projection interface."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.stop = asyncio.Event()
        self.profile = load_profile()
        self.feed_health: dict[str, Any] = {}
        self.projection_bytes = 0
        self.writer = self
        self.sequence = 0
        self.rtds_receipt: tuple[int, int, int] | None = None
        self.session = uuid4().hex
        self.files: dict[str, Any] = {}

    def write(self, name: str, value: dict) -> None:
        if name not in self.files:
            self.files[name] = (self.output / name).open("a", encoding="utf-8")
        handle = self.files[name]
        handle.write(json.dumps(value, default=str, ensure_ascii=False) + "\n")
        handle.flush()

    def close(self) -> None:
        for handle in self.files.values():
            handle.close()

    def submit(self, body: str | bytes, **kwargs: Any) -> tuple[RawEnvelope, Future]:
        self.sequence += 1
        raw = RawEnvelope.create(
            body=body, collection_id="twap-channel-comparison", session_id=self.session,
            sequence=self.sequence, **kwargs,
        )
        # The production connector excludes outbound auth and its acknowledgement.
        # Persist only price frames, never server error/auth echoes.
        msg = decode(raw.bytes())
        if isinstance(msg, dict) and msg.get("channel") == "price.crypto.twap":
            self.write("raw_polybolt.jsonl", asdict(raw))
        ack: Future = Future()
        ack.set_result(None)  # Frame is flushed before the connector projects it.
        return raw, ack

    async def observe(self, raw: RawEnvelope) -> None:
        for dataset, row in normalize(raw):
            if dataset == "reference_observations":
                if row.get("feed_id") != "chainlink:btcusd:twap:60":
                    continue
                self.write("ticks.jsonl", {
                    "channel": "polybolt", "source_ns": row["source_time"],
                    "received_ns": raw.received_at,
                    "received_mono_ns": raw.received_monotonic_ns,
                    "value": str(row["value"]), "delivery_kind": row["delivery_kind"],
                    "connection": raw.connection_id, "frame": raw.raw_record_id,
                    "precision_origin": row.get("precision_origin"),
                })
            elif dataset == "processing_results" and row.get("status") == "REJECTED":
                await self.event("normalization_rejected", source="polybolt")

    async def event(self, kind: str, **details: Any) -> None:
        self.health("polybolt", kind, details)
        if details.get("permanent"):
            self.stop.set()

    def health(self, channel: str, kind: str, details: dict) -> None:
        # No arbitrary exception/server text, API triplets or outbound frames in logs.
        allowed = {"source", "reason", "permanent", "generation", "connection_id",
                   "previous", "current", "timeout_s", "error", "topic", "symbol"}
        self.write("events.jsonl", {
            "channel": channel, "event": kind, "received_ns": time.time_ns(),
            **{k: v for k, v in details.items() if k in allowed},
        })

    def rtds_health(self, kind: str, details: dict) -> None:
        if self.stop.is_set() and kind in {"reconnecting", "heartbeat_timeout"}:
            kind = "shutdown_" + kind
        self.health("rtds", kind, details)

    def publish(self, event: Any) -> None:
        """Use the real live adapter's normalization, without a trading dispatcher."""
        if not isinstance(event, SettlementReferenceUpdated) or self.rtds_receipt is None:
            return
        wall, mono, frame = self.rtds_receipt
        meta = event.ingress
        self.write("ticks.jsonl", {
            "channel": "rtds", "source_ns": int(event.ts_event.timestamp()) * NS
            + event.ts_event.microsecond * 1000,
            "received_ns": wall, "received_mono_ns": mono,
            "normalizer_received_at": event.ts_received.isoformat(),
            "value": str(event.settlement.price),
            "delivery_kind": "live" if meta is None else meta.delivery_kind,
            "connection": None if meta is None else meta.connection_generation,
            "frame": frame,
        })

    @asynccontextmanager
    async def connect_rtds(self, url: str, **kwargs: Any):
        import websockets

        capture = self
        async with websockets.connect(url, **kwargs) as ws:
            class Socket:
                async def send(self, body: Any) -> None:
                    await ws.send(body)

                async def close(self) -> None:
                    await ws.close()

                async def recv(self) -> Any:
                    body = await ws.recv()
                    wall, mono = time.time_ns(), time.monotonic_ns()
                    capture.sequence += 1
                    capture.rtds_receipt = (wall, mono, capture.sequence)
                    with suppress(ValueError, TypeError):
                        msg = json.loads(body)
                        if isinstance(msg, dict) and msg.get("topic") == "crypto_prices_twap_sixty":
                            capture.write("raw_rtds.jsonl", {
                                "received_ns": wall, "received_mono_ns": mono,
                                "frame": capture.sequence,
                                "body": body.decode() if isinstance(body, bytes) else body,
                            })
                    return body

            yield Socket()


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()] if path.exists() else []


def summarize(output: Path) -> dict:
    ticks = load_rows(output / "ticks.jsonl")
    events = load_rows(output / "events.jsonl")
    first: dict[str, dict[int, dict]] = {"polybolt": {}, "rtds": {}}
    values: dict[tuple[str, int], set[Decimal]] = {}
    channels = {}
    for channel in first:
        rows = [r for r in ticks if r["channel"] == channel]
        live = [r for r in rows if r["delivery_kind"] == "live"]
        duplicates = 0
        backwards = 0
        last_ts = None
        for row in sorted(live, key=lambda r: r["received_mono_ns"]):
            ts = row["source_ns"]
            if last_ts is not None and ts < last_ts:
                backwards += 1
            last_ts = ts
            values.setdefault((channel, ts), set()).add(Decimal(row["value"]))
            if ts in first[channel]:
                duplicates += 1
            else:
                first[channel][ts] = row
        all_values: dict[int, set[Decimal]] = {}
        for row in rows:
            all_values.setdefault(row["source_ns"], set()).add(Decimal(row["value"]))
        raw = load_rows(output / f"raw_{channel}.jsonl")
        full_precision = 0
        for frame in raw:
            message = json.loads(frame["body_text"] if channel == "polybolt" else frame["body"])
            payload = message.get("payload") or {}
            points = payload.get("data", []) if "data" in payload else [payload]
            full_precision += sum(point.get("full_accuracy_value") is not None for point in points)
        channels[channel] = {
            "live_rows": len(live), "unique_live_timestamps": len(first[channel]),
            "history_rows": len(rows) - len(live), "duplicate_live_rows": duplicates,
            "out_of_order_live_rows": backwards,
            "conflicting_history_or_live_timestamps": sum(len(v) > 1 for v in all_values.values()),
            "raw_price_frames": len(raw), "raw_points_with_full_accuracy_value": full_precision,
            "normalized_price_frames": len({v.get("frame") for v in rows}),
            "conflicting_live_timestamps": sum(
                len(v) > 1 for (c, _), v in values.items() if c == channel),
            "source_to_receipt_host_clock": distribution([
                (r["received_ns"] - r["source_ns"]) / 1e6 for r in first[channel].values()]),
            "source_errors": sum(e["channel"] == channel and (
                e["event"] in {"source_error", "normalization_rejected", "heartbeat_timeout"}
                or (e["event"] == "reconnecting" and "error" in e)) for e in events),
            "connections": sum(e["channel"] == channel and e["event"] in
                               {"connected", "subscribed"} for e in events),
        }
    p, r = first["polybolt"], first["rtds"]
    shared = sorted(p.keys() & r.keys())
    pairs = []
    with localcontext() as ctx:
        ctx.prec = 80
        for ts in shared:
            delta = Decimal(r[ts]["value"]) - Decimal(p[ts]["value"])
            pairs.append({
                "source_ns": ts, "source_utc": iso(ts),
                "polybolt_value": p[ts]["value"], "rtds_value": r[ts]["value"],
                "rtds_minus_polybolt_value": str(delta), "exact_equal": delta == 0,
                "rtds_minus_polybolt_arrival_ms":
                (r[ts]["received_mono_ns"] - p[ts]["received_mono_ns"]) / 1e6,
            })
    if p and r:
        start, end = max(min(p), min(r)), min(max(p), max(r))
    else:
        start, end = 1, 0
    expected = set(range(((start + NS - 1) // NS) * NS, end + 1, NS))
    missing = {c: sorted(expected - set(first[c])) for c in first}
    boundary = []
    for ts in sorted(expected):
        if ts % (300 * NS):
            continue
        item: dict[str, Any] = {"source_utc": iso(ts)}
        for c in first:
            candidates = [v for v in ticks if v["channel"] == c and v["source_ns"] == ts]
            candidate = min(candidates, key=lambda v: v["received_mono_ns"]) if candidates else None
            if candidate:
                lag = (candidate["received_ns"] - ts) / 1e6
                history = candidate["delivery_kind"] != "live"
                item[c] = {"value": candidate["value"], "arrival_ms": lag,
                           "delivery_kind": candidate["delivery_kind"],
                           "within_policy_bound_using_host_clock":
                           0 <= lag <= (30000 if history else 5000)}
        boundary.append(item)
    mismatch = sum(not v["exact_equal"] for v in pairs)
    conflicts = sum(v["conflicting_history_or_live_timestamps"] for v in channels.values())
    only = {c: sorted((set(first[c]) - set(first[other])) & expected)
            for c, other in [("polybolt", "rtds"), ("rtds", "polybolt")]}
    shared_gaps = sorted(expected - set(p) - set(r))
    if len(pairs) < 10:
        verdict = "INSUFFICIENT_DATA"
    elif mismatch or conflicts or any(only.values()):
        verdict = "DIFFERENCES_OBSERVED"
    elif shared_gaps:
        verdict = "MATCHED_VALUES_WITH_SHARED_GAPS"
    else:
        verdict = "MATCHED_ON_OBSERVED_INTERVAL"
    report = {
        "verdict": verdict, "not_a_strategy_qualification": True,
        "channels": channels, "matched_live_timestamps": len(pairs),
        "exact_value_mismatches": mismatch,
        "max_absolute_value_difference": str(max(
            (abs(Decimal(v["rtds_minus_polybolt_value"])) for v in pairs), default=Decimal(0))),
        "relative_arrival_rtds_minus_polybolt": distribution([
            v["rtds_minus_polybolt_arrival_ms"] for v in pairs]),
        "common_live_source_interval": {"start_utc": iso(start), "end_utc": iso(end)}
        if start <= end else None,
        "missing_at_assumed_1s_cadence": {
            c: {"count": len(v), "source_times_utc": [iso(ts) for ts in v]}
            for c, v in missing.items()},
        "live_timestamps_seen_only_on_one_channel": {
            c: [iso(ts) for ts in v] for c, v in only.items()},
        "shared_cadence_gaps_utc": [iso(ts) for ts in shared_gaps],
        "five_minute_boundaries": boundary,
        "notes": [
            "Exact source timestamps only; no interpolation or nearest-price matching.",
            "First live arrival is compared; history is excluded from live latency and coverage.",
            "Positive relative arrival means RTDS arrived later; both use one monotonic clock.",
            "Absolute lag includes publication lag and host clock error, not just network.",
            "Missing counts use the overlap of live source ranges; startup/end tails are excluded.",
            "One-second cadence is an explicit diagnostic assumption, not a provider guarantee.",
            "Matching samples do not prove future feed values or availability equivalent.",
            "Tiny exact-value differences are reported, without assuming a trading impact.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    fields = ["source_ns", "source_utc", "polybolt_value", "rtds_value",
              "rtds_minus_polybolt_value", "exact_equal", "rtds_minus_polybolt_arrival_ms"]
    with (output / "pairs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(pairs)
    lines = ["# TWAP channel comparison", "", f"Result: **{verdict}**", "",
             f"Matched live source timestamps: {len(pairs)}.",
             f"Exact value mismatches: {mismatch}; conflicting live timestamps: {conflicts}.",
             f"Timestamps seen on just one channel: {sum(len(v) for v in only.values())}; "
             f"cadence gaps shared by both: {len(shared_gaps)}.",
             "", "Relative arrival, milliseconds (RTDS minus PolyBolt):",
             json.dumps(report["relative_arrival_rtds_minus_polybolt"]), ""]
    for c, info in channels.items():
        lines.append(f"- {c}: {info['unique_live_timestamps']} live timestamps, "
                     f"{info['history_rows']} history ticks, "
                     f"{len(missing[c])} missing overlap seconds, "
                     f"{info['source_errors']} source errors.")
    lines += ["", *report["notes"], "", "Files: ticks.jsonl, raw_*.jsonl, events.jsonl, "
              "pairs.csv and summary.json. Raw frames preserve message formatting/precision."]
    (output / "report.md").write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    return report


async def capture_channels(output: Path, seconds: float, dotenv: Path) -> dict:
    credentials = load_credentials(dotenv, required=True)
    capture = Capture(output)
    adapter = RtdsChainlinkAdapter(
        connect=capture.connect_rtds, on_health=capture.rtds_health, heartbeat_timeout_s=25)
    start_wall, start_mono = time.time_ns(), time.monotonic_ns()
    capture.write("run.jsonl", {"event": "started", "started_utc": iso(start_wall),
                               "requested_seconds": seconds, "trading_enabled": False,
                               "rtds_heartbeat_timeout_s": 25,
                               "polybolt_liveness_s": capture.profile.reference.liveness_s})
    tasks = [asyncio.create_task(record_stream(capture, "polybolt", "reference",
                                              credentials=credentials)),
             asyncio.create_task(adapter.run(capture))]
    try:
        await asyncio.wait_for(capture.stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
    finally:
        capture.stop.set()
        await adapter.stop()
        done, pending = await asyncio.wait(tasks, timeout=5)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        capture.write("run.jsonl", {
            "event": "stopped", "ended_utc": iso(time.time_ns()),
            "wall_minus_monotonic_elapsed_ms":
            ((time.time_ns() - start_wall) - (time.monotonic_ns() - start_mono)) / 1e6,
            "task_error_types": [type(t.exception()).__name__ for t in done
                                 if not t.cancelled() and t.exception() is not None],
        })
        capture.close()
    return summarize(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=1200,
                        help="Capture duration (default 1200).")
    parser.add_argument("--dotenv", type=Path, default=ROOT / ".env")
    parser.add_argument("--out", type=Path, help="New empty output directory.")
    parser.add_argument("--analyze", type=Path, help="Recompute reports from a previous capture.")
    args = parser.parse_args()
    if args.analyze:
        report = summarize(args.analyze.resolve())
        output = args.analyze.resolve()
    else:
        if not 0 < args.seconds <= 86400:
            parser.error("--seconds must be positive and at most 86400")
        output = args.out or ROOT / "var/observation/twap_channel_compare" / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
        if output.exists() and any(output.iterdir()):
            parser.error("output directory must be empty; recordings are never mixed")
        output.mkdir(parents=True, exist_ok=True)
        print(f"Read-only TWAP comparison for {args.seconds:g}s; output: {output}", flush=True)
        try:
            report = asyncio.run(capture_channels(output, args.seconds, args.dotenv))
        except KeyboardInterrupt:
            report = summarize(output)
        except Exception as exc:
            print(f"Capture could not finish: {type(exc).__name__}. No trading was started.")
            return 2
    print(json.dumps({"verdict": report["verdict"], "pairs": report["matched_live_timestamps"],
                      "value_mismatches": report["exact_value_mismatches"],
                      "report": str(output / "report.md")}, indent=2))
    return 0 if report["verdict"] == "MATCHED_ON_OBSERVED_INTERVAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
