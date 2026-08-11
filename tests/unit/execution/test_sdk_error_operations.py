import asyncio

from tyrex_pm.adapters.polymarket.sdk_errors import (
    PolymarketErrorCategory,
    PolymarketOperation,
    classify_polymarket_error,
)


def test_read_timeout_is_retryable_transport_failure() -> None:
    result = classify_polymarket_error(
        TimeoutError("read timed out"),
        operation=PolymarketOperation.READ,
    )
    assert result.category is PolymarketErrorCategory.TRANSPORT
    assert result.retryable


def test_dispatch_timeout_is_ambiguous_and_never_blind_retried() -> None:
    result = classify_polymarket_error(
        TimeoutError("post response timed out"),
        operation=PolymarketOperation.DISPATCH,
    )
    assert result.category is PolymarketErrorCategory.UNCERTAIN_DISPATCH
    assert not result.retryable


def test_dispatch_cancellation_is_also_ambiguous() -> None:
    result = classify_polymarket_error(
        asyncio.CancelledError(),
        operation=PolymarketOperation.DISPATCH,
    )
    assert result.category is PolymarketErrorCategory.UNCERTAIN_DISPATCH
    assert not result.retryable
