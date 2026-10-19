"""Captured live run (raw capture) -> warehouse collection -> decision reconstruction.

A signal-only observation recorded with ``recording.raw_inputs`` holds every input the
runtime received, in receipt order, with receipt wall and monotonic times
(``recording/raw.py``). This module:

1. **imports** the capture into its own warehouse collection
   (``var/warehouse_captures``) through ``RawWriter`` and the existing normalizers,
   keeping each record's actual receipt times and order. Source formats:

   =================  =========================================================
   RTDS TWAP          byte-exact frames; warehouse ``rtds`` normalizer (live parser)
   Binance trades     byte-exact frames; existing ``binance`` normalizer
   CLOB market WS     SDK event dumps rewritten in venue-message form
                      (``sdk_to_venue_message``), existing ``clob`` normalizer
   CLOB REST book     SDK payload dump in REST form (audit only: book reconstruction
                      anchors on websocket snapshots, as for collector data)
   Gamma metadata     SDK-converted event dict; each market gets the event slug the
                      family matcher keys on (``gamma_markets_from_sdk``)
   clock              live ``ClockSyncSnapshot`` as a clock observation applied
                      exactly as live (``clock_sync.LIVE_SNAPSHOT_SOURCE``)
   connections        CLOB connect/disconnect per binding epoch, feed health
   =================  =========================================================

2. **reconstructs the trader's decisions** for every window the capture evaluated:
   ``run_window`` from the capture start (same warm-up as live), evaluating at the
   captured evaluation instants, never executing intents (observation context stays
   flat), favorable (zero-delay) scenario, the live reference-availability rule.
3. **compares** them with the decisions the run recorded, evaluation by evaluation.

Simulating alternative fills is a different question (``run_window`` with a scenario
and execution); it needs no capture-specific code and is not claimed here.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from tyrex_pm.data_warehouse.contracts import RawEnvelope, digest
from tyrex_pm.recording.records import iter_records

from .clock_sync import LIVE_SNAPSHOT_SOURCE

REPO = Path(__file__).resolve().parents[4]
CAPTURE_WAREHOUSE = REPO / "var" / "warehouse_captures"
IMPORT_VERSION = "capture_import_v1"
COMPARE_FIELDS = ("action", "reason", "chosen_side")
NUMERIC_FIELDS = {"p_up_point": 1e-9, "sigma": 1e-12, "sealed_k": 1e-9, "up_ask": 0,
                  "up_bid": 0, "down_ask": 0, "down_bid": 0, "tau_s": 1.0}
FIDELITY = {
    "reference_twap": "byte_exact",
    "binance_trades": "byte_exact",
    "binance_depth": "byte_exact",
    "clob_market": "sdk_model_rewritten_as_venue_message",
    "clob_rest_book": "sdk_model_rewritten_as_rest_book (audit only)",
    "market_metadata": "sdk_converted_gamma_event (+event slug per market)",
    "clock": "live_clock_snapshot",
    "connections": "host_event",
    "bindings": "host_event",
    "evaluations": "host_event (evaluation instants)",
}


# -- format adapters -------------------------------------------------------------------

def _iso_to_ms(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value)
    if text.isdigit():
        return text
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return str(int(round(stamp.timestamp() * 1000)))


def sdk_to_venue_message(dump: dict) -> dict | None:
    """A Polymarket SDK market-channel event dump -> the venue's websocket message."""
    kind = str(dump.get("type") or "")
    payload = dict(dump.get("payload") or {})
    ts = _iso_to_ms(payload.get("timestamp"))
    if kind == "book":
        return {"event_type": "book", "asset_id": payload["token_id"],
                "market": payload["market"], "bids": payload.get("bids") or [],
                "asks": payload.get("asks") or [], "hash": payload.get("hash"),
                "timestamp": ts}
    if kind == "price_change":
        return {"event_type": "price_change", "market": payload["market"], "timestamp": ts,
                "price_changes": [
                    {"asset_id": c["token_id"], "price": c["price"], "size": c["size"],
                     "side": c["side"], "hash": c.get("hash"), "best_bid": c.get("best_bid"),
                     "best_ask": c.get("best_ask")}
                    for c in payload.get("price_changes") or []]}
    if kind == "last_trade_price":
        return {"event_type": "last_trade_price", "asset_id": payload["token_id"],
                "market": payload.get("market"), "price": payload.get("price"),
                "size": payload.get("size"), "side": payload.get("side"),
                "fee_rate_bps": payload.get("fee_rate_bps"),
                "transaction_hash": payload.get("transaction_hash"), "timestamp": ts}
    if kind == "tick_size_change":
        return {"event_type": "tick_size_change", "asset_id": payload.get("token_id"),
                "market": payload.get("market"), "old_tick_size": payload.get("old_tick_size"),
                "new_tick_size": payload.get("new_tick_size"), "timestamp": ts}
    if kind:
        return {"event_type": kind, **payload, "timestamp": ts}
    return None


