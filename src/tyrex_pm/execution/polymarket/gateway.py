"""One asynchronous Polymarket SDK boundary for execution and account evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from tyrex_pm.adapters.polymarket.sdk_secure import (
    balance_allowance_to_decimal,
    build_async_secure_client,
)
from tyrex_pm.execution.cancel import CancelResult, cancel_result_from_sdk
from tyrex_pm.execution.coordinator import (
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import (
    ExecutionEvidence,
    OrderSnapshotObserved,
    TradeStatus,
    TradeStatusObserved,
    new_envelope,
)
from tyrex_pm.execution.orders import (
    LimitOrderSpec,
    MarketBuyOrderSpec,
    MarketSellOrderSpec,
    OrderSide,
    OrderSpec,
)
from tyrex_pm.execution.polymarket.user_events import (
    UserStreamOrderEvidence,
    UserStreamTradeEvidence,
    normalize_user_stream_message,
)

EvidenceHandler = Callable[[ExecutionEvidence], Awaitable[Any]]
GapHandler = Callable[[str], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None]]
TickSizeResolver = Callable[[str], Decimal | None]
TickSizeFetcher = Callable[[str], Awaitable[Decimal | None]]


class VenueOrderPreparationError(RuntimeError):
    """Failure while adapting or signing an order before any venue POST."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = code
        self.preparation_details = dict(details or {})
        self.original_error = original_error


def _protection_price(spec: OrderSpec) -> Decimal | None:
    if isinstance(spec, MarketBuyOrderSpec):
        return spec.worst_price
    if isinstance(spec, MarketSellOrderSpec):
        return spec.minimum_price
    if isinstance(spec, LimitOrderSpec):
        return spec.limit_price
    return None


def _adapt_price_to_tick(price: Decimal, tick_size: Decimal, *, side: OrderSide) -> Decimal:
    """Move a protection price onto the grid without weakening its constraint."""
    tick = Decimal(str(tick_size))
    requested = Decimal(str(price))
    if not tick.is_finite() or tick <= 0 or tick >= 1:
        raise ValueError(f"invalid tick size: {tick}")
    rounding = ROUND_FLOOR if side is OrderSide.BUY else ROUND_CEILING
    adapted = (requested / tick).to_integral_value(rounding=rounding) * tick
    if adapted < tick or adapted > Decimal("1") - tick:
        raise ValueError(
            f"adapted {side.value} protection price {adapted} is outside "
            f"[{tick}, {Decimal('1') - tick}]"
        )
    return adapted


def _adapt_spec_to_tick(spec: OrderSpec, tick_size: Decimal) -> OrderSpec:
    requested = _protection_price(spec)
    if requested is None:  # pragma: no cover - all current specs carry a price
        return spec
    effective = _adapt_price_to_tick(requested, tick_size, side=spec.side)
    metadata = {
        **dict(spec.metadata or {}),
        "venue_tick_size": str(tick_size),
        "requested_protection_price": str(requested),
        "effective_protection_price": str(effective),
    }
    if isinstance(spec, MarketBuyOrderSpec):
        return replace(spec, worst_price=effective, metadata=metadata)
    if isinstance(spec, MarketSellOrderSpec):
        return replace(spec, minimum_price=effective, metadata=metadata)
    return replace(spec, limit_price=effective, metadata=metadata)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _model_mapping(model: Any) -> dict[str, Any]:
    if isinstance(model, Mapping):
        return dict(model)
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dict(dump(by_alias=True, mode="json"))
    raise TypeError(f"unsupported SDK model shape: {type(model).__name__}")


def _user_event_mapping(event: Any) -> dict[str, Any]:
    raw = _model_mapping(event)
    event_type = str(raw.get("type") or raw.get("event_type") or "").lower()
    payload = raw.get("payload")
    if isinstance(payload, Mapping):
        return {"type": event_type, "payload": dict(payload), "topic": "user"}
    return raw


def _signed_digest(signed: Any) -> str:
    try:
        payload = _model_mapping(signed)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        encoded = repr(signed)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _accepted_response(response: Any, *, spec: OrderSpec) -> GatewaySubmissionResult:
    accepted = bool(getattr(response, "ok", False))
    if not accepted:
        return GatewaySubmissionResult(
            accepted=False,
            venue_order_id=None,
            status="rejected",
            error_code=str(getattr(response, "code", "unknown")),
            message=str(getattr(response, "message", "order rejected")),
        )
    making = Decimal(str(getattr(response, "making_amount", 0)))
    taking = Decimal(str(getattr(response, "taking_amount", 0)))
    shares = taking if spec.side is OrderSide.BUY else making
    return GatewaySubmissionResult(
        accepted=True,
        venue_order_id=str(getattr(response, "order_id")),
        status=str(getattr(response, "status", "accepted")),
        cumulative_matched_shares=shares,
        trade_ids=tuple(str(value) for value in getattr(response, "trade_ids", ()) or ()),
    )


