"""Source decoding with terminal accounting for every raw payload item."""

from __future__ import annotations

from typing import Any, Callable

from ..contracts import (
    PARSER_VERSION,
    SCHEMA_VERSION,
    RawEnvelope,
    canonical,
    decode,
    digest,
    epoch_ns,
    exact,
    identity,
    instrument_id,
    market_id,
)
from ..families.btc_5m import normalized_families
from ..families.event_driven import EventQuestion

FAMILIES = [*normalized_families(), EventQuestion()]


def match_family(item: dict):
    for family in FAMILIES:
        if family.matches(item):
            return family
    return None


EXTERNAL_VALIDATORS: dict[tuple[str, str], Callable[[dict], None]] = {}


def register_external(name: str, version: str, validator: Callable[[dict], None]) -> None:
    if (name, version) in EXTERNAL_VALIDATORS:
        raise ValueError("duplicate external payload schema")
    EXTERNAL_VALIDATORS[name, version] = validator


def envelope(
    raw: RawEnvelope,
    path: str,
    dataset: str,
    source_time: Any = None,
    precision: str = "ms",
    history: bool = False,
) -> dict:
    return {
        "record_id": identity(raw.raw_record_id, path, dataset, PARSER_VERSION),
        "raw_record_id": raw.raw_record_id,
        "payload_path": path,
        "collection_id": raw.collection_id,
        "session_id": raw.session_id,
        "connection_id": raw.connection_id,
        "ingest_sequence": raw.ingest_sequence,
        "source_id": raw.source_id,
        "stream_id": raw.stream_id,
        "source_time": epoch_ns(source_time, precision),
        "source_time_precision": precision if source_time is not None else None,
        "received_at": raw.received_at,
        "received_monotonic_ns": raw.received_monotonic_ns,
        "clock_sample_id": raw.clock_sample_id,
        "delivery_kind": "subscription_history"
        if history
        else ("rest_observation" if raw.transport == "http" else "live"),
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "validation_status": "VALID",
        "validation_reasons": [],
    }


def levels(items: list, *, reverse: bool, probability: bool = False) -> list:
    result = []
    seen = set()
    for item in items:
        price, size = (item["price"], item["size"]) if isinstance(item, dict) else item
        p, q = exact(price), exact(size)
        if p < 0 or q < 0 or (probability and p > 1) or p in seen:
            raise ValueError("invalid or duplicate book level")
        seen.add(p)
        result.append({"price": p, "quantity": q})
    return sorted(result, key=lambda row: row["price"], reverse=reverse)


def normalize(raw: RawEnvelope) -> list[tuple[str, dict]]:
    result: list[tuple[str, dict]] = []
    try:
        payload = decode(raw.bytes())
    except (ValueError, UnicodeError):
        status = "CONTROL" if raw.bytes().strip() in (b"PONG", b"PING", b"pong") else "REJECTED"
        return [
            (
                "processing_results",
                {
                    **envelope(raw, "$", "processing_results"),
                    "status": status,
                    "emitted_record_ids": [],
                    "emitted_datasets": [],
                    "reasons": [] if status == "CONTROL" else ["invalid_json"],
                },
            )
        ]
    items = list(enumerate(payload)) if isinstance(payload, list) else [(None, payload)]
    if not items:
        items = [(None, [])]
    for index, item in items:
        path = "$" if index is None else f"$[{index}]"
        emitted: list[tuple[str, dict]] = []
        status, reasons = "NORMALIZED", []
        try:
            if raw.http_status is not None and not 200 <= raw.http_status < 300:
                status = "CONTROL"
            elif not isinstance(item, dict):
                status = (
                    "CONTROL"
                    if (item == [] or raw.endpoint_id in ("binance_time", "clob_time"))
                    else "UNKNOWN_TYPE"
                )
            else:
                emitted = dispatch(raw, path, item)
                if not emitted:
                    status = (
                        "CONTROL"
                        if (
                            any(k in item for k in ("op", "ping", "pong"))
                            or raw.endpoint_id in ("binance_time", "clob_time", "resolutions")
                            or raw.source_id == "gamma"
                        )
                        else "UNKNOWN_TYPE"
                    )
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            status, reasons = "REJECTED", [type(exc).__name__]
            emitted = []
        result.extend(emitted)
        result.append(
            (
                "processing_results",
                {
                    **envelope(raw, path, "processing_results"),
                    "status": status,
                    "emitted_record_ids": [r["record_id"] for _, r in emitted],
                    "emitted_datasets": sorted({d for d, _ in emitted}),
                    "reasons": reasons,
                },
            )
        )
    return result


