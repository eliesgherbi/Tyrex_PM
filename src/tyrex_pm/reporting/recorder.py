"""FactRecorder protocol (REC-02)."""

from __future__ import annotations

from typing import Any, Protocol


class FactRecorder(Protocol):
    def emit(self, fact_type: str, payload: dict[str, Any]) -> None: ...


class NoOpFactRecorder:
    __slots__ = ()

    def emit(self, fact_type: str, payload: dict[str, Any]) -> None:
        _ = (fact_type, payload)


_NO_OP = NoOpFactRecorder()


def no_op_recorder() -> FactRecorder:
    return _NO_OP
