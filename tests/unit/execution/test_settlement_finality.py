"""A latest payout is clearance only for the inspection that read it."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.adapters.polymarket.ctf_payout import CONDITIONAL_TOKENS_ADDRESS
from tyrex_pm.execution.coordinator import AccountExecutionCoordinator
from tyrex_pm.execution.evidence import MarketSettlementObserved, SessionOpened, new_envelope
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState, SessionIdentity
from tyrex_pm.execution.settlement_cache import SettlementCache, SettlementProof, proof_is_reusable
from tyrex_pm.execution.startup_reconciliation import (
    AccountIdentity,
    Holding,
    InspectionFacts,
    MarketLifecycleObservation,
    ReadCoverage,
    assess,
)
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.runtime.startup_controller import StartupController

CONDITION = "0x" + "11" * 32
WALLET = "0x" + "ab" * 20


class _Page:
    def __init__(self, items) -> None:
        self.items = items
        self.has_more = False


class _Pages:
    def __init__(self, items) -> None:
        self.page = _Page(items)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        yield self.page


class _Payout:
    def __init__(self) -> None:
        self.mode = "one"
        self.calls = 0
        self.wallet = WALLET

    def list_positions(self, **_kwargs):
        return _Pages(
            (
                {
                    "condition_id": CONDITION,
                    "token_id": "token",
                    "size": "7",
                    "outcome_index": 0,
                },
            )
        )

    def list_open_orders(self, **_kwargs):
        return _Pages(())

    def list_activity(self, **_kwargs):
        return _Pages(())

    def list_markets(self, **_kwargs):
        state = SimpleNamespace(closed=True, accepting_orders=False, neg_risk=False)
        return _Pages((SimpleNamespace(state=state, resolution=None),))

    async def eth_call(self, *, to, data):
        assert to == CONDITIONAL_TOKENS_ADDRESS
        self.calls += 1
        if self.mode == "fail":
            raise RuntimeError("payout read failed")
        if data[2:10] == "dd34de67":
            return "0x" + "0" * 63 + "1"
        digit = "0" if self.mode == "zero" else "1"
        return "0x" + "0" * 63 + digit


def _session() -> ExecutionSessionState:
    return ExecutionSessionState(
        identity=SessionIdentity(
            session_id="s1",
            strategy_id="ask70",
            market_id=CONDITION,
            window_id="w",
            token_id="token",
        ),
        phase=ExecutionPhase.MANUAL_INTERVENTION,
        confirmed_position_shares=Decimal("7"),
    )


def _codes(facts) -> set[str]:
    return {issue.code for issue in assess(replace(facts, sessions=(_session(),))).issues}


def _settlement_payout(facts) -> str | None:
    for issue in assess(replace(facts, sessions=(_session(),))).issues:
        if issue.code == "SETTLEMENT_HOLDING" and issue.evidence:
            return issue.evidence[0].fields["payout_per_share"]
    return None


async def _open(coordinator: AccountExecutionCoordinator) -> None:
    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="ask70",
            market_id=CONDITION,
            window_id="w",
            token_id="token",
        )
    )


def _settlements(journal: MemoryExecutionJournal) -> list[MarketSettlementObserved]:
    return [event for event in journal.load("s1") if isinstance(event, MarketSettlementObserved)]


async def _noop() -> None:
    return None


def _coordinator(journal: MemoryExecutionJournal) -> AccountExecutionCoordinator:
    return AccountExecutionCoordinator(
        journal=journal,
        gateway=SimpleNamespace(close=_noop),
        final_gate=lambda _spec: (True, None),
    )


@pytest.mark.asyncio
async def test_changed_payout_revalidation_failure_and_restart_change_clearance() -> None:
    reader = _Payout()
    gateway = PolymarketAsyncGateway(client=reader, payout_eth_call=reader.eth_call)
    first = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert first.lifecycles[CONDITION].status == "RESOLVED_FINAL"
    assert first.lifecycles[CONDITION].proof_supported is True
    assert _settlement_payout(first) == "1"
    calls_after_first = reader.calls

    proven = _coordinator(MemoryExecutionJournal())
    await _open(proven)
    await StartupController(coordinator=proven).inspect(replace(first, sessions=(_session(),)))
    proven_rows = _settlements(proven.journal)
    assert len(proven_rows) == 1
    assert proven_rows[0].payout_per_share == Decimal("1")
    assert proven_rows[0].supported is True

    gateway.settlement_cache.store_provisional_latest(
        condition_id=CONDITION,
        outcome_index=0,
        payout_per_share=Decimal("1"),
        token_id="token",
    )
    reader.mode = "zero"
    changed = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert reader.calls > calls_after_first
    assert changed.lifecycles[CONDITION].payout_per_share == Decimal("0")
    assert _settlement_payout(changed) == "0"
    changed_book = _coordinator(MemoryExecutionJournal())
    await _open(changed_book)
    await StartupController(coordinator=changed_book).inspect(
        replace(changed, sessions=(_session(),))
    )
    changed_rows = _settlements(changed_book.journal)
    assert len(changed_rows) == 1
    assert changed_rows[0].payout_per_share == Decimal("0")

    reader.mode = "fail"
    failed = await gateway.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert failed.lifecycles[CONDITION].status != "RESOLVED_FINAL"
    assert failed.lifecycles[CONDITION].proof_supported is False
    assert "SETTLEMENT_HOLDING" not in _codes(failed)
    failed_book = _coordinator(MemoryExecutionJournal())
    await _open(failed_book)
    await StartupController(coordinator=failed_book).inspect(
        replace(failed, sessions=(_session(),))
    )
    assert _settlements(failed_book.journal) == []

    restarted = PolymarketAsyncGateway(client=reader, payout_eth_call=reader.eth_call)
    restarted.settlement_cache = SettlementCache()
    restarted.settlement_cache.store_provisional_latest(
        condition_id=CONDITION,
        outcome_index=0,
        payout_per_share=Decimal("1"),
        token_id="token",
    )
    before_restart = reader.calls
    reader.mode = "one"
    again = await restarted.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert reader.calls > before_restart
    assert again.lifecycles[CONDITION].payout_per_share == Decimal("1")
    assert restarted.settlement_cache.lookup(
        chain_id=137,
        contract=CONDITIONAL_TOKENS_ADDRESS,
        condition_id=CONDITION,
        outcome_index=0,
        token_id="token",
    ) is None
    seeded = SettlementProof(
        chain_id=137,
        contract=CONDITIONAL_TOKENS_ADDRESS,
        condition_id=CONDITION,
        outcome_index=0,
        payout_per_share=Decimal("1"),
        proof_source="payoutNumerators/payoutDenominator",
        block_tag="finalized",
        finality="finalized",
        block_identity="0xblock",
        token_id="token",
    )
    restarted.settlement_cache._proofs[restarted.settlement_cache._key(seeded)] = seeded
    reader.mode = "zero"
    before_seeded = reader.calls
    ignored = await restarted.startup_inspection(journal=MemoryExecutionJournal(), sessions=())
    assert reader.calls > before_seeded
    assert ignored.lifecycles[CONDITION].payout_per_share == Decimal("0")
    assert _settlement_payout(ignored) == "0"
    await proven.close()
    await changed_book.close()
    await failed_book.close()


def test_only_a_finalized_matching_proof_is_reusable() -> None:
    cache = SettlementCache()
    stored = cache.store_provisional_latest(
        condition_id=CONDITION,
        outcome_index=0,
        payout_per_share=Decimal("1"),
        token_id="token",
    )
    assert stored is not None
    assert stored.finality == "provisional_latest"
    assert stored.block_tag == "latest"
    assert stored.block_identity == ""
    assert proof_is_reusable(stored) is False
    assert (
        cache.lookup(
            chain_id=137,
            contract=CONDITIONAL_TOKENS_ADDRESS,
            condition_id=CONDITION,
            outcome_index=0,
            token_id="token",
        )
        is None
    )
    finalized = SettlementProof(
        chain_id=137,
        contract=CONDITIONAL_TOKENS_ADDRESS,
        condition_id=CONDITION,
        outcome_index=0,
        payout_per_share=Decimal("1"),
        proof_source="payoutNumerators/payoutDenominator",
        block_tag="finalized",
        finality="finalized",
        block_identity="0xblock",
        token_id="token",
    )
    assert proof_is_reusable(finalized) is True
    cache._proofs[cache._key(finalized)] = finalized
    assert cache.lookup(
        chain_id=137,
        contract=CONDITIONAL_TOKENS_ADDRESS,
        condition_id=CONDITION,
        outcome_index=0,
        token_id="token",
    ) == finalized
    assert (
        cache.lookup(
            chain_id=137,
            contract=CONDITIONAL_TOKENS_ADDRESS,
            condition_id=CONDITION,
            outcome_index=0,
            token_id="other-token",
        )
        is None
    )
    assessment = assess(
        InspectionFacts(
            identity=AccountIdentity(chain_id=137, environment="production", wallet=WALLET),
            coverage=ReadCoverage(
                complete=True,
                positions_complete=True,
                orders_complete=True,
                activity_complete=True,
            ),
            sessions=(_session(),),
            holdings=(
                Holding(condition_id=CONDITION, token_id="token", shares=Decimal("7")),
            ),
            lifecycles={
                CONDITION: MarketLifecycleObservation(
                    condition_id=CONDITION,
                    status="RESOLVED_FINAL",
                    payout_per_share=Decimal("1"),
                    proof_supported=True,
                    reason="mapped to another token",
                    token_payouts={"other-token": Decimal("1")},
                )
            },
            bound_wallet=WALLET,
        )
    )
    assert all(issue.code != "SETTLEMENT_HOLDING" for issue in assessment.issues)
