"""Order-metadata (tick size, fee, minimum) warming and refresh."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from tyrex_pm.runtime.host.shared import (
    _ORDER_METADATA_REFRESH_LEAD_S,
    _ORDER_METADATA_REFRESH_MAX_ATTEMPTS,
    _ORDER_METADATA_RETRY_BASE_S,
    _ORDER_METADATA_RETRY_MAX_S,
    _ORDER_METADATA_WARM_AMOUNT,
    _ORDER_METADATA_WARM_MAX_PRICE,
    _ORDER_METADATA_WARM_TTL_S,
)


class OrderMetadataMixin:
    """Order-metadata (tick size, fee, minimum) warming and refresh.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def _market_token_ids(self, market_session: Any) -> tuple[str, ...]:
        market = market_session.market
        return (market.yes.token_id.value, market.no.token_id.value)

    def _token_metadata_is_warm(self, token_id: str, *, now: float | None = None) -> bool:
        warmed_at = self._order_metadata_warmed_at.get(token_id)
        if warmed_at is None:
            return False
        clock = time.monotonic() if now is None else now
        return (clock - warmed_at) <= _ORDER_METADATA_WARM_TTL_S

    def _refresh_order_metadata_capability(self) -> None:
        if self.active_market is None:
            self.capabilities.order_metadata_ready = False
            self._record_readiness_if_changed()
            return
        now = time.monotonic()
        ready = all(
            self._token_metadata_is_warm(token_id, now=now)
            for token_id in self._market_token_ids(self.active_market)
        )
        self.capabilities.order_metadata_ready = ready
        self._record_readiness_if_changed()

    def _token_metadata_refresh_due(self, token_id: str, now: float) -> bool:
        warmed_at = self._order_metadata_warmed_at.get(token_id)
        if warmed_at is None:
            return True
        return (now - warmed_at) >= _ORDER_METADATA_WARM_TTL_S - _ORDER_METADATA_REFRESH_LEAD_S

    def _schedule_order_metadata_warm(
        self, market_session: Any, *, refresh_ahead: bool = False
    ) -> None:
        """Warm order metadata for a market's tokens.

        Without ``refresh_ahead`` only tokens that are not warm are fetched (market prepare /
        activation). With it, tokens close to expiry are refreshed too, so an active market never
        loses readiness mid-window. At most one request per token is in flight; failures back off
        and stop after a bounded number of attempts (entries then stay blocked).
        """
        token_ids = self._market_token_ids(market_session)
        now = time.monotonic()
        pending = tuple(
            token_id
            for token_id in token_ids
            if token_id not in self._order_metadata_inflight
            and (
                self._token_metadata_refresh_due(token_id, now)
                if refresh_ahead
                else not self._token_metadata_is_warm(token_id, now=now)
            )
            and now >= self._order_metadata_retry_after.get(token_id, 0.0)
            and self._order_metadata_failures.get(token_id, 0)
            < _ORDER_METADATA_REFRESH_MAX_ATTEMPTS
        )
        self._refresh_order_metadata_capability()
        if not pending:
            return
        self._order_metadata_inflight.update(pending)
        if refresh_ahead:
            self.run_recorder.record(
                "ORDER_METADATA_REFRESH_SCHEDULED",
                {
                    "token_ids": list(pending),
                    "reason": [
                        "missing_or_expired"
                        if not self._token_metadata_is_warm(token_id, now=now)
                        else "expiring"
                        for token_id in pending
                    ],
                },
            )

        async def _run() -> None:
            try:
                await self._warm_order_metadata(pending)
            except Exception as exc:  # noqa: BLE001 - readiness owns failure
                self._record_runtime_error("order_metadata_warm", exc)
                for token_id in pending:
                    self._order_metadata_warmed_at.pop(token_id, None)
                    self._note_order_metadata_failure(token_id)
                self._refresh_order_metadata_capability()
            finally:
                self._order_metadata_inflight.difference_update(pending)

        task = asyncio.create_task(_run(), name="order-metadata-warm")
        self._order_metadata_warm_tasks.add(task)
        task.add_done_callback(self._order_metadata_warm_tasks.discard)

    def _note_order_metadata_failure(self, token_id: str) -> None:
        failures = self._order_metadata_failures.get(token_id, 0) + 1
        self._order_metadata_failures[token_id] = failures
        delay = min(
            _ORDER_METADATA_RETRY_MAX_S, _ORDER_METADATA_RETRY_BASE_S * (2 ** (failures - 1))
        )
        self._order_metadata_retry_after[token_id] = time.monotonic() + delay
        if failures == _ORDER_METADATA_REFRESH_MAX_ATTEMPTS:
            self.run_recorder.record(
                "ORDER_METADATA_REFRESH_EXHAUSTED",
                {"token_id": token_id, "attempts": failures},
            )

    def _note_order_metadata_success(self, token_id: str) -> None:
        self._order_metadata_failures.pop(token_id, None)
        self._order_metadata_retry_after.pop(token_id, None)

    def _maybe_refresh_active_order_metadata(self) -> None:
        """Tick hook: keep the active market's metadata fresh before it expires."""
        if self.active_market is not None:
            self._schedule_order_metadata_warm(self.active_market, refresh_ahead=True)
        else:
            self._refresh_order_metadata_capability()

    async def _warm_order_metadata(self, token_ids: tuple[str, ...]) -> None:
        self.run_recorder.record(
            "ORDER_METADATA_WARM_STARTED",
            {"token_ids": list(token_ids)},
        )
        result = await self.gateway.warm_order_metadata(
            token_ids,
            max_price=_ORDER_METADATA_WARM_MAX_PRICE,
            max_spend=self.config.risk.maximum_total_debit,
            amount=min(_ORDER_METADATA_WARM_AMOUNT, self.config.risk.maximum_total_debit),
        )
        now = time.monotonic()
        for item in result.tokens:
            payload = {
                "token_id": item.token_id,
                "ok": item.ok,
                "elapsed_ms": item.elapsed_ms,
                "error": item.error,
            }
            if item.ok:
                self._order_metadata_warmed_at[item.token_id] = now
                self._note_order_metadata_success(item.token_id)
                self.run_recorder.record("ORDER_METADATA_WARM_OK", payload)
            else:
                # A failed refresh keeps entries blocked; it is retried with backoff.
                self._order_metadata_warmed_at.pop(item.token_id, None)
                self._note_order_metadata_failure(item.token_id)
                self.run_recorder.record("ORDER_METADATA_WARM_FAILED", payload)
        self._refresh_order_metadata_capability()
        self.run_recorder.record(
            "ORDER_METADATA_WARM_FINISHED",
            {
                "ok": result.ok,
                "warmed_token_ids": list(result.warmed_token_ids),
                "token_results": [
                    {
                        "token_id": item.token_id,
                        "ok": item.ok,
                        "elapsed_ms": item.elapsed_ms,
                        "error": item.error,
                    }
                    for item in result.tokens
                ],
            },
        )