def rest_book_from_sdk(dump: dict, market_by_token: dict[str, str]) -> dict | None:
    book = dump.get("book") or {}
    token = str(dump.get("token_id") or (book.get("instrument_id") or {}).get("value") or "")
    if not token:
        return None
    return {"asset_id": token, "market": market_by_token.get(token, ""),
            "bids": [{"price": lv["price"], "size": lv["quantity"]}
                     for lv in book.get("bids") or []],
            "asks": [{"price": lv["price"], "size": lv["quantity"]}
                     for lv in book.get("asks") or []],
            "timestamp": _iso_to_ms(book.get("ts_event")), "hash": dump.get("hash"),
            "tick_size": dump.get("tick_size"), "min_order_size": dump.get("min_order_size")}


def gamma_markets_from_sdk(event: dict) -> dict:
    """SDK-converted Gamma event -> markets the warehouse family matcher identifies."""
    markets = []
    for market in event.get("markets") or []:
        item = dict(market)
        item.setdefault("slug", event.get("slug"))
        item.setdefault("endDate", event.get("endDate"))
        markets.append(item)
    return {"markets": markets}


def clock_sample_from_snapshot(snapshot: dict) -> dict:
    measured = datetime.fromisoformat(str(snapshot["measured_at_wall_utc"]))
    return {
        "sample_id": snapshot.get("snapshot_id"),
        "measurement_source": f"{LIVE_SNAPSHOT_SOURCE}:{snapshot['sync_status']}",
        "status": "OBSERVED",
        "estimated_offset_ns": int(round(float(snapshot["estimated_offset_ms"]) * 1e6)),
        "uncertainty_ns": int(snapshot["uncertainty_ms"]) * 1_000_000,
        "server_at": int(measured.timestamp() * 1e9),
        "request_at": None, "response_at": None, "rtt_ns": None,
    }


# -- import ----------------------------------------------------------------------------

def _envelope(record: dict, body: str, *, collection: str, session: str, sequence: int,
              source: str, stream: str, transport: str, **request: Any) -> RawEnvelope:
    data = body.encode("utf-8")
    return RawEnvelope(
        raw_record_id=f"{session}:{sequence}", collection_id=collection, session_id=session,
        ingest_sequence=sequence, source_id=source, stream_id=stream,
        connection_id=None if record.get("connection_id") is None
        else str(record["connection_id"]),
        received_at=int(record["received_at"]),
        received_monotonic_ns=int(record["received_monotonic_ns"]),
        transport=transport, body_text=body, body_base64=None, body_sha256=digest(data),
        **request)


