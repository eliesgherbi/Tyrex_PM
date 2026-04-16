"""YAML loaders with validation (secrets stay in ``.env``)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.strategy.validation_constants import DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS


def _root(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


@dataclass(frozen=True, slots=True)
class TokenFilterSettings:
    """Strategy YAML ``token_filter`` block."""

    enabled: bool
    allowlisted_token_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExitFilterSettings:
    """Strategy YAML ``filters.exit_filter`` (optional block)."""

    enabled: bool = False
    #: ``mirror_guru`` | ``full_exit`` — ``full_exit`` only when ``enabled``.
    exit_method: str = "mirror_guru"


@dataclass(frozen=True, slots=True)
class StaticAmountSettings:
    """``filters.significance_filter.static_amount``."""

    enabled: bool = False
    amount_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class SignificanceConvictionSettings:
    """``filters.significance_filter.significance_conviction``."""

    enabled: bool = False
    lookback_trades: int = 20
    #: v1: ``median`` only.
    threshold_method: str = "median"


@dataclass(frozen=True, slots=True)
class SignificanceFilterSettings:
    static_amount: StaticAmountSettings
    significance_conviction: SignificanceConvictionSettings


@dataclass(frozen=True, slots=True)
class LayerAFiltersSettings:
    """Optional strategy YAML ``filters`` root — omit for legacy behavior."""

    exit_filter: ExitFilterSettings
    significance_filter: SignificanceFilterSettings


def _default_layer_a_filters() -> LayerAFiltersSettings:
    return LayerAFiltersSettings(
        exit_filter=ExitFilterSettings(False, "mirror_guru"),
        significance_filter=SignificanceFilterSettings(
            static_amount=StaticAmountSettings(False, 0.0),
            significance_conviction=SignificanceConvictionSettings(False, 20, "median"),
        ),
    )


@dataclass(frozen=True, slots=True)
class BotSellValidateSettings:
    """Optional strategy YAML ``bot_sell_validate`` block (isolated Scenario A harness)."""

    sell_delay_seconds: float
    max_cycles: int
    #: Use marketable limits (top-of-book + tick bump + slippage cap) — validation only.
    validation_aggressive_limits: bool = True
    #: Extra whole-tick steps through the book beyond the anchor (BUY: up).
    validation_buy_aggression_ticks: int = 2
    #: Extra whole-tick steps beyond anchor (SELL: down).
    validation_sell_aggression_ticks: int = 2
    #: Max relative move vs guru/reference price bound (0–1).
    validation_max_slippage_fraction: float = 0.08
    #: When runtime enables REST book, allow CLOB snapshot for pricing (see live YAML).
    validation_rest_book_for_pricing: bool = True
    #: Scenario A validation SELL only: shave ``bps/10_000`` off long ``net_position`` before cap vs BUY fill.
    validation_sell_inventory_haircut_bps: float = DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS


@dataclass(frozen=True, slots=True)
class StrategySettings:
    guru_wallet_address: str
    token_filter: TokenFilterSettings
    copy_scale: float
    strategy_dedup_state_path: str | None = None
    #: Optional conviction-weighted sizing (default off = proportional ``copy_scale`` only).
    conviction_sizing_enabled: bool = False
    conviction_sizing_cap: float = 2.0
    conviction_sizing_lookback_trades: int = 20
    #: When set, :func:`build_guru_trading_node` wires
    #: :class:`~tyrex_pm.strategy.bot_sell_validate_strategy.BotSellValidateStrategy`
    #: instead of :class:`~tyrex_pm.strategy.copy_strategy.CopyStrategy`.
    bot_sell_validate: BotSellValidateSettings | None = None
    #: Optional Layer A filters (``filters:`` in strategy YAML). Omitted YAML → defaults (all off).
    layer_a: LayerAFiltersSettings = _default_layer_a_filters()


@dataclass(frozen=True, slots=True)
class RiskSettings:
    """
    Pre-trade risk parameters — **deployment-budget** model.

    **Per-order:** ``order_deploy = price_ref × quantity`` bounded by ``max_notional_*`` / ``min_notional_*``
    policies (``deny`` | ``cap``). **BUY-only** minimum when ``min_notional_usd_per_order > 0``.
    **Per-token:** ``token_deploy + order_deploy`` vs ``max_token_notional_usd_open`` where
    ``token_deploy`` is pending (resting ``leaves ×`` limit) + filled
    (``abs(signed_qty) × avg_px_open``) on that token. **SELL:** the additive open-cap check is
    skipped only after ``filled_usd_for_token >= order_deploy`` (exit inventory gate — no naked
    oversell); see ``ConfiguredRiskPolicy._sell_exit_inventory_gate``.
    **Portfolio:** ``portfolio_deploy + order_deploy`` vs ``max_portfolio_notional_usd_open`` where
    ``portfolio_deploy`` sums the same pending/filled semantics across Polymarket. **SELL:** same
    bypass rule as per-token when the inventory gate passes.

    **Framework-truth gates:** Finite ``max_portfolio_notional_usd_open`` and/or
    ``max_concurrent_guru_resting_orders`` require **live** ``execution_mode`` (see
    :func:`validate_phase_b_runtime_contract`). Concurrent guru rests and collateral reserve are separate fields below.
    """

    #: Max **order_deploy** (USD) for a single new intent; ``price_ref × quantity``.
    max_notional_usd_per_order: float
    max_token_notional_usd_open: float
    kill_switch: bool
    fail_on_missing_price_for_notional: bool
    #: When true, pre-trade risk requires fresh account + allowance snapshots (live ops).
    capital_gate_enabled: bool = False
    #: Maximum age of the cached **account** snapshot before refresh; fail-closed if refresh fails.
    max_account_snapshot_age_seconds: float = 30.0
    #: Maximum age of the cached **allowance** snapshot before refresh.
    max_allowance_snapshot_age_seconds: float = 120.0
    #: Optional minimum collateral balance (USDC, same units as py-clob ``balance`` strings).
    min_collateral_balance_usd: float | None = None
    #: Optional minimum allowance (USDC) from py-clob ``allowance`` field.
    min_allowance_usd: float | None = None
    #: If true and per-token cap is finite, deny when token deployment (pending+filled) cannot be
    #: computed cleanly; if false, missing filled leg is treated as **0** (underestimate).
    fail_on_unresolved_token_deployment: bool = False
    #: Portfolio-wide deployment cap vs ``portfolio_deploy + order_deploy``. ``inf`` = off.
    max_portfolio_notional_usd_open: float = float("inf")
    #: If true and portfolio cap is finite, deny when portfolio deployment sum cannot be computed.
    #: If false, treat unresolvable filled legs as **0** for portfolio total (underestimate).
    fail_on_unresolved_portfolio_deployment: bool = True
    #: Max concurrent **guru-origin** resting orders. ``None`` = disabled.
    max_concurrent_guru_resting_orders: int | None = None
    #: USDC collateral floor held back from new **BUY** risk (after capital mins when gate is on).
    collateral_reserve_usd: float = 0.0
    #: **BUY** only: floor when ``> 0`` — ``deny`` rejects below; ``cap`` bumps qty to meet min.
    min_notional_usd_per_order: float = 0.0
    #: ``deny`` | ``cap`` — below minimum: reject or bump quantity (BUY only; ignored if min is 0).
    min_notional_policy: str = "deny"
    #: ``deny`` | ``cap`` — above maximum: reject or clip quantity down to cap.
    max_notional_policy: str = "cap"
    #: When **true**, :class:`~tyrex_pm.risk.configured.ConfiguredRiskPolicy` applies
    #: ``TradableStateHealth`` §10 before capital/deployment gates. Compose wires
    #: :class:`~tyrex_pm.runtime.tradable_state.nautilus_live_health.NautilusLiveExecutionHealthSource`.
    tradable_state_health_gate_enabled: bool = False
    #: §10 — allow SELL under ``DEGRADED_OMS`` when **true** (default **false**).
    allow_exit_when_degraded_oms: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    trader_id: str
    execution_mode: str
    guru_poll_interval_seconds: float
    data_api_base_url: str
    guru_dedup_state_path: str
    guru_state_path: str
    guru_activity_limit: int
    guru_startup_backfill_seconds: float
    guru_max_activity_pages_per_poll: int
    logging_level: str
    clob_host: str
    chain_id: int
    #: Full ``InstrumentId`` strings (``condition_id-token_id.POLYMARKET``); shared
    #: ``load_ids`` for data + exec clients. **Live:** empty list enables zero-bootstrap
    #: (dynamic instrument resolution).
    polymarket_instrument_ids: tuple[str, ...]
    #: Outcome ``token_id`` → full ``InstrumentId`` string (from ``polymarket_instrument_ids``).
    polymarket_token_to_instrument: tuple[tuple[str, str], ...]
    #: Gamma+CLOB resolve for unknown guru ``token_id``; activate into ``Cache``.
    #: **Live** with non-empty ``polymarket_instrument_ids``: opt-in. **Live** with empty ids: implied.
    polymarket_dynamic_instruments: bool
    #: Max **new** dynamic cache inserts per process (0 = no new adds).
    polymarket_dynamic_max_activations: int
    #: Gamma HTTP API base (Get Markets with ``clob_token_ids``).
    polymarket_gamma_base_url: str
    #: Timeout for Gamma HTTP calls (seconds).
    polymarket_gamma_http_timeout_seconds: float
    #: Max guru outcome tokens to pre-resolve at node build from Data API ``/activity``
    #: (0 = skip self-bootstrap). Only used when ``polymarket_instrument_ids`` is empty.
    polymarket_startup_token_warmup_max: int
    #: Nautilus :class:`~nautilus_trader.live.config.LiveExecEngineConfig`
    #: ``position_check_interval_secs`` — periodic venue-vs-cache position reconciliation (**live**).
    #: ``None`` disables (Nautilus default). Loader sets **45** for live when YAML omits the key.
    exec_position_check_interval_secs: float | None = None
    #: Same Nautilus config: ``open_check_interval_secs`` — periodic open-order / venue order
    #: reconciliation (**live**). ``None`` disables (Nautilus default: no open-check task).
    #: Loader sets **20** for live when YAML omits the key; **shadow** keeps ``None`` (no
    #: ``TradingNode`` exec engine on the shadow path).
    exec_open_check_interval_secs: float | None = None
    #: At compose, preload follower positions into ``Cache`` (Data API ``/positions``),
    #: bypassing ``polymarket_dynamic_max_activations`` so startup/periodic reconciliation can resolve
    #: every held outcome. ``0`` = off. Loader sets **128** for live when YAML omits the key.
    polymarket_wallet_position_warmup_max: int = 0
    #: ``poll_only`` | ``rtds_shadow`` | ``rtds_primary`` — guru market-data ingest mode.
    guru_ingest_mode: str = "poll_only"
    #: Optional rollout label for logs (informational; pairs with ``guru_ingest_mode``).
    guru_ingest_phase: str = "0"
    guru_rtds_url: str = "wss://ws-live-data.polymarket.com"
    guru_rtds_liveness_timeout_seconds: float = 120.0
    guru_rtds_reconnect_retry_initial_seconds: float = 1.0
    guru_rtds_reconnect_retry_max_seconds: float = 60.0
    guru_rtds_ping_interval_seconds: float = 5.0
    guru_poll_fallback_enabled: bool = True
    guru_poll_fallback_interval_seconds: float | None = None
    guru_gap_fill_enabled: bool = True
    guru_gap_fill_lookback_seconds: float = 60.0
    guru_proxy_wallet_validation_required: bool = False
    guru_stream_queue_drain_interval_ms: int = 50
    #: ---- Book-aware execution (framework ``NautilusGuruExecutionPort`` only; default off) ----
    #: Order-size policy is **risk** only. Execution snaps to instrument tick/size step internally
    #: before ``submit_order`` (not operator-configurable).
    execution_entry_guard_enabled: bool = False
    #: Max ticks market may move against follower vs guru reference (0 = treat guard as off).
    execution_max_entry_slippage_ticks: int = 0
    execution_book_depth_clip_enabled: bool = False
    execution_book_depth_utilization_cap: float = 1.0
    execution_book_rest_snapshot_enabled: bool = False
    #: If true and guard/clip need L2 but no snapshot: skip submit; if false: skip feature only.
    execution_book_strict: bool = False
    execution_limit_timeout_enabled: bool = False
    execution_limit_timeout_seconds: float = 30.0
    #: Persist structured reporting facts under ``var/reporting/runs/<run_id>/`` when true.
    reporting_enabled: bool = False
    reporting_base_dir: str = "var/reporting/runs"
    reporting_sink_max_queue: int = 50_000
    reporting_sink_batch_size: int = 128
    #: When ``reporting_enabled``, pull wallet / collateral snapshots for facts even if
    #: ``capital_gate_enabled`` is false (best-effort; does not block trading).
    reporting_capital_observability_enabled: bool = True
    #: Extra ``account_snapshot`` facts with ``snapshot_trigger=periodic`` at most this often
    #: (wall clock, seconds). ``0`` disables periodic-only snapshots (risk/submit/deny still record).
    reporting_capital_snapshot_period_seconds: float = 300.0
    #: Phase 3 — ``startup_readiness.md`` §8.5: ``deadline_mono = T0 + this`` (seconds).
    startup_readiness_timeout_seconds: float = 120.0
    #: Shadow: when **true**, run full gate instead of immediate READY (§8.3 dev flag).
    startup_strict_shadow: bool = False
    #: Live: allow ``DEGRADED`` / ``NO_NEW_ENTRIES`` when health is ``DEGRADED_OMS`` (§8.2.4).
    startup_allow_degraded_live: bool = False
    #: §8.5.2 — ``exit`` (non-zero process exit) vs ``no_trade`` (keep running, block all submits).
    startup_not_ready_behavior: str = "exit"
    #: Phase 4 — ``shutdown_drain.md`` §5: cancel-and-drain before ``node.stop()`` (live; shadow skips).
    shutdown_drain_enabled: bool = True
    #: Bounded wait for open orders to clear after cancel (seconds); default **30** (frozen §14).
    shutdown_drain_timeout_seconds: float = 30.0
    #: YAML opt-in to skip drain (``TYREX_SHUTDOWN_DRAIN_OVERRIDE`` env also); loud log when active.
    shutdown_drain_override: bool = False
    #: Phase 5 — ``execution_truth_alignment.md`` §14: Data API vs CLOB for adapter position reports.
    polymarket_use_data_api_for_positions: bool = False
    #: Phase 5 — ``LiveExecEngineConfig.open_check_open_only``; ``None`` = omit kwarg (Nautilus default).
    live_exec_open_check_open_only: bool | None = None
    #: Venue Sync Truth — continuous wallet instrument discovery (live only).
    wallet_sync_enabled: bool = False
    #: Interval between wallet sync poll cycles (seconds). Floor: 5.0.
    wallet_sync_poll_interval_seconds: float = 15.0
    #: Max seconds after on_start before startup is considered timed out. Floor: 30.0.
    wallet_sync_startup_deadline_seconds: float = 120.0
    #: Max cycles a single condition_id may fail resolution before terminal. Floor: 1.
    wallet_sync_per_instrument_max_retries: int = 3
    venue_state_ttl_seconds: float = 30.0
    venue_state_cash_poll_interval_seconds: float = 10.0
    venue_state_refresh_force_max_ms: int = 500


def _polymarket_token_instrument_map(
    poly_ids: tuple[str, ...],
    *,
    path: Path,
) -> tuple[tuple[str, str], ...]:
    """
    **Package-source-confirmed:** ``get_polymarket_token_id`` extracts CLOB token
    from ``InstrumentId``.
    """
    m: dict[str, str] = {}
    for instr_s in poly_ids:
        try:
            iid = InstrumentId.from_str(instr_s)
            tid = get_polymarket_token_id(iid)
        except ValueError as exc:
            raise ValueError(
                f"{path}: invalid polymarket_instrument_ids entry {instr_s!r}: {exc}",
            ) from exc
        if tid in m and m[tid] != instr_s:
            raise ValueError(
                f"{path}: duplicate outcome token_id {tid!r} in polymarket_instrument_ids "
                f"({m[tid]!r} vs {instr_s!r})",
            )
        m[tid] = instr_s
    return tuple(sorted(m.items()))


def _normalize_token_list(raw_list: list[Any], *, path: Path, ctx: str) -> tuple[str, ...]:
    norm = tuple(str(x).strip() for x in raw_list if str(x).strip())
    if len(norm) != len(set(norm)):
        raise ValueError(f"{path}: duplicate token ids in {ctx}")
    return norm


def _parse_layer_a_filters(p: Path, raw: Any) -> LayerAFiltersSettings:
    if raw is None:
        return _default_layer_a_filters()
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: filters must be a mapping when present")
    ex_raw = raw.get("exit_filter")
    if ex_raw is None:
        ex_raw = {}
    if not isinstance(ex_raw, dict):
        raise ValueError(f"{p}: filters.exit_filter must be a mapping when present")
    ex_en = bool(ex_raw.get("enabled", False))
    ex_method = str(ex_raw.get("exit_method", "mirror_guru")).strip()
    if ex_en and ex_method not in ("mirror_guru", "full_exit"):
        raise ValueError(
            f"{p}: filters.exit_filter.exit_method must be mirror_guru or full_exit "
            f"when enabled (got {ex_method!r})",
        )
    if not ex_en:
        ex_method = "mirror_guru"
    exit_filter = ExitFilterSettings(enabled=ex_en, exit_method=ex_method)

    sig_root = raw.get("significance_filter")
    if sig_root is None:
        sig_root = {}
    if not isinstance(sig_root, dict):
        raise ValueError(f"{p}: filters.significance_filter must be a mapping when present")

    st_raw = sig_root.get("static_amount")
    if st_raw is None:
        st_raw = {}
    if not isinstance(st_raw, dict):
        raise ValueError(f"{p}: filters.significance_filter.static_amount must be a mapping when present")
    st_en = bool(st_raw.get("enabled", False))
    st_amt = float(st_raw.get("amount_usd", 0.0))
    if st_en:
        if st_amt <= 0:
            raise ValueError(
                f"{p}: filters.significance_filter.static_amount.amount_usd must be > 0 when enabled",
            )
    static_amount = StaticAmountSettings(enabled=st_en, amount_usd=st_amt)

    cv_raw = sig_root.get("significance_conviction")
    if cv_raw is None:
        cv_raw = {}
    if not isinstance(cv_raw, dict):
        raise ValueError(
            f"{p}: filters.significance_filter.significance_conviction must be a mapping when present",
        )
    cv_en = bool(cv_raw.get("enabled", False))
    lookback = int(cv_raw.get("lookback_trades", 20))
    method = str(cv_raw.get("threshold_method", "median")).strip()
    if cv_en:
        if lookback < 1:
            raise ValueError(
                f"{p}: filters.significance_filter.significance_conviction.lookback_trades "
                "must be >= 1 when enabled",
            )
        if method != "median":
            raise ValueError(
                f"{p}: filters.significance_filter.significance_conviction.threshold_method "
                f"must be median in v1 (got {method!r})",
            )
    significance_conviction = SignificanceConvictionSettings(
        enabled=cv_en,
        lookback_trades=lookback,
        threshold_method=method,
    )
    significance_filter = SignificanceFilterSettings(
        static_amount=static_amount,
        significance_conviction=significance_conviction,
    )
    return LayerAFiltersSettings(exit_filter=exit_filter, significance_filter=significance_filter)


def load_strategy_settings(path: str | Path) -> StrategySettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    if "min_follow_notional_usd" in raw:
        raise ValueError(
            f"{p}: obsolete key min_follow_notional_usd — removed; use risk YAML "
            "min_notional_usd_per_order and min_notional_policy",
        )
    if "guru_wallet_address" not in raw:
        raise ValueError(f"{p}: missing required key: guru_wallet_address")
    gw = str(raw["guru_wallet_address"]).strip()
    if not gw.startswith("0x") or len(gw) != 42:
        raise ValueError(f"{p}: guru_wallet_address must be 0x + 40 hex chars")

    tf_raw = raw.get("token_filter")
    if not isinstance(tf_raw, dict):
        raise ValueError(f"{p}: token_filter must be a mapping")
    if "enabled" not in tf_raw:
        raise ValueError(f"{p}: token_filter.enabled is required")

    enabled = bool(tf_raw["enabled"])
    tokens_field = tf_raw.get("allowlisted_token_ids")
    if tokens_field is None:
        tokens_field = []
    if not isinstance(tokens_field, list):
        raise ValueError(f"{p}: token_filter.allowlisted_token_ids must be a list")

    norm = _normalize_token_list(
        tokens_field,
        path=p,
        ctx="token_filter.allowlisted_token_ids",
    )
    if enabled and not norm:
        raise ValueError(
            f"{p}: when token_filter.enabled is true, "
            "token_filter.allowlisted_token_ids must be non-empty"
        )

    token_filter = TokenFilterSettings(enabled=enabled, allowlisted_token_ids=norm)

    scale = float(raw.get("copy_scale", 1.0))
    if scale < 0:
        raise ValueError(f"{p}: copy_scale must be >= 0")

    dedup = raw.get("strategy_dedup_state_path")
    dedup_s = str(dedup).strip() if dedup else None

    conv_en = bool(raw.get("conviction_sizing_enabled", False))
    conv_cap = float(raw.get("conviction_sizing_cap", 2.0))
    lookback = int(raw.get("conviction_sizing_lookback_trades", 20))

    if conv_en:
        if lookback < 1:
            raise ValueError(f"{p}: conviction_sizing_lookback_trades must be >= 1 when enabled")
        if conv_cap <= 0:
            raise ValueError(f"{p}: conviction_sizing_cap must be > 0 when conviction_sizing_enabled")

    bsv_raw = raw.get("bot_sell_validate")
    bsv: BotSellValidateSettings | None = None
    if bsv_raw is not None:
        if not isinstance(bsv_raw, dict):
            raise ValueError(f"{p}: bot_sell_validate must be a mapping when present")
        delay = float(bsv_raw.get("sell_delay_seconds", 5.0))
        if delay < 0.0:
            raise ValueError(f"{p}: bot_sell_validate.sell_delay_seconds must be >= 0")
        mc = int(bsv_raw.get("max_cycles", 1))
        if mc < 1:
            raise ValueError(f"{p}: bot_sell_validate.max_cycles must be >= 1")
        agg = bool(bsv_raw.get("validation_aggressive_limits", True))
        b_ticks = int(bsv_raw.get("validation_buy_aggression_ticks", 2))
        s_ticks = int(bsv_raw.get("validation_sell_aggression_ticks", 2))
        if b_ticks < 0 or s_ticks < 0:
            raise ValueError(
                f"{p}: validation_*_aggression_ticks must be >= 0",
            )
        msf = float(bsv_raw.get("validation_max_slippage_fraction", 0.08))
        if msf < 0.0 or msf > 1.0:
            raise ValueError(
                f"{p}: bot_sell_validate.validation_max_slippage_fraction must be in [0, 1]",
            )
        rest_book = bool(bsv_raw.get("validation_rest_book_for_pricing", True))
        hcut = float(
            bsv_raw.get(
                "validation_sell_inventory_haircut_bps",
                DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS,
            ),
        )
        if hcut < 0.0 or hcut > 10_000.0:
            raise ValueError(
                f"{p}: bot_sell_validate.validation_sell_inventory_haircut_bps must be in [0, 10000]",
            )
        bsv = BotSellValidateSettings(
            sell_delay_seconds=delay,
            max_cycles=mc,
            validation_aggressive_limits=agg,
            validation_buy_aggression_ticks=b_ticks,
            validation_sell_aggression_ticks=s_ticks,
            validation_max_slippage_fraction=msf,
            validation_rest_book_for_pricing=rest_book,
            validation_sell_inventory_haircut_bps=hcut,
        )

    layer_a = _parse_layer_a_filters(p, raw.get("filters"))

    return StrategySettings(
        guru_wallet_address=gw,
        token_filter=token_filter,
        copy_scale=scale,
        strategy_dedup_state_path=dedup_s,
        conviction_sizing_enabled=conv_en,
        conviction_sizing_cap=conv_cap,
        conviction_sizing_lookback_trades=lookback,
        bot_sell_validate=bsv,
        layer_a=layer_a,
    )


def load_risk_settings(path: str | Path) -> RiskSettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    for obsolete in (
        "max_order_quantity",
        "fail_on_unresolved_portfolio_exposure",
        "portfolio_sizing_mode",
        "fail_on_unresolved_position_for_token_cap",
    ):
        if obsolete in raw:
            raise ValueError(
                f"{p}: obsolete risk key {obsolete!r} — removed in deployment-budget model; "
                "see Docs/CONFIG_MODEL.md (use max_notional_usd_per_order / "
                "fail_on_unresolved_token_deployment / fail_on_unresolved_portfolio_deployment; "
                "remove portfolio_sizing_mode and marked-exposure keys)",
            )

    mn = raw.get("max_notional_usd_per_order")
    if mn is None:
        raise ValueError(f"{p}: max_notional_usd_per_order is required")
    max_notional_usd_per_order = float(mn)
    if max_notional_usd_per_order <= 0:
        raise ValueError(f"{p}: max_notional_usd_per_order must be positive")

    mt = raw.get("max_token_notional_usd_open")
    max_token = float("inf") if mt is None else float(mt)
    if max_token <= 0:
        raise ValueError(f"{p}: max_token_notional_usd_open must be positive or null (unlimited)")

    kill_switch = bool(raw.get("kill_switch", False))
    fail_on_missing_price_for_notional = bool(
        raw.get("fail_on_missing_price_for_notional", True)
    )

    capital_gate = bool(raw.get("capital_gate_enabled", False))
    acct_age = float(raw.get("max_account_snapshot_age_seconds", 30.0))
    if acct_age < 0:
        raise ValueError(f"{p}: max_account_snapshot_age_seconds must be >= 0")
    allow_age = float(raw.get("max_allowance_snapshot_age_seconds", 120.0))
    if allow_age < 0:
        raise ValueError(f"{p}: max_allowance_snapshot_age_seconds must be >= 0")

    mc_raw = raw.get("min_collateral_balance_usd")
    min_collateral = None if mc_raw is None else float(mc_raw)

    ma_raw = raw.get("min_allowance_usd")
    min_allow = None if ma_raw is None else float(ma_raw)

    fail_unresolved_token = bool(raw.get("fail_on_unresolved_token_deployment", False))

    mp_raw = raw.get("max_portfolio_notional_usd_open")
    max_portfolio = float("inf") if mp_raw is None else float(mp_raw)
    if not math.isinf(max_portfolio) and max_portfolio <= 0:
        raise ValueError(
            f"{p}: max_portfolio_notional_usd_open must be positive or null/omitted (unlimited)",
        )

    fail_portfolio_dep = bool(raw.get("fail_on_unresolved_portfolio_deployment", True))

    mc_raw = raw.get("max_concurrent_guru_resting_orders")
    max_conc: int | None
    if mc_raw is None:
        max_conc = None
    else:
        max_conc = int(mc_raw)
        if max_conc < 1:
            raise ValueError(
                f"{p}: max_concurrent_guru_resting_orders must be null/omitted (off) "
                "or an integer >= 1",
            )

    collateral_reserve = float(raw.get("collateral_reserve_usd", 0.0))
    if collateral_reserve < 0:
        raise ValueError(f"{p}: collateral_reserve_usd must be >= 0")
    if collateral_reserve > 0 and not capital_gate:
        raise ValueError(
            f"{p}: collateral_reserve_usd > 0 requires capital_gate_enabled: true",
        )

    min_order = float(raw.get("min_notional_usd_per_order", 0.0))
    if min_order < 0:
        raise ValueError(f"{p}: min_notional_usd_per_order must be >= 0")

    min_pol = str(raw.get("min_notional_policy", "deny")).strip().lower()
    max_pol = str(raw.get("max_notional_policy", "cap")).strip().lower()
    if min_pol not in ("deny", "cap"):
        raise ValueError(f"{p}: min_notional_policy must be deny or cap (got {min_pol!r})")
    if max_pol not in ("deny", "cap"):
        raise ValueError(f"{p}: max_notional_policy must be deny or cap (got {max_pol!r})")

    tsh_gate = bool(raw.get("tradable_state_health_gate_enabled", False))
    allow_deg = bool(raw.get("allow_exit_when_degraded_oms", False))

    return RiskSettings(
        max_notional_usd_per_order=max_notional_usd_per_order,
        max_token_notional_usd_open=max_token,
        kill_switch=kill_switch,
        fail_on_missing_price_for_notional=fail_on_missing_price_for_notional,
        capital_gate_enabled=capital_gate,
        max_account_snapshot_age_seconds=acct_age,
        max_allowance_snapshot_age_seconds=allow_age,
        min_collateral_balance_usd=min_collateral,
        min_allowance_usd=min_allow,
        fail_on_unresolved_token_deployment=fail_unresolved_token,
        max_portfolio_notional_usd_open=max_portfolio,
        fail_on_unresolved_portfolio_deployment=fail_portfolio_dep,
        max_concurrent_guru_resting_orders=max_conc,
        collateral_reserve_usd=collateral_reserve,
        min_notional_usd_per_order=min_order,
        min_notional_policy=min_pol,
        max_notional_policy=max_pol,
        tradable_state_health_gate_enabled=tsh_gate,
        allow_exit_when_degraded_oms=allow_deg,
    )


def phase_b_framework_truth_gates_active(risk: RiskSettings) -> bool:
    """True if any configured gate requires Nautilus ``Cache`` framework truth (portfolio cap, concurrent rests)."""
    portfolio_on = not math.isinf(risk.max_portfolio_notional_usd_open)
    conc_on = risk.max_concurrent_guru_resting_orders is not None
    return portfolio_on or conc_on


def framework_phase_b_eligible(runtime: RuntimeSettings) -> bool:
    """True iff this runtime can run framework-truth gates (currently: ``execution_mode == live``)."""
    return runtime.execution_mode == "live"


def validate_phase_b_runtime_contract(risk: RiskSettings, runtime: RuntimeSettings) -> None:
    """
    Reject unsupported risk/runtime combinations for framework-truth gates and reserve.

    Call from :func:`tyrex_pm.runtime.guru_compose.build_guru_trading_node` so
    invalid assemblies fail at startup with explicit :class:`ValueError`.

    **Not** a substitute for :func:`load_risk_settings` checks that need no runtime
    (e.g. reserve vs ``capital_gate_enabled``).
    """
    if runtime.execution_mode == "shadow":
        if risk.collateral_reserve_usd > 0:
            raise ValueError(
                "collateral_reserve_usd > 0 is invalid when execution_mode is shadow "
                "(no py-clob collateral snapshot path on the guru node)",
            )
        if phase_b_framework_truth_gates_active(risk):
            raise ValueError(
                "Framework-truth gates are invalid when execution_mode is shadow "
                "(finite max_portfolio_notional_usd_open and/or "
                "max_concurrent_guru_resting_orders set)",
            )

    if phase_b_framework_truth_gates_active(risk) and not framework_phase_b_eligible(runtime):
        raise ValueError(
            "Framework-truth gates require execution_mode=live "
            f"(got mode={runtime.execution_mode!r})",
        )


def load_runtime_settings(path: str | Path) -> RuntimeSettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    tid = str(raw.get("trader_id") or "").strip()
    if not tid or "-" not in tid:
        raise ValueError(f"{p}: trader_id must look like NAME-001")

    mode = str(raw.get("execution_mode", "shadow")).lower().strip()
    if mode not in ("shadow", "live"):
        raise ValueError(f"{p}: execution_mode must be shadow or live")

    poll = float(raw.get("guru_poll_interval_seconds", 30.0))
    if poll <= 0:
        raise ValueError(f"{p}: guru_poll_interval_seconds must be positive")

    api = str(raw.get("data_api_base_url", "https://data-api.polymarket.com")).rstrip("/")
    dedup = raw.get("guru_dedup_state_path")
    dedup_s = str(dedup).strip() if dedup else "var/guru_dedup.json"

    state = raw.get("guru_state_path")
    state_s = str(state).strip() if state else "var/guru_watermark.json"

    activity_limit = int(raw.get("guru_activity_limit", 200))
    if not (1 <= activity_limit <= 500):
        raise ValueError(f"{p}: guru_activity_limit must be between 1 and 500")

    backfill = float(raw.get("guru_startup_backfill_seconds", 0.0))
    if backfill < 0:
        raise ValueError(f"{p}: guru_startup_backfill_seconds must be >= 0")

    max_pages = int(raw.get("guru_max_activity_pages_per_poll", 4))
    if not (1 <= max_pages <= 20):
        raise ValueError(f"{p}: guru_max_activity_pages_per_poll must be between 1 and 20")

    log_level = str(raw.get("logging_level", "INFO")).upper()
    clob = str(raw.get("clob_host", "https://clob.polymarket.com")).rstrip("/")
    chain_id = int(raw.get("chain_id", 137))

    for obsolete_key in ("polymarket_nautilus_live", "polymarket_framework_submit"):
        if obsolete_key in raw:
            raise ValueError(
                f"{p}: obsolete key {obsolete_key!r} — execution_mode: live always uses "
                "Nautilus Trader with framework order submit; remove this key from runtime YAML",
            )

    pdi = bool(raw.get("polymarket_dynamic_instruments", False))

    inst_raw = raw.get("polymarket_instrument_ids")
    if inst_raw is None:
        inst_raw = []
    if not isinstance(inst_raw, list):
        raise ValueError(f"{p}: polymarket_instrument_ids must be a list of strings")
    poly_ids = tuple(str(x).strip() for x in inst_raw if str(x).strip())

    # **Package-source-confirmed:** ``PolymarketInstrumentProvider.load_ids_async`` no-ops when
    # ``instrument_ids`` is empty. Live + empty ids ⇒ implicit dynamic instrument universe.
    if mode == "live" and not poly_ids:
        pdi = True

    if pdi and mode != "live":
        raise ValueError(
            f"{p}: polymarket_dynamic_instruments is only meaningful when execution_mode is live",
        )

    dyn_max = int(raw.get("polymarket_dynamic_max_activations", 32))
    if dyn_max < 0:
        raise ValueError(f"{p}: polymarket_dynamic_max_activations must be >= 0")

    gamma_url = str(
        raw.get("polymarket_gamma_base_url", "https://gamma-api.polymarket.com"),
    ).rstrip("/")
    gamma_timeout = float(raw.get("polymarket_gamma_http_timeout_seconds", 15.0))
    if gamma_timeout <= 0:
        raise ValueError(f"{p}: polymarket_gamma_http_timeout_seconds must be positive")

    warmup_max = int(raw.get("polymarket_startup_token_warmup_max", 32))
    if warmup_max < 0:
        raise ValueError(f"{p}: polymarket_startup_token_warmup_max must be >= 0")

    if "exec_position_check_interval_seconds" not in raw:
        exec_pos_check: float | None = 45.0 if mode == "live" else None
    else:
        ep_raw = raw["exec_position_check_interval_seconds"]
        if ep_raw is None:
            exec_pos_check = None
        else:
            exec_pos_check = float(ep_raw)
            if exec_pos_check <= 0:
                raise ValueError(
                    f"{p}: exec_position_check_interval_seconds must be positive or null (off)",
                )

    if "exec_open_check_interval_seconds" not in raw:
        exec_open_check: float | None = 20.0 if mode == "live" else None
    else:
        eo_raw = raw["exec_open_check_interval_seconds"]
        if eo_raw is None:
            exec_open_check = None
        else:
            exec_open_check = float(eo_raw)
            if exec_open_check <= 0:
                raise ValueError(
                    f"{p}: exec_open_check_interval_seconds must be positive or null (off)",
                )

    if "polymarket_wallet_position_warmup_max" not in raw:
        wallet_pos_warmup = 128 if mode == "live" else 0
    else:
        wallet_pos_warmup = int(raw["polymarket_wallet_position_warmup_max"])
        if wallet_pos_warmup < 0:
            raise ValueError(f"{p}: polymarket_wallet_position_warmup_max must be >= 0")
        if wallet_pos_warmup > 0 and mode != "live":
            raise ValueError(
                f"{p}: polymarket_wallet_position_warmup_max is only valid when execution_mode is live",
            )

    token_map = _polymarket_token_instrument_map(poly_ids, path=p) if poly_ids else ()

    ingest_mode = str(raw.get("guru_ingest_mode", "poll_only")).lower().strip()
    if ingest_mode not in ("poll_only", "rtds_shadow", "rtds_primary"):
        raise ValueError(
            f"{p}: guru_ingest_mode must be poll_only, rtds_shadow, or rtds_primary",
        )
    ingest_phase = str(raw.get("guru_ingest_phase", "0")).strip()
    rtds_url = str(raw.get("guru_rtds_url", "wss://ws-live-data.polymarket.com")).strip()
    rtds_live = float(raw.get("guru_rtds_liveness_timeout_seconds", 120.0))
    if rtds_live <= 0:
        raise ValueError(f"{p}: guru_rtds_liveness_timeout_seconds must be positive")
    rtds_r0 = float(raw.get("guru_rtds_reconnect_retry_initial_seconds", 1.0))
    rtds_rmax = float(raw.get("guru_rtds_reconnect_retry_max_seconds", 60.0))
    if rtds_r0 <= 0 or rtds_rmax < rtds_r0:
        raise ValueError(f"{p}: invalid RTDS reconnect backoff seconds")
    rtds_ping = float(raw.get("guru_rtds_ping_interval_seconds", 5.0))
    if rtds_ping <= 0:
        raise ValueError(f"{p}: guru_rtds_ping_interval_seconds must be positive")
    poll_fb_en = bool(raw.get("guru_poll_fallback_enabled", True))
    poll_fb_iv = raw.get("guru_poll_fallback_interval_seconds")
    poll_fb_interval = float(poll_fb_iv) if poll_fb_iv is not None else None
    if poll_fb_interval is not None and poll_fb_interval <= 0:
        raise ValueError(f"{p}: guru_poll_fallback_interval_seconds must be positive or omitted")
    gap_fill_en = bool(raw.get("guru_gap_fill_enabled", True))
    gap_lb = float(raw.get("guru_gap_fill_lookback_seconds", 60.0))
    if gap_lb < 0:
        raise ValueError(f"{p}: guru_gap_fill_lookback_seconds must be >= 0")
    proxy_val = bool(raw.get("guru_proxy_wallet_validation_required", False))
    drain_ms = int(raw.get("guru_stream_queue_drain_interval_ms", 50))
    if drain_ms < 10:
        raise ValueError(f"{p}: guru_stream_queue_drain_interval_ms must be >= 10")

    for _obsolete_exec in (
        "venue_size_alignment_mode",
        "execution_venue_normalize_enabled",
    ):
        if _obsolete_exec in raw:
            raise ValueError(
                f"{p}: obsolete key {_obsolete_exec} — removed; "
                "Order-size policy is risk YAML only; execution quantizes to instrument tick/step internally.",
            )
    ex_guard = bool(raw.get("execution_entry_guard_enabled", False))
    ex_slip_ticks = int(raw.get("execution_max_entry_slippage_ticks", 0))
    ex_depth = bool(raw.get("execution_book_depth_clip_enabled", False))
    ex_cap = float(raw.get("execution_book_depth_utilization_cap", 1.0))
    ex_rest = bool(raw.get("execution_book_rest_snapshot_enabled", False))
    ex_strict = bool(raw.get("execution_book_strict", False))
    ex_to_en = bool(raw.get("execution_limit_timeout_enabled", False))
    ex_to_s = float(raw.get("execution_limit_timeout_seconds", 30.0))

    if ex_slip_ticks < 0:
        raise ValueError(f"{p}: execution_max_entry_slippage_ticks must be >= 0")
    if ex_guard and ex_slip_ticks <= 0:
        raise ValueError(
            f"{p}: execution_entry_guard_enabled requires execution_max_entry_slippage_ticks > 0",
        )
    if ex_depth and not (0.0 < ex_cap <= 1.0 + 1e-9):
        raise ValueError(
            f"{p}: execution_book_depth_utilization_cap must be in (0, 1] when depth clip enabled",
        )
    if ex_to_en and ex_to_s <= 0:
        raise ValueError(
            f"{p}: execution_limit_timeout_seconds must be positive when timeout enabled",
        )

    reporting_en = bool(raw.get("reporting_enabled", False))
    reporting_base = str(raw.get("reporting_base_dir", "var/reporting/runs")).strip()
    if not reporting_base or ".." in reporting_base:
        raise ValueError(f"{p}: reporting_base_dir must be non-empty without '..'")
    r_max_q = int(raw.get("reporting_sink_max_queue", 50_000))
    if r_max_q < 100:
        raise ValueError(f"{p}: reporting_sink_max_queue must be >= 100")
    r_batch = int(raw.get("reporting_sink_batch_size", 128))
    if r_batch < 1:
        raise ValueError(f"{p}: reporting_sink_batch_size must be >= 1")

    r_cap_obs = bool(raw.get("reporting_capital_observability_enabled", True))
    r_cap_period = float(raw.get("reporting_capital_snapshot_period_seconds", 300.0))
    if r_cap_period < 0:
        raise ValueError(f"{p}: reporting_capital_snapshot_period_seconds must be >= 0")

    su_to = float(raw.get("startup_readiness_timeout_seconds", 120.0))
    if su_to <= 0:
        raise ValueError(f"{p}: startup_readiness_timeout_seconds must be positive")
    su_strict_sh = bool(raw.get("startup_strict_shadow", False))
    su_deg = bool(raw.get("startup_allow_degraded_live", False))
    su_nrb = str(raw.get("startup_not_ready_behavior", "exit")).strip().lower()
    if su_nrb not in ("exit", "no_trade"):
        raise ValueError(
            f"{p}: startup_not_ready_behavior must be exit or no_trade (got {su_nrb!r})",
        )
    if mode != "live" and su_deg:
        raise ValueError(
            f"{p}: startup_allow_degraded_live is only valid when execution_mode is live",
        )

    sd_en = bool(raw.get("shutdown_drain_enabled", True))
    sd_to = float(raw.get("shutdown_drain_timeout_seconds", 30.0))
    if sd_to <= 0:
        raise ValueError(f"{p}: shutdown_drain_timeout_seconds must be positive")
    sd_override = bool(raw.get("shutdown_drain_override", False))

    use_data_api_pos = bool(raw.get("polymarket_use_data_api_for_positions", False))
    if "live_exec_open_check_open_only" in raw:
        oc_raw = raw["live_exec_open_check_open_only"]
        if oc_raw is None:
            live_oc_open_only: bool | None = None
        else:
            live_oc_open_only = bool(oc_raw)
    else:
        live_oc_open_only = None

    # -- Wallet sync -------------------------------------------------------
    ws_enabled_raw = raw.get("wallet_sync_enabled")
    if ws_enabled_raw is None:
        ws_enabled = mode == "live"
    else:
        ws_enabled = bool(ws_enabled_raw)
    if ws_enabled and mode != "live":
        raise ValueError(
            f"{p}: wallet_sync_enabled requires execution_mode=live",
        )
    ws_poll = float(raw.get("wallet_sync_poll_interval_seconds", 15.0))
    if ws_poll < 5.0:
        raise ValueError(
            f"{p}: wallet_sync_poll_interval_seconds must be >= 5.0",
        )
    ws_deadline = float(raw.get("wallet_sync_startup_deadline_seconds", 120.0))
    if ws_deadline < 30.0:
        raise ValueError(
            f"{p}: wallet_sync_startup_deadline_seconds must be >= 30.0",
        )
    ws_retries = int(raw.get("wallet_sync_per_instrument_max_retries", 3))
    if ws_retries < 1:
        raise ValueError(
            f"{p}: wallet_sync_per_instrument_max_retries must be >= 1",
        )

    # -- VenueState (Tier A) -----------------------------------------------
    vs_ttl = float(raw.get("venue_state_ttl_seconds", 30.0))
    vs_cash_poll = float(raw.get("venue_state_cash_poll_interval_seconds", 10.0))
    vs_ref_ms = int(raw.get("venue_state_refresh_force_max_ms", 500))
    if vs_ttl <= 0.0:
        raise ValueError(f"{p}: venue_state_ttl_seconds must be > 0")
    if vs_cash_poll < 3.0:
        raise ValueError(
            f"{p}: venue_state_cash_poll_interval_seconds must be >= 3.0",
        )
    if vs_ref_ms < 1:
        raise ValueError(f"{p}: venue_state_refresh_force_max_ms must be >= 1")

    return RuntimeSettings(
        trader_id=tid,
        execution_mode=mode,
        guru_poll_interval_seconds=poll,
        data_api_base_url=api,
        guru_dedup_state_path=dedup_s,
        guru_state_path=state_s,
        guru_activity_limit=activity_limit,
        guru_startup_backfill_seconds=backfill,
        guru_max_activity_pages_per_poll=max_pages,
        logging_level=log_level,
        clob_host=clob,
        chain_id=chain_id,
        polymarket_instrument_ids=poly_ids,
        polymarket_token_to_instrument=token_map,
        polymarket_dynamic_instruments=pdi,
        polymarket_dynamic_max_activations=dyn_max,
        polymarket_gamma_base_url=gamma_url,
        polymarket_gamma_http_timeout_seconds=gamma_timeout,
        polymarket_startup_token_warmup_max=warmup_max,
        exec_position_check_interval_secs=exec_pos_check,
        exec_open_check_interval_secs=exec_open_check,
        polymarket_wallet_position_warmup_max=wallet_pos_warmup,
        guru_ingest_mode=ingest_mode,
        guru_ingest_phase=ingest_phase,
        guru_rtds_url=rtds_url,
        guru_rtds_liveness_timeout_seconds=rtds_live,
        guru_rtds_reconnect_retry_initial_seconds=rtds_r0,
        guru_rtds_reconnect_retry_max_seconds=rtds_rmax,
        guru_rtds_ping_interval_seconds=rtds_ping,
        guru_poll_fallback_enabled=poll_fb_en,
        guru_poll_fallback_interval_seconds=poll_fb_interval,
        guru_gap_fill_enabled=gap_fill_en,
        guru_gap_fill_lookback_seconds=gap_lb,
        guru_proxy_wallet_validation_required=proxy_val,
        guru_stream_queue_drain_interval_ms=drain_ms,
        execution_entry_guard_enabled=ex_guard,
        execution_max_entry_slippage_ticks=ex_slip_ticks,
        execution_book_depth_clip_enabled=ex_depth,
        execution_book_depth_utilization_cap=ex_cap,
        execution_book_rest_snapshot_enabled=ex_rest,
        execution_book_strict=ex_strict,
        execution_limit_timeout_enabled=ex_to_en,
        execution_limit_timeout_seconds=ex_to_s,
        reporting_enabled=reporting_en,
        reporting_base_dir=reporting_base,
        reporting_sink_max_queue=r_max_q,
        reporting_sink_batch_size=r_batch,
        reporting_capital_observability_enabled=r_cap_obs,
        reporting_capital_snapshot_period_seconds=r_cap_period,
        startup_readiness_timeout_seconds=su_to,
        startup_strict_shadow=su_strict_sh,
        startup_allow_degraded_live=su_deg,
        startup_not_ready_behavior=su_nrb,
        shutdown_drain_enabled=sd_en,
        shutdown_drain_timeout_seconds=sd_to,
        shutdown_drain_override=sd_override,
        polymarket_use_data_api_for_positions=use_data_api_pos,
        live_exec_open_check_open_only=live_oc_open_only,
        wallet_sync_enabled=ws_enabled,
        wallet_sync_poll_interval_seconds=ws_poll,
        wallet_sync_startup_deadline_seconds=ws_deadline,
        wallet_sync_per_instrument_max_retries=ws_retries,
        venue_state_ttl_seconds=vs_ttl,
        venue_state_cash_poll_interval_seconds=vs_cash_poll,
        venue_state_refresh_force_max_ms=vs_ref_ms,
    )