def dispatch(raw: RawEnvelope, path: str, item: dict) -> list[tuple[str, dict]]:
    rows = []

    def emit(dataset: str, values: dict, suffix: str = "", **timing: Any) -> None:
        rows.append((dataset, {**envelope(raw, path + suffix, dataset, **timing), **values}))

    if raw.transport == "collector_event" and not (
            raw.source_id == "clob" and raw.stream_id == "captured_applied_rest"):
        emit(
            "operational_events",
            {
                "event_type": item.get("type", "unknown"),
                "reason": item.get("reason"),
                "details": canonical(item),
            },
        )
        if item.get("type") == "feed_binding":
            emit("feed_bindings", item["binding"])
        if item.get("type") == "feed_definition":
            emit("feeds", item["feed"])
        if item.get("type") == "clock_sample":
            emit("clock_observations", item["sample"])
        return rows
    if raw.source_id == "gamma":
        if "markets" in item and "id" not in item:
            for n, market in enumerate(item["markets"]):
                rows.extend(dispatch(raw, f"{path}.markets[{n}]", market))
            return rows
        if "markets" in item:
            event_id = f"polymarket:event:{item['id']}"
            emit(
                "events",
                {
                    "event_id": event_id,
                    "venue_event_id": str(item["id"]),
                    "first_seen_record_id": raw.raw_record_id,
                },
            )
            for n, market in enumerate(item["markets"]):
                rows.extend(dispatch(raw, f"{path}.markets[{n}]", market))
                condition = market.get("conditionId")
                if condition:
                    emit(
                        "entity_relationships",
                        {
                            "relationship_id": identity(event_id, condition),
                            "parent_type": "event",
                            "parent_id": event_id,
                            "child_type": "market",
                            "child_id": market_id(condition),
                            "relationship_type": "event-market",
                            "operation": "OBSERVED",
                        },
                        f".relationships[{n}]",
                    )
            for n, series in enumerate(item.get("series", [])):
                series_id = f"polymarket:series:{series['id']}"
                emit(
                    "series",
                    {
                        "series_id": series_id,
                        "venue_series_id": str(series["id"]),
                        "first_seen_record_id": raw.raw_record_id,
                    },
                    f".series[{n}]",
                )
                emit(
                    "entity_relationships",
                    {
                        "relationship_id": identity(series_id, event_id),
                        "parent_type": "series",
                        "parent_id": series_id,
                        "child_type": "event",
                        "child_id": event_id,
                        "relationship_type": "series-event",
                        "operation": "OBSERVED",
                    },
                    f".series_relationships[{n}]",
                )
            return rows
        family = match_family(item)
        if family is None:
            return []
        entity = family.identify(item)
        mid = entity["market_id"]
        emit(
            "markets",
            {
                "market_id": mid,
                "condition_id": entity["condition_id"],
                "venue_market_id": entity["venue_market_id"],
                "family": entity["family"],
                "underlying_asset": entity.get("underlying_asset"),
                "duration_s": entity.get("duration_s"),
                "first_seen_record_id": raw.raw_record_id,
            },
        )
        for n, token in enumerate(entity["instruments"]):
            emit(
                "instruments",
                {
                    **token,
                    "market_id": mid,
                    "chain_id": "137",
                    "price_unit": "USD_per_share",
                    "quantity_unit": "shares",
                    "first_seen_record_id": raw.raw_record_id,
                },
                f".instruments[{n}]",
            )
        emit_metadata(emit, mid, item, entity)
        return rows
    if raw.source_id == "clob" and raw.endpoint_id == "clob_market":
        condition = str(
            item.get("condition_id")
            or item.get("conditionId")
            or decode(raw.query_redacted or "{}").get("condition", "")
        )
        if not condition:
            raise ValueError("missing CLOB condition")
        emit_metadata(emit, market_id(condition), item, {})
        return rows
    if raw.source_id == "clob":
        kind = item.get("event_type", "book" if "bids" in item and "asks" in item else "")
        ts = item.get("timestamp")
        if kind == "book":
            emit(
                "book_snapshots",
                {
                    "instrument_id": instrument_id(str(item["asset_id"])),
                    "market_id": market_id(str(item["market"])),
                    "snapshot_origin": (
                        "captured_rest_syncing" if item.get("capture_sync_health") == "SYNCING"
                        else "captured_rest_ready")
                    if raw.stream_id == "captured_applied_rest"
                    and raw.transport == "collector_event"
                    else ("rest_audit" if raw.transport == "http" else "websocket"),
                    "depth_scope": "published_aggregated",
                    "provider_book_hash": item.get("hash"),
                    "bids": levels(item["bids"], reverse=True, probability=True),
                    "asks": levels(item["asks"], reverse=False, probability=True),
                },
                source_time=ts,
            )
        elif kind == "price_change":
            changes = []
            for n, change in enumerate(item["price_changes"]):
                p, q = exact(change["price"]), exact(change["size"])
                if not 0 <= p <= 1 or q < 0 or change["side"] not in ("BUY", "SELL"):
                    raise ValueError("invalid book update")
                changes.append(
                    {
                        "item_index": n,
                        "instrument_id": instrument_id(str(change["asset_id"])),
                        "side": change["side"],
                        "price": p,
                        "new_quantity": q,
                        "provider_book_hash": change.get("hash"),
                        "reported_best_bid": exact(change["best_bid"])
                        if change.get("best_bid")
                        else None,
                        "reported_best_ask": exact(change["best_ask"])
                        if change.get("best_ask")
                        else None,
                    }
                )
            emit(
                "book_updates",
                {"market_id": market_id(str(item["market"])), "changes": changes},
                source_time=ts,
            )
        elif kind == "last_trade_price":
            emit(
                "trade_prints",
                {
                    "instrument_id": instrument_id(str(item["asset_id"])),
                    "trade_kind": "public_print",
                    "price": exact(item["price"]),
                    "quantity": exact(item["size"]) if item.get("size") is not None else None,
                    "reported_side": item.get("side"),
                    "transaction_hash": item.get("transaction_hash"),
                    "reported_fee_information": canonical(
                        {"fee_rate_bps": item.get("fee_rate_bps")}
                    ),
                },
                source_time=ts,
            )
        elif kind == "best_bid_ask":
            emit(
                "quote_observations",
                {
                    "instrument_id": instrument_id(str(item["asset_id"])),
                    **{
                        k: exact(item[k]) if item.get(k) is not None else None
                        for k in ("best_bid", "best_ask")
                    },
                    "reported_spread": exact(item["spread"])
                    if item.get("spread") is not None
                    else None,
                },
                source_time=ts,
            )
        elif kind in ("tick_size_change", "new_market", "market_resolved"):
            emit(
                "market_changes",
                {
                    "entity_type": "token" if item.get("asset_id") else "market",
                    "entity_id": instrument_id(str(item["asset_id"]))
                    if item.get("asset_id")
                    else market_id(str(item.get("market") or item.get("id"))),
                    "change_type": kind,
                    "old_value": canonical(item.get("old_tick_size")),
                    "new_value": canonical(item),
                    "provider_event_type": kind,
                },
                source_time=ts,
            )
        return rows
    if raw.source_id == "polybolt":
        if "payload" not in item:
            return []
        if item.get("channel") != "price.crypto.twap":
            raise ValueError("unexpected reference channel")
        payload = item["payload"]
        points = payload.get("data", []) if item.get("snapshot") else [payload]
        for n, point in enumerate(points):
            source = point.get("source", payload.get("source"))
            symbol = point.get("symbol", payload.get("symbol"))
            if source != "chainlink" or symbol != "btcusd":
                raise ValueError("reference vendor/symbol mismatch")
            value = point.get("full_accuracy_value", point.get("value"))
            emit(
                "reference_observations",
                {
                    "feed_id": "chainlink:btcusd:twap:60",
                    "value": exact(value),
                    "unit": "USD",
                    "measurement_type": "TWAP",
                    "aggregation_window_s": 60,
                    "provider_dropped_count": item.get("dropped"),
                    "precision_origin": "full_accuracy_value"
                    if "full_accuracy_value" in point
                    else "value",
                    "provider_sequence": str(item["seq"]) if item.get("seq") is not None else None,
                    "provider_sequence_scope": (
                        f"{raw.session_id}/{raw.connection_id}/{item['channel']}"
                    ),
                },
                f".payload[{n}]",
                source_time=point.get("timestamp"),
                history=bool(item.get("snapshot")),
            )
        return rows
    if raw.source_id == "rtds":
        rtds_reference_rows(raw, item, emit)
        return rows
    if raw.source_id == "binance":
        event = item.get("data", item)
        iid = "binance:spot:BTCUSDT"
        if "lastUpdateId" in event:
            emit(
                "book_snapshots",
                {
                    "instrument_id": iid,
                    "snapshot_origin": "rest_bootstrap",
                    "depth_scope": "bootstrap_5000_plus_observed_updates",
                    "depth_limit": 5000,
                    "provider_update_id": str(event["lastUpdateId"]),
                    "bids": levels(event["bids"], reverse=True),
                    "asks": levels(event["asks"], reverse=False),
                },
            )
        elif event.get("e") == "trade":
            emit(
                "trade_prints",
                {
                    "instrument_id": iid,
                    "provider_trade_id": str(event["t"]),
                    "trade_kind": "individual",
                    "price": exact(event["p"]),
                    "quantity": exact(event["q"]),
                    "buyer_is_maker": event["m"],
                },
                source_time=event.get("T"),
            )
        elif event.get("e") == "depthUpdate":
            changes = []
            for side, field in (("BUY", "b"), ("SELL", "a")):
                for price, size in event[field]:
                    p, q = exact(price), exact(size)
                    if p < 0 or q < 0:
                        raise ValueError("negative level")
                    changes.append(
                        {
                            "item_index": len(changes),
                            "instrument_id": iid,
                            "side": side,
                            "price": p,
                            "new_quantity": q,
                        }
                    )
            emit(
                "book_updates",
                {
                    "first_update_id": str(event["U"]),
                    "last_update_id": str(event["u"]),
                    "changes": changes,
                },
                source_time=event.get("E"),
            )
        return rows
    if raw.source_id == "resolution":
        for n, state in enumerate(item.get("data", [])):
            emit(
                "resolution_observations",
                {
                    "market_id": market_id(str(state["condition_id"])),
                    "provider_status": state.get("status"),
                    "payouts": [exact(p) for p in state.get("payouts", [])],
                    "payout_unit": "UNKNOWN",  # W0 must qualify units/status enums before labels.
                    "resolved_at": epoch_ns(state.get("resolved_at"), "iso"),
                    "transaction_hash": state.get("transaction_hash"),
                    "resolved_block": str(state["resolved_block"])
                    if state.get("resolved_block")
                    else None,
                    "additional_fields": canonical(state),
                },
                f".data[{n}]",
            )
        return rows
    if raw.source_id == "external":
        key = (item["payload_schema_id"], item["payload_schema_version"])
        if key not in EXTERNAL_VALIDATORS:
            return []
        EXTERNAL_VALIDATORS[key](item["payload"])
        emit("external_observations", {**item, "payload": canonical(item["payload"])})
    return rows


