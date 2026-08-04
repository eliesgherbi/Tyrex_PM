"""M1/M2 reporting core tests."""

from __future__ import annotations

import json
from pathlib import Path

from tyrex_pm.reporting import (
    EventFamily,
    LineageIds,
    ReportingHealth,
    ReportingLane,
    classify_stale_running,
    default_reporting_config,
    load_reporting_config,
    open_run_reporter,
)
from tyrex_pm.reporting.config import reporting_config_from_mapping
from tyrex_pm.reporting.contracts import ReportingEvent, SCHEMA_VERSION, new_event_id, utc_now
from tyrex_pm.reporting.summary import ClosestCandidate

REPO_ROOT = Path(__file__).resolve().parents[1]
FULL_YAML = REPO_ROOT / "config" / "reporting" / "full.yaml"
MINIMAL_YAML = REPO_ROOT / "config" / "reporting" / "minimal.yaml"


def test_envelope_validation(tmp_path: Path):
    rep = open_run_reporter(run_dir=tmp_path / "r1", run_id="r1", mode="observe", strategy_id="z_gap")
    evt = rep.emit_dict(
        event_family=EventFamily.DECISION.value,
        event_type="decision.evaluated",
        payload={"action": "WAIT", "reason_code": "MODEL_NOT_READY"},
        producer="test",
    )
    assert evt.schema_version == SCHEMA_VERSION
    assert evt.sequence == 1
    rep.finalize()
    assert (tmp_path / "r1" / "manifest.json").is_file()
    assert (tmp_path / "r1" / "run_summary.json").is_file()


def test_summary_counts_survive_analytics_drop(tmp_path: Path):
    cfg = default_reporting_config(profile="full")
    # tiny analytics queue to force drops
    from tyrex_pm.reporting.config import ReportingConfig

    cfg = ReportingConfig(
        schema_version=1,
        profile="full",
        per_evaluation=True,
        indicators=True,
        signals=True,
        debug_host_trace=False,
        closest_candidates_per_reason=5,
        raw_recording_enabled=False,
        analytics_max_queue=1,
        batch_size=100,
    )
    rep = open_run_reporter(
        run_dir=tmp_path / "r2", run_id="r2", mode="observe", strategy_id="demo", config=cfg
    )
    for i in range(20):
        rep.emit_dict(
            event_family=EventFamily.DECISION.value,
            event_type="decision.evaluated",
            payload={
                "action": "SKIP",
                "reason_code": "Z_OUT_OF_BAND",
                "failed_gates": ["Z_IN_BAND"],
                "gates_not_evaluated": ["EDGE_VS_THETA"],
            },
            producer="test",
            lineage=LineageIds(evaluation_id=f"e{i}", decision_id=f"d{i}"),
        )
    summary_path = rep.finalize()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["strategy_evaluations"]["completed"] == 20
    assert summary["decisions_and_gates"]["primary_reason_distribution"]["Z_OUT_OF_BAND"] == 20
    assert summary["decisions_and_gates"]["all_failed_gates"]["Z_IN_BAND"] == 20
    assert summary["reporting_health"]["state"] in {
        ReportingHealth.DEGRADED_ANALYTICS.value,
        ReportingHealth.HEALTHY.value,
    }
    # critical lane still healthy unless critical failed
    assert summary["reporting_health"]["state"] != ReportingHealth.CRITICAL_AUDIT_FAILURE.value


