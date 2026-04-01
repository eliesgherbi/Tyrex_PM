"""Signal entry/exit policies."""

from __future__ import annotations

import pytest

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.signal.entry import GuruFollowEntryPolicy, GuruMirrorExitPolicy


def _sig(**kwargs) -> GuruTradeSignal:
    base = dict(
        source_trade_id="id1",
        ts_event_ms=1,
        side="BUY",
        token_id="100",
        size_raw=1.0,
        price_raw=0.5,
        raw_payload_ref=None,
    )
    base.update(kwargs)
    return GuruTradeSignal(**base)


@pytest.mark.parametrize(
    ("policy_cls", "side", "token", "accept", "reason"),
    [
        (GuruFollowEntryPolicy, "BUY", "100", True, ReasonCode.GURU_ENTRY_CANDIDATE),
        (GuruFollowEntryPolicy, "BUY", "999", False, ReasonCode.NOT_ALLOWLISTED),
        (GuruFollowEntryPolicy, "SELL", "100", False, ReasonCode.COPY_SKIP),
        (GuruMirrorExitPolicy, "SELL", "100", True, ReasonCode.GURU_EXIT_MIRROR),
        (GuruMirrorExitPolicy, "SELL", "999", False, ReasonCode.NOT_ALLOWLISTED),
        (GuruMirrorExitPolicy, "BUY", "100", False, ReasonCode.COPY_SKIP),
    ],
)
def test_entry_exit_table(policy_cls, side, token, accept, reason) -> None:
    allow = frozenset({"100"})
    p = policy_cls(allow)
    d = p.evaluate(_sig(side=side, token_id=token))
    assert d.accept is accept
    assert d.reason_code == reason


def test_missing_token_entry() -> None:
    p = GuruFollowEntryPolicy(frozenset({"100"}))
    d = p.evaluate(_sig(token_id=None))
    assert not d.accept
    assert d.reason_code == ReasonCode.MISSING_TOKEN_ID
