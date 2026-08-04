"""R7D.2: sealed ack policy cannot broaden; multi-record residual registry."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tyrex_pm.runtime.r7_ack_policy import (
    AcknowledgmentPolicy,
    PolicyIdentity,
    assert_policy_not_broadened,
    load_acknowledgment_policy,
    select_rows_for_policy,
    write_acknowledgment_policy,
)
from tyrex_pm.runtime.r7_ack_regenerate import regenerate_acknowledgment
from tyrex_pm.runtime.r7_lifecycle_residuals import (
    LifecycleResidualRecord,
    LifecycleResidualRegistry,
    close_residual_if_zero,
    evaluate_residuals_for_entry,
    migrate_dust_to_registry,
    read_residual_registry,
    residual_identity_key,
    upsert_residual,
    write_residual_registry,
)
from tyrex_pm.runtime.r7_lifecycle_dust import (
    INCIDENT_DUST_TOKEN,
    default_incident_dust_record,
    write_lifecycle_dust,
)
from tyrex_pm.runtime.r7_position_ack import AckError, identity_key
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


def _policy_from_rows(rows: list[dict[str, Any]]) -> AcknowledgmentPolicy:
    ids = [
        PolicyIdentity(
            condition_id=str(r["conditionId"]),
            token_id=str(r["asset"]),
            slug=str(r.get("slug") or ""),
        )
        for r in rows
    ]
    return AcknowledgmentPolicy(
        schema_version="r7_acknowledgment_policy_v1",
        policy_id="ACK_RESOLVED_REDEEMABLE_UNTOUCHED_R7A1",
        expected_count=4,
        identities=ids,
        created_at=datetime.now(timezone.utc).isoformat(),
        source="test_sealed",
        sealed=True,
    )


def _seed_policy(tmp_path: Path, rows: list[dict[str, Any]] | None = None) -> AcknowledgmentPolicy:
    rows = rows or _four()
    pol = _policy_from_rows(rows)
    write_acknowledgment_policy(pol, repo_root=tmp_path)
    return pol


def test_regenerate_uses_sealed_policy_not_all_resolved(tmp_path: Path) -> None:
    rows = _four()
    _seed_policy(tmp_path, rows)
    fifth = {
        "conditionId": "0xextra",
        "asset": "textra",
        "outcome": "Up",
        "size": "7",
        "redeemable": True,
        "curPrice": 0,
        "slug": "extra",
    }
    report = regenerate_acknowledgment(
        repo_root=tmp_path,
        output_path=tmp_path / "var" / "state" / "r7" / "position_acknowledgment.json",
        positions_provider=lambda: rows + [fifth],
        write_residual_state=False,
    )
    assert report["ok"] is True
    assert len(report["positions"]) == 4
    suffixes = {p["token_suffix"] for p in report["positions"]}
    assert "textra"[-8:] not in suffixes
    assert "textra" not in "".join(report["policy_identity_keys"])


def test_fifth_resolved_not_auto_acknowledged(tmp_path: Path) -> None:
    pol = _seed_policy(tmp_path)
    inventory = _four() + [
        {
            "conditionId": "0xfifth",
            "asset": "tfifth",
            "size": "1",
            "redeemable": True,
            "outcome": "Up",
        }
    ]
    selected = select_rows_for_policy(inventory, pol)
    assert len(selected) == 4
    assert all(str(r["asset"]) != "tfifth" for r in selected)
    assert_policy_not_broadened(pol, selected)


def test_changed_token_or_condition_blocks_regeneration(tmp_path: Path) -> None:
    rows = _four()
    _seed_policy(tmp_path, rows)
    changed = list(rows)
    changed[0] = {**changed[0], "asset": "tCHANGED"}
    with pytest.raises(AckError, match="ACK_POLICY_IDENTITY_MISSING_OR_CHANGED"):
        regenerate_acknowledgment(
            repo_root=tmp_path,
            output_path=tmp_path / "ack.json",
            positions_provider=lambda: changed,
            write_residual_state=False,
        )


def test_regeneration_cannot_broaden_policy(tmp_path: Path) -> None:
    rows = _four()
    pol = _seed_policy(tmp_path, rows)
    # Attempt to select extra via assert
    with pytest.raises(AckError, match="ACK_POLICY_BROADEN"):
        assert_policy_not_broadened(
            pol,
            rows
            + [
                {
                    "conditionId": "0xnew",
                    "asset": "tnew",
                    "size": "1",
                    "redeemable": True,
                }
            ],
        )


def test_policy_source_separate_from_disposable_reports(tmp_path: Path) -> None:
    rows = _four()
    _seed_policy(tmp_path, rows)
    reports = tmp_path / "var" / "reporting" / "r7"
    reports.mkdir(parents=True)
    (reports / "old_ack.json").write_text("{}", encoding="utf-8")
    # Delete reports; policy + regenerate still work
    import shutil

    shutil.rmtree(reports)
    assert not reports.exists()
    pol = load_acknowledgment_policy(repo_root=tmp_path)
    assert len(pol.identities) == 4
    report = regenerate_acknowledgment(
        repo_root=tmp_path,
        output_path=tmp_path / "var" / "state" / "r7" / "position_acknowledgment.json",
        positions_provider=lambda: rows,
        write_residual_state=False,
    )
    assert report["ok"] is True
    assert "config/r7/acknowledgment_policy.json" in report["policy_paths"]
    assert "reporting" not in report["policy_paths"][0]


def test_missing_policy_blocks_blind_regenerate(tmp_path: Path) -> None:
    with pytest.raises(AckError, match="ACK_POLICY_MISSING"):
        regenerate_acknowledgment(
            repo_root=tmp_path,
            output_path=tmp_path / "ack.json",
            positions_provider=lambda: _four(),
            write_residual_state=False,
        )


def test_two_markets_separate_dust_records(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    reg = LifecycleResidualRegistry()
    r1 = LifecycleResidualRecord(
        condition_id="0xcondA",
        token_id="tokenA",
        originating_run_id="run-1",
        market_slug="btc-updown-5m-111",
        acquired_quantity="9.47",
        exited_quantity="9.47",
        residual_quantity="0.0005",
        min_tradable="0.01",
        classification="FLAT_WITH_DUST",
        provenance="lifecycle_1",
        created_at=now,
        updated_at=now,
        last_reconciliation_source="test",
        tradable=False,
    )
    r2 = LifecycleResidualRecord(
        condition_id="0xcondB",
        token_id="tokenB",
        originating_run_id="run-2",
        market_slug="btc-updown-5m-222",
        acquired_quantity="5",
        exited_quantity="4.999",
        residual_quantity="0.001",
        min_tradable="0.01",
        classification="FLAT_WITH_DUST",
        provenance="lifecycle_2",
        created_at=now,
        updated_at=now,
        last_reconciliation_source="test",
        tradable=False,
    )
    upsert_residual(reg, r1)
    upsert_residual(reg, r2)
    write_residual_registry(reg, repo_root=tmp_path)
    loaded = read_residual_registry(repo_root=tmp_path)
    assert loaded is not None
    assert len(loaded.open_residuals()) == 2
    keys = set(loaded.residuals)
    assert residual_identity_key("0xcondA", "tokenA", "run-1") in keys
    assert residual_identity_key("0xcondB", "tokenB", "run-2") in keys
    # Second lifecycle must not overwrite first
    assert loaded.residuals[r1.identity_key].provenance == "lifecycle_1"
    assert loaded.residuals[r2.identity_key].provenance == "lifecycle_2"
    ev = evaluate_residuals_for_entry(loaded, selected_token_id="tokenC")
    assert ev["ok"] is True
    assert ev["open_count"] == 2


def test_second_lifecycle_does_not_overwrite_first_dust(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    reg = LifecycleResidualRegistry()
    first = LifecycleResidualRecord(
        condition_id="0x1",
        token_id="t1",
        originating_run_id="run-a",
        market_slug="m1",
        acquired_quantity="1",
        exited_quantity="0.999",
        residual_quantity="0.001",
        min_tradable="0.01",
        classification="FLAT_WITH_DUST",
        provenance="first",
        created_at=now,
        updated_at=now,
        last_reconciliation_source="a",
        tradable=False,
    )
    upsert_residual(reg, first)
    second = LifecycleResidualRecord(
        condition_id="0x2",
        token_id="t2",
        originating_run_id="run-b",
        market_slug="m2",
        acquired_quantity="2",
        exited_quantity="2",
        residual_quantity="0.0002",
        min_tradable="0.01",
        classification="FLAT_WITH_DUST",
        provenance="second",
        created_at=now,
        updated_at=now,
        last_reconciliation_source="b",
        tradable=False,
    )
    upsert_residual(reg, second)
    write_residual_registry(reg, repo_root=tmp_path)
    again = read_residual_registry(repo_root=tmp_path)
    assert again is not None
    assert again.residuals[first.identity_key].provenance == "first"
    assert len(again.open_residuals()) == 2


def test_tradable_residual_blocks_entry(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    reg = LifecycleResidualRegistry()
    upsert_residual(
        reg,
        LifecycleResidualRecord(
            condition_id="0x1",
            token_id="tbig",
            originating_run_id="run-x",
            market_slug="m",
            acquired_quantity="5",
            exited_quantity="0",
            residual_quantity="5",
            min_tradable="0.01",
            classification="RESIDUAL_EXPOSURE",
            provenance="unsafe",
            created_at=now,
            updated_at=now,
            last_reconciliation_source="test",
            tradable=True,
        ),
    )
    write_residual_registry(reg, repo_root=tmp_path)
    path, rows = _write_ack(tmp_path)
    _seed_policy(tmp_path, rows)
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            acknowledgment_path=path,
            positions_provider=lambda: rows,
            dry_run=True,
            mutation_transport=RaisingTransport(),
        )
    )
    assert result.outcome is TerminalOutcome.BLOCKED
    assert "TRADABLE_RESIDUAL_EXPOSURE" in result.report["blockers"]


def test_known_non_tradable_dust_does_not_disappear(tmp_path: Path) -> None:
    write_lifecycle_dust(repo_root=tmp_path, record=default_incident_dust_record())
    reg = migrate_dust_to_registry(repo_root=tmp_path, force_incident=False)
    assert any(r.token_id == INCIDENT_DUST_TOKEN for r in reg.open_residuals())
    path = tmp_path / "var" / "runtime_state" / "r7" / "lifecycle_residuals.json"
    assert path.exists()
    # Other market residual also retained
    now = datetime.now(timezone.utc).isoformat()
    upsert_residual(
        reg,
        LifecycleResidualRecord(
            condition_id="0xother",
            token_id="tother",
            originating_run_id="run-other",
            market_slug="other-market",
            acquired_quantity="1",
            exited_quantity="0.9995",
            residual_quantity="0.0005",
            min_tradable="0.01",
            classification="FLAT_WITH_DUST",
            provenance="other",
            created_at=now,
            updated_at=now,
            last_reconciliation_source="test",
            tradable=False,
        ),
    )
    write_residual_registry(reg, repo_root=tmp_path)
    loaded = read_residual_registry(repo_root=tmp_path)
    assert loaded is not None
    assert len(loaded.open_residuals()) == 2
    assert INCIDENT_DUST_TOKEN in {r.token_id for r in loaded.open_residuals()}


def test_exact_zero_closes_but_retains_provenance(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    reg = LifecycleResidualRegistry()
    rec = LifecycleResidualRecord(
        condition_id="0x1",
        token_id="tz",
        originating_run_id="run-z",
        market_slug="m",
        acquired_quantity="1",
        exited_quantity="1",
        residual_quantity="0",
        min_tradable="0.01",
        classification="FLAT_WITH_DUST",
        provenance="keep-me",
        created_at=now,
        updated_at=now,
        last_reconciliation_source="test",
        tradable=False,
    )
    upsert_residual(reg, rec)
    close_residual_if_zero(
        reg,
        condition_id="0x1",
        token_id="tz",
        originating_run_id="run-z",
        source="recon",
    )
    assert reg.residuals[rec.identity_key].closed is True
    assert reg.residuals[rec.identity_key].provenance == "keep-me"
    assert reg.residuals[rec.identity_key].historical_provenance_retained is True
    assert len(reg.open_residuals()) == 0


def test_cleanup_policy_remains_none() -> None:
    from tyrex_pm.runtime.r7_lifecycle_residuals import CLEANUP_POLICY_NONE, incident_residual_record

    r = incident_residual_record()
    assert r.cleanup_policy == CLEANUP_POLICY_NONE
    assert r.tradable is False


def test_identity_key_format() -> None:
    assert identity_key("0xa", "t1") == "0xa|t1"
