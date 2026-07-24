"""YAML resolve / adapt / Z-Gap completeness (framework usability)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.runtime.config import ObserveConfig, SourceMode, load_observe_config
from tyrex_pm.runtime.yaml_config.adapt import adapt_to_observe_config
from tyrex_pm.runtime.yaml_config.errors import ConfigError
from tyrex_pm.runtime.yaml_config.load import load_yaml_mapping
from tyrex_pm.runtime.yaml_config.resolve import resolve_run_config
from tyrex_pm.runtime.yaml_config.serialize import resolved_to_show_dict
from tyrex_pm.runtime.yaml_config.zgap_schema import assert_zgap_field_coverage
from tyrex_pm.strategies.z_gap.config import ZGapConfig

ROOT = Path(__file__).resolve().parents[1]


def _paths(**overrides):
    base = {
        "strategy_path": ROOT / "config/strategies/z_gap.yaml",
        "risk_path": ROOT / "config/risk/example_risk.yaml",
        "execution_path": ROOT / "config/execution/example.yaml",
        "runtime_path": ROOT / "config/runtime/observe_btc_5m.yaml",
        "mode": "observe",
        "scenarios_dir": ROOT / "config/scenarios",
    }
    base.update(overrides)
    return base


def test_zgap_field_coverage_contract():
    assert_zgap_field_coverage()


def test_load_valid_examples():
    for rel in (
        "config/strategies/z_gap.yaml",
        "config/risk/example_risk.yaml",
        "config/execution/example.yaml",
        "config/execution/shadow_example.yaml",
        "config/runtime/observe_btc_5m.yaml",
        "config/scenarios/aggressive.yaml",
    ):
        load_yaml_mapping(ROOT / rel)


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_yaml_mapping(ROOT / "config/strategies/does_not_exist.yaml")


def test_invalid_yaml(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(":\n  - [", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_yaml_mapping(p)


def test_non_mapping_root(tmp_path: Path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_yaml_mapping(p)


def test_unknown_strategy_field(tmp_path: Path):
    p = tmp_path / "s.yaml"
    p.write_text("strategy: z_gap\nparameters:\n  nope: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown"):
        resolve_run_config(**_paths(strategy_path=p))


def test_unsupported_strategy(tmp_path: Path):
    p = tmp_path / "s.yaml"
    p.write_text("strategy: other\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported strategy"):
        resolve_run_config(**_paths(strategy_path=p))


def test_wrong_type_bool(tmp_path: Path):
    p = tmp_path / "s.yaml"
    p.write_text(
        "strategy: z_gap\nparameters:\n  entry:\n    require_repricing_edge: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="bool"):
        resolve_run_config(**_paths(strategy_path=p))


def test_defaults_fill_omitted_optional(tmp_path: Path):
    p = tmp_path / "s.yaml"
    p.write_text("strategy: z_gap\nparameters: {}\n", encoding="utf-8")
    resolved = resolve_run_config(**_paths(strategy_path=p))
    assert resolved.zgap.entry.theta_fill_floor == ZGapConfig().entry.theta_fill_floor
    assert resolved.zgap.ptb_time_quality.max_ptb_lag_ms == 5000


def test_scenario_leaf_override_and_precedence():
    resolved = resolve_run_config(**_paths(scenario="aggressive", run_name="t1"))
    assert resolved.zgap.entry.theta_take == Decimal("0.02")
    assert resolved.zgap.entry.z_min == Decimal("0.5")
    assert resolved.risk.max_spread == Decimal("0.80")
    assert resolved.shadow.max_hold.total_seconds() == 300
    assert resolved.runtime.runtime_duration.total_seconds() == 120
    # Base YAML theta_take was 0.05; scenario wins
    assert resolved.mode.value == "observe"
    assert resolved.run_name == "t1"


def test_scenario_unknown_path(tmp_path: Path):
    scen = tmp_path / "badscen.yaml"
    scen.write_text(
        "strategy:\n  parameters:\n    entry:\n      not_a_field: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown scenario path"):
        resolve_run_config(
            **_paths(scenario="badscen", scenarios_dir=tmp_path),
        )


def test_unsafe_scenario_name():
    with pytest.raises(ConfigError, match="invalid scenario"):
        resolve_run_config(**_paths(scenario="../evil"))


def test_shadow_requires_enable_oms():
    with pytest.raises(ConfigError, match="enable_oms"):
        resolve_run_config(**_paths(mode="shadow"))  # example.yaml has enable_oms false


def test_complete_zgap_mapping_preserves_all_fields():
    resolved = resolve_run_config(**_paths())
    # Fields historically dropped by zgap_config_from_runtime
    assert resolved.zgap.entry.theta_fill_floor == Decimal("0.03")
    assert resolved.zgap.entry.block_abs_z == Decimal("20")
    assert resolved.zgap.entry.require_repricing_edge is True
    assert resolved.zgap.entry.tie_epsilon == Decimal("0.001")
    assert resolved.zgap.entry.exit_friction_reserve == Decimal("0")
    assert resolved.zgap.realization.min_exit_depth == Decimal("0")
    assert resolved.zgap.realization.slippage_included_in_executable_bid is True
    assert resolved.zgap.thesis.reset_on_stale_model is True
    assert resolved.zgap.time_resolution.sell_vs_resolve_margin == Decimal("0")
    assert resolved.zgap.time_resolution.resolution_capability_default is False
    assert resolved.zgap.ptb_time_quality.max_ptb_lag_ms == 5000
    assert resolved.zgap.ptb_time_quality.max_clock_uncertainty_ms == 250
    assert resolved.zgap.friction.provisional_resolve_penalty_per_share == Decimal("0")
    assert resolved.zgap.friction.fee_curve.fee_rate == Decimal("0.07")


def test_adapt_preserves_strategy_and_pure_config():
    resolved = resolve_run_config(**_paths(scenario="aggressive"))
    cfg = adapt_to_observe_config(resolved)
    assert cfg.strategy_kind == "z_gap"
    assert cfg.zgap_pure is not None
    assert cfg.zgap_pure.entry.theta_take == Decimal("0.02")
    assert cfg.zgap_pure.entry.theta_fill_floor == Decimal("0.03")
    assert cfg.risk is not None
    assert cfg.risk.target_notional == Decimal("5")
    assert cfg.max_book_spread == resolved.runtime.signal_max_book_spread
    assert cfg.mode is SourceMode.FIXTURE  # source=fixture


def test_target_notional_owned_by_risk_not_strategy():
    resolved = resolve_run_config(**_paths())
    cfg = adapt_to_observe_config(resolved)
    assert cfg.risk.target_notional == Decimal("5")
    assert cfg.z_gap is not None
    assert cfg.z_gap.target_notional == cfg.risk.target_notional


def test_show_config_includes_defaults(tmp_path: Path):
    p = tmp_path / "s.yaml"
    p.write_text("strategy: z_gap\n", encoding="utf-8")
    resolved = resolve_run_config(**_paths(strategy_path=p))
    shown = resolved_to_show_dict(resolved)
    assert shown["strategy"]["parameters"]["entry"]["theta_fill_floor"] == "0.03"
    assert "fee_curve" in shown["strategy"]["parameters"]["friction"]


def test_live_mode_requires_polymarket_live_execution():
    with pytest.raises(ConfigError, match="polymarket_live"):
        resolve_run_config(**_paths(mode="live"))


def test_live_profile_resolves():
    resolved = resolve_run_config(
        strategy_path=ROOT / "config/strategies/z_gap.yaml",
        risk_path=ROOT / "config/risk/tiny_live_5usd.yaml",
        execution_path=ROOT / "config/execution/polymarket_live.yaml",
        runtime_path=ROOT / "config/runtime/live_btc_5m.yaml",
        mode="live",
    )
    assert resolved.mode.value == "live"
    assert resolved.n7_mapping is not None
    assert resolved.runtime.require_ssr_price_match is False


def test_btc_window_preserves_strategy_kind():
    """Regression: observe --btc-window must not drop strategy_kind / z_gap."""
    from tyrex_pm.application import cli as cli_mod

    class NS:
        config = ROOT / "config/observe_z_gap_fixture_f3.json"
        mode = None
        fixture = None
        output = None
        event_slug = None
        event_url = None
        duration_s = None
        btc_window = "next"

    # Force live so --btc-window is allowed; monkeypatch slug resolver.
    import json

    raw = json.loads((ROOT / "config/observe_z_gap_fixture_f3.json").read_text(encoding="utf-8"))
    raw["mode"] = "live"
    raw["event_slug"] = "btc-updown-5m-1"
    raw.pop("fixture_path", None)
    tmp = ROOT / "var" / "tmp_btc_window_cfg.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(raw), encoding="utf-8")
    NS.config = tmp

    cli_mod.current_btc_updown_slug = lambda: "btc-updown-5m-current"
    cli_mod.next_btc_updown_slug = lambda: "btc-updown-5m-next"
    cfg = cli_mod._build_observe_config(NS())
    assert cfg.strategy_kind == "z_gap"
    assert cfg.z_gap is not None
    assert cfg.event_slug == "btc-updown-5m-next"


def test_observe_fixture_e2e_from_yaml():
    from datetime import datetime, timezone

    from tyrex_pm.core.clock import FakeClock
    from tyrex_pm.runtime.observe_host import ObserveHost

    resolved = resolve_run_config(
        **_paths(scenario="aggressive", run_name="pytest_yaml_observe")
    )
    cfg = adapt_to_observe_config(resolved)
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    host = ObserveHost(cfg, clock=clock)
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert cfg.strategy_kind == "z_gap"
    assert len(result.decisions) >= 1
    assert host.binding.config.entry.theta_take == Decimal("0.02")
    assert host.binding.config.entry.theta_fill_floor == Decimal("0.03")


def test_shadow_fixture_e2e_from_yaml():
    from datetime import datetime, timezone

    from tyrex_pm.core.clock import FakeClock
    from tyrex_pm.runtime.shadow_host import ShadowHost

    resolved = resolve_run_config(
        **_paths(
            execution_path=ROOT / "config/execution/shadow_example.yaml",
            mode="shadow",
            scenario="aggressive",
            run_name="pytest_yaml_shadow",
        )
    )
    cfg = adapt_to_observe_config(resolved)
    clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
    host = ShadowHost(cfg, clock=clock)
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert cfg.strategy_kind == "z_gap"
    assert cfg.shadow is not None and cfg.shadow.enable_oms
    assert len(result.decisions) >= 1


def test_legacy_json_z_gap_still_loads():
    cfg = load_observe_config(ROOT / "config/observe_z_gap_fixture_f3.json")
    assert isinstance(cfg, ObserveConfig)
    assert cfg.strategy_kind == "z_gap"
