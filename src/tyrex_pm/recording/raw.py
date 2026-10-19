"""Raw input capture for live and observation runs (integration plan P5).

Every inbound input is captured as received, together with its receive wall time and
monotonic time, its source and stream, and its connection id. Capture goes into the
``raw`` stream of the run's ``Recorder``, so the bounds, accounting, failure exposure
and close behaviour are those of the recording core.

Fidelity is declared per input (``body_kind``):

* ``byte_exact``: the frame exactly as the socket delivered it (RTDS, Binance trades,
  Binance depth, clock HTTP body);
* ``sdk_model``: the venue SDK hands Tyrex typed objects only (CLOB market websocket,
  REST book bootstrap, Gamma discovery). These are captured as a faithful JSON dump of
  that object. They are not the venue's bytes;
* ``host_event``: facts the host produces (connection lifecycle, clock samples, market
  bindings and promotions, evaluation instants), in the warehouse collector-event shape.

``completeness()`` reports, per input class, what was captured and how faithfully, and
whether a run can be reconstructed decision for decision. Reconstructing decisions
(feeds, warm-up, clock, connection events, metadata, evaluation instants) is a separate
question from simulating fills, which also needs execution and account state. That
state is kept in the execution journal, not in raw capture.
"""

from __future__ import annotations

import base64
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable

RAW_SCHEMA = "tyrex_raw_input_v1"

# Input classes needed to reconstruct a decision (integration plan amendment 3).
INPUT_CLASSES = {
    "reference_twap": ("rtds", "byte_exact"),
    "binance_trades": ("binance", "byte_exact"),
    "binance_depth": ("binance", "byte_exact"),
    "clob_market": ("clob", "sdk_model"),
    "clob_rest_book": ("clob", "sdk_model"),
    "market_metadata": ("gamma", "sdk_model"),
    "clock": ("collector", "host_event"),
    "connections": ("collector", "host_event"),
    "bindings": ("collector", "host_event"),
    "evaluations": ("collector", "host_event"),
}

# Known gaps for exact replay of a captured run with the current loader. Reported in
# the completeness manifest instead of being papered over.
REPLAY_GAPS = (
    "rtds: the warehouse has no normalizer for crypto_prices_twap_sixty; replay reads "
    "the polybolt reference series",
    "clob: market websocket and REST book are SDK objects, not venue bytes; the "
    "warehouse clob normalizer expects venue JSON",
    "gamma: discovery returns SDK-converted event dicts, not the raw Gamma response",
    "binance depth: live uses @depth20 partial snapshots; the warehouse normalizes "
    "@depth diff streams only",
)


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):  # pydantic SDK models
        try:
            return _jsonable(dump(mode="json"), depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value), depth + 1)
    if hasattr(value, "__dict__"):
        return {k: _jsonable(v, depth + 1) for k, v in vars(value).items()
                if not k.startswith("_")}
    return str(value)


class RawCapture:
    """Tee for raw inputs into the run's recorder ``raw`` stream. Never raises."""

    def __init__(self, recorder: Any, *, stream: str = "raw",
                 on_fault: Callable[[str], None] | None = None) -> None:
        self.recorder = recorder
        self.stream = stream
        self.on_fault = on_fault
        self.sequence = 0
        self.captured: dict[str, int] = {}
        self.capture_errors = 0
        self.fault: str | None = None
        # Inputs the strategy's input contract does not use (adapter not started).
        self.not_expected: set[str] = set()

    def not_in_contract(self, *input_classes: str) -> None:
        self.not_expected.update(input_classes)

    def _put(self, input_class: str, record: dict) -> None:
        if self.fault is not None:
            return
        try:
            self.sequence += 1
            record["seq"] = self.sequence
            record["input_class"] = input_class
            if self.recorder.capture(self.stream, record,
                                     capture_ns=record.get("received_at")):
                self.captured[input_class] = self.captured.get(input_class, 0) + 1
        except Exception as exc:  # noqa: BLE001 - capture never stops the feeds
            self.capture_errors += 1
            self.fault = f"{type(exc).__name__}: {exc}"
            if self.on_fault is not None:
                try:
                    self.on_fault(self.fault)
                except Exception:  # noqa: BLE001, S110
                    pass

    def message(self, input_class: str, body: Any, *, connection: Any = None,
                transport: str = "websocket", body_kind: str | None = None,
                received_at: int | None = None, received_mono: int | None = None,
                **extra: Any) -> None:
        """One inbound message (``str``/``bytes`` are captured byte-exact)."""
        source, default_kind = INPUT_CLASSES.get(input_class, ("unknown", "sdk_model"))
        record: dict[str, Any] = {
            "schema": RAW_SCHEMA, "source": source, "stream": input_class,
            "connection_id": None if connection is None else str(connection),
            "received_at": time.time_ns() if received_at is None else received_at,
            "received_monotonic_ns": (time.monotonic_ns() if received_mono is None
                                      else received_mono),
            "transport": transport,
        }
        if isinstance(body, (bytes, bytearray)):
            try:
                record["body_text"] = bytes(body).decode("utf-8")
            except UnicodeDecodeError:
                record["body_base64"] = base64.b64encode(bytes(body)).decode("ascii")
            record["body_kind"] = body_kind or "byte_exact"
        elif isinstance(body, str):
            record["body_text"] = body
            record["body_kind"] = body_kind or "byte_exact"
        else:
            record["body_json"] = _jsonable(body)
            record["body_kind"] = body_kind or default_kind
        record.update({k: _jsonable(v) for k, v in extra.items()})
        self._put(input_class, record)

    def event(self, input_class: str, kind: str, payload: dict | None = None) -> None:
        """A host fact in the warehouse collector-event shape (``{"type": ...}``)."""
        self._put(input_class, {
            "schema": RAW_SCHEMA, "source": "collector", "stream": input_class,
            "connection_id": None, "received_at": time.time_ns(),
            "received_monotonic_ns": time.monotonic_ns(), "transport": "collector_event",
            "body_kind": "host_event",
            "body_json": {"type": kind, **_jsonable(dict(payload or {}))},
        })

    def sink(self, input_class: str, **extra: Any) -> Callable[[Any, Any], None]:
        """Adapter hook ``(raw_frame, connection_id)``, called before parsing."""
        def capture(raw: Any, connection: Any = None) -> None:
            self.message(input_class, raw, connection=connection, **extra)
        return capture

    def completeness(self, *, expected: tuple[str, ...] | None = None) -> dict:
        expected = tuple(n for n in (expected or INPUT_CLASSES) if n not in self.not_expected)
        classes = {}
        for name in expected:
            source, kind = INPUT_CLASSES.get(name, ("unknown", "unknown"))
            classes[name] = {"source": source, "fidelity": kind,
                             "records": self.captured.get(name, 0)}
        missing = [n for n, c in classes.items() if c["records"] == 0]
        not_exact = [n for n, c in classes.items() if c["fidelity"] == "sdk_model"]
        return {
            "schema": RAW_SCHEMA,
            "classes": classes,
            "not_in_input_contract": sorted(self.not_expected),
            "missing_classes": missing,
            "sdk_model_classes": not_exact,
            "capture_fault": self.fault,
            "capture_errors": self.capture_errors,
            # Every needed class was captured without a fault (an absent class, e.g. a
            # feed outside the strategy's input contract, is listed under missing).
            "inputs_captured": not missing and self.fault is None,
            # Byte-exact replay of the decision path through the warehouse loader.
            "decision_complete": False,
            "decision_complete_reasons": list(REPLAY_GAPS),
            "fill_simulation": "needs execution/account state: execution journal, "
                               "not raw capture",
        }
