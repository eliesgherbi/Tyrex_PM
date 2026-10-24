"""Recorded execution is evidence-backed, ordered and never inferred from final balance."""
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.execution.evidence import ReconciliationObserved, SessionOpened, new_envelope
from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal
from tyrex_pm.recording.config import RecordingConfig
from tyrex_pm.recording.raw import RawCapture
from tyrex_pm.recording.recorder import Recorder
from tyrex_pm.recording.trading import execution_prefixes, export_evidence, restore_feedback
from tyrex_pm.research.strategy_replay.capture_replay import trading_inputs


def capture(tmp_path, *, quantity="5", cursor="balance", gap=False):
    state_dir = tmp_path / "state"
    journal = SqliteExecutionJournal(state_dir / "execution.sqlite3")
    journal.append(SessionOpened(
        **dict(new_envelope(session_id="s", dedupe_key="open"), event_id="open"),
        strategy_id="settlement_value", market_id="m", window_id="w", token_id="up"))
    journal.append(ReconciliationObserved(
        **dict(new_envelope(session_id="s", dedupe_key="balance"), event_id="balance"),
        source="paper", complete=True, confirmed_position_shares=Decimal(5),
        sellable_shares=Decimal(5), open_order_ids=()))
    recorder = Recorder(tmp_path / "recording", RecordingConfig(raw_inputs=True),
                        streams={"raw": "tyrex_raw_input_v1"}, manifest={
                            "strategy_parameters": {"entry": {}}, "run_id": "run"})
    recorder.start()
    raw = RawCapture(recorder)
    raw.event("trading_context", "context", {
        "evaluation_id": 1, "window_id": "w", "session_id": "s",
        "applied_event_id": cursor, "ledger_sequence": None,
        "context": {"position_quantity": quantity, "lifecycle": {"phase": "ACTIVE"}}})
    if gap:
        raw.sequence += 1
    raw.event("trading_decision", "decision", {
        "evaluation_id": 1, "window_id": "w", "intents": [],
        "decision": {"action": "HOLD", "at_ns": 1}})
    recorder.close()
    export_evidence(SimpleNamespace(state_directory=state_dir, settlement=None), tmp_path, "run")
    journal.close()
    return tmp_path


def test_context_uses_the_applied_prefix_not_final_position(tmp_path):
    folder = capture(tmp_path)
    prefixes = execution_prefixes(folder / "execution_evidence.sqlite3")
    assert prefixes["open"]["position_quantity"] == "0"
    assert prefixes["balance"]["position_quantity"] == "5"
    assert trading_inputs(folder)["verified_contexts"] == 1


@pytest.mark.parametrize("quantity,cursor,gap", [("6", "balance", False),
                         ("5", "open", False), ("5", "missing", False), ("5", "balance", True)])
def test_unsupported_or_missing_evidence_cannot_claim_equal(tmp_path, quantity, cursor, gap):
    folder = capture(tmp_path, quantity=quantity, cursor=cursor, gap=gap)
    with pytest.raises(ValueError):
        trading_inputs(folder)


def test_export_checksum_is_verified(tmp_path):
    folder = capture(tmp_path)
    path = folder / "execution_evidence.json"
    data = json.loads(path.read_text())
    data["files"]["execution_evidence.sqlite3"] = "wrong"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checksum"):
        trading_inputs(folder)


def test_export_cannot_omit_the_execution_checksum(tmp_path):
    folder = capture(tmp_path)
    path = folder / "execution_evidence.json"
    data = json.loads(path.read_text())
    data["files"] = {}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="required evidence checksums"):
        trading_inputs(folder)


def test_missing_raw_tail_cannot_claim_a_complete_trading_capture(tmp_path):
    folder = capture(tmp_path)
    (folder / "recording/raw_completeness.json").write_text(
        json.dumps({"stream": {"written": 100}}))
    with pytest.raises(ValueError, match="tail missing"):
        trading_inputs(folder)


def test_strategy_feedback_requires_its_original_intent():
    with pytest.raises(ValueError, match="preceding captured intent"):
        restore_feedback(None, {"kind": "entry", "payload": {"intent_id": "unknown"}}, {})


