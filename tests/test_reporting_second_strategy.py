"""Fake second-strategy diagnostics registration (no reporting-core changes)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.reporting import open_run_reporter
from tyrex_pm.reporting.adapters import emit_decision_from_eval
from tyrex_pm.reporting.contracts import StrategyDiagnosticsBlob


@dataclass
class FakeMomentumDiagnosticsContract:
    """Minimal second-strategy contract registered at composition time."""

    diagnostics_schema_version: str = "fake_momentum.v1"

    def build_diagnostics(self, ctx: Mapping[str, Any]) -> StrategyDiagnosticsBlob:
        return StrategyDiagnosticsBlob(
            namespace="fake_momentum",
            schema_version=self.diagnostics_schema_version,
            values={
                "momentum": str(ctx.get("momentum")),
                "threshold": str(ctx.get("threshold")),
            },
        )

    def build_gates(self, ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
        passed = float(ctx.get("momentum", 0)) >= float(ctx.get("threshold", 1))
        return [
            {
                "gate_id": "momentum_threshold",
                "passed": passed,
                "detail": {"momentum": ctx.get("momentum"), "threshold": ctx.get("threshold")},
            }
        ]

    def closest_candidate_fields(self, ctx: Mapping[str, Any]) -> dict[str, Any] | None:
        return None


def test_fake_second_strategy_registers_without_core_changes(tmp_path: Path):
    contract = FakeMomentumDiagnosticsContract()
    ctx = {"momentum": 1.5, "threshold": 1.0}
    rep = open_run_reporter(
        run_dir=tmp_path / "fake_strat",
        run_id="fs1",
        mode="observe",
        strategy_id="fake_momentum",
        strategy_version="test",
        diagnostics=contract,  # type: ignore[arg-type]
    )
    diagnostics = contract.build_diagnostics(ctx)
    gates = contract.build_gates(ctx)
    emit_decision_from_eval(
        rep,
        action="ENTER",
        reason_code="MOMENTUM_OK",
        decision_id="d1",
        evaluation_id="e1",
        gates=gates,
        diagnostics=diagnostics,
        producer="fake_momentum_binding",
        strategy_id="fake_momentum",
        strategy_version="test",
    )
    rep.finalize()
    audit = (tmp_path / "fake_strat" / "audit_events.jsonl").read_text(encoding="utf-8")
    # Decision may be analytics or critical depending on intent; check either lane.
    analytics = (tmp_path / "fake_strat" / "analytics_events.jsonl").read_text(encoding="utf-8")
    blob = audit + analytics
    assert "fake_momentum.v1" in blob or "momentum_threshold" in blob
    assert "MOMENTUM_OK" in blob