def capture_envelopes(recording_dir: Path, *, collection: str, session: str,
                      stats: dict) -> Iterator[RawEnvelope]:
    market_by_token: dict[str, str] = {}
    sequence = 0
    for record in iter_records(recording_dir, "raw"):
        kind = record.get("input_class")
        stats.setdefault("captured", {}).setdefault(kind, 0)
        stats["captured"][kind] += 1
        body: str | None = None
        source = stream = transport = ""
        request: dict[str, Any] = {}
        if kind in ("reference_twap", "binance_trades", "binance_depth"):
            body = record.get("body_text")
            if body is None and record.get("body_base64"):
                stats.setdefault("binary_skipped", 0)
                stats["binary_skipped"] += 1
                continue
            if not body or body in ("PING", "PONG"):
                stats.setdefault("control_frames_skipped", 0)
                stats["control_frames_skipped"] += 1
                continue
            source = "rtds" if kind == "reference_twap" else "binance"
            stream, transport = f"capture-{kind}", "websocket"
        elif kind == "clob_market":
            message = sdk_to_venue_message(record.get("body_json") or {})
            if message is None:
                continue
            body, source, stream, transport = json.dumps(message), "clob", "capture-clob", (
                "websocket")
        elif kind == "clob_rest_book":
            item = rest_book_from_sdk(record.get("body_json") or {}, market_by_token)
            if item is None:
                continue
            body, source, stream, transport = json.dumps(item), "clob", "clob_books", "http"
            request = {"endpoint_id": "clob_books", "http_status": 200}
        elif kind == "market_metadata":
            body = json.dumps(gamma_markets_from_sdk(record.get("body_json") or {}))
            source, stream, transport = "gamma", "gamma_events", "http"
            request = {"endpoint_id": "gamma_events", "http_status": 200}
        elif kind in ("clock", "connections", "bindings", "evaluations"):
            event = dict(record.get("body_json") or {})
            if kind == "clock":
                event = {"type": "clock_sample",
                         "sample": clock_sample_from_snapshot(event["snapshot"])}
            elif kind == "bindings":
                binding = event.get("binding") or {}
                for token in (binding.get("up_token_id"), binding.get("down_token_id")):
                    if token:
                        market_by_token[str(token)] = str(binding.get("condition_id"))
                event = {"type": f"capture_{event.get('type')}", "details": event}
            elif kind == "connections" and event.get("feed") == "clob_market":
                # Same connection id as the CLOB records of that binding epoch.
                event = {**event, "source": "clob",
                         "connection_id": f"{event.get('binding_id')}:"
                                          f"{event.get('connection_epoch')}"}
            elif kind == "connections":
                event = {**event, "type": f"feed_{event.get('type')}"}
            elif kind == "evaluations":
                event = {**event, "type": "capture_evaluation"}
            body, source, stream, transport = json.dumps(event, default=str), "collector", (
                "operations"), "collector_event"
        else:
            stats.setdefault("unknown_classes", {}).setdefault(str(kind), 0)
            stats["unknown_classes"][str(kind)] += 1
            continue
        sequence += 1
        yield _envelope(record, body, collection=collection, session=session,
                        sequence=sequence, source=source, stream=stream, transport=transport,
                        **request)


def capture_identity(capture_dir: Path) -> str:
    raw = sorted((Path(capture_dir) / "recording" / "raw").glob("*.manifest.json"))
    payload = [json.loads(p.read_text(encoding="utf-8"))["sha256"] for p in raw]
    return hashlib.sha256(json.dumps([IMPORT_VERSION, payload]).encode()).hexdigest()[:16]