def test_captured_snapshot_clock_changes_no_market_input_or_probability():
    from dataclasses import make_dataclass
    from datetime import datetime, timedelta, timezone

    from tyrex_pm.recording.trading import restore_evaluation_clock

    snap = make_dataclass("Snap", ["observed_at", "price"], frozen=True)
    ready = make_dataclass("Ready", ["snapshot"], frozen=True)
    at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    original = ready(snap(at + timedelta(microseconds=500), Decimal(123)))
    restored = restore_evaluation_clock(original, {"snapshot_observed_at": at.isoformat(),
                                                  "p_up_point": 0.999})
    assert restored.snapshot.observed_at == at
    assert restored.snapshot.price == original.snapshot.price
    assert not hasattr(restored.snapshot, "p_up_point")
    assert original.snapshot.observed_at != at
    with pytest.raises(ValueError, match="clock outside"):
        restore_evaluation_clock(original, {
            "snapshot_observed_at": (at + timedelta(seconds=5)).isoformat()})


def _feedback_driver():
    from datetime import datetime, timezone

    from tyrex_pm.core.clock import FakeClock
    from tyrex_pm.strategies.settlement_value.driver import create_settlement_value_driver
    from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

    return create_settlement_value_driver(
        config=settlement_value_config_from_parameters({"retry": {"enabled": True}}),
        clock=FakeClock(_wall=datetime(2026, 10, 20, tzinfo=timezone.utc)), window_id="w")


@pytest.mark.parametrize("kind", ["NOT_SENT", "ZERO_FILL", "PARTIAL", "FILLED", "UNCERTAIN"])
def test_recorded_entry_feedback_preserves_production_state_and_budgets(kind):
    from datetime import datetime, timezone

    from tyrex_pm.recording.trading import feedback_input
    from tyrex_pm.strategies.outcomes import EntryOutcome, EntryOutcomeKind, RetryClass

    direct, replay = _feedback_driver(), _feedback_driver()
    direct.state.entry_intent_id = replay.state.entry_intent_id = "regenerated"
    outcome = EntryOutcome(kind=EntryOutcomeKind(kind), intent_id="regenerated",
                           at=datetime(2026, 10, 20, tzinfo=timezone.utc),
                           retry_class=(RetryClass.RETRY_WHEN_CLEARED if kind == "NOT_SENT"
                                        else RetryClass.RETRY_AFTER_ZERO_FILL),
                           confirmed_shares=Decimal(5), cost=Decimal(2),
                           posted=kind != "NOT_SENT", prepare_failed=kind == "NOT_SENT")
    direct.on_entry_outcome(outcome)
    payload = {**feedback_input(outcome), "intent_id": "captured"}
    restore_feedback(replay, {"kind": "entry", "payload": payload},
                     {"captured": "regenerated"})
    assert replay.persistence_slice() == direct.persistence_slice()


@pytest.mark.parametrize("kind", ["NOT_SENT", "NO_FILL", "PARTIAL", "FLAT", "UNCERTAIN"])
def test_recorded_exit_feedback_preserves_partial_or_uncertain_management(kind):
    from datetime import datetime, timezone

    from tyrex_pm.recording.trading import feedback_input
    from tyrex_pm.strategies.outcomes import ExitOutcome, ExitOutcomeKind

    direct, replay = _feedback_driver(), _feedback_driver()
    direct.state.exit_intent_id = replay.state.exit_intent_id = "regenerated"
    outcome = ExitOutcome(kind=ExitOutcomeKind(kind), intent_id="regenerated",
                          at=datetime(2026, 10, 20, tzinfo=timezone.utc),
                          remaining_shares=Decimal(3), sold_shares=Decimal(2),
                          proceeds=Decimal("1.2"), posted=kind != "NOT_SENT")
    direct.on_exit_outcome(outcome)
    payload = {**feedback_input(outcome), "intent_id": "captured"}
    restore_feedback(replay, {"kind": "exit", "payload": payload},
                     {"captured": "regenerated"})
    assert replay.persistence_slice() == direct.persistence_slice()


