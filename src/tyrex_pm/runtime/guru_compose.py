"""Compose `TradingNode` + guru actor + copy strategy (shadow or live)."""

from __future__ import annotations

from nautilus_trader.common import Environment
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings, StrategySettings
from tyrex_pm.data.guru_monitor import GuruMonitorActor, GuruMonitorActorConfig
from tyrex_pm.execution.polymarket_policy import PolymarketExecutionPolicy
from tyrex_pm.execution.port import NoOpExecutionPort
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.clob_factory import build_clob_client_from_env
from tyrex_pm.strategy.copy_strategy import CopyStrategy, CopyStrategyConfig


def build_guru_trading_node(
    strategy: StrategySettings,
    risk: RiskSettings,
    runtime: RuntimeSettings,
) -> tuple[TradingNode, ConfiguredRiskPolicy]:
    """
    Build a ``TradingNode`` with ``GuruMonitorActor`` + ``CopyStrategy`` registered.

    Returns ``(node, risk_policy)`` with ``node.build()`` / ``node.run()`` still up to the caller.
    """
    cfg = TradingNodeConfig(
        trader_id=TraderId(runtime.trader_id),
        environment=Environment.LIVE,
        data_clients={},
        exec_clients={},
        logging=LoggingConfig(log_level=runtime.logging_level),
        load_state=False,
        save_state=False,
    )
    node = TradingNode(config=cfg)

    risk_pol = ConfiguredRiskPolicy(risk)
    dedup_path = strategy.strategy_dedup_state_path or runtime.guru_dedup_state_path

    guru_cfg = GuruMonitorActorConfig(
        guru_wallet_address=strategy.guru_wallet_address,
        poll_interval_secs=runtime.guru_poll_interval_seconds,
        data_api_base_url=runtime.data_api_base_url,
        dedup_state_path=dedup_path,
    )
    guru = GuruMonitorActor(guru_cfg)

    copy_cfg = CopyStrategyConfig(
        allowlisted_token_ids=strategy.allowlisted_token_ids,
        execution_mode=runtime.execution_mode,
        copy_scale=strategy.copy_scale,
    )
    strat = CopyStrategy(copy_cfg)
    strat.set_risk_policy(risk_pol)

    if runtime.execution_mode == "shadow":
        strat.set_execution_port(NoOpExecutionPort())
    elif runtime.execution_mode == "live":
        client = build_clob_client_from_env(runtime)
        strat.set_execution_port(
            PolymarketExecutionPolicy(
                client,
                runtime,
                on_submit_ok=risk_pol.note_fill_assumption,
            )
        )
    else:
        raise RuntimeError(f"Unknown execution_mode: {runtime.execution_mode}")

    node.trader.add_actor(guru)
    node.trader.add_strategy(strat)

    return node, risk_pol
