"""
Polymarket CLOB execution: `OrderIntent` → signed LIMIT via py-clob-client.

Isolates venue I/O from strategy. **Live only** — shadow runs use `NoOpExecutionPort`.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent

_log = logging.getLogger(__name__)


class PolymarketExecutionPolicy:
    """
    LIMIT orders at `intent.price_ref` (from guru trade). Fails if price is missing.

    **Latency:** each submit is a synchronous HTTP round-trip (~50–200ms typical);
    keep strategy path non-blocking beyond this call — future: queue + worker.

    **Minimum notional:** optional floor via `TYREX_MIN_BUY_NOTIONAL_USD` (default ``1``)
    for BUY, aligned with observed CLOB rejection messages.
    """

    def __init__(
        self,
        client: ClobClient,
        runtime: RuntimeSettings,
        *,
        on_submit_ok: Any | None = None,
    ) -> None:
        self._client = client
        self._runtime = runtime
        self._on_submit_ok = on_submit_ok

    def submit_intent(self, intent: OrderIntent, *, mode: str) -> None:
        if mode != "live":
            return
        if intent.price_ref is None:
            _log.warning(
                "event=%s component=polymarket_exec correlation_id=%s detail=missing_price",
                ReasonCode.LIVE_ORDER_ERROR,
                intent.correlation_id,
            )
            return

        qty = float(intent.quantity)
        price = float(intent.price_ref)
        side = intent.side.upper()
        if side == "BUY":
            min_n = float(os.environ.get("TYREX_MIN_BUY_NOTIONAL_USD", "1"))
            if min_n > 0 and price * qty + 1e-9 < min_n:
                _log.warning(
                    "event=%s correlation_id=%s est=%s min=%s",
                    ReasonCode.LIVE_ORDER_ERROR,
                    intent.correlation_id,
                    price * qty,
                    min_n,
                )
                return

        fee_bps = self._client.get_fee_rate_bps(intent.token_id)
        order_args = OrderArgs(
            token_id=intent.token_id,
            price=price,
            size=qty,
            side=side,
            fee_rate_bps=fee_bps,
        )
        try:
            resp = self._client.create_and_post_order(order_args)
        except Exception as exc:
            _log.exception(
                "event=%s correlation_id=%s err=%s",
                ReasonCode.LIVE_ORDER_ERROR,
                intent.correlation_id,
                exc,
            )
            return

        oid = _extract_order_id(resp)
        payload = json.dumps(resp) if isinstance(resp, dict) else str(resp)
        if len(payload) > 400:
            payload = payload[:400] + "…"
        _log.info(
            "event=%s correlation_id=%s order_id_prefix=%s response_prefix=%s",
            ReasonCode.LIVE_ORDER_SUBMIT,
            intent.correlation_id,
            oid[:18] + "…" if len(oid) > 20 else oid,
            payload,
        )
        if self._on_submit_ok:
            self._on_submit_ok(intent)


def _extract_order_id(response: object) -> str:
    if not isinstance(response, dict):
        return ""
    for key in ("orderID", "orderId", "order_id", "id"):
        val = response.get(key)
        if val is not None:
            return str(val)
    return ""
