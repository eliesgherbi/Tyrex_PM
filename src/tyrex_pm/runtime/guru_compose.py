"""Compose `TradingNode` + guru actor + copy strategy (shadow or live)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from nautilus_trader.adapters.polymarket import (
    POLYMARKET,
    PolymarketDataClientConfig,
    PolymarketExecClientConfig,
    PolymarketLiveDataClientFactory,
    PolymarketLiveExecClientFactory,
)
from nautilus_trader.common import Environment
from nautilus_trader.common.config import InstrumentProviderConfig, LoggingConfig
from nautilus_trader.config import RoutingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from tyrex_pm.config.loaders import (
    RiskSettings,
    RuntimeSettings,
    StrategySettings,
    validate_phase_b_runtime_contract,
)
from tyrex_pm.data.guru_monitor import GuruMonitorActor, GuruMonitorActorConfig
from tyrex_pm.execution.nautilus_guru_exec import NautilusGuruExecutionPort
from tyrex_pm.execution.polymarket_policy import PolymarketExecutionPolicy
from tyrex_pm.execution.port import NoOpExecutionPort
from tyrex_pm.risk.configured import ConfiguredRiskPolicy
from tyrex_pm.runtime.clob_factory import build_clob_client_from_env
from tyrex_pm.runtime.guru_cache_warmup import warm_polymarket_cache_from_guru_activity
from tyrex_pm.runtime.guru_instrument_dynamic import GuruInstrumentDynamicController
from tyrex_pm.runtime.phase_b_startup import phase_b_startup_summary_line
from tyrex_pm.runtime.polymarket_nautilus_env import ensure_polymarket_l2_env_from_pk_if_missing
from tyrex_pm.runtime.portfolio_exposure import NautilusPortfolioExposureAggregator
from tyrex_pm.runtime.state_readers import (
    ClobAllowanceStateProvider,
    NautilusAccountSnapshotProvider,
    NautilusExecutionStateReader,
    NautilusPositionStateReader,
)
from tyrex_pm.runtime.guru_run_logging import GuruNautilusFileLogging
from tyrex_pm.strategy.copy_strategy import CopyStrategy, CopyStrategyConfig

_LOG = logging.getLogger(__name__)


def _trading_node_logging_config(
    runtime: RuntimeSettings,
    nautilus_file_logging: GuruNautilusFileLogging | None,
) -> LoggingConfig:
    """Nautilus stdout level always set; optional file sink persists component/kernel logs."""
    if nautilus_file_logging is None:
        return LoggingConfig(log_level=runtime.logging_level)
    return LoggingConfig(
        log_level=runtime.logging_level,
        log_level_file=runtime.logging_level,
        log_directory=nautilus_file_logging.log_directory,
        log_file_name=nautilus_file_logging.log_file_stem,
        clear_log_file=True,
    )


@dataclass(frozen=True, slots=True)
class GuruTradingAssembly:
    """Return value of :func:`build_guru_trading_node` (node, risk, readers)."""

    node: TradingNode
    risk_policy: ConfiguredRiskPolicy
    execution_state: NautilusExecutionStateReader
    account_snapshots: NautilusAccountSnapshotProvider
    allowance: ClobAllowanceStateProvider | None
    #: Set for Nautilus live + framework submit; uses ``Portfolio.net_exposure``.
    position_state: NautilusPositionStateReader | None
    #: **Phase B B1:** Portfolio exposure aggregation (``E_pending``, ``E_filled_net``,
    #: ``E_portfolio``); ``None`` when not on framework-submit path.
    portfolio_exposure: NautilusPortfolioExposureAggregator | None


@lru_cache(maxsize=1)
def _tyrex_polymarket_client_ids() -> tuple[str, str]:
    """Stable registry keys for Polymarket live data/exec clients (not the spike script)."""
    return ("POLYMARKET-TYREX-DATA", "POLYMARKET-TYREX-EXEC")


def _instrument_load_ids(runtime: RuntimeSettings) -> frozenset[InstrumentId]:
    return frozenset(InstrumentId.from_str(s) for s in runtime.polymarket_instrument_ids)


def build_guru_trading_node(
    strategy: StrategySettings,
    risk: RiskSettings,
    runtime: RuntimeSettings,
    *,
    nautilus_file_logging: GuruNautilusFileLogging | None = None,
) -> GuruTradingAssembly:
    """
    Build a ``TradingNode`` with ``GuruMonitorActor`` + ``CopyStrategy`` registered.

    **Phase B (B0):** :func:`~tyrex_pm.config.loaders.validate_phase_b_runtime_contract`
    runs first so framework-truth gates and reserve cannot be enabled on unsupported paths.

    **Phase B (B5):** After registering guru + strategy, logs one **INFO** line via
    :func:`~tyrex_pm.runtime.phase_b_startup.phase_b_startup_summary_line` (active gate
    summary only — see ``Docs/OPERATIONS.md``).

    When ``runtime.polymarket_nautilus_live`` is true and ``execution_mode`` is ``live``,
    registers **Polymarket data + exec** client factories with shared ``InstrumentProviderConfig``
    (**Spike-observed** / **Docs-confirmed**). Otherwise keeps empty client maps (legacy side-channel submit).

    Returns a :class:`GuruTradingAssembly` with ``node.build()`` / ``node.run()`` still up to the caller.

    **Package-source-confirmed:** factories require L2 env vars; :func:`~tyrex_pm.runtime.polymarket_nautilus_env.ensure_polymarket_l2_env_from_pk_if_missing`
    fills them from ``POLYMARKET_PK`` when needed (same contract as the Step 2 spike).

    **Nautilus file logging:** When ``nautilus_file_logging`` is set (``run_guru.py``),
    :class:`~nautilus_trader.common.config.LoggingConfig` receives ``log_directory``,
    ``log_file_name`` (stem without ``.log``), ``log_level_file``, and ``clear_log_file``
    so component / kernel / adapter lines use the **framework-native** file sink — not
    Python stdout teeing.
    """
    validate_phase_b_runtime_contract(risk, runtime)

    use_nautilus = (
        runtime.polymarket_nautilus_live and runtime.execution_mode == "live"
    )
    use_framework_submit = (
        use_nautilus
        and runtime.polymarket_framework_submit
        and runtime.execution_mode == "live"
    )
    data_key, exec_key = _tyrex_polymarket_client_ids()

    if use_nautilus:
        ensure_polymarket_l2_env_from_pk_if_missing()
        instrument_provider_cfg = InstrumentProviderConfig(
            load_ids=_instrument_load_ids(runtime),
        )
        routing = RoutingConfig(default=True, venues=frozenset({POLYMARKET}))

        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        funder = os.environ.get("POLYMARKET_FUNDER")
        data_cfg = PolymarketDataClientConfig(
            signature_type=sig_type,
            funder=funder,
            instrument_provider=instrument_provider_cfg,
            routing=routing,
        )
        exec_cfg = PolymarketExecClientConfig(
            signature_type=sig_type,
            funder=funder,
            instrument_provider=instrument_provider_cfg,
            routing=routing,
        )
        cfg = TradingNodeConfig(
            trader_id=TraderId(runtime.trader_id),
            environment=Environment.LIVE,
            data_clients={data_key: data_cfg},
            exec_clients={exec_key: exec_cfg},
            logging=_trading_node_logging_config(runtime, nautilus_file_logging),
            load_state=False,
            save_state=False,
        )
    else:
        cfg = TradingNodeConfig(
            trader_id=TraderId(runtime.trader_id),
            environment=Environment.LIVE,
            data_clients={},
            exec_clients={},
            logging=_trading_node_logging_config(runtime, nautilus_file_logging),
            load_state=False,
            save_state=False,
        )

    node = TradingNode(config=cfg)

    if use_nautilus:
        node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
        node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)

    exec_reader = NautilusExecutionStateReader(node.cache)
    account_provider = NautilusAccountSnapshotProvider(node.portfolio)
    allowance_provider: ClobAllowanceStateProvider | None = None
    if runtime.execution_mode == "live":
        allowance_provider = ClobAllowanceStateProvider.from_runtime(runtime)

    position_reader: NautilusPositionStateReader | None = None
    portfolio_agg: NautilusPortfolioExposureAggregator | None = None
    if use_nautilus:
        position_reader = NautilusPositionStateReader(
            node.portfolio,
            node.cache,
            dict(runtime.polymarket_token_to_instrument),
        )

    if use_nautilus and use_framework_submit:
        portfolio_agg = NautilusPortfolioExposureAggregator(
            node.portfolio,
            node.cache,
            exec_reader,
            dict(runtime.polymarket_token_to_instrument),
        )

    risk_pol = ConfiguredRiskPolicy(
        risk,
        execution_reader=exec_reader,
        account_snapshot=account_provider,
        allowance_provider=allowance_provider,
        position_reader=position_reader
        if (use_nautilus and use_framework_submit)
        else None,
        portfolio_exposure=portfolio_agg,
        token_open_authoritative_for_pending=not use_framework_submit,
    )
    dedup_path = strategy.strategy_dedup_state_path or runtime.guru_dedup_state_path

    guru_cfg = GuruMonitorActorConfig(
        guru_wallet_address=strategy.guru_wallet_address,
        poll_interval_secs=runtime.guru_poll_interval_seconds,
        data_api_base_url=runtime.data_api_base_url,
        dedup_state_path=dedup_path,
        watermark_state_path=runtime.guru_state_path,
        activity_limit=runtime.guru_activity_limit,
        startup_backfill_seconds=runtime.guru_startup_backfill_seconds,
        max_activity_pages_per_poll=runtime.guru_max_activity_pages_per_poll,
    )
    guru = GuruMonitorActor(guru_cfg)

    copy_cfg = CopyStrategyConfig(
        token_filter_enabled=strategy.token_filter.enabled,
        allowlisted_token_ids=strategy.token_filter.allowlisted_token_ids,
        execution_mode=runtime.execution_mode,
        copy_scale=strategy.copy_scale,
    )
    strat = CopyStrategy(copy_cfg)
    strat.set_risk_policy(risk_pol)

    if runtime.execution_mode == "shadow":
        strat.set_execution_port(NoOpExecutionPort())
    elif runtime.execution_mode == "live":
        if use_framework_submit:
            need_dynamic = runtime.polymarket_dynamic_instruments or not bool(
                runtime.polymarket_instrument_ids,
            )
            dynamic_ctrl: GuruInstrumentDynamicController | None = None
            if need_dynamic:
                clob_dynamic = build_clob_client_from_env(runtime)
                dynamic_ctrl = GuruInstrumentDynamicController(
                    node.cache,
                    clob_dynamic,
                    runtime,
                )
                if not runtime.polymarket_instrument_ids:
                    warm_polymarket_cache_from_guru_activity(
                        dynamic_ctrl,
                        guru_wallet_address=strategy.guru_wallet_address,
                        runtime=runtime,
                    )
            strat.set_execution_port(
                NautilusGuruExecutionPort(strat, runtime, dynamic=dynamic_ctrl),
            )
        else:
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

    _LOG.info(
        "%s",
        phase_b_startup_summary_line(
            risk,
            runtime,
            b1_aggregator_wired=portfolio_agg is not None,
        ),
    )

    return GuruTradingAssembly(
        node=node,
        risk_policy=risk_pol,
        execution_state=exec_reader,
        account_snapshots=account_provider,
        allowance=allowance_provider,
        position_state=position_reader
        if (use_nautilus and use_framework_submit)
        else None,
        portfolio_exposure=portfolio_agg,
    )
