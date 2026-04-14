"""Compose guru node without running the event loop."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tyrex_pm.config.loaders import (
    load_risk_settings,
    load_runtime_settings,
    load_strategy_settings,
)
from tyrex_pm.execution.nautilus_guru_exec import NautilusGuruExecutionPort
from tyrex_pm.runtime.guru_compose import build_guru_trading_node
from tyrex_pm.runtime.lifecycle import NautilusExecEngineClientsConnected, SpikePendingExecClientsConnected
from tyrex_pm.runtime.tradable_state import NautilusLiveExecutionHealthSource
from tyrex_pm.runtime.guru_instrument_dynamic import GuruInstrumentDynamicController
from tyrex_pm.runtime.guru_run_logging import GuruNautilusFileLogging
from tyrex_pm.strategy.bot_sell_validate_strategy import BotSellValidateStrategy

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Prefer this scenario for compose / integration-style tests (stable paths, current knobs).
_LIFECYCLE_SCENARIO_DIR = _REPO_ROOT / "config" / "scenarios" / "lifecycle_test"
_LIFECYCLE_STRATEGY_YAML = _LIFECYCLE_SCENARIO_DIR / "guru_follow.yaml"
_LIFECYCLE_RISK_SHADOW_YAML = _LIFECYCLE_SCENARIO_DIR / "guru_follow_risk_shadow.yaml"


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_registers_bot_sell_validate_strategy(mock_node_cls: MagicMock, tmp_path: Path) -> None:
    strat = load_strategy_settings(_LIFECYCLE_STRATEGY_YAML)
    assert strat.bot_sell_validate is not None
    risk = load_risk_settings(_LIFECYCLE_RISK_SHADOW_YAML)
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-BSV-001",
                "execution_mode": "shadow",
                "guru_poll_interval_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)
    mock_instance = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance
    build_guru_trading_node(strat, risk, runtime)
    reg = mock_instance.trader.add_strategy.call_args_list[0].args[0]
    assert isinstance(reg, BotSellValidateStrategy)


def test_compose_shadow_builds(tmp_path: Path) -> None:
    strat = load_strategy_settings(_LIFECYCLE_STRATEGY_YAML)
    risk = load_risk_settings(_LIFECYCLE_RISK_SHADOW_YAML)
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-CMP-001",
                "execution_mode": "shadow",
                "guru_poll_interval_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)

    assembly = build_guru_trading_node(strat, risk, runtime)
    assert isinstance(
        assembly.startup_readiness_gate._exec_connected,
        SpikePendingExecClientsConnected,
    )
    assert assembly.risk_policy is not None
    assert assembly.execution_state is not None
    assert assembly.account_snapshots is not None
    assert assembly.capital_state is not None
    assert assembly.tradable_state_health is None  # gate off by default
    assert assembly.execution_lifecycle is not None
    assert assembly.startup_readiness_gate is not None
    assert assembly.allowance is None  # shadow: no CLOB allowance reads
    assert assembly.position_state is None
    assert assembly.deployment_budget is None
    assembly.node.build()
    assert assembly.node.is_built


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_live_nautilus_registers_factories(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """Live + polymarket_nautilus: node config has data/exec clients and factories."""
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-NAU-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-0xdef.POLYMARKET"],
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)

    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    with patch.dict(os.environ, {"POLYMARKET_PK": "0x" + "1" * 64}, clear=False):
        with patch(
            "tyrex_pm.runtime.guru_compose.build_clob_client_from_env",
            return_value=MagicMock(),
        ):
            with patch(
                "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_wallet_positions",
            ):
                with patch(
                    "tyrex_pm.runtime.guru_compose.ensure_polymarket_l2_env_from_pk_if_missing",
                ):
                    assembly = build_guru_trading_node(strat, risk, runtime)

    assert assembly.allowance is not None
    cfg = mock_node_cls.call_args.kwargs["config"]
    assert len(cfg.data_clients) == 1
    assert len(cfg.exec_clients) == 1
    assert cfg.exec_engine.position_check_interval_secs == 45.0
    assert cfg.exec_engine.open_check_interval_secs == 20.0
    assert cfg.exec_engine.open_check_open_only is True
    ec = next(iter(cfg.exec_clients.values()))
    assert ec.use_data_api is True  # wallet_sync_enabled defaults to True for live
    mock_instance.add_data_client_factory.assert_called_once()
    mock_instance.add_exec_client_factory.assert_called_once()
    assert isinstance(
        assembly.startup_readiness_gate._exec_connected,
        NautilusExecEngineClientsConnected,
    )


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_live_execution_alignment_yaml_overrides(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """Phase 5 — adapter ``use_data_api`` + engine ``open_check_open_only`` from runtime YAML."""
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-EA-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-0xdef.POLYMARKET"],
                "polymarket_use_data_api_for_positions": True,
                "live_exec_open_check_open_only": False,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)

    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    with patch.dict(os.environ, {"POLYMARKET_PK": "0x" + "1" * 64}, clear=False):
        with patch(
            "tyrex_pm.runtime.guru_compose.build_clob_client_from_env",
            return_value=MagicMock(),
        ):
            with patch(
                "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_wallet_positions",
            ):
                with patch(
                    "tyrex_pm.runtime.guru_compose.ensure_polymarket_l2_env_from_pk_if_missing",
                ):
                    _ = build_guru_trading_node(strat, risk, runtime)

    cfg = mock_node_cls.call_args.kwargs["config"]
    assert cfg.exec_engine.open_check_open_only is False
    ec = next(iter(cfg.exec_clients.values()))
    assert ec.use_data_api is True


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_live_exec_open_check_null_omits_open_interval(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """YAML null disables open-check; single LiveExecEngineConfig still carries position."""
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-NAU-OPENNULL",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-0xdef.POLYMARKET"],
                "exec_open_check_interval_seconds": None,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)

    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    with patch.dict(os.environ, {"POLYMARKET_PK": "0x" + "1" * 64}, clear=False):
        with patch(
            "tyrex_pm.runtime.guru_compose.build_clob_client_from_env",
            return_value=MagicMock(),
        ):
            with patch(
                "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_wallet_positions",
            ):
                with patch(
                    "tyrex_pm.runtime.guru_compose.ensure_polymarket_l2_env_from_pk_if_missing",
                ):
                    _ = build_guru_trading_node(strat, risk, runtime)

    cfg = mock_node_cls.call_args.kwargs["config"]
    assert cfg.exec_engine.position_check_interval_secs == 45.0
    assert cfg.exec_engine.open_check_interval_secs is None


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_live_position_check_off_keeps_open_when_yaml_says_so(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """Position check disabled in YAML but open-check default still applies when set explicitly."""
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-NAU-POSOFF",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-0xdef.POLYMARKET"],
                "exec_position_check_interval_seconds": None,
                "exec_open_check_interval_seconds": 15,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)

    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    with patch.dict(os.environ, {"POLYMARKET_PK": "0x" + "1" * 64}, clear=False):
        with patch(
            "tyrex_pm.runtime.guru_compose.build_clob_client_from_env",
            return_value=MagicMock(),
        ):
            with patch(
                "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_wallet_positions",
            ):
                with patch(
                    "tyrex_pm.runtime.guru_compose.ensure_polymarket_l2_env_from_pk_if_missing",
                ):
                    _ = build_guru_trading_node(strat, risk, runtime)

    cfg = mock_node_cls.call_args.kwargs["config"]
    assert cfg.exec_engine.position_check_interval_secs is None
    assert cfg.exec_engine.open_check_interval_secs == 15.0


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_live_wires_nautilus_port(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-FW-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-99999.POLYMARKET"],
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)

    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    with patch.dict(os.environ, {"POLYMARKET_PK": "0x" + "1" * 64}, clear=False):
        with patch(
            "tyrex_pm.runtime.guru_compose.build_clob_client_from_env",
            return_value=MagicMock(),
        ):
            with patch(
                "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_wallet_positions",
            ):
                with patch(
                    "tyrex_pm.runtime.guru_compose.ensure_polymarket_l2_env_from_pk_if_missing",
                ):
                    assembly = build_guru_trading_node(strat, risk, runtime)

    assert assembly.position_state is not None
    add_strategy_calls = mock_instance.trader.add_strategy.call_args_list
    assert add_strategy_calls
    copy_strat = add_strategy_calls[0].args[0]
    assert isinstance(copy_strat._execution, NautilusGuruExecutionPort)
    assert assembly.deployment_budget is not None


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_zero_bootstrap_wires_dynamic_and_warmup(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-ZB-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": [],
                "polymarket_startup_token_warmup_max": 3,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)
    assert runtime.polymarket_instrument_ids == ()
    assert runtime.polymarket_dynamic_instruments is True

    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    with patch.dict(os.environ, {"POLYMARKET_PK": "0x" + "1" * 64}, clear=False):
        with patch(
            "tyrex_pm.runtime.guru_compose.ensure_polymarket_l2_env_from_pk_if_missing",
        ):
            with patch(
                "tyrex_pm.runtime.guru_compose.build_clob_client_from_env",
                return_value=MagicMock(),
            ):
                with patch(
                    "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_guru_activity",
                ) as warm:
                    with patch(
                        "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_wallet_positions",
                    ) as wwallet:
                        _ = build_guru_trading_node(strat, risk, runtime)

    wwallet.assert_called_once()
    warm.assert_called_once()
    copy_strat = mock_instance.trader.add_strategy.call_args_list[0].args[0]
    port = copy_strat._execution
    assert isinstance(port, NautilusGuruExecutionPort)
    assert isinstance(port._dynamic, GuruInstrumentDynamicController)


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_nautilus_file_logging_config_when_requested(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """TradingNode LoggingConfig gets framework file fields from GuruNautilusFileLogging."""
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-LOG-001",
                "execution_mode": "live",
                "polymarket_instrument_ids": ["0xabc-0xdef.POLYMARKET"],
                "logging_level": "INFO",
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)
    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    nautilus_log = tmp_path / "logs" / "live" / "session_nautilus.log"
    nfl = GuruNautilusFileLogging(
        log_directory=str(nautilus_log.parent.resolve()),
        log_file_stem=nautilus_log.stem,
    )

    with patch.dict(os.environ, {"POLYMARKET_PK": "0x" + "1" * 64}, clear=False):
        with patch(
            "tyrex_pm.runtime.guru_compose.build_clob_client_from_env",
            return_value=MagicMock(),
        ):
            with patch(
                "tyrex_pm.runtime.guru_compose.warm_polymarket_cache_from_wallet_positions",
            ):
                with patch(
                    "tyrex_pm.runtime.guru_compose.ensure_polymarket_l2_env_from_pk_if_missing",
                ):
                    build_guru_trading_node(strat, risk, runtime, nautilus_file_logging=nfl)

    cfg = mock_node_cls.call_args.kwargs["config"]
    log_cfg = cfg.logging
    assert log_cfg.log_directory == str(nautilus_log.parent.resolve())
    assert log_cfg.log_file_name == "session_nautilus"
    assert log_cfg.clear_log_file is True
    assert log_cfg.log_level_file == "INFO"
    assert log_cfg.log_level == "INFO"


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_without_nautilus_file_logging_uses_level_only(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parent.parent
    strat = load_strategy_settings(root / "config" / "strategy" / "guru_follow.yaml")
    risk = load_risk_settings(root / "config" / "risk" / "guru_follow_risk.yaml")
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-LOG-DEF-001",
                "execution_mode": "shadow",
                "logging_level": "WARNING",
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)
    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_node_cls.return_value = mock_instance

    build_guru_trading_node(strat, risk, runtime)

    cfg = mock_node_cls.call_args.kwargs["config"]
    log_cfg = cfg.logging
    assert log_cfg.log_level == "WARNING"
    assert log_cfg.log_level_file is None
    assert log_cfg.log_directory is None
    assert log_cfg.log_file_name is None


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_tradable_health_gate_wires_nautilus_live_source(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    strat = load_strategy_settings(_LIFECYCLE_STRATEGY_YAML)
    risk = replace(
        load_risk_settings(_LIFECYCLE_RISK_SHADOW_YAML),
        tradable_state_health_gate_enabled=True,
    )
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-HLTH-001",
                "execution_mode": "shadow",
                "guru_poll_interval_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)
    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_ee = MagicMock()
    mock_ee._startup_reconciliation_event = asyncio.Event()
    mock_instance.kernel = MagicMock(exec_engine=mock_ee)
    mock_node_cls.return_value = mock_instance

    assembly = build_guru_trading_node(strat, risk, runtime)
    assert isinstance(assembly.tradable_state_health, NautilusLiveExecutionHealthSource)


@patch("tyrex_pm.runtime.guru_compose.TradingNode")
def test_compose_tradable_health_gate_raises_when_exec_engine_not_live_shape(
    mock_node_cls: MagicMock,
    tmp_path: Path,
) -> None:
    strat = load_strategy_settings(_LIFECYCLE_STRATEGY_YAML)
    risk = replace(
        load_risk_settings(_LIFECYCLE_RISK_SHADOW_YAML),
        tradable_state_health_gate_enabled=True,
    )
    live = tmp_path / "live.yaml"
    live.write_text(
        yaml.safe_dump(
            {
                "trader_id": "TEST-HLTH-BAD-001",
                "execution_mode": "shadow",
                "guru_poll_interval_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_settings(live)
    mock_instance = MagicMock()
    mock_instance.cache = MagicMock()
    mock_instance.portfolio = MagicMock()
    mock_instance.trader = MagicMock()
    mock_instance.kernel = MagicMock(exec_engine=object())
    mock_node_cls.return_value = mock_instance

    with pytest.raises(RuntimeError, match="tradable_state_health_gate_enabled"):
        build_guru_trading_node(strat, risk, runtime)
