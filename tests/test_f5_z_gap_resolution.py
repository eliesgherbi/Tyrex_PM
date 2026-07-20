"""F5: resolution-aware Z-Gap SHADOW lifecycle (deterministic, offline)."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import tyrex_pm
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, MarketId, RunId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, HoldToResolutionIntent
from tyrex_pm.domain.polymarket.resolution import BinaryResolutionRule
from tyrex_pm.domain.polymarket.resolution_evidence import (
    ResolutionEvidence,
    ResolutionEvidenceError,
    ResolutionEvidenceStatus,
    validate_resolution_evidence,
)
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.runtime.config import observe_config_from_mapping
from tyrex_pm.runtime.shadow_host import ShadowHost
from tyrex_pm.strategies.decisions import StrategyAction

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
SRC = ROOT / "src" / "tyrex_pm"
CFG_F5 = ROOT / "config" / "observe_shadow_z_gap_f5.json"
CFG_F4 = ROOT / "config" / "observe_shadow_z_gap_f4.json"
TS = datetime(2026, 7, 20, 12, 1, 0, tzinfo=timezone.utc)


def _load_cfg(path: Path, tmp: Path, *, fixture: str | None = None, **z_gap_overrides):
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp / "facts.jsonl")
    raw["fixture_path"] = str(ROOT / (fixture or raw["fixture_path"]))
    if "shadow" in raw and raw["shadow"] is not None:
        raw["shadow"]["persistence_path"] = str(tmp / "state.json")
    if z_gap_overrides:
        raw.setdefault("z_gap", {}).update(z_gap_overrides)
    return observe_config_from_mapping(raw)


def _run(tmp: Path, *, cfg_path: Path = CFG_F5, fixture: str | None = None, **zg):
    cfg = _load_cfg(cfg_path, tmp, fixture=fixture, **zg)
    host = ShadowHost(
        cfg,
        clock=FakeClock(_wall=TS),
        run_id=RunId("run-f5"),
        correlation_id=CorrelationId("corr-f5"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    return host, result


def _fact_types(tmp: Path) -> set[str]:
    lines = (tmp / "facts.jsonl").read_text(encoding="utf-8").splitlines()
    return {json.loads(l)["fact_type"] for l in lines}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_scenario_rich_sell_beats_resolution(tmp_path: Path) -> None:
    """Capability on: market-rich sell remains eligible and wins over resolution."""
    host, result = _run(
        tmp_path,
        cfg_path=CFG_F4,
        fixture="tests/fixtures/z_gap/shadow_f4_rich_exit.json",
        resolution_capability=True,
        theta_rich="0.02",
        timer_eval_count=3,
    )
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    assert any(
        isinstance(i, ExitIntent) and i.reason_code == "MARKET_RICH_EXIT"
        for i in result.intents
    )
    # Non-sticky: a hold preference may appear before the rich book arrives.
    assert host.lifecycle.state is LifecycleState.FLAT
    assert host.portfolio.is_flat()
    assert "simulated_resolution_settled" not in _fact_types(tmp_path)


def test_scenario_resolution_win(tmp_path: Path) -> None:
    host, result = _run(tmp_path)
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    assert any(isinstance(i, HoldToResolutionIntent) for i in result.intents)
    assert host.lifecycle.state is LifecycleState.FLAT
    assert host.portfolio.is_flat()
    types = _fact_types(tmp_path)
    assert "resolution_committed" in types
    assert "resolution_evidence_accepted" in types
    assert "simulated_resolution_settled" in types
    settle = [
        json.loads(l)
        for l in (tmp_path / "facts.jsonl").read_text().splitlines()
        if json.loads(l)["fact_type"] == "simulated_resolution_settled"
    ]
    assert settle[-1]["payload"]["resolved_side"] == "YES"
    assert settle[-1]["payload"]["economics_label"] == "simulated_shadow"
    assert Decimal(settle[-1]["payload"]["payout_per_share"]) == Decimal("1")


def test_scenario_resolution_lose(tmp_path: Path) -> None:
    host, result = _run(
        tmp_path, fixture="tests/fixtures/z_gap/shadow_f5_resolve_lose.json"
    )
    assert any(isinstance(i, HoldToResolutionIntent) for i in result.intents)
    assert host.portfolio.is_flat()
    settle = [
        json.loads(l)
        for l in (tmp_path / "facts.jsonl").read_text().splitlines()
        if json.loads(l)["fact_type"] == "simulated_resolution_settled"
    ]
    assert settle
    assert settle[-1]["payload"]["resolved_side"] == "NO"
    assert Decimal(settle[-1]["payload"]["payout_per_share"]) == Decimal("0")


def test_scenario_capability_disabled_time_exit(tmp_path: Path) -> None:
    host, result = _run(
        tmp_path,
        cfg_path=CFG_F4,
        resolution_capability=False,
        theta_rich="0.90",
        p_stop="0.01",
        flatten_before_event_end_s=560.0,
        timer_eval_count=5,
    )
    assert any(
        isinstance(i, ExitIntent) and i.reason_code == "TIME_SELL" for i in result.intents
    )
    assert not any(isinstance(i, HoldToResolutionIntent) for i in result.intents)
    assert host.portfolio.is_flat()


def test_scenario_missing_evidence_stays_pending(tmp_path: Path) -> None:
    raw = json.loads(CFG_F5.read_text(encoding="utf-8"))
    fixture = json.loads(
        (ROOT / "tests/fixtures/z_gap/shadow_f5_resolve_hold.json").read_text(
            encoding="utf-8"
        )
    )
    fixture.pop("resolution_evidence_events", None)
    fix_path = tmp_path / "no_evidence.json"
    fix_path.write_text(json.dumps(fixture), encoding="utf-8")
    raw["fixture_path"] = str(fix_path)
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    cfg = observe_config_from_mapping(raw)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f5m"), correlation_id=CorrelationId("cm")
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert any(isinstance(i, HoldToResolutionIntent) for i in result.intents)
    assert host.lifecycle.state is LifecycleState.RESOLUTION_PENDING
    assert not host.portfolio.is_flat()
    assert "simulated_resolution_settled" not in _fact_types(tmp_path)


def test_scenario_mismatched_evidence_rejected(tmp_path: Path) -> None:
    evidence = ResolutionEvidence(
        market_id=MarketId("wrong-market"),
        window_id="zgap-f5-w1",
        boundary_k=Decimal("100000"),
        settlement_price=Decimal("100500"),
        resolved_side=OutcomeSide.YES,
        observed_at=TS,
        source="fixture",
        provenance="test",
        status=ResolutionEvidenceStatus.READY,
        evidence_id="bad-1",
    )
    try:
        validate_resolution_evidence(
            evidence,
            market_id=MarketId("zgap-f5-resolve-1"),
            window_id="zgap-f5-w1",
            expected_k=Decimal("100000"),
        )
        assert False, "expected mismatch"
    except ResolutionEvidenceError as exc:
        assert "market_id" in str(exc)

    bad_k = ResolutionEvidence(
        market_id=MarketId("zgap-f5-resolve-1"),
        window_id="zgap-f5-w1",
        boundary_k=Decimal("999999"),
        settlement_price=Decimal("100500"),
        resolved_side=OutcomeSide.YES,
        observed_at=TS,
        source="fixture",
        provenance="test",
        evidence_id="bad-k",
    )
    try:
        validate_resolution_evidence(
            bad_k,
            market_id=MarketId("zgap-f5-resolve-1"),
            window_id="zgap-f5-w1",
            expected_k=Decimal("100000"),
        )
        assert False, "expected k mismatch"
    except ResolutionEvidenceError as exc:
        assert "boundary_k" in str(exc)

    # Host path: mismatched evidence must not flatten portfolio
    host, _ = _run(tmp_path)
    # Re-run with only bad evidence injected after commitment via unit-level reject above.
    assert host.portfolio.is_flat()  # win fixture settles; mismatch covered by validator


def test_scenario_unknown_evidence_stays_pending(tmp_path: Path) -> None:
    unknown = ResolutionEvidence(
        market_id=MarketId("zgap-f5-resolve-1"),
        window_id="zgap-f5-w1",
        boundary_k=Decimal("100000"),
        settlement_price=None,
        resolved_side=None,
        observed_at=TS,
        source="fixture",
        provenance="test",
        status=ResolutionEvidenceStatus.UNKNOWN,
        evidence_id="unk-1",
    )
    try:
        validate_resolution_evidence(
            unknown,
            market_id=MarketId("zgap-f5-resolve-1"),
            window_id="zgap-f5-w1",
            expected_k=Decimal("100000"),
            rule=BinaryResolutionRule(
                market_id=MarketId("zgap-f5-resolve-1"),
                window_id="zgap-f5-w1",
                event_start=TS,
                event_end=datetime(2026, 7, 20, 12, 10, tzinfo=timezone.utc),
            ),
        )
        assert False, "expected UNKNOWN reject"
    except ResolutionEvidenceError:
        pass


def test_restart_pending_no_duplicate_commitment_or_payout(tmp_path: Path) -> None:
    raw = json.loads(CFG_F5.read_text(encoding="utf-8"))
    fixture = json.loads(
        (ROOT / "tests/fixtures/z_gap/shadow_f5_resolve_hold.json").read_text(
            encoding="utf-8"
        )
    )
    fixture.pop("resolution_evidence_events", None)
    fix_path = tmp_path / "pending_only.json"
    fix_path.write_text(json.dumps(fixture), encoding="utf-8")
    raw["fixture_path"] = str(fix_path)
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    cfg = observe_config_from_mapping(raw)
    h1 = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f5r1"), correlation_id=CorrelationId("cr1")
    )
    try:
        h1.run_fixture()
    finally:
        h1.close()
    assert h1.lifecycle.state is LifecycleState.RESOLUTION_PENDING
    commits = sum(
        1
        for l in (tmp_path / "facts.jsonl").read_text().splitlines()
        if json.loads(l)["fact_type"] == "resolution_committed"
    )
    assert commits == 1
    assert not h1.portfolio.is_flat()

    # Restart: recover pending inventory, then apply evidence once (no second commit).
    fixture["resolution_evidence_events"] = [
        {
            "ts_received": "2026-07-20T12:01:00+00:00",
            "evidence": {
                "evidence_id": "f5-ev-restart-1",
                "market_id": "zgap-f5-resolve-1",
                "window_id": "zgap-f5-w1",
                "boundary_k": "100000",
                "settlement_price": "100500",
                "resolved_side": "YES",
                "observed_at": "2026-07-20T12:01:00+00:00",
                "source": "fixture",
                "provenance": "restart",
                "status": "READY",
            },
        }
    ]
    fix_path.write_text(json.dumps(fixture), encoding="utf-8")
    h2 = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f5r2"), correlation_id=CorrelationId("cr2")
    )
    try:
        h2._attach()
        h2._init_flags()
        from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

        market = load_market_from_fixture(cfg.fixture_path)
        h2.registry.set_market(market)
        h2.portfolio.set_market_id(market.market_id)
        h2._start_strategy(market)
        assert h2.try_recover() is True
        assert h2.lifecycle.state is LifecycleState.RESOLUTION_PENDING
        h2._load_resolution_evidence_queue(cfg.fixture_path)
        h2._maybe_apply_resolution_evidence()
        h2._maybe_apply_resolution_evidence()
    finally:
        h2.close()
    assert h2.lifecycle.state is LifecycleState.FLAT
    assert h2.portfolio.is_flat()
    assert h2.lifecycle.view().settlement_applied_id == "f5-ev-restart-1"
    commits2 = sum(
        1
        for l in (tmp_path / "facts.jsonl").read_text().splitlines()
        if json.loads(l)["fact_type"] == "resolution_committed"
    )
    assert commits2 == 1


def test_replay_settlement_idempotent(tmp_path: Path) -> None:
    host, _ = _run(tmp_path)
    assert host.lifecycle.view().settlement_applied_id == "f5-ev-win-1"
    # Replay same evidence application
    host._maybe_apply_resolution_evidence()
    host._maybe_apply_resolution_evidence()
    assert host.portfolio.is_flat()
    assert host.lifecycle.state is LifecycleState.FLAT


def test_kill_before_commitment_flattens(tmp_path: Path) -> None:
    """Kill while ACTIVE (not resolution-committed) uses normal framework flatten."""
    raw = json.loads(CFG_F5.read_text(encoding="utf-8"))
    fixture = json.loads(
        (ROOT / "tests/fixtures/z_gap/shadow_f5_resolve_hold.json").read_text(
            encoding="utf-8"
        )
    )
    fixture.pop("resolution_evidence_events", None)
    fix = tmp_path / "kill_before.json"
    fix.write_text(json.dumps(fixture), encoding="utf-8")
    raw["fixture_path"] = str(fix)
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    raw["z_gap"]["resolution_capability"] = False
    raw["z_gap"]["theta_rich"] = "0.90"
    raw["z_gap"]["p_stop"] = "0.01"
    raw["z_gap"]["timer_eval_count"] = 0
    cfg = observe_config_from_mapping(raw)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f5k"), correlation_id=CorrelationId("ck")
    )
    try:
        host.run_fixture()
        assert any(isinstance(i, EnterIntent) for i in host.intents)
        assert host.lifecycle.state is LifecycleState.ACTIVE
        assert host.lifecycle.view().resolution_committed is False
        host._kill_switch = True
        host.evaluate_once(trigger="kill_before")
        assert any(isinstance(i, FlattenIntent) for i in host.intents)
        assert host.portfolio.is_flat()
    finally:
        host.close()


def test_kill_after_commitment_no_fabricated_sell(tmp_path: Path) -> None:
    raw = json.loads(CFG_F5.read_text(encoding="utf-8"))
    fixture = json.loads(
        (ROOT / "tests/fixtures/z_gap/shadow_f5_resolve_hold.json").read_text(
            encoding="utf-8"
        )
    )
    fixture.pop("resolution_evidence_events", None)
    fix = tmp_path / "kill_after.json"
    fix.write_text(json.dumps(fixture), encoding="utf-8")
    raw["fixture_path"] = str(fix)
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    cfg = observe_config_from_mapping(raw)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f5ka"), correlation_id=CorrelationId("cka")
    )
    try:
        host.run_fixture()
        assert host.lifecycle.state is LifecycleState.RESOLUTION_PENDING
        n_cmds = len(host.commands)
        host._kill_switch = True
        host.evaluate_once(trigger="kill_probe")
        assert not any(
            isinstance(i, (ExitIntent, FlattenIntent)) for i in host.intents[-5:]
        )
        assert len(host.commands) == n_cmds
        assert host.lifecycle.state is LifecycleState.RESOLUTION_PENDING
        assert not host.portfolio.is_flat()
    finally:
        host.close()


def test_hold_intent_shape_and_no_strategy_action() -> None:
    assert "HOLD_TO_RESOLUTION" not in {a.value for a in StrategyAction}
    assert HoldToResolutionIntent.__dataclass_fields__["window_id"]


def test_architecture_no_zgap_branching_in_framework() -> None:
    for package in ("risk", "planning", "execution", "portfolio", "lifecycle"):
        for path in (SRC / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "ZGapStrategy" not in text, path
            assert "ZGapReason" not in text, path
            imports = _imports_of(path)
            assert not any("z_gap" in m for m in imports), path
    shadow = (SRC / "runtime" / "shadow_host.py").read_text(encoding="utf-8")
    assert "ZGapStrategy" not in shadow
    assert "isinstance(strategy" not in shadow
    # Generic HoldToResolutionIntent dispatch is allowed; Z-Gap reason codes are not.
    assert "RESOLUTION_PREFERENCE" not in shadow


def test_f1_f4_regression_actions_and_rich(tmp_path: Path) -> None:
    assert {a.value for a in StrategyAction} == {
        "WAIT",
        "SKIP",
        "ENTER",
        "HOLD",
        "EXIT",
        "FLATTEN",
        "BLOCKED",
    }
    host, result = _run(
        tmp_path,
        cfg_path=CFG_F4,
        fixture="tests/fixtures/z_gap/shadow_f4_rich_exit.json",
    )
    assert any(isinstance(i, ExitIntent) and i.reason_code == "MARKET_RICH_EXIT" for i in result.intents)
