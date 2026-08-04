"""Normalize official ``polymarket-client`` ``Paginator`` → record streams.

SDK contract (0.2.0): ``Paginator.__iter__`` yields ``Page[T]`` objects.
``Page.items`` holds the domain records. Never treat a ``Page`` as a record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")


class SdkPaginationError(RuntimeError):
    """SDK pagination container violated the expected Page[T] contract."""


@dataclass(frozen=True, kw_only=True)
class PaginationDrainResult:
    items: tuple[Any, ...]
    page_count: int
    record_count: int


def _page_type() -> type:
    from polymarket.pagination import Page

    return Page


def _paginator_type() -> type:
    from polymarket.pagination import Paginator

    return Paginator


def is_sdk_page(obj: Any) -> bool:
    try:
        return isinstance(obj, _page_type())
    except ImportError:  # pragma: no cover
        return type(obj).__name__ == "Page" and hasattr(obj, "items") and hasattr(obj, "has_more")


def assert_not_sdk_page(obj: Any, *, context: str) -> None:
    if is_sdk_page(obj):
        raise SdkPaginationError(f"{context}:refused_page_as_record")


def iter_sdk_page_items(paginator: Any) -> Iterator[Any]:
    """Yield domain records from an SDK ``Paginator`` (never ``Page`` objects)."""
    try:
        Paginator = _paginator_type()
        Page = _page_type()
    except ImportError as exc:  # pragma: no cover
        raise SdkPaginationError("polymarket_pagination_unavailable") from exc

    if not isinstance(paginator, Paginator):
        raise SdkPaginationError(f"expected_paginator:got={type(paginator).__name__}")

    # Explicit page walk — same semantics as Paginator.iter_items(), with shape checks.
    for page in paginator:
        if not isinstance(page, Page):
            raise SdkPaginationError(f"expected_page:got={type(page).__name__}")
        items = page.items
        if not isinstance(items, tuple):
            raise SdkPaginationError(f"page_items_not_tuple:got={type(items).__name__}")
        for item in items:
            if isinstance(item, Page):
                raise SdkPaginationError("nested_page_in_items")
            yield item


def drain_sdk_paginator(paginator: Any) -> PaginationDrainResult:
    """Materialize all records from a paginator; track page vs record counts."""
    try:
        Paginator = _paginator_type()
        Page = _page_type()
    except ImportError as exc:  # pragma: no cover
        raise SdkPaginationError("polymarket_pagination_unavailable") from exc

    if not isinstance(paginator, Paginator):
        raise SdkPaginationError(f"expected_paginator:got={type(paginator).__name__}")

    page_count = 0
    records: list[Any] = []
    for page in paginator:
        if not isinstance(page, Page):
            raise SdkPaginationError(f"expected_page:got={type(page).__name__}")
        page_count += 1
        items = page.items
        if not isinstance(items, tuple):
            raise SdkPaginationError(f"page_items_not_tuple:got={type(items).__name__}")
        for item in items:
            if isinstance(item, Page):
                raise SdkPaginationError("nested_page_in_items")
            records.append(item)
    return PaginationDrainResult(
        items=tuple(records),
        page_count=page_count,
        record_count=len(records),
    )


def map_sdk_paginator(
    paginator: Any,
    convert: Callable[[Any], T],
) -> tuple[list[T], PaginationDrainResult]:
    """Drain paginator and convert each record; converters never see ``Page``."""
    drained = drain_sdk_paginator(paginator)
    out: list[T] = []
    for item in drained.items:
        assert_not_sdk_page(item, context="map_sdk_paginator")
        out.append(convert(item))
    return out, drained
