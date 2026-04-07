"""Fill-triggered position snapshots (INT-ST-02)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

EmitFn = Callable[[str, dict[str, Any]], None]


def emit_position_snapshot(
    reader: Any | None,
    *,
    instrument_id_str: str,
    token_id: str | None,
    mark_price: float | None,
    correlation_id: str | None,
    trigger: str,
    emit: EmitFn | None,
) -> None:
    """
    Emit a ``position`` fact when a ``NautilusPositionStateReader`` is wired.

    ``net_exposure_usd`` is best-effort (requires ``token_id`` + ``mark_price`` for
    :meth:`~tyrex_pm.runtime.state_readers.NautilusPositionStateReader.filled_exposure_usd_best_effort`).
    """
    if emit is None or not instrument_id_str:
        return
    net: float | None = None
    if reader is not None and token_id and mark_price is not None:
        try:
            fn = getattr(reader, "filled_exposure_usd_best_effort", None)
            if callable(fn):
                net = fn(token_id, mark_price)
        except Exception:  # noqa: BLE001
            net = None
    pl: dict[str, Any] = {"instrument_id": instrument_id_str}
    if token_id:
        pl["token_id"] = token_id
    if mark_price is not None:
        pl["mark_price"] = mark_price
    if correlation_id:
        pl["correlation_id"] = correlation_id
    pl["trigger"] = trigger
    if net is not None:
        pl["net_exposure_usd"] = net
    emit("position", pl)