def import_capture(capture_dir: Path, *, root: Path = CAPTURE_WAREHOUSE) -> dict:
    """Write the capture into its own collection and normalize it (idempotent)."""
    from tyrex_pm.data_warehouse.capture.writer import RawWriter
    from tyrex_pm.data_warehouse.config import Storage
    from tyrex_pm.data_warehouse.normalize.worker import normalize_once

    capture_dir = Path(capture_dir)
    identity = capture_identity(capture_dir)
    collection = f"capture-{identity}"
    root.mkdir(parents=True, exist_ok=True)
    collection_dir = root / "collections" / collection
    report_path = collection_dir / "import.json"
    if report_path.exists():
        return json.loads(report_path.read_text(encoding="utf-8"))
    collection_dir.mkdir(parents=True, exist_ok=True)
    profile = {"kind": "captured_live_run", "import_version": IMPORT_VERSION,
               "capture": str(capture_dir), "capture_identity": identity}
    profile_hash = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    (collection_dir / "collection.json").write_text(json.dumps({
        "collection_id": collection, "created_at": time.time_ns(), "profile": profile,
        "profile_hash": profile_hash, "source_contract_version": IMPORT_VERSION,
        "schema_version": "1", "parser_version": "1"}, indent=1), encoding="utf-8")
    session = uuid.uuid4().hex
    writer = RawWriter(root, collection, session, profile_hash, Storage())
    stats: dict[str, Any] = {}
    futures = []
    try:
        for envelope in capture_envelopes(capture_dir / "recording", collection=collection,
                                          session=session, stats=stats):
            futures.append(writer.submit_envelope(envelope))
            if len(futures) >= 5000:
                for future in futures:
                    future.result(timeout=120)
                futures.clear()
        for future in futures:
            future.result(timeout=120)
    finally:
        writer.close()
    normalized = normalize_once(root, collection)
    report = {"collection": collection, "root": str(root), "capture": str(capture_dir),
              "capture_identity": identity, "import_version": IMPORT_VERSION,
              "records_written": writer.durable, "stats": stats, "normalize": normalized,
              "fidelity": FIDELITY}
    report_path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return report


# -- reconstruction --------------------------------------------------------------------

def _captured_evaluations(capture_dir: Path) -> dict[str, list[int]]:
    instants: dict[str, list[int]] = {}
    for record in iter_records(Path(capture_dir) / "recording", "raw"):
        if record.get("input_class") != "evaluations":
            continue
        body = record.get("body_json") or {}
        instants.setdefault(str(body.get("window_id")), []).append(int(record["received_at"]))
    return instants


def _capture_start(capture_dir: Path) -> int:
    for record in iter_records(Path(capture_dir) / "recording", "raw"):
        return int(record["received_at"])
    raise ValueError("empty capture")


def _live_decisions(capture_dir: Path) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    path = Path(capture_dir) / "decisions.jsonl"
    if not path.exists():
        raise FileNotFoundError("only signal-only observation captures are reconstructed "
                                "(decisions.jsonl); a trading run also needs its execution "
                                "state from the execution journal")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows.setdefault(str(row.get("window_id")), []).append(row)
    return rows


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except ArithmeticError:
        return None


def compare_decisions(live: list[dict], replayed: list[dict]) -> dict:
    """Evaluation-by-evaluation comparison (same order: one row per evaluation)."""
    mismatches: list[dict] = []
    per_field: dict[str, int] = {}
    for index, (a, b) in enumerate(zip(live, replayed)):
        diffs = {}
        for name in COMPARE_FIELDS:
            if (a.get(name) or None) != (b.get(name) or None):
                diffs[name] = (a.get(name), b.get(name))
        for name, tolerance in NUMERIC_FIELDS.items():
            x, y = _num(a.get(name)), _num(b.get(name))
            if (x is None) != (y is None) or (x is not None and abs(x - y) > tolerance):
                diffs[name] = (a.get(name), b.get(name))
        for name in diffs:
            per_field[name] = per_field.get(name, 0) + 1
        if diffs:
            mismatches.append({"index": index, "live_at_ns": a.get("at_ns"),
                               "replay_at_ns": b.get("at_ns"), "diffs": diffs})
    same_decision = sum(
        1 for a, b in zip(live, replayed)
        if all((a.get(n) or None) == (b.get(n) or None) for n in COMPARE_FIELDS))
    return {"live_evaluations": len(live), "replayed_evaluations": len(replayed),
            "compared": min(len(live), len(replayed)),
            "decision_equal": same_decision,
            "fully_equal": min(len(live), len(replayed)) - len(mismatches),
            "field_mismatch_counts": per_field, "first_mismatches": mismatches[:8]}