def test_critical_independent_of_analytics_pressure(tmp_path: Path):
    from tyrex_pm.reporting.config import ReportingConfig

    cfg = ReportingConfig(
        schema_version=1,
        profile="full",
        per_evaluation=True,
        indicators=True,
        signals=True,
        debug_host_trace=False,
        closest_candidates_per_reason=5,
        raw_recording_enabled=False,
        analytics_max_queue=1,
        batch_size=100,
    )
    rep = open_run_reporter(run_dir=tmp_path / "r3", run_id="r3", mode="live", config=cfg)
    for _ in range(10):
        rep.emit_dict(
            event_family=EventFamily.DECISION.value,
            event_type="decision.evaluated",
            payload={"action": "WAIT", "reason_code": "WAIT"},
            producer="test",
        )
    ok = rep.persist_critical(
        ReportingEvent(
            schema_version=SCHEMA_VERSION,
            event_id=new_event_id(),
            run_id="r3",
            mode="live",
            sequence=rep._seq + 1,
            event_family=EventFamily.PRE_MUTATION.value,
            event_type="mutation.pre_exposure_increase",
            event_time=utc_now(),
            record_time=utc_now(),
            producer="test",
            lane=ReportingLane.CRITICAL,
            payload={"exposure_increasing": True},
        )
    )
    # bump seq manually was wrong — use API
    assert rep.health != ReportingHealth.CRITICAL_AUDIT_FAILURE or ok
    # Proper path:
    evt = rep.build_pre_mutation_event(
        producer="test",
        payload={
            "exposure_increasing": True,
            "mutation_type": "enter",
            "side": "BUY",
            "quantity": "5",
        },
    )
    assert rep.persist_critical(evt) is True
    assert rep.allows_new_exposure is True
    rep.finalize()
    audit = (tmp_path / "r3" / "audit_events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any("pre_exposure_increase" in line for line in audit)


def test_checkpoint_readable_after_partial(tmp_path: Path):
    rep = open_run_reporter(run_dir=tmp_path / "r4", run_id="r4", mode="observe")
    rep.emit_dict(
        event_family=EventFamily.DECISION.value,
        event_type="decision.evaluated",
        payload={"action": "WAIT", "reason_code": "MODEL_NOT_READY"},
        producer="test",
    )
    rep.checkpoint()
    summary = json.loads((tmp_path / "r4" / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["status"]["lifecycle"] == "RUNNING"
    assert summary["status"]["terminal_status"] == "PARTIAL"
    manifest = json.loads((tmp_path / "r4" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["lifecycle"] == "RUNNING"
    # do not finalize — simulate crash
    rep._writer.close()
    stale = classify_stale_running(
        {**manifest, "last_checkpoint_at": "2000-01-01T00:00:00Z"},
        stale_after_s=1.0,
    )
    assert stale["stale"] is True
    assert stale["classified_as"] == "ABORTED"


def test_pre_mutation_ack_failure_blocks_exposure(tmp_path: Path):
    rep = open_run_reporter(run_dir=tmp_path / "r5", run_id="r5", mode="live")
    rep.force_critical_failure_for_tests()
    evt = rep.build_pre_mutation_event(
        producer="test",
        payload={"exposure_increasing": True, "mutation_type": "enter"},
    )
    assert rep.persist_critical(evt) is False
    assert rep.health is ReportingHealth.CRITICAL_AUDIT_FAILURE
    assert rep.allows_new_exposure is False
    rep.finalize(terminal_status="ABORTED", terminal_reason="critical_audit_failure", clean_shutdown=False)


def test_reject_disable_mandatory_config():
    try:
        reporting_config_from_mapping(
            {"schema_version": 1, "profile": "full", "outputs": {"audit_events": False}}
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "mandatory" in str(exc).lower() or "cannot disable" in str(exc).lower()


def test_minimal_preserves_counts(tmp_path: Path):
    cfg = default_reporting_config(profile="minimal")
    rep = open_run_reporter(run_dir=tmp_path / "r6", run_id="r6", mode="observe", config=cfg)
    for _ in range(5):
        rep.emit_dict(
            event_family=EventFamily.DECISION.value,
            event_type="decision.evaluated",
            payload={"action": "SKIP", "reason_code": "TAU_OUT_OF_BAND"},
            producer="test",
        )
    summary = json.loads(rep.finalize().read_text(encoding="utf-8"))
    assert summary["strategy_evaluations"]["completed"] == 5
    assert summary["strategy_evaluations"]["detailed_persisted"] == 0
    assert summary["decisions_and_gates"]["primary_reason_distribution"]["TAU_OUT_OF_BAND"] == 5


def test_closest_candidate_accumulation(tmp_path: Path):
    rep = open_run_reporter(run_dir=tmp_path / "r7", run_id="r7", mode="observe")
    rep.emit_dict(
        event_family=EventFamily.DECISION.value,
        event_type="decision.evaluated",
        payload={
            "action": "SKIP",
            "reason_code": "BELOW_THRESHOLD",
            "closest_candidate": {
                "reached_edge_evaluation": True,
                "evaluation_id": "e1",
                "selected_leg": "UP",
                "executable_net_edge": "0.04",
                "required_edge": "0.05",
                "signed_margin": -0.01,
                "primary_reason": "BELOW_THRESHOLD",
            },
        },
        producer="test",
    )
    summary = json.loads(rep.finalize().read_text(encoding="utf-8"))
    assert summary["closest_candidates"]["no_comparable_candidate"] is False
    assert "BELOW_THRESHOLD" in summary["closest_candidates"]["candidates_by_reason"]


def test_full_profile_yaml_enables_optional_analytics():
    assert FULL_YAML.is_file()
    cfg = load_reporting_config(FULL_YAML)
    assert cfg.profile == "full"
    assert cfg.per_evaluation is True
    assert cfg.indicators is True
    assert cfg.signals is True
    assert cfg.debug_host_trace is False
    assert cfg.raw_recording_enabled is False


def test_minimal_profile_yaml_reduces_analytics_but_keeps_counters():
    assert MINIMAL_YAML.is_file()
    cfg = load_reporting_config(MINIMAL_YAML)
    assert cfg.profile == "minimal"
    assert cfg.per_evaluation is False
    assert cfg.indicators is False
    assert cfg.signals is False
    assert cfg.debug_host_trace is False
    assert cfg.raw_recording_enabled is False
    # Aggregate counters (strategy_evaluations, decisions_and_gates) are always
    # produced by RunReporter regardless of profile; verified via
    # test_minimal_preserves_counts above.


def test_full_and_minimal_yaml_reject_disabling_mandatory_outputs():
    import yaml as _yaml

    for path in (FULL_YAML, MINIMAL_YAML):
        raw = dict(_yaml.safe_load(path.read_text(encoding="utf-8")))
        for bad_key in ("manifest", "run_summary", "audit_events"):
            tampered = dict(raw)
            tampered["outputs"] = {bad_key: False}
            try:
                reporting_config_from_mapping(tampered)
                assert False, f"expected ValueError disabling {bad_key} for {path.name}"
            except ValueError as exc:
                assert "mandatory" in str(exc).lower() or "cannot disable" in str(exc).lower()


def test_reject_disable_operational_channel():
    try:
        reporting_config_from_mapping(
            {"schema_version": 1, "profile": "full", "channels": {"operational": False}}
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "mandatory" in str(exc).lower() or "cannot disable" in str(exc).lower()


def test_secret_redaction_in_payload(tmp_path: Path):
    rep = open_run_reporter(run_dir=tmp_path / "r8", run_id="r8", mode="observe")
    rep.emit_dict(
        event_family=EventFamily.OPERATIONAL.value,
        event_type="operational.note",
        payload={"private_key": "0xabc", "wallet_address": "0xPUBLIC"},
        producer="test",
        force_critical=True,
    )
    rep.finalize()
    line = (tmp_path / "r8" / "audit_events.jsonl").read_text(encoding="utf-8").strip()
    data = json.loads(line)
    assert data["payload"]["private_key"] == "[REDACTED]"
    assert data["payload"]["wallet_address"] == "0xPUBLIC"