def emit_metadata(emit: Callable, mid: str, item: dict, entity: dict) -> None:
    content_hash = digest(canonical(item))
    tick = item.get("mts", item.get("minimum_tick_size", item.get("orderPriceMinTickSize")))
    minimum = item.get("mos", item.get("minimum_order_size", item.get("orderMinSize")))
    emit(
        "metadata_versions",
        {
            "metadata_version_id": identity(mid, content_hash),
            "entity_type": "market",
            "entity_id": mid,
            "content_hash": content_hash,
            "title": item.get("title"),
            "question": item.get("question"),
            "description": item.get("description"),
            "rules_text": item.get("description"),
            "window_start": entity.get("window_start"),
            "window_end": entity.get("window_end"),
            "resolution_source": item.get("resolutionSource"),
            "rule_fingerprint": entity.get("rule_fingerprint"),
            "active": item.get("active"),
            "closed": item.get("closed"),
            "accepting_orders": item.get("acceptingOrders", item.get("accepting_orders")),
            "enable_order_book": item.get("enableOrderBook", item.get("enable_order_book")),
            "negative_risk": item.get("negRisk", item.get("neg_risk")),
            "tick_size": exact(tick) if tick is not None else None,
            "minimum_order_size_value": exact(minimum) if minimum is not None else None,
            # Polymarket's orderMinSize / mos is a number of shares (outcome tokens).
            "minimum_order_size_unit": "shares" if minimum is not None else "UNKNOWN",
            "fees_enabled": item.get("feesEnabled"),
            "fee_schedule": canonical(
                item.get("fd", item.get("fee_schedule", item.get("feeSchedule")))
            ),
            "reward_configuration": canonical(item.get("r", item.get("rewards"))),
            "family_attributes": canonical(entity),
            "additional_fields": canonical(item),
        },
    )


