"""Normalize official CLOB cancel responses. Never treat HTTP 200 as full success."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _value(model: Any, name: str, default: Any = None) -> Any:
    if isinstance(model, dict):
        return model.get(name, default)
    return getattr(model, name, default)


def _is_already_matched(reason: str) -> bool:
    return "already matched" in reason.lower()


def _is_not_found(reason: str) -> bool:
    text = reason.lower()
    return "not found" in text or "does not exist" in text


def _is_delay_window(reason: str) -> bool:
    text = reason.lower()
    return "delay" in text or "pending" in text or "cannot be cancelled" in text


@dataclass(frozen=True)
class CancelResult:
    canceled: tuple[str, ...]
    not_canceled: tuple[tuple[str, str], ...]

    @property
    def matched_instead(self) -> tuple[str, ...]:
        return tuple(
            order_id
            for order_id, reason in self.not_canceled
            if _is_already_matched(reason) and not _is_not_found(reason)
        )

    @property
    def delay_pending(self) -> tuple[str, ...]:
        return tuple(order_id for order_id, reason in self.not_canceled if _is_delay_window(reason))

    @property
    def fully_cleared(self) -> bool:
        return not self.not_canceled or set(self.matched_instead) | {
            order_id for order_id, reason in self.not_canceled if _is_not_found(reason)
        } == {order_id for order_id, _reason in self.not_canceled}


def cancel_result_from_sdk(raw: Any) -> CancelResult:
    canceled_raw = _value(raw, "canceled", ()) or ()
    not_canceled_raw = _value(raw, "not_canceled", {}) or {}
    canceled = tuple(str(item) for item in canceled_raw if str(item).strip())
    if isinstance(not_canceled_raw, Mapping):
        leftover = tuple(
            (str(order_id), str(reason))
            for order_id, reason in not_canceled_raw.items()
            if str(order_id).strip()
        )
    else:
        leftover = ()
    return CancelResult(canceled=canceled, not_canceled=leftover)
