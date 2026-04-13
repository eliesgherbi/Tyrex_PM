"""Layer A shared types — signal-side only (no Nautilus imports)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from tyrex_pm.core.types import GuruTradeSignal

Branch = Literal["entry", "exit"]


def json_safe_metadata(d: dict[str, Any]) -> dict[str, Any]:
    """Restrict to JSON-serializable scalars and nested dicts."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        sk = str(k)
        if v is None or isinstance(v, (str, int, float, bool)):
            out[sk] = v
        elif isinstance(v, dict):
            out[sk] = json_safe_metadata(dict(v))
        elif isinstance(v, (list, tuple)):
            out[sk] = [
                json_safe_metadata(dict(x)) if isinstance(x, dict) else x
                for x in v
                if x is None or isinstance(x, (str, int, float, bool, dict))
            ]
        else:
            out[sk] = str(v)
    return out


@dataclass(frozen=True, slots=True)
class LayerAOutcome:
    accept: bool
    reason_code: str
    detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", json_safe_metadata(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class LayerAStepRecord:
    filter_name: str
    branch: str
    accept: bool
    reason_code: str
    detail: str | None
    metadata: dict[str, Any]

    def as_fact_payload(self, correlation_id: str) -> dict[str, Any]:
        md = json_safe_metadata(dict(self.metadata))
        return {
            "correlation_id": correlation_id,
            "filter_name": self.filter_name,
            "branch": self.branch,
            "accept": self.accept,
            "reason_code": self.reason_code,
            "detail": self.detail if self.detail is not None else "",
            "metadata": md,
        }


@runtime_checkable
class LayerAContext(Protocol):
    """Injected runtime boundary for follower position (``full_exit``)."""

    def follower_long_qty_for_outcome_token(self, token_id: str) -> float | None:
        """
        Resolved long quantity for outcome ``token_id`` (``>= 0``).

        ``None`` means instrument/cache resolution failed (unresolved).
        ``0.0`` means flat long — caller maps to no-position deny for ``full_exit``.
        Must not raise for normal resolution paths; see exit filter for unreadable handling.
        """


class LayerAFilter(Protocol):
    """Single Layer A step (gating or interpretation)."""

    name: str

    def evaluate(
        self,
        sig: GuruTradeSignal,
        *,
        branch: Branch,
        ctx: LayerAContext | None,
    ) -> LayerAOutcome: ...