RTDS_TWAP_FEED_ID = "polymarket_rtds:crypto_prices_twap_sixty:btc/usd"


def rtds_reference_rows(raw: RawEnvelope, item: dict, emit: Callable) -> None:
    """Polymarket RTDS ``crypto_prices_twap_sixty`` frames, parsed by the live adapter's
    own functions (``rtds_normalize``): a live tick, or the subscription-history snapshot
    (each point flagged ``history`` with the snapshot's actual receipt time)."""
    from datetime import datetime, timezone

    from tyrex_pm.adapters.polymarket.rtds_normalize import (
        normalize_chainlink_tick,
        normalize_twap_history,
    )

    received = datetime.fromtimestamp(raw.received_at / 1e9, tz=timezone.utc)
    counter = iter(range(1, 1_000_000))
    common = dict(ts_received=received, receive_monotonic_ns=raw.received_monotonic_ns,
                  connection_generation=int(raw.connection_id or 0), clock_uncertainty_ms=None)
    events = normalize_twap_history(item, next_sequence=lambda: next(counter), **common)
    history = bool(events)
    if not events:
        tick = normalize_chainlink_tick(item, ingress_sequence=max(1, raw.ingest_sequence),
                                        **common)
        events = [] if tick is None else [tick]
    for n, event in enumerate(events):
        point = event.settlement
        payload = item.get("payload") or {}
        exact_origin = ("full_accuracy_value" if (
            "full_accuracy_value" in payload
            or any("full_accuracy_value" in p for p in payload.get("data") or [])
        ) else "value")
        emit(
            "reference_observations",
            {
                "feed_id": RTDS_TWAP_FEED_ID,
                "value": point.price,
                "unit": "USD",
                "measurement_type": "TWAP",
                "aggregation_window_s": 60,
                "provider_dropped_count": None,
                "precision_origin": exact_origin,
                "provider_sequence": None if item.get("timestamp") is None
                else str(item.get("timestamp")),
                "provider_sequence_scope": f"{raw.session_id}/{raw.connection_id}/rtds",
            },
            f".payload[{n}]",
            source_time=int(round(point.ts_event.timestamp() * 1000)),
            history=history,
        )
