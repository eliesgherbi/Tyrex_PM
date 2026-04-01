"""Network integration: PolymarketDataLoader + CLOB book (opt-in)."""

import os

import pytest

from tyrex_pm.data.resolution import resolve_market_slug


@pytest.mark.network
@pytest.mark.asyncio
async def test_resolve_reference_slug():
    if os.environ.get("TYREX_NETWORK_TESTS") != "1":
        pytest.skip("Set TYREX_NETWORK_TESTS=1 to run Polymarket HTTP tests")
    r = await resolve_market_slug("gta-vi-released-before-june-2026")
    assert r.slug == "gta-vi-released-before-june-2026"
    assert r.token_id.isdigit() or r.token_id[:2].isalnum()
    assert r.book_status in {"ok_liquidity", "empty_book", "book_error"}
    assert r.book_status != "book_error", r.book_detail
