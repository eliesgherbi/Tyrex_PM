"""Signal-only observation: structurally order-free, comparable records, multi-window."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import InstrumentId, MarketId, StrategyId, TokenId, new_correlation_id
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.intents import EnterIntent, new_intent_id
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.facts.contract import EligibilityFacts
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import book_quote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.runtime.observation import (
    ObservationConfig,
    ObservationConfigError,
    ObservationRecorder,
    ObservationSession,
    load_observation_config,
    run_observation,
)
from tyrex_pm.strategies.decisions import StrategyAction, StrategyDecision
from tyrex_pm.strategies.evaluation import StrategyEvaluation

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 6, 17, 52, tzinfo=timezone.utc)
FORBIDDEN = (
    "tyrex_pm.execution",
    "tyrex_pm.adapters.polymarket.sdk_secure",
    "tyrex_pm.adapters.polymarket.authentication",
)


def test_observation_import_closure_has_no_execution_or_signing_modules() -> None:
    code = (
        "import sys, tyrex_pm.runtime.observation, tyrex_pm.runtime.market_data_runtime, "
        "tyrex_pm.strategies.settlement_value.plugin;"
        f"bad=[m for m in sys.modules if m.startswith({FORBIDDEN!r})];"
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def _config(tmp_path: Path, **kw) -> ObservationConfig:
    return ObservationConfig(
        run_name="t",
        strategy_kind="settlement_value",
        strategy_parameters={},
        output_directory=tmp_path,
        **kw,
    )


def _snapshot() -> DecisionSnapshot:
    mid = MarketId("m")
    market = BinaryMarket(
        market_id=mid, condition_id="c", question="q",
        yes=Instrument(instrument_id=InstrumentId("up"), market_id=mid, token_id=TokenId("up"),
                       outcome=OutcomeSide.YES),
        no=Instrument(instrument_id=InstrumentId("down"), market_id=mid,
                      token_id=TokenId("down"), outcome=OutcomeSide.NO),
        event_start=T0 - timedelta(minutes=2), event_end=T0 + timedelta(minutes=3),
    )
    book = BookSnapshot(instrument_id=InstrumentId("up"), ts_event=T0,
                        bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("9")),),
                        asks=(BookLevel(price=Decimal("0.5"), quantity=Decimal("9")),))
    fresh = FreshnessAssessment(is_fresh=True, age_ms=0, threshold_ms=1,
                                timestamp_basis=TimestampBasis.EVENT_TIME,
                                reason_code=FreshnessReason.FRESH, observed_at=T0)
    return DecisionSnapshot(
        market=market, yes_book=book, no_book=None, yes_quote=book_quote(book),
        no_quote=book_quote(None), reference=None, yes_freshness=fresh, no_freshness=fresh,
        reference_freshness=fresh, observed_at=T0, correlation_id=new_correlation_id(),
    )


class Binding:
    def __init__(self) -> None:
        self.contexts = []

    def evaluate(self, *, market_snapshot, context, **_):
        self.contexts.append(context)
        intent = EnterIntent(
            intent_id=new_intent_id(), strategy_id=StrategyId("settlement_value"),
            instrument_id=InstrumentId("up"), market_id=MarketId("m"), created_at=T0,
            correlation_id=market_snapshot.correlation_id, causation_id=None,
            reason_code="EDGE_CLEARS_COST", target_notional=Decimal("4"),
            max_price=Decimal("0.5"), evidence={"intended_gross_shares": "8"},
        )
        decision = StrategyDecision(action=StrategyAction.ENTER, reason_code="EDGE_CLEARS_COST",
                                    decided_at=T0, correlation_id=market_snapshot.correlation_id,
                                    evidence={})
        return StrategyEvaluation(
            decision=decision, intents=(intent,), strategy_id=StrategyId("settlement_value"),
            eligibility=EligibilityFacts(strategy_inputs_eligible=True, model_ready=True),
            reporting_context={"decision_row": {"action": "ENTER", "reason": "EDGE_CLEARS_COST"}},
        )


def test_recommendations_are_records_only_and_context_stays_flat(tmp_path: Path) -> None:
    binding = Binding()
    snap = _snapshot()
    ready = SimpleNamespace(
        ok=True, snapshot=snap, binding=binding, dyn=None, sealed=None, skip_reasons=(),
        binance_raw=Decimal("100"), model_spot=Decimal("100"),
        session=SimpleNamespace(window_id="w1", market=snap.market),
    )
    runtime = SimpleNamespace(prepare_aligned_eval=lambda: ready, active=None,
                              indicator_graph=None)
    recorder = ObservationRecorder(tmp_path, flush_rows=1)
    session = ObservationSession(config=_config(tmp_path), recorder=recorder,
                                 clock=FakeClock(_wall=T0))
    for _ in range(2):
        session.on_evaluation(runtime)
    recorder.flush()
    assert session.recommendations == 2
    assert all(c.position_quantity == 0 and c.entry_allowed for c in binding.contexts)
    lines = (tmp_path / "recommendations.jsonl").read_text().splitlines()
    recs = [json.loads(line) for line in lines]
    assert all(r["hypothetical"] and r["no_order_submitted"] for r in recs)
    assert recs[0]["up_asks"] == [["0.5", "9"]]
    decisions = (tmp_path / "decisions.jsonl").read_text().splitlines()
    assert json.loads(decisions[0])["schema"] == "settlement_decision_record_v1"


def test_run_observation_stops_after_max_windows_and_passes_no_execution_hooks(
    tmp_path: Path,
) -> None:
    seen: dict = {}

    async def fake_runner(**kwargs):
        seen.update(kwargs)
        kwargs["on_evaluation"](kwargs["runtime"])  # no active session -> host skip
        for i in range(3):
            if kwargs["should_stop"]():
                break
            kwargs["on_rollover"]({"from_window_id": f"w{i}", "to_window_id": f"w{i + 1}"})
        await kwargs["on_async_tick"](kwargs["runtime"])
        return SimpleNamespace(errors=[])

    result = asyncio.run(
        run_observation(_config(tmp_path, max_windows=2), out_dir=tmp_path,
                        market_data_runner=fake_runner, clock=FakeClock(_wall=T0))
    )
    assert result["rollovers"] == 2 and result["recommendations"] == 0
    assert not {"before_promote", "evaluation_ready", "on_shutdown"} & set(seen)
    assert (tmp_path / "observation_summary.json").exists()
    skipped = json.loads((tmp_path / "decisions.jsonl").read_text().splitlines()[0])
    assert skipped["action"] == "HOST_SKIPPED" and "no_active_session" in skipped["reason"]


def test_observation_explain_level_writes_explanations_and_input_timing(
    tmp_path: Path,
) -> None:
    from tyrex_pm.recording import RecordingConfig, RecordingLevel
    from tyrex_pm.recording.config import ExplainTrigger
    from tyrex_pm.recording.records import iter_records

    async def fake_runner(**kwargs):
        kwargs["on_evaluation"](kwargs["runtime"])  # host skip: a data fault, explained
        return SimpleNamespace(errors=[])

    config = _config(tmp_path, max_windows=1, recording=RecordingConfig(
        level=RecordingLevel.EXPLAIN, explain_trigger=ExplainTrigger.ACTIONABLE))
    result = asyncio.run(run_observation(config, out_dir=tmp_path,
                                         market_data_runner=fake_runner,
                                         clock=FakeClock(_wall=T0)))
    assert result["recording"]["streams"]["explain"]["written"] == 1
    assert "decisions" not in result["recording"]["streams"]  # written by observation
    (explained,) = iter_records(tmp_path / "recording", "explain")
    assert explained["action"] == "HOST_SKIPPED" and explained["mode"] == "observe"
    row = json.loads((tmp_path / "decisions.jsonl").read_text().splitlines()[0])
    assert set(row["inputs"]) >= {"decision_at", "up_book", "reference", "up_leg"}


def test_observation_config_is_strict(tmp_path: Path) -> None:
    good = load_observation_config(ROOT / "config" / "observe" / "settlement_value_observe.yaml")
    assert good.strategy_kind == "settlement_value" and good.max_windows == 12
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nmode: live\nstrategy: {kind: settlement_value}\n")
    with pytest.raises(ObservationConfigError, match="observe"):
        load_observation_config(bad)
    bad.write_text(
        "schema_version: 1\nmode: observe\nrisk: {}\nstrategy: {kind: settlement_value}\n"
    )
    with pytest.raises(ObservationConfigError, match="unknown"):
        load_observation_config(bad)


def test_cli_observe_refuses_live_and_requires_explicit_feed_flag() -> None:
    from tyrex_pm.application.cli import main

    cfg = str(ROOT / "config" / "observe" / "settlement_value_observe.yaml")
    assert main(["observe", "--config", cfg, "--live"]) == 2
    assert main(["observe", "--config", cfg]) == 2
    assert main(["observe", "--config", cfg, "--validate-config"]) == 0
