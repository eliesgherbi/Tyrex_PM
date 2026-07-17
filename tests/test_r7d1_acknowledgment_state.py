"""R7D.1 durable acknowledgment gate — reports vs state separation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from datetime import datetime, timezone

from tyrex_pm.runtime.r7_ack_gate import enforce_acknowledgment_gate
from tyrex_pm.runtime.r7_ack_policy import (
    AcknowledgmentPolicy,
    PolicyIdentity,
    write_acknowledgment_policy,
)
from tyrex_pm.runtime.r7_ack_regenerate import regenerate_acknowledgment
from tyrex_pm.runtime.r7_lifecycle_dust import (
    INCIDENT_DUST_TOKEN,
    default_incident_dust_record,
    write_lifecycle_dust,
)
from tyrex_pm.runtime.r7_lifecycle_residuals import migrate_dust_to_registry
from tyrex_pm.runtime.r7_paths import (
    DEFAULT_ACKNOWLEDGMENT_PATH,
    R7B_REPORT_DIR,
    R7_STATE_DIR,
)
from tyrex_pm.runtime.r7_position_ack import build_acknowledgment, write_acknowledgment
from tyrex_pm.runtime.r7b_live_once import TerminalOutcome, run_r7b_live_once
from test_r7b_live_once import RaisingTransport, _base_args, _write_ack


def _four() -> list[dict[str, Any]]:
    return [
        {
            "conditionId": f"0xc{i}",
            "asset": f"t{i}",
            "outcome": "Up",
            "size": "5",
            "redeemable": True,
            "curPrice": 0,
            "slug": f"s{i}",
        }
        for i in range(4)
    ]


def _seed_policy(tmp_path: Path, rows: list[dict[str, Any]] | None = None) -> None:
    rows = rows or _four()
    write_acknowledgment_policy(
        AcknowledgmentPolicy(
            schema_version="r7_acknowledgment_policy_v1",
            policy_id="ACK_RESOLVED_REDEEMABLE_UNTOUCHED_R7A1",
            expected_count=4,
            identities=[
                PolicyIdentity(
                    condition_id=str(r["conditionId"]),
                    token_id=str(r["asset"]),
                    slug=str(r.get("slug") or ""),
                )
                for r in rows
            ],
            created_at=datetime.now(timezone.utc).isoformat(),
            source="test_r7d1",
            sealed=True,
        ),
        repo_root=tmp_path,
    )


def test_deleting_reports_does_not_delete_durable_ack(tmp_path: Path) -> None:
    state = tmp_path / "var" / "state" / "r7"
    reports = tmp_path / "var" / "reporting" / "r7b"
    state.mkdir(parents=True)
    reports.mkdir(parents=True)
    ack_path = state / "position_acknowledgment.json"
    rows = _four()
    ack = build_acknowledgment(raw_positions=rows, commit_identity="c")
    write_acknowledgment(ack_path, ack)
    (reports / "report_old.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(reports)
    assert ack_path.exists()
    assert not reports.exists()
    assert R7_STATE_DIR.as_posix().endswith("var/state/r7")
    assert "reporting" in R7B_REPORT_DIR.as_posix()
    assert "state" in DEFAULT_ACKNOWLEDGMENT_PATH.as_posix()


def test_missing_durable_ack_blocks_dry(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=missing,
            dry_run=True,
            mutation_transport=RaisingTransport(),
        )
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert "ACKNOWLEDGMENT_ARTIFACT_MISSING" in result.report["blockers"]


def test_missing_durable_ack_blocks_live(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            acknowledgment_path=tmp_path / "missing.json",
        )
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert "ACKNOWLEDGMENT_ARTIFACT_MISSING" in result.report["blockers"]
    assert spy.submitted == []


def test_malformed_artifact_blocks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    result = run_r7b_live_once(
        _base_args(tmp_path, acknowledgment_path=bad, dry_run=True)
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert "ACKNOWLEDGMENT_ARTIFACT_INVALID" in result.report["blockers"]


def test_explicit_nonexistent_override_blocks(tmp_path: Path) -> None:
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=tmp_path / "override_missing.json",
            dry_run=True,
        )
    )
    assert "ACKNOWLEDGMENT_ARTIFACT_MISSING" in result.report["blockers"]


def test_none_path_blocks() -> None:
    g = enforce_acknowledgment_gate(
        acknowledgment_path=None,
        raw_positions=_four(),
        require_path=True,
    )
    assert not g.ok
    assert "ACKNOWLEDGMENT_PATH_REQUIRED" in g.blockers


def test_valid_regenerated_artifact_permits_dry(tmp_path: Path) -> None:
    rows = _four()
    _seed_policy(tmp_path, rows)
    report = regenerate_acknowledgment(
        repo_root=tmp_path,
        output_path=tmp_path / "var" / "state" / "r7" / "position_acknowledgment.json",
        positions_provider=lambda: rows,
        write_residual_state=True,
    )
    assert report["ok"] is True
    assert report["mutations_attempted"] is False
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=Path(report["path"]),
            positions_provider=lambda: rows,
            dry_run=True,
            mutation_transport=RaisingTransport(),
        )
    )
    assert result.outcome is TerminalOutcome.DRY_OK
    assert result.report["acknowledgment"]["ok"] is True
    assert result.report["acknowledgment"]["content_hash"]
    assert result.report["lifecycle_dust"] is not None
    assert result.report["lifecycle_dust"]["classification"] == "FLAT_WITH_DUST"
    assert result.report["lifecycle_residuals"] is not None


def test_changed_fingerprint_blocks(tmp_path: Path) -> None:
    path, rows = _write_ack(tmp_path)
    changed = list(rows)
    changed[0] = {**changed[0], "size": "99"}
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=path,
            positions_provider=lambda: changed,
            dry_run=True,
        )
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert any(
        b in result.report["blockers"]
        for b in ("ACK_POSITION_SET_MISMATCH", "ACKNOWLEDGMENT_INVALID")
    ) or result.report["acknowledgment"]["ok"] is False


def test_incomplete_inventory_blocks(tmp_path: Path) -> None:
    path, _rows = _write_ack(tmp_path)
    incomplete = [{"conditionId": "0x1", "asset": "t1", "size": "5"}]
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=path,
            positions_provider=lambda: incomplete,
            dry_run=True,
        )
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert "ACK_INVENTORY_ROW_INCOMPLETE" in result.report["blockers"]


def test_dry_and_live_share_ack_gate(tmp_path: Path) -> None:
    missing = tmp_path / "gone.json"
    dry = run_r7b_live_once(
        _base_args(tmp_path, acknowledgment_path=missing, dry_run=True)
    )
    live = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=missing,
            execute_live=True,
            mutation_transport=SpyMutationTransport(),
        )
    )
    assert dry.outcome is TerminalOutcome.BLOCKED
    assert live.outcome is TerminalOutcome.BLOCKED
    assert dry.report["blockers"][0] == live.report["blockers"][0]


def test_dust_visible_and_not_ack_target(tmp_path: Path) -> None:
    path, rows = _write_ack(tmp_path)
    write_lifecycle_dust(repo_root=tmp_path, record=default_incident_dust_record())
    migrate_dust_to_registry(repo_root=tmp_path, force_incident=False)
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=path,
            positions_provider=lambda: rows,
            dry_run=True,
        )
    )
    assert result.outcome is TerminalOutcome.DRY_OK
    dust = result.report["lifecycle_dust"]
    assert dust["token_id"] == INCIDENT_DUST_TOKEN
    assert dust["in_acknowledgment_set"] is False
    assert dust["classification"] == "FLAT_WITH_DUST"
    ack_tokens = {p["token_suffix"] for p in result.report["acknowledgment"]["positions"]}
    assert INCIDENT_DUST_TOKEN[-8:] not in ack_tokens


def test_regeneration_zero_mutations(tmp_path: Path) -> None:
    rows = _four()
    _seed_policy(tmp_path, rows)
    report = regenerate_acknowledgment(
        repo_root=tmp_path,
        output_path=tmp_path / "ack.json",
        positions_provider=lambda: rows,
        write_residual_state=False,
    )
    assert report["mutations_attempted"] is False
    assert report["mutations_enabled"] is False
    assert Path(report["path"]).exists()
