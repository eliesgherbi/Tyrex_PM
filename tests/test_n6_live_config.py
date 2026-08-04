"""N6 Gate 1 — config, authorization, timing ladder, lineage, architecture."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import tyrex_pm
from tyrex_pm.core.intents import IntentId
from tyrex_pm.execution.lineage import (
    LineageRegistry,
    SubmissionAttemptState,
    SubmissionLineage,
    new_submission_attempt_id,
    request_fingerprint,
)
from tyrex_pm.planning.plan import PlanId
from tyrex_pm.runtime.live_config import LiveConfig, LiveScope
from tyrex_pm.runtime.n6_authorization import MutationAuthorization
from tyrex_pm.runtime.scope_a_ladder import ScopeATimingLadder

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
SRC = ROOT / "src" / "tyrex_pm"
T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
EVENT_END = T0 + timedelta(minutes=5)


def _imports_of(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# --- LiveConfig ---------------------------------------------------------------


def test_live_config_defaults_off() -> None:
    c = LiveConfig()
    assert c.enabled is False
    assert c.mutations_enabled is False
    assert c.scope is LiveScope.A


def test_mutations_enabled_requires_enabled() -> None:
    with pytest.raises(ValueError):
        LiveConfig(mutations_enabled=True)  # enabled defaults False → fail closed


def test_scope_b_raises() -> None:
    with pytest.raises(ValueError):
        LiveConfig(scope=LiveScope.B)


def test_fingerprint_stable_and_sensitive() -> None:
    a = LiveConfig(
        enabled=True,
        mutations_enabled=True,
        scope=LiveScope.A,
        max_order_notional=Decimal("10"),
        hard_collateral_cap=Decimal("10"),
    )
    b = LiveConfig(
        enabled=True,
        mutations_enabled=True,
        scope=LiveScope.A,
        max_order_notional=Decimal("10"),
        hard_collateral_cap=Decimal("10"),
    )
    assert a.fingerprint() == b.fingerprint()

    c = LiveConfig(
        enabled=True,
        mutations_enabled=True,
        scope=LiveScope.A,
        max_order_notional=Decimal("9"),
        hard_collateral_cap=Decimal("10"),
    )
    assert a.fingerprint() != c.fingerprint()


# --- MutationAuthorization -----------------------------------------------------


def test_authorization_rejects_real_venue_mutation() -> None:
    with pytest.raises(ValueError):
        MutationAuthorization(
            reason="x",
            transport_kind="fake",
            issued_at=datetime.now(timezone.utc),
            allows_real_venue_mutation=True,
        )


def test_authorization_requires_fake_transport_kind() -> None:
    with pytest.raises(ValueError):
        MutationAuthorization(
            reason="x",
            transport_kind="sdk",
            issued_at=datetime.now(timezone.utc),
        )


def test_authorization_for_fake_transport() -> None:
    auth = MutationAuthorization.for_fake_transport()
    assert auth.transport_kind == "fake"
    assert auth.allows_real_venue_mutation is False


# --- ScopeATimingLadder --------------------------------------------------------


def _valid_ladder(**overrides) -> ScopeATimingLadder:
    kwargs = dict(
        event_end=EVENT_END,
        last_allowed_entry_before_end=timedelta(minutes=4),
        discretionary_exit_cutoff_before_end=timedelta(minutes=3),
        mandatory_flatten_start_before_end=timedelta(minutes=2),
        residual_operator_deadline_before_end=timedelta(minutes=1),
        event_end_safety_buffer=timedelta(seconds=30),
        acknowledgment_timeout=timedelta(seconds=5),
        cancel_recon_budget=timedelta(seconds=10),
    )
    kwargs.update(overrides)
    return ScopeATimingLadder(**kwargs)


def test_ladder_valid_ordering_constructs() -> None:
    ladder = _valid_ladder()
    assert ladder.last_allowed_entry_at == EVENT_END - timedelta(minutes=4)
    assert ladder.mandatory_flatten_start_at == EVENT_END - timedelta(minutes=2)
    assert ladder.entry_allowed(EVENT_END - timedelta(minutes=5)) is True
    assert ladder.entry_allowed(EVENT_END - timedelta(minutes=3)) is False


def test_ladder_out_of_order_raises() -> None:
    with pytest.raises(ValueError):
        _valid_ladder(
            last_allowed_entry_before_end=timedelta(minutes=1),
            discretionary_exit_cutoff_before_end=timedelta(minutes=2),
        )


def test_ladder_non_positive_ack_timeout_raises() -> None:
    with pytest.raises(ValueError):
        _valid_ladder(acknowledgment_timeout=timedelta(0))


# --- Lineage -------------------------------------------------------------------


def test_request_fingerprint_stable_and_ambiguous_blocks() -> None:
    args = dict(
        intent_id=IntentId("i1"),
        plan_id=PlanId("p1"),
        market_id="m1",
        instrument_id="tok-yes",
        side="BUY",
        quantity=Decimal("10"),
        limit_price=Decimal("0.5"),
        client_order_id="c1",
    )
    fp1 = request_fingerprint(**args)
    fp2 = request_fingerprint(**args)
    assert fp1 == fp2

    fp_other = request_fingerprint(**{**args, "market_id": "m2"})
    assert fp1 != fp_other

    reg = LineageRegistry()
    lin = SubmissionLineage(
        intent_id=IntentId("i1"),
        plan_id=PlanId("p1"),
        request_fingerprint=fp1,
        submission_attempt_id=new_submission_attempt_id(),
        state=SubmissionAttemptState.AMBIGUOUS,
    )
    reg.register(lin)
    assert reg.ambiguous_for_fingerprint(fp1) is True
    assert reg.has_dispatched_or_open(fp1) is True
    assert reg.ambiguous_for_fingerprint(fp_other) is False


# --- Architecture --------------------------------------------------------------


def test_zgap_strategies_do_not_import_execution_polymarket_or_r7() -> None:
    zgap_dir = SRC / "strategies" / "z_gap"
    files = sorted(zgap_dir.rglob("*.py"))
    assert files, "expected z_gap strategy modules"
    for path in files:
        imports = _imports_of(path)
        for mod in imports:
            assert not mod.startswith(
                "tyrex_pm.execution.polymarket"
            ), f"{path.name} imports venue execution module {mod}"
            assert not mod.startswith(
                "tyrex_pm.runtime.r7"
            ), f"{path.name} imports r7 runtime {mod}"


def test_n6_live_host_does_not_import_r7() -> None:
    imports = _imports_of(SRC / "runtime" / "n6_live_host.py")
    assert not any(m.startswith("tyrex_pm.runtime.r7") for m in imports)
    assert not any(m == "old" or m.startswith("old.") for m in imports)


def test_cli_has_no_zgap_execute_live() -> None:
    from tyrex_pm.application.cli import build_parser

    text = (SRC / "application" / "cli.py").read_text(encoding="utf-8")
    lowered = text.lower()
    # --execute-live remains wired only for the r7b operator one-shot.
    assert "--execute-live" in text
    # No generic N6 live-execution subcommand / host import.
    assert "n6-live" not in lowered
    for retired in ("n6-status", "n6-preflight", "n6-recon", "n6-kill-inspect"):
        assert retired not in lowered
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices  # noqa: SLF001
    for retired in ("n6-status", "n6-preflight", "n6-recon", "n6-kill-inspect", "live-once"):
        assert retired not in choices
    imports = _imports_of(SRC / "application" / "cli.py")
    assert not any("n6_live_host" in m for m in imports)
    # YAML `run --mode live --live` arms N7 (not N6 / not --execute-live).
    run = None
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        if getattr(action, "choices", None) and "run" in action.choices:
            run = action.choices["run"]
    assert run is not None
    run_opts = {opt for a in run._actions for opt in a.option_strings}  # noqa: SLF001
    assert "--live" in run_opts
    assert "--execute-live" not in run_opts
    mode_action = next(a for a in run._actions if "--mode" in a.option_strings)
    assert set(mode_action.choices or ()) == {"observe", "shadow", "live"}
    assert "n7_operator" in text or "run_yaml_live" in text or "live_run" in text