@dataclass(frozen=True)
class OrderMetadataWarmTokenResult:
    token_id: str
    ok: bool
    elapsed_ms: float
    error: str | None = None


@dataclass(frozen=True)
class OrderMetadataWarmResult:
    """Outcome of discarded create_market_order warm calls (no venue POST)."""

    tokens: tuple[OrderMetadataWarmTokenResult, ...]

    @property
    def ok(self) -> bool:
        return bool(self.tokens) and all(item.ok for item in self.tokens)

    @property
    def warmed_token_ids(self) -> tuple[str, ...]:
        return tuple(item.token_id for item in self.tokens if item.ok)


@dataclass
class PolymarketAsyncGateway:
    """AsyncSecureClient adapter.

    The same client and asyncio loop own order preparation, POST, account reads
    and the authenticated stream.  There is intentionally no sync fallback.
    """

    client: Any
    tick_size_resolver: TickSizeResolver | None = None
    tick_size_fetcher: TickSizeFetcher | None = None
    client_guard: Any | None = None
    prepare_budget_ms: float | None = None
    refresh_wait_budget_ms: float | None = None
    metadata_cache_miss_ms: float = 500.0
    evidence_sink: Callable[[str, Mapping[str, Any]], None] | None = None
    warmed_token_ids: set[str] = field(default_factory=set)
    _prepared_specs: dict[str, OrderSpec] = field(default_factory=dict)
    _stream_task: asyncio.Task[None] | None = None
    _stream_handle: Any | None = None
    _stream_stop: asyncio.Event = field(default_factory=asyncio.Event)
    _abandoned_prepares: set[asyncio.Task[Any]] = field(default_factory=set)

    @classmethod
    async def from_env(cls, env: dict[str, str] | None = None) -> "PolymarketAsyncGateway":
        return cls(client=await build_async_secure_client(env=env))

    async def warm_order_metadata(
        self,
        token_ids: Sequence[str],
        *,
        max_price: Decimal,
        max_spend: Decimal,
        amount: Decimal | None = None,
    ) -> OrderMetadataWarmResult:
        """Populate the official SDK order-metadata cache without posting.

        Uses a discarded protected BUY ``create_market_order`` so tick / neg-risk
        / fee resolution lands in ``AsyncOrderMetadataCache``. Never calls
        ``post_order`` and never retains prepared digests.
        """
        spend = Decimal("1") if amount is None else Decimal(str(amount))
        if spend <= 0:
            raise ValueError("warm amount must be positive")
        spend_cap = Decimal(str(max_spend))
        price_cap = Decimal(str(max_price))
        if spend_cap <= 0 or price_cap <= 0:
            raise ValueError("warm max_spend and max_price must be positive")
        if spend > spend_cap:
            spend = spend_cap

        results: list[OrderMetadataWarmTokenResult] = []
        seen: set[str] = set()
        for token_id in token_ids:
            if not token_id or token_id in seen:
                continue
            seen.add(token_id)
            started = time.monotonic()
            try:
                await self.client.create_market_order(
                    token_id=token_id,
                    side="BUY",
                    amount=spend,
                    max_spend=spend_cap,
                    max_price=price_cap,
                    order_type="FAK",
                )
            except Exception as exc:  # noqa: BLE001 - warm failure is readiness evidence
                results.append(
                    OrderMetadataWarmTokenResult(
                        token_id=token_id,
                        ok=False,
                        elapsed_ms=round((time.monotonic() - started) * 1_000, 3),
                        error=f"{type(exc).__name__}:{exc}",
                    )
                )
            else:
                self.warmed_token_ids.add(token_id)
                results.append(
                    OrderMetadataWarmTokenResult(
                        token_id=token_id,
                        ok=True,
                        elapsed_ms=round((time.monotonic() - started) * 1_000, 3),
                    )
                )
        return OrderMetadataWarmResult(tokens=tuple(results))

    async def _resolve_tick_size(self, token_id: str) -> Decimal | None:
        tick_size = (
            None if self.tick_size_resolver is None else self.tick_size_resolver(token_id)
        )
        if tick_size is not None:
            return tick_size
        if self.tick_size_fetcher is None:
            return None
        return await self.tick_size_fetcher(token_id)

    async def _invoke_create(self, dispatch_spec: OrderSpec) -> Any:
        if isinstance(dispatch_spec, MarketBuyOrderSpec):
            return await self.client.create_market_order(
                token_id=dispatch_spec.token_id,
                side="BUY",
                amount=dispatch_spec.spend_amount,
                max_spend=dispatch_spec.maximum_total_debit,
                max_price=dispatch_spec.worst_price,
                order_type=dispatch_spec.time_in_force.value,
            )
        if isinstance(dispatch_spec, MarketSellOrderSpec):
            return await self.client.create_market_order(
                token_id=dispatch_spec.token_id,
                side="SELL",
                shares=dispatch_spec.shares,
                min_price=dispatch_spec.minimum_price,
                order_type=dispatch_spec.time_in_force.value,
            )
        if isinstance(dispatch_spec, LimitOrderSpec):
            return await self.client.create_limit_order(
                token_id=dispatch_spec.token_id,
                price=dispatch_spec.limit_price,
                size=dispatch_spec.shares,
                side=dispatch_spec.side.value,
                post_only=bool(dispatch_spec.metadata.get("post_only")),
                expiration=dispatch_spec.expiration_epoch_s,
            )
        raise TypeError(f"unsupported order spec: {type(dispatch_spec).__name__}")

    def _track_abandoned_prepare(self, task: asyncio.Task[Any], lease: Any) -> None:
        self._abandoned_prepares.add(task)

        async def _finish() -> None:
            try:
                signed = await task
            except Exception:
                signed = None
            finally:
                if lease is not None:
                    await lease.release()
                self._abandoned_prepares.discard(task)
            if signed is not None:
                self._prepared_specs.pop(_signed_digest(signed), None)

        follow = asyncio.create_task(_finish(), name="abandoned-prepare")
        self._abandoned_prepares.add(follow)
        follow.add_done_callback(self._abandoned_prepares.discard)

    async def prepare_order(self, spec: OrderSpec) -> PreparedOrder:
        requested_price = _protection_price(spec)
        details: dict[str, Any] = {
            "requested_protection_price": requested_price,
            "effective_protection_price": None,
            "tick_size": None,
            "prepare_queue_wait_ms": None,
            "tick_resolve_ms": None,
            "create_market_order_ms": None,
            "refresh_wait_ms": None,
            "refresh_elapsed_ms": None,
            "order_metadata_cache_miss": False,
        }
        guard = self.client_guard
        if guard is not None and self.refresh_wait_budget_ms is not None:
            waited = await guard.wait_until_refresh_idle(
                timeout_s=float(self.refresh_wait_budget_ms) / 1000.0
            )
            details["refresh_wait_ms"] = waited.wait_ms
            details["refresh_elapsed_ms"] = waited.refresh_elapsed_ms
            if not waited.idle:
                raise VenueOrderPreparationError(
                    "ACCOUNT_REFRESH_IN_FLIGHT",
                    (
                        "account refresh still owns the SDK client after "
                        f"{self.refresh_wait_budget_ms}ms; refusing to start sign"
                    ),
                    details=details,
                )

        lease = None
        queue_started = time.monotonic()
        if guard is not None:
            lease = await guard.acquire_sdk_slot(active=True)
        details["prepare_queue_wait_ms"] = round((time.monotonic() - queue_started) * 1_000, 3)
        try:
            tick_started = time.monotonic()
            tick_size = await self._resolve_tick_size(spec.token_id)
            details["tick_resolve_ms"] = round((time.monotonic() - tick_started) * 1_000, 3)
            details["tick_size"] = tick_size
            if tick_size is None:
                raise VenueOrderPreparationError(
                    "TICK_SIZE_UNAVAILABLE",
                    f"authoritative tick size is unavailable for token {spec.token_id}",
                    details=details,
                )
            try:
                dispatch_spec = _adapt_spec_to_tick(spec, tick_size)
            except ValueError as exc:
                raise VenueOrderPreparationError(
                    "PRICE_ADAPTATION_FAILED",
                    str(exc),
                    details=details,
                    original_error=exc,
                ) from exc
            effective_price = _protection_price(dispatch_spec)
            details["effective_protection_price"] = effective_price
            create_started = time.monotonic()
            create_task = asyncio.create_task(self._invoke_create(dispatch_spec))
            budget_s = (
                None
                if self.prepare_budget_ms is None
                else float(self.prepare_budget_ms) / 1000.0
            )
            try:
                if budget_s is None:
                    signed = await create_task
                else:
                    signed = await asyncio.wait_for(asyncio.shield(create_task), timeout=budget_s)
            except TimeoutError:
                details["create_market_order_ms"] = round(
                    (time.monotonic() - create_started) * 1_000, 3
                )
                details["order_metadata_cache_miss"] = spec.token_id in self.warmed_token_ids
                if details["order_metadata_cache_miss"] and self.evidence_sink is not None:
                    self.evidence_sink(
                        "ORDER_METADATA_CACHE_MISS",
                        {
                            "token_id": spec.token_id,
                            "create_market_order_ms": details["create_market_order_ms"],
                            "metadata_cache_miss_ms": float(self.metadata_cache_miss_ms),
                            "reason": "PREPARE_BUDGET_EXCEEDED",
                        },
                    )
                self._track_abandoned_prepare(create_task, lease)
                lease = None
                raise VenueOrderPreparationError(
                    "PREPARE_BUDGET_EXCEEDED",
                    f"create_market_order exceeded prepare_budget_ms={self.prepare_budget_ms}",
                    details=details,
                )
            except VenueOrderPreparationError:
                raise
            except Exception as exc:
                details["create_market_order_ms"] = round(
                    (time.monotonic() - create_started) * 1_000, 3
                )
                raise VenueOrderPreparationError(
                    "SDK_ORDER_PREPARATION_FAILED",
                    str(exc),
                    details=details,
                    original_error=exc,
                ) from exc
            details["create_market_order_ms"] = round(
                (time.monotonic() - create_started) * 1_000, 3
            )
            cache_miss = bool(
                spec.token_id in self.warmed_token_ids
                and details["create_market_order_ms"] is not None
                and details["create_market_order_ms"] >= float(self.metadata_cache_miss_ms)
            )
            details["order_metadata_cache_miss"] = cache_miss
            if cache_miss and self.evidence_sink is not None:
                self.evidence_sink(
                    "ORDER_METADATA_CACHE_MISS",
                    {
                        "token_id": spec.token_id,
                        "create_market_order_ms": details["create_market_order_ms"],
                        "metadata_cache_miss_ms": float(self.metadata_cache_miss_ms),
                    },
                )
            digest = _signed_digest(signed)
            self._prepared_specs[digest] = dispatch_spec
            return PreparedOrder(
                local_order_id=spec.order_id,
                digest=digest,
                payload=signed,
                dispatch_spec=dispatch_spec,
                requested_protection_price=requested_price,
                effective_protection_price=effective_price,
                tick_size=tick_size,
                prepare_queue_wait_ms=details["prepare_queue_wait_ms"],
                tick_resolve_ms=details["tick_resolve_ms"],
                create_market_order_ms=details["create_market_order_ms"],
                refresh_wait_ms=details["refresh_wait_ms"],
                refresh_elapsed_ms=details["refresh_elapsed_ms"],
                order_metadata_cache_miss=cache_miss,
            )
        finally:
            if lease is not None:
                await lease.release()

    async def cancel_orders(self, order_ids: Sequence[str]) -> CancelResult:
        """Cancel specific session-owned orders. Never calls cancel_all."""
        ids = tuple(str(item) for item in order_ids if str(item).strip())
        if not ids:
            return CancelResult(canceled=(), not_canceled=())
        if len(ids) == 1:
            raw = await self.client.cancel_order(order_id=ids[0])
        else:
            raw = await self.client.cancel_orders(order_ids=ids)
        return cancel_result_from_sdk(raw)

    async def cancel_token_orders(
        self, *, token_id: str, market_id: str | None = None
    ) -> CancelResult:
        """Cancel our resting orders on one token. Not cancel_all."""
        raw = await self.client.cancel_market_orders(token_id=token_id, market=market_id)
        return cancel_result_from_sdk(raw)

    async def discard_prepared(self, prepared: PreparedOrder) -> None:
        """Forget a signed order that the final gate refused to dispatch."""
        self._prepared_specs.pop(prepared.digest, None)

    async def post_order(self, prepared: PreparedOrder) -> GatewaySubmissionResult:
        try:
            spec = self._prepared_specs.pop(prepared.digest)
        except KeyError as exc:
            raise RuntimeError("prepared order is unknown or already posted") from exc
        response = await self.client.post_order(prepared.payload)
        return _accepted_response(response, spec=spec)

    async def list_open_orders(
        self, *, token_id: str | None = None, market_id: str | None = None
    ) -> tuple[Any, ...]:
        paginator = self.client.list_open_orders(token_id=token_id, market=market_id)
        return tuple([item async for item in paginator.iter_items()])

    async def list_account_trades(
        self, *, token_id: str | None = None, market_id: str | None = None
    ) -> tuple[Any, ...]:
        paginator = self.client.list_account_trades(token_id=token_id, market=market_id)
        return tuple([item async for item in paginator.iter_items()])

    async def conditional_balance(self, token_id: str) -> tuple[Decimal, Decimal | None]:
        raw = await self.client.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
        return balance_allowance_to_decimal(raw, conditional=True)

    async def collateral_balance(self) -> tuple[Decimal, Decimal | None]:
        raw = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        return balance_allowance_to_decimal(raw, conditional=False)

    async def start_user_stream(
        self,
        *,
        session_id: str,
        markets: tuple[str, ...],
        on_evidence: EvidenceHandler,
        on_gap: GapHandler,
        on_ready: ReadyHandler | None = None,
    ) -> None:
        if self._stream_task is not None:
            raise RuntimeError("user stream is already running")
        self._stream_stop.clear()
        self._stream_task = asyncio.create_task(
            self._run_user_stream(
                session_id=session_id,
                markets=markets,
                on_evidence=on_evidence,
                on_gap=on_gap,
                on_ready=on_ready,
            ),
            name="polymarket-user-stream",
        )
        await asyncio.sleep(0)

    async def _run_user_stream(
        self,
        *,
        session_id: str,
        markets: tuple[str, ...],
        on_evidence: EvidenceHandler,
        on_gap: GapHandler,
        on_ready: ReadyHandler | None,
    ) -> None:
        from polymarket.streams import UserSpec

        backoff_s = 0.25
        while not self._stream_stop.is_set():
            try:
                handle = await self.client.subscribe(UserSpec(markets=list(markets)))
                self._stream_handle = handle
                if on_ready is not None:
                    await on_ready()
                async for sdk_event in handle:
                    if self._stream_stop.is_set():
                        break
                    evidence = self._normalize_stream_event(
                        session_id=session_id, sdk_event=sdk_event
                    )
                    if evidence is not None:
                        await on_evidence(evidence)
                if not self._stream_stop.is_set():
                    raise ConnectionError("authenticated user stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - gap is authoritative state
                if self._stream_stop.is_set():
                    return
                await on_gap(f"{type(exc).__name__}:{exc}")
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 5.0)
            else:
                backoff_s = 0.25

    def _normalize_stream_event(
        self, *, session_id: str, sdk_event: Any
    ) -> ExecutionEvidence | None:
        normalized = normalize_user_stream_message(_user_event_mapping(sdk_event))
        now = _utc_now()
        if isinstance(normalized, UserStreamOrderEvidence):
            if normalized.venue_order_id is None or normalized.side is None:
                return None
            return OrderSnapshotObserved(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=(
                        f"stream-order:{normalized.venue_order_id}:"
                        f"{normalized.status}:{normalized.cumulative_matched_qty}"
                    ),
                ),
                source="user_stream",
                venue_order_id=normalized.venue_order_id,
                order_id=normalized.client_order_id,
                side=OrderSide(normalized.side.upper()),
                original_shares=normalized.original_size,
                cumulative_matched_shares=normalized.cumulative_matched_qty,
                status=str(normalized.status or "UNKNOWN"),
            )
        if isinstance(normalized, UserStreamTradeEvidence):
            if normalized.venue_order_id is None:
                return None
            try:
                status = TradeStatus(normalized.status.upper())
            except ValueError:
                return None
            return TradeStatusObserved(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"stream-trade:{normalized.venue_trade_id}:{status.value}",
                ),
                source="user_stream",
                venue_trade_id=normalized.venue_trade_id,
                venue_order_id=normalized.venue_order_id,
                order_id=normalized.client_order_id,
                token_id=normalized.instrument_token_id,
                side=OrderSide(normalized.side.upper()),
                shares=normalized.size,
                price=normalized.price,
                status=status,
                venue_event_at=now,
            )
        return None

    async def stop_user_stream(self) -> None:
        self._stream_stop.set()
        handle = self._stream_handle
        self._stream_handle = None
        if handle is not None:
            close = getattr(handle, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result
        if self._stream_task is not None:
            self._stream_task.cancel()
            await asyncio.gather(self._stream_task, return_exceptions=True)
            self._stream_task = None

    async def close(self) -> None:
        await self.stop_user_stream()
        if self._abandoned_prepares:
            await asyncio.gather(*tuple(self._abandoned_prepares), return_exceptions=True)
        await self.client.close()