def test_late_confirmation_after_uncertainty_counts_one_post_and_survives_seed():
    from datetime import datetime, timezone

    from tyrex_pm.recording.trading import feedback_input
    from tyrex_pm.strategies.outcomes import EntryOutcome, EntryOutcomeKind

    driver = _feedback_driver()
    driver.state.entry_intent_id = "generated"
    ids = {"original": "generated"}
    at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    for kind, resolved in ((EntryOutcomeKind.UNCERTAIN, False), (EntryOutcomeKind.FILLED, True)):
        outcome = EntryOutcome(kind=kind, intent_id="original", at=at, posted=True,
                               resolved_from_uncertain=resolved,
                               confirmed_shares=Decimal(5), cost=Decimal(2))
        restore_feedback(driver, {"kind": "entry", "payload": feedback_input(outcome)}, ids)
    assert driver.state.window_posts == 1
    restore_feedback(driver, {"kind": "hold", "payload": {}}, ids)
    restored = _feedback_driver()
    restore_feedback(restored, {"kind": "seed", "payload": driver.persistence_slice()}, {})
    assert restored.persistence_slice() == driver.persistence_slice()


@pytest.mark.asyncio
async def test_paper_fills_partial_sell_and_restart_are_reduced_from_export(tmp_path):
    from tyrex_pm.core.snapshots import BookLevel
    from tyrex_pm.domain.polymarket.fees import FeeCurveParams
    from tyrex_pm.execution.coordinator import AccountExecutionCoordinator
    from tyrex_pm.execution.evidence import ExecutionRole
    from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec
    from tyrex_pm.execution.paper import PaperAccount, PaperVenue
    from tyrex_pm.execution.reconciliation import SessionReconciler

    async def gate(spec):
        return True, None

    account = PaperAccount.open(tmp_path / "paper.json", initial_collateral=Decimal(20))
    venue = PaperVenue(account=account, order_latency_ms=0,
                       fee_curve=lambda _: FeeCurveParams(fee_rate=Decimal('.07'),
                                                         exponent=Decimal(1)))
    book = SimpleNamespace(asks=(BookLevel(price=Decimal('.4'), quantity=Decimal(100)),),
                           bids=(BookLevel(price=Decimal('.6'), quantity=Decimal(3)),))
    venue.book_provider = lambda: SimpleNamespace(
        up=SimpleNamespace(token_id="up", book=book),
        down=SimpleNamespace(token_id="down", book=None))
    path = tmp_path / "state/execution.sqlite3"
    journal = SqliteExecutionJournal(path)
    coordinator = AccountExecutionCoordinator(journal=journal, gateway=venue, final_gate=gate)
    await coordinator.apply(SessionOpened(
        **new_envelope(session_id="s", dedupe_key="open"), strategy_id="settlement_value",
        market_id="m", window_id="w", token_id="up"))
    await coordinator.submit(session_id="s", role=ExecutionRole.ENTRY, spec=MarketBuyOrderSpec(
        order_id="buy", market_id="m", token_id="up", instrument_id="up",
        spend_amount=Decimal(4), maximum_total_debit=Decimal(5), worst_price=Decimal('.5'),
        estimated_shares=Decimal(8)))
    await SessionReconciler(venue).reconcile(coordinator, "s")
    held = coordinator.state("s").confirmed_position_shares
    assert held > 3
    first_cursor = coordinator.applied_event_ids["s"]
    await coordinator.submit(session_id="s", role=ExecutionRole.EXIT, spec=MarketSellOrderSpec(
        order_id="sell", market_id="m", token_id="up", instrument_id="up",
        shares=held, minimum_price=Decimal('.5')))
    await SessionReconciler(venue).reconcile(coordinator, "s")
    remaining = coordinator.state("s").confirmed_position_shares
    assert remaining == held - 3
    last_cursor = coordinator.applied_event_ids["s"]
    await coordinator.close()
    restored = AccountExecutionCoordinator(journal=SqliteExecutionJournal(path),
                                           gateway=venue, final_gate=gate)
    assert (await restored.restore("s")).confirmed_position_shares == remaining
    export_evidence(SimpleNamespace(state_directory=path.parent, settlement=None),
                    tmp_path, "paper")
    states = execution_prefixes(tmp_path / "execution_evidence.sqlite3")
    assert Decimal(states[first_cursor]["position_quantity"]) == held
    assert Decimal(states[last_cursor]["position_quantity"]) == remaining
    await restored.close()
