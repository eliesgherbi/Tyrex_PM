"""Independent PTB reference resolution for commissioning (post-window attestation)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Polymarket BTC Up/Down 5m resolves on Chainlink Data Streams BTC/USD per market description.
# Phase A commissioning uses the Ethereum mainnet Chainlink BTC/USD aggregator V3 as an
# independent on-chain reference (distinct from Polymarket RTDS relay and Tyrex sidecar path).
INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3 = "chainlink_aggregator_v3_eth_mainnet_btc_usd"
BTC_USD_AGGREGATOR = "0xF4030086522a5bEEa6518e28D4d8B4E0E4688E3"
AGGREGATOR_DECIMALS = 8

# latestRoundData() selector
_LATEST_ROUND_SELECTOR = "0xfeaf968c"
# getRoundData(uint80) selector head
_GET_ROUND_SELECTOR = "0x9a6fc8f5"


@dataclass(frozen=True)
class IndependentReferenceResult:
    source: str
    available: bool
    price: str | None
    round_timestamp: float | None
    event_start_ts: float | None
    lag_ms: float | None
    fail_reason: str | None
    endpoint: str
    field_name: str
    why_independent: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "available": self.available,
            "price": self.price,
            "round_timestamp": self.round_timestamp,
            "event_start_ts": self.event_start_ts,
            "lag_ms": self.lag_ms,
            "fail_reason": self.fail_reason,
            "endpoint": self.endpoint,
            "field_name": "answer",
            "why_independent": self.why_independent,
        }


def _rpc_url() -> str | None:
    return os.environ.get("TYREX_ETH_RPC_URL") or os.environ.get("ETH_RPC_URL")


def _eth_call(*, to: str, data: str, rpc_url: str) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(rpc_url, json=payload)
        resp.raise_for_status()
        body = resp.json()
    result = body.get("result")
    return str(result) if result else None


def _decode_latest_round(hex_data: str) -> tuple[int, int, int, int, int] | None:
    if not hex_data or hex_data in {"0x", "0x0"}:
        return None
    raw = hex_data[2:]
    if len(raw) < 64 * 5:
        return None
    words = [int(raw[i : i + 64], 16) for i in range(0, 64 * 5, 64)]
    return words[0], words[1], words[2], words[3], words[4]


def fetch_independent_ptb_reference(
    *,
    event_start_ts: float,
    max_round_lag_s: float = 7200.0,
) -> IndependentReferenceResult:
    """Fetch independent Chainlink BTC/USD reference nearest to event_start_ts."""
    rpc = _rpc_url()
    why = (
        "Reads Ethereum mainnet Chainlink BTC/USD aggregator V3 via JSON-RPC eth_call; "
        "not Polymarket RTDS, not Tyrex sidecar, not Tyrex live boundary selection path."
    )
    if not rpc:
        return IndependentReferenceResult(
            source=INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
            available=False,
            price=None,
            round_timestamp=None,
            event_start_ts=event_start_ts,
            lag_ms=None,
            fail_reason="TYREX_ETH_RPC_URL not configured",
            endpoint="eth_call",
            field_name="answer",
            why_independent=why,
        )
    try:
        hex_data = _eth_call(to=BTC_USD_AGGREGATOR, data=_LATEST_ROUND_SELECTOR, rpc_url=rpc)
        decoded = _decode_latest_round(hex_data or "")
        if decoded is None:
            return IndependentReferenceResult(
                source=INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
                available=False,
                price=None,
                round_timestamp=None,
                event_start_ts=event_start_ts,
                lag_ms=None,
                fail_reason="latestRoundData decode failed",
                endpoint=rpc,
                field_name="answer",
                why_independent=why,
            )
        _round_id, answer, _started, updated_at, _answered_in = decoded
        updated_f = float(updated_at)
        lag_ms = abs(updated_f - float(event_start_ts)) * 1000.0
        if abs(updated_f - float(event_start_ts)) > max_round_lag_s:
            return IndependentReferenceResult(
                source=INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
                available=False,
                price=None,
                round_timestamp=updated_f,
                event_start_ts=event_start_ts,
                lag_ms=lag_ms,
                fail_reason=f"nearest round too far from boundary ({lag_ms:.0f}ms)",
                endpoint=rpc,
                field_name="answer",
                why_independent=why,
            )
        price = Decimal(answer) / Decimal(10**AGGREGATOR_DECIMALS)
        return IndependentReferenceResult(
            source=INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
            available=True,
            price=str(price),
            round_timestamp=updated_f,
            event_start_ts=event_start_ts,
            lag_ms=lag_ms,
            fail_reason=None,
            endpoint=rpc,
            field_name="answer",
            why_independent=why,
        )
    except Exception as exc:
        log.warning("independent PTB reference fetch failed: %s", exc)
        return IndependentReferenceResult(
            source=INDEPENDENT_SOURCE_CHAINLINK_AGGREGATOR_V3,
            available=False,
            price=None,
            round_timestamp=None,
            event_start_ts=event_start_ts,
            lag_ms=None,
            fail_reason=f"{type(exc).__name__}: {exc}",
            endpoint=rpc or "eth_call",
            field_name="answer",
            why_independent=why,
        )


def write_independent_reference_artifact(path: Any, result: IndependentReferenceResult) -> None:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
