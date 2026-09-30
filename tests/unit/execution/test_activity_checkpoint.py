"""Incremental activity coverage and process-local settlement proofs."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.adapters.polymarket.startup_reads import collect_inspection_facts
from tyrex_pm.execution.activity_checkpoint import (
    FULL_VERIFY_EVERY,
    LEDGER_META_KEY,
    OVERLAP_S,
    load_activity_ledger,
    save_activity_ledger,
)
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal, SqliteExecutionJournal

WALLET = "0x" + "cd" * 20
CONDITION = "0x" + "11" * 32


class _Page:
    def __init__(self, items, *, has_more: bool) -> None:
        self.items = items
        self.has_more = has_more


class _Sequence:
    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for index, items in enumerate(self.pages):
            yield _Page(items, has_more=index < len(self.pages) - 1)


class _Exploding:
    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        yield _Page(
            (_row("kept", 1_700_000_000, "0x" + "aa" * 32),),
            has_more=True,
        )
        raise RuntimeError("page failed")


def _row(activity_id: str, timestamp: int, tx_hash: str, *, shares: str = "1") -> dict:
    return {
        "id": activity_id,
        "timestamp": timestamp,
        "type": "TRADE",
        "condition_id": "cond",
        "token_id": "token",
        "side": "BUY",
        "shares": shares,
        "transaction_hash": tx_hash,
    }


class _ActivityClient:
    def __init__(self, pages_for_calls: list) -> None:
        self.pages_for_calls = list(pages_for_calls)
        self.calls: list[dict] = []
        self.position_reads = 0
        self.order_reads = 0
        self.wallet = WALLET

    def list_positions(self, **_kwargs):
        self.position_reads += 1
        return _Sequence([[]])

    def list_open_orders(self, **_kwargs):
        self.order_reads += 1
        return _Sequence([[]])

    def list_activity(self, **kwargs):
        self.calls.append(dict(kwargs))
        source = self.pages_for_calls.pop(0)
        return source


def _ids(facts) -> set[str]:
    return {activity.activity_id for activity in facts.activities}


@pytest.mark.asyncio
async def test_incremental_activity_keeps_overlap_duplicates_and_history() -> None:
    stamp = 1_700_000_100
    same_tx = "0x" + "11" * 32
    first_pages = _Sequence(
        [
            [
                _row("early", stamp - 10, same_tx),
                _row("boundary-a", stamp, same_tx),
            ],
            [
                _row("boundary-a", stamp, same_tx, shares="1"),
                _row("boundary-b", stamp, "0x" + "22" * 32),
            ],
        ]
    )
    later = _Sequence(
        [
            [
                _row("boundary-b", stamp, "0x" + "22" * 32),
                _row("newer", stamp + 5, "0x" + "33" * 32),
            ]
        ]
    )
    client = _ActivityClient([first_pages, later])
    journal = MemoryExecutionJournal()
    first = await collect_inspection_facts(client, journal=journal)
    assert first.activity_mode == "full"
    assert first.activity_pages == 2
    assert _ids(first) == {"early", "boundary-a", "boundary-b"}
    assert first.activity_ledger is not None
    boundary = set(first.activity_ledger["checkpoint"]["boundary_activity_ids"])
    assert boundary == {"boundary-a", "boundary-b"}
    save_activity_ledger(journal, first.activity_ledger)

    second = await collect_inspection_facts(client, journal=journal)
    assert second.activity_mode == "incremental"
    assert client.calls[1]["start"] == stamp - OVERLAP_S
    assert client.calls[1]["page_size"] == 500
    assert _ids(second) == {"early", "boundary-a", "boundary-b", "newer"}
    assert client.position_reads == 4
    assert client.order_reads == 4
    assert second.activity_ledger is not None
    save_activity_ledger(journal, second.activity_ledger)
    stored = load_activity_ledger(journal)
    assert stored is not None
    assert {row["activity_id"] for row in stored["records"]} == _ids(second)


@pytest.mark.asyncio
async def test_failed_pagination_does_not_advance_the_checkpoint() -> None:
    journal = MemoryExecutionJournal()
    baseline = await collect_inspection_facts(
        _ActivityClient([_Sequence([[_row("old", 1_700_000_000, "0x" + "aa" * 32)]])]),
        journal=journal,
    )
    save_activity_ledger(journal, baseline.activity_ledger)
    before = journal.get_meta(LEDGER_META_KEY)
    failed = await collect_inspection_facts(
        _ActivityClient([_Exploding()]),
        journal=journal,
    )
    assert failed.coverage.activity_complete is False
    assert failed.activity_ledger is None
    assert journal.get_meta(LEDGER_META_KEY) == before
    assert _ids(failed) == {"old"}


@pytest.mark.asyncio
async def test_restart_reloads_accumulated_activity(tmp_path) -> None:
    path = tmp_path / "execution.sqlite3"
    journal = SqliteExecutionJournal(path)
    first = await collect_inspection_facts(
        _ActivityClient([_Sequence([[_row("old", 1_700_000_000, "0x" + "aa" * 32)]])]),
        journal=journal,
    )
    save_activity_ledger(journal, first.activity_ledger)
    journal.close()
    reopened = SqliteExecutionJournal(path)
    second = await collect_inspection_facts(
        _ActivityClient([_Sequence([[_row("new", 1_700_000_050, "0x" + "bb" * 32)]])]),
        journal=reopened,
    )
    assert _ids(second) == {"old", "new"}
    assert second.activity_mode == "incremental"
    reopened.close()


@pytest.mark.asyncio
async def test_full_verification_recovers_a_delayed_record() -> None:
    journal = MemoryExecutionJournal()
    current = await collect_inspection_facts(
        _ActivityClient([_Sequence([[_row("current", 1_700_000_500, "0x" + "aa" * 32)]])]),
        journal=journal,
    )
    ledger = current.activity_ledger
    assert ledger is not None
    ledger["checkpoint"]["incremental_since_full"] = FULL_VERIFY_EVERY
    save_activity_ledger(journal, ledger)
    recovered = await collect_inspection_facts(
        _ActivityClient(
            [
                _Sequence(
                    [
                        [
                            _row("delayed", 1_700_000_000, "0x" + "bb" * 32),
                            _row("current", 1_700_000_500, "0x" + "aa" * 32),
                        ]
                    ]
                )
            ]
        ),
        journal=journal,
    )
    assert recovered.activity_mode == "full"
    assert recovered.activity_ledger["checkpoint"]["incremental_since_full"] == 0
    assert _ids(recovered) == {"delayed", "current"}


@pytest.mark.asyncio
async def test_latest_payout_is_read_again_and_does_not_reuse_a_provisional_cache() -> None:
    from tyrex_pm.adapters.polymarket.ctf_payout import CONDITIONAL_TOKENS_ADDRESS

    calls: list[str] = []

    async def eth_call(*, to, data):
        assert to == CONDITIONAL_TOKENS_ADDRESS
        calls.append(data)
        if data[2:10] == "dd34de67":
            return "0x" + "0" * 63 + "1"
        return "0x" + "0" * 63 + "1"

    client = _PayoutClient()
    gateway = PolymarketAsyncGateway(client=client, payout_eth_call=eth_call)
    first = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert first.lifecycles[CONDITION].status == "RESOLVED_FINAL"
    assert len(calls) == 2
    positions_after_first = client.position_reads
    orders_after_first = client.order_reads
    second = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert second.lifecycles[CONDITION].status == "RESOLVED_FINAL"
    assert second.lifecycles[CONDITION].proof_supported is True
    assert len(calls) == 4
    assert client.position_reads > positions_after_first
    assert client.order_reads > orders_after_first
    stored = gateway.settlement_cache.store_provisional_latest(
        condition_id=CONDITION,
        outcome_index=0,
        payout_per_share=Decimal("1"),
        token_id="token",
    )
    assert stored is not None
    assert (
        gateway.settlement_cache.lookup(
            chain_id=137,
            contract=CONDITIONAL_TOKENS_ADDRESS,
            condition_id=CONDITION,
            outcome_index=0,
            token_id="token",
        )
        is None
    )


class _PayoutClient:
    def __init__(self) -> None:
        self.wallet = WALLET
        self.position_reads = 0
        self.order_reads = 0

    def list_positions(self, **_kwargs):
        self.position_reads += 1
        return _Sequence(
            [
                [
                    {
                        "condition_id": CONDITION,
                        "token_id": "token",
                        "size": "7",
                        "outcome_index": 0,
                    }
                ]
            ]
        )

    def list_open_orders(self, **_kwargs):
        self.order_reads += 1
        return _Sequence([[]])

    def list_activity(self, **_kwargs):
        return _Sequence([[]])

    def list_markets(self, **_kwargs):
        state = SimpleNamespace(closed=True, accepting_orders=False, neg_risk=False)
        return _Sequence([[SimpleNamespace(state=state, resolution=None)]])


def test_activity_timestamp_is_unix_seconds() -> None:
    from tyrex_pm.adapters.polymarket.startup_reads import activity_from_sdk

    viewed = activity_from_sdk(_row("a", 1_700_000_000, "0x" + "ab" * 32))
    assert viewed.observed_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