def reconstruct(capture_dir: Path, *, out: Path | None = None,
                root: Path = CAPTURE_WAREHOUSE) -> dict:
    """Import (once) and reconstruct every evaluated window of an observation capture."""
    from tyrex_pm.data_warehouse.query.reader import Reader
    from tyrex_pm.research.replay.config import load_scenario
    from tyrex_pm.strategies.registry import get_strategy_plugin

    from .loader import WindowCatalog, load_window
    from .run import ReplayExecutionPolicy, run_window

    capture_dir = Path(capture_dir)
    imported = import_capture(capture_dir, root=root)
    manifest = json.loads((capture_dir / "observation_manifest.json").read_text("utf-8"))
    plugin = get_strategy_plugin(manifest["strategy_kind"])
    config = plugin.load_config(dict(manifest["strategy_parameters"]),
                                f"observe:{manifest['run_name']}")
    completeness = json.loads((capture_dir / "recording" / "raw_completeness.json")
                              .read_text(encoding="utf-8"))
    catalog = WindowCatalog(Reader(Path(imported["root"]), imported["collection"]))
    windows = {ref.slug: ref for ref in catalog.windows()}
    instants = _captured_evaluations(capture_dir)
    live = _live_decisions(capture_dir)
    start = _capture_start(capture_dir)
    scenario = replace(load_scenario(REPO / "config/research/scenarios/favorable.yaml"),
                       name="capture_reconstruction")
    policy = ReplayExecutionPolicy(reference_availability="reference_availability_v1",
                                   execution_contract="replay_execution_contract_v2")
    results = []
    for window_id, moments in sorted(instants.items()):
        ref = windows.get(window_id)
        if ref is None:
            results.append({"window_id": window_id, "error": "no gamma metadata imported"})
            continue
        window = load_window(catalog, ref.market_id,
                             warmup_s=max(0.0, (ref.window_start_ns - start) / 1e9),
                             after_s=30.0)
        started = time.perf_counter()
        result = run_window(window, strategy_config=config, scenario=scenario,
                            strategy_kind=manifest["strategy_kind"],
                            maximum_trade_age_ms=None,
                            clock_observations=catalog.clock_observations_for(
                                window.range_start_ns, window.range_end_ns),
                            execution_policy=policy, evaluation_instants=moments,
                            observe_only=True)
        comparison = compare_decisions(live.get(window_id, []), result["decisions"])
        results.append({
            "window_id": window_id, "market_id": ref.market_id,
            "range_start_ns": window.range_start_ns, "evaluations": len(moments),
            "replay_seconds": round(time.perf_counter() - started, 1),
            "seals": result["seals"],
            "reference_series": result["provenance"]["replayed_reference_series"],
            "clock_correction": result["provenance"]["clock_correction"],
            "recommendations_live": sum(1 for r in live.get(window_id, [])
                                        if r.get("action") == "ENTER"),
            "recommendations_replayed": len(result.get("recommendations", [])),
            "comparison": comparison,
        })
    equal = all(r.get("comparison", {}).get("fully_equal") == r.get("comparison", {}).get(
        "live_evaluations") == r.get("comparison", {}).get("replayed_evaluations")
        for r in results) and bool(results)
    report = {
        "capture": str(capture_dir), "import": {k: imported[k] for k in (
            "collection", "root", "capture_identity", "records_written", "normalize",
            "fidelity")},
        "raw_completeness": {k: completeness.get(k) for k in (
            "inputs_captured", "missing_classes", "not_in_input_contract", "capture_fault")},
        "scenario": "favorable (zero delays), observe_only, captured evaluation instants",
        "execution_policy": policy.identity(),
        "windows": results,
        # Decisions reconstructed equal to the live ones at every captured evaluation
        # (decision reconstruction). Fill simulation is a separate question.
        "decisions_reconstructed_equal": equal,
    }
    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "reconstruction.json").write_text(json.dumps(report, indent=1, default=str),
                                                 encoding="utf-8")
    return report
