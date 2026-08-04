"""YAML run --mode live: resolve, N7 binding, isolation, fake rehearsals."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.application.cli import main
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.runtime.n7_sealed import n7_sealed_from_mapping
from tyrex_pm.runtime.yaml_config.adapt import adapt_to_observe_config
from tyrex_pm.runtime.yaml_config.errors import ConfigError
from tyrex_pm.runtime.yaml_config.live_run import run_yaml_live
from tyrex_pm.runtime.yaml_config.n7_bridge import sealed_from_resolved
from tyrex_pm.runtime.yaml_config.resolve import RunMode, resolve_run_config
from tyrex_pm.runtime.yaml_config.serialize import resolved_to_show_dict
from tyrex_pm.strategies.z_gap.config import ZGapConfig

ROOT = Path(__file__).resolve().parents[1]


def _live_resolve(**kwargs):
    return resolve_run_config(
        strategy_path=kwargs.get("strategy", ROOT / "config/strategies/z_gap.yaml"),
        risk_path=kwargs.get("risk", ROOT / "config/risk/tiny_live_5usd.yaml"),
        execution_path=kwargs.get(
            "execution", ROOT / "config/execution/polymarket_live.yaml"
        ),
        runtime_path=kwargs.get("runtime", ROOT / "config/runtime/live_btc_5m.yaml"),
        mode="live",
        run_name=kwargs.get("run_name"),
    )


def test_cli_accepts_live_mode_validate():
    assert (
        main(
            [
                "run",
                "--mode",
                "live",
                "--runtime",
                str(ROOT / "config/runtime/live_btc_5m.yaml"),
                "--validate-config",
            ]
        )
        == 0
    )


def test_observe_shadow_cli_unchanged():
    assert (
        main(
            [
                "run",
                "--strategy",
                str(ROOT / "config/strategies/z_gap.yaml"),
                "--risk",
                str(ROOT / "config/risk/example_risk.yaml"),
                "--execution",
                str(ROOT / "config/execution/example.yaml"),
                "--runtime",
                str(ROOT / "config/runtime/observe_btc_5m.yaml"),
                "--mode",
                "observe",
                "--validate-config",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "run",
                "--strategy",
                str(ROOT / "config/strategies/z_gap.yaml"),
                "--risk",
                str(ROOT / "config/risk/example_risk.yaml"),
                "--execution",
                str(ROOT / "config/execution/shadow_example.yaml"),
                "--runtime",
                str(ROOT / "config/runtime/observe_btc_5m.yaml"),
                "--mode",
                "shadow",
                "--validate-config",
            ]
        )
        == 0
    )


def test_live_yaml_resolves_and_binds_n7():
    resolved = _live_resolve(run_name="live_bind")
    assert resolved.mode is RunMode.LIVE
    assert resolved.execution_kind == "polymarket_live"
    assert resolved.risk.runtime_mode is RuntimeMode.LIVE_TINY
    assert resolved.runtime.source == "live"
    assert resolved.runtime.require_ssr_price_match is False
    sealed = sealed_from_resolved(resolved)
    assert sealed.max_buy_collateral == Decimal("5.00")
    assert sealed.require_ssr_price_match is False
    assert sealed.one_shot is True
    assert sealed.max_entry_lineages == 1
    again = n7_sealed_from_mapping(resolved.n7_mapping or {})
    assert again.fingerprint() == sealed.fingerprint()


def test_zgap_parameters_preserved_from_strategy_yaml():
    resolved = _live_resolve()
    default = ZGapConfig()
    assert resolved.zgap.entry.theta_take == default.entry.theta_take
    assert resolved.zgap.entry.z_min == default.entry.z_min
    assert resolved.zgap.volatility.half_life_s == default.volatility.half_life_s


def test_ssr_disabled_and_chainlink_authority_in_show():
    resolved = _live_resolve()
    show = resolved_to_show_dict(resolved)
    assert show["runtime"]["require_ssr_price_match"] is False
    assert show["requested_mode"] == "live"
    assert show["effective_mode"] == "live"
    assert show["provenance"]["host_binding"] == "n7_operator_oneshot"


def test_fee_inclusive_cap_five_dollars():
    sealed = sealed_from_resolved(_live_resolve())
    assert sealed.max_buy_collateral <= Decimal("5.00")
    assert sealed.live.hard_collateral_cap == Decimal("5.00")


def test_observe_cannot_use_polymarket_live_execution():
    with pytest.raises(ConfigError, match="execution.kind=shadow"):
        resolve_run_config(
            strategy_path=ROOT / "config/strategies/z_gap.yaml",
            risk_path=ROOT / "config/risk/example_risk.yaml",
            execution_path=ROOT / "config/execution/polymarket_live.yaml",
            runtime_path=ROOT / "config/runtime/observe_btc_5m.yaml",
            mode="observe",
        )


def test_shadow_cannot_use_polymarket_live_execution():
    with pytest.raises(ConfigError, match="execution.kind=shadow"):
        resolve_run_config(
            strategy_path=ROOT / "config/strategies/z_gap.yaml",
            risk_path=ROOT / "config/risk/example_risk.yaml",
            execution_path=ROOT / "config/execution/polymarket_live.yaml",
            runtime_path=ROOT / "config/runtime/observe_btc_5m.yaml",
            mode="shadow",
        )


def test_yaml_cannot_convert_observe_to_live():
    """Runtime YAML must not set host mode; observe stays observe."""
    resolved = resolve_run_config(
        strategy_path=ROOT / "config/strategies/z_gap.yaml",
        risk_path=ROOT / "config/risk/example_risk.yaml",
        execution_path=ROOT / "config/execution/example.yaml",
        runtime_path=ROOT / "config/runtime/observe_btc_5m.yaml",
        mode="observe",
    )
    assert resolved.mode is RunMode.OBSERVE
    assert resolved.n7_mapping is None
    with pytest.raises(ValueError, match="does not support --mode live"):
        # Prove adapt path refuses live
        live = _live_resolve()
        adapt_to_observe_config(live)


def test_observe_adapt_has_no_oms_mutations():
    resolved = resolve_run_config(
        strategy_path=ROOT / "config/strategies/z_gap.yaml",
        risk_path=ROOT / "config/risk/example_risk.yaml",
        execution_path=ROOT / "config/execution/example.yaml",
        runtime_path=ROOT / "config/runtime/observe_btc_5m.yaml",
        mode="observe",
    )
    cfg = adapt_to_observe_config(resolved)
    assert cfg.shadow is None or cfg.shadow.enable_oms is False


def test_live_without_arm_flag_is_dry_only_via_cli_gate():
    # --live omitted → fake not used; we only validate CLI rejects coupling
    # Observe cannot pass --live
    code = main(
        [
            "run",
            "--strategy",
            str(ROOT / "config/strategies/z_gap.yaml"),
            "--risk",
            str(ROOT / "config/risk/example_risk.yaml"),
            "--execution",
            str(ROOT / "config/execution/example.yaml"),
            "--runtime",
            str(ROOT / "config/runtime/observe_btc_5m.yaml"),
            "--mode",
            "observe",
            "--live",
            "--validate-config",
        ]
    )
    assert code == 2


def test_fake_live_rehearsal_entry_exit(tmp_path: Path):
    resolved = _live_resolve(run_name="fake_full")
    result = run_yaml_live(
        resolved=resolved,
        repo=ROOT,
        out_dir=tmp_path / "fake_full",
        dotenv=None,
        live=False,
        fake_rehearsal=True,
    )
    assert result.ok
    assert result.real_venue_mutations == 0
    assert result.outcome == "PASS_FAKE_FLAT"
    payload = json.loads(
        (tmp_path / "fake_full" / "attachments" / "operator_outcome.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["requested_mode"] == "live"
    assert payload["effective_mode"] == "live"
    assert payload["real_venue_mutations"] == 0
    assert "resolved_configuration" in payload
    assert payload["require_ssr_price_match"] is False
    assert payload["ptb_authority"] == "chainlink_sealed_k"
    assert Decimal(payload["max_buy_collateral"]) == Decimal("5.00")
    assert payload["entry"]["status"] == "ACKNOWLEDGED"
    assert payload["economics"]["flat"] is True
    assert (tmp_path / "fake_full" / "run_summary.json").is_file()
    assert not (tmp_path / "fake_full" / "n7_oneshot_report.json").exists()


def test_fake_no_signal_zero_mutations(tmp_path: Path):
    resolved = _live_resolve(run_name="fake_nosig")
    result = run_yaml_live(
        resolved=resolved,
        repo=ROOT,
        out_dir=tmp_path / "fake_nosig",
        dotenv=None,
        live=False,
        fake_rehearsal=False,
        fake_no_signal=True,
    )
    assert result.ok
    assert result.real_venue_mutations == 0
    assert result.outcome == "PASS_N7_SAFE_NO_ENTRY"
    payload = json.loads(
        (tmp_path / "fake_nosig" / "attachments" / "operator_outcome.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["entry"] is None
    assert payload["real_venue_mutations"] == 0


def test_cli_fake_rehearsal(tmp_path: Path):
    code = main(
        [
            "run",
            "--mode",
            "live",
            "--runtime",
            str(ROOT / "config/runtime/live_btc_5m.yaml"),
            "--run-name",
            "cli_fake",
            "--out-dir",
            str(tmp_path / "cli_fake"),
            "--fake-rehearsal",
        ]
    )
    assert code == 0


def test_one_market_one_lineage_via_sealed_host(tmp_path: Path):
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from helpers_n7 import fill_order, make_enter, make_n7_host, yes_book

    sealed = sealed_from_resolved(_live_resolve())
    host = make_n7_host(persistence_path=tmp_path / "state.json", sealed=sealed)
    book = yes_book(host)
    r1 = host.try_enter(make_enter(host), book=book)
    assert r1["status"] == "ACKNOWLEDGED"
    fill_order(host, r1["order_id"], qty=r1["sizing"]["quantity"], price="0.50")
    r2 = host.try_enter(make_enter(host), book=book)
    assert r2["abort"] == "reentry_refused"
    assert host.refuse_second_window("other")["refused"] is True
    assert host.inner.real_venue_mutations == 0  # FakeTransport


def test_ambiguous_submission_no_duplicate_entry(tmp_path: Path):
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from helpers_n7 import make_enter, make_n7_host, yes_book

    sealed = sealed_from_resolved(_live_resolve())
    host = make_n7_host(persistence_path=tmp_path / "amb.json", sealed=sealed)
    host.transport.submit_behavior = "timeout"
    r1 = host.try_enter(make_enter(host), book=yes_book(host))
    assert r1["status"] == "AMBIGUOUS"
    host.transport.submit_behavior = "accept"
    r2 = host.try_enter(make_enter(host), book=yes_book(host))
    assert r2["status"] in {"ABORT", "BLOCKED_DUPLICATE", "SKIP"}
    assert host.inner.real_venue_mutations == 0


def test_exit_cannot_exceed_confirmed_inventory(tmp_path: Path):
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from helpers_n7 import fill_order, make_enter, make_exit, make_n7_host, yes_book

    sealed = sealed_from_resolved(_live_resolve())
    host = make_n7_host(persistence_path=tmp_path / "inv.json", sealed=sealed)
    book = yes_book(host)
    entered = host.try_enter(make_enter(host), book=book)
    qty = Decimal(entered["sizing"]["quantity"])
    fill_order(host, entered["order_id"], qty=qty, price="0.50")
    half = (qty / 2).quantize(Decimal("0.000001"))
    exited = host.try_exit(
        make_exit(host), book=book, limit_price=Decimal("0.49"), quantity=half
    )
    assert exited["status"] == "ACKNOWLEDGED"
    assert Decimal(str(exited["exit_qty"])) == half
    assert Decimal(str(exited["exit_qty"])) <= qty


def test_import_modules_cannot_submit():
    sealed = sealed_from_resolved(_live_resolve())
    # Constructing sealed config must leave mutations OFF
    assert sealed.live.mutations_enabled is False
    assert sealed.live.enabled is False


def test_reports_preserve_resolved_configuration(tmp_path: Path):
    resolved = _live_resolve(run_name="report_cfg")
    result = run_yaml_live(
        resolved=resolved,
        repo=ROOT,
        out_dir=tmp_path / "report_cfg",
        dotenv=None,
        live=False,
        fake_rehearsal=True,
    )
    payload = json.loads(
        (tmp_path / "report_cfg" / "attachments" / "operator_outcome.json").read_text(
            encoding="utf-8"
        )
    )
    cfg = payload["resolved_configuration"]
    assert cfg["strategy"]["strategy"] == "z_gap"
    assert cfg["execution"]["kind"] == "polymarket_live"
    assert cfg["risk"]["live_limits"]["max_buy_collateral"] == "5.00"
    assert "theta_take" in cfg["strategy"]["parameters"]["entry"]
