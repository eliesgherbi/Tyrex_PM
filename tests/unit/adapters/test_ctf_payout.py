"""Fake-transport tests for the standard Polygon CTF payout reader."""

from decimal import Decimal

import pytest

from tyrex_pm.adapters.polymarket.ctf_payout import (
    CONDITIONAL_TOKENS_ADDRESS,
    PAYOUT_DENOMINATOR_SELECTOR,
    PAYOUT_NUMERATOR_SELECTOR,
    condition_id_word,
    read_binary_payout,
)

CONDITION = "0x" + "11" * 32


def test_selectors_match_keccak_of_the_canonical_signatures() -> None:
    from Crypto.Hash import keccak

    def selector(signature: str) -> bytes:
        digest = keccak.new(digest_bits=256)
        digest.update(signature.encode())
        return digest.digest()[:4]

    assert selector("payoutDenominator(bytes32)") == PAYOUT_DENOMINATOR_SELECTOR
    assert selector("payoutNumerators(bytes32,uint256)") == PAYOUT_NUMERATOR_SELECTOR


@pytest.mark.asyncio
async def test_zero_denominator_is_not_a_payout() -> None:
    seen: list[str] = []

    async def eth_call(*, to, data):
        assert to == CONDITIONAL_TOKENS_ADDRESS
        seen.append(data)
        return "0x" + "0" * 64

    payout = await read_binary_payout(eth_call, condition_id=CONDITION, outcome_index=0)
    assert payout is None
    assert len(seen) == 1
    assert seen[0].startswith("0x" + PAYOUT_DENOMINATOR_SELECTOR.hex())
    assert condition_id_word(CONDITION) is not None


@pytest.mark.asyncio
async def test_binary_numerator_is_divided_by_the_denominator() -> None:
    async def eth_call(*, to, data):
        assert to == CONDITIONAL_TOKENS_ADDRESS
        if data.startswith("0x" + PAYOUT_DENOMINATOR_SELECTOR.hex()):
            return "0x" + format(2, "064x")
        assert data.startswith("0x" + PAYOUT_NUMERATOR_SELECTOR.hex())
        return "0x" + format(1, "064x")

    payout = await read_binary_payout(eth_call, condition_id=CONDITION, outcome_index=1)
    assert payout == Decimal("0.5")


@pytest.mark.asyncio
async def test_non_binary_index_is_not_read() -> None:
    async def eth_call(*, to, data):
        raise AssertionError(f"unexpected call {to} {data}")

    assert await read_binary_payout(eth_call, condition_id=CONDITION, outcome_index=2) is None
