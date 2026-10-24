"""Exact wire-independent identities, serialization, and reception contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

SCHEMA_VERSION = "1"
PARSER_VERSION = "2"  # 2: metadata minimum order size unit recorded as shares
QUALITY_VERSION = "4"


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda v: str(v) if isinstance(v, Decimal) else asdict(v),
        allow_nan=False,
    )


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def identity(*parts: Any) -> str:
    return digest(canonical(parts))


def decode(body: str | bytes) -> Any:
    def invalid(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(body, parse_float=Decimal, parse_constant=invalid)


def exact(value: Any) -> Decimal:
    if isinstance(value, (float, bool)) or value is None:
        raise ValueError("exact decimal requires decimal, integer, or string")
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("non-finite decimal")
        with localcontext() as ctx:
            ctx.prec = 110
            scaled = number.scaleb(30)
            if scaled != scaled.to_integral_value() or abs(scaled) >= Decimal(10) ** 76:
                raise ValueError("decimal does not fit Decimal256(76,30) exactly")
        return number
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc


def epoch_ns(value: Any, unit: str = "ms") -> int | None:
    if value is None or value == "":
        return None
    if unit == "iso":
        text = str(value)
        fractional = re.search(r"\.(\d+)(?=Z|[+-]\d{2}:?\d{2}$)", text)
        fractional_ns = 0
        if fractional:
            digits = fractional[1]
            if len(digits) > 9:
                raise ValueError("ISO timestamp exceeds nanosecond precision")
            fractional_ns = int(digits.ljust(9, "0"))
            text = text[: fractional.start()] + text[fractional.end() :]
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timestamp must have timezone")
        delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + fractional_ns
    multipliers = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1000, "ns": 1}
    number = Decimal(str(value)) * multipliers[unit]
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("timestamp not an exact nanosecond")
    result = int(number)
    if not -(2**63) < result < 2**63:
        raise ValueError("timestamp outside int64")
    return result


def market_id(condition: str) -> str:
    return f"polymarket:condition:{condition}"


def instrument_id(token: str) -> str:
    return f"polymarket:137:outcome:{token}"


@dataclass(frozen=True)
class RawEnvelope:
    raw_record_id: str
    collection_id: str
    session_id: str
    ingest_sequence: int
    source_id: str
    stream_id: str
    connection_id: str | None
    received_at: int
    received_monotonic_ns: int
    transport: str
    body_text: str | None
    body_base64: str | None
    body_sha256: str
    request_id: str | None = None
    method: str | None = None
    endpoint_id: str | None = None
    query_redacted: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    request_started_at: int | None = None
    response_headers: str | None = None
    clock_sample_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        body: str | bytes,
        collection_id: str,
        session_id: str,
        sequence: int,
        source: str,
        stream: str,
        connection: str | None = None,
        transport: str = "websocket",
        received_at: int | None = None,
        **request: Any,
    ) -> RawEnvelope:
        # Application bytes only; outbound authentication frames never enter this method.
        payload = body.encode("utf-8") if isinstance(body, str) else body
        try:
            body_text, body_base64 = payload.decode("utf-8"), None
        except UnicodeDecodeError:
            body_text, body_base64 = None, base64.b64encode(payload).decode("ascii")
        return cls(
            f"{session_id}:{sequence}",
            collection_id,
            session_id,
            sequence,
            source,
            stream,
            connection,
            time.time_ns() if received_at is None else received_at,
            time.monotonic_ns(),
            transport,
            body_text,
            body_base64,
            digest(payload),
            **request,
        )

    def bytes(self) -> bytes:
        if (self.body_text is None) == (self.body_base64 is None):
            raise ValueError("raw envelope must have exactly one body representation")
        data = (
            self.body_text.encode("utf-8")
            if self.body_text is not None
            else base64.b64decode(self.body_base64, validate=True)
        )
        if digest(data) != self.body_sha256:
            raise ValueError("raw body checksum mismatch")
        if self.raw_record_id != f"{self.session_id}:{self.ingest_sequence}":
            raise ValueError("raw identity mismatch")
        return data


def file_digest(path) -> str:
    """Hash immutable files without allocating their full contents."""
    import hashlib

    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
