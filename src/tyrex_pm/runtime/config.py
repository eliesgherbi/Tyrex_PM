from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from tyrex_pm.core.enums import ExecutionMode, OrderStyle
from tyrex_pm.core.errors import ConfigError


STRATEGY_KIND_GURU_FOLLOW = "guru_follow"
STRATEGY_KIND_SELL_TEST = "sell_test"
_VALID_STRATEGY_KINDS = (STRATEGY_KIND_GURU_FOLLOW, STRATEGY_KIND_SELL_TEST)


SELL_TEST_PRICING_FIXED = "fixed"
SELL_TEST_PRICING_AUTO = "auto"
_VALID_SELL_TEST_PRICING_MODES = (SELL_TEST_PRICING_FIXED, SELL_TEST_PRICING_AUTO)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(p: Path) -> dict[str, Any]:
    if not p.is_file():
        raise ConfigError(f"missing config: {p}")
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class GuruConfig:
    wallet: str
    data_api_poll_interval_s: float
    data_api_limit: int
    data_api_max_pages_per_poll: int


@dataclass(frozen=True)
class FiltersConfig:
    token_allowlist: frozenset[str]
    min_notional_usd: Decimal
    significance_min_notional_usd: Decimal
    min_conviction_score: Decimal
    exclude_untradeable_markets: bool


@dataclass(frozen=True)
class ConvictionConfig:
    enabled: bool
    score_min: Decimal
    score_max: Decimal
    min_multiplier: Decimal
    max_multiplier: Decimal


@dataclass(frozen=True)
class SizingConfig:
    copy_scale: Decimal
    conviction: ConvictionConfig
    #: BUY entry: fixed USD notional (ignores copy_scale and conviction when True).
    static_enabled: bool
    static_amount_usd: Decimal


@dataclass(frozen=True)
class ExitsConfig:
    dust_notional_usd: Decimal
    sell_mode: str  # proportional_to_guru | full_bot_position
    #: Validation: after a copied guru BUY, schedule a demo SELL (see ``scheduled_exit_demo``).
    demo_forced_exit_enabled: bool = False
    demo_forced_exit_delay_s: float = 3.0


@dataclass(frozen=True)
class StrategyConfig:
    guru: GuruConfig
    filters: FiltersConfig
    sizing: SizingConfig
    exits: ExitsConfig


@dataclass(frozen=True)
class SellTestBuyConfig:
    """One BUY leg the sell_test strategy should emit at startup.

    ``pricing_mode``:

    * ``fixed`` (default, backward compatible) — submit at ``limit_price`` exactly.
      The order rests on the book unless ``limit_price`` already crosses the ask.
    * ``auto`` — at run time, fetch the venue order book for ``token_id`` and pick
      ``best_ask + aggression_ticks * tick_size`` so the BUY is marketable. When
      ``limit_price`` is set under ``auto`` it is used as a *fallback* if the book
      lookup fails (e.g. transient venue error). ``max_price`` is an optional
      upper guardrail: if the resolved aggressive price would exceed it, the
      strategy falls back instead of paying through the cap.
    """

    enabled: bool
    notional_usd: Decimal
    #: ``fixed``: required and used verbatim. ``auto``: optional fallback.
    limit_price: Decimal | None
    order_style: OrderStyle
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 1
    max_price: Decimal | None = None


@dataclass(frozen=True)
class SellTestSellConfig:
    """SELL leg fired ``delay_s`` after the BUY becomes sellable inventory.

    ``pricing_mode``:

    * ``fixed`` (default) — submit at ``limit_price`` if set, otherwise re-use the
      BUY's limit price (mirrors the guru-follow demo exit; deliberately simple
      and observable).
    * ``auto`` — at SELL emission time, fetch the venue book and pick
      ``best_bid - aggression_ticks * tick_size`` so the SELL is marketable.
      ``limit_price`` is the fallback when the lookup fails. ``min_price`` is an
      optional lower guardrail: the strategy refuses to dump below it and falls
      back to ``limit_price`` instead.
    """

    enabled: bool
    delay_s: float
    order_style: OrderStyle
    limit_price: Decimal | None
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 1
    min_price: Decimal | None = None


@dataclass(frozen=True)
class SellTestStrategyConfig:
    """Standalone strategy used to validate the V2 SELL / exit path end-to-end.

    See ``Docs/Implementation/sell_feature/`` for design context. This strategy
    does not poll guru activity; it emits one BUY for ``token_id`` then schedules
    one SELL after sellable inventory is observed (live) or after the synthetic
    fill (shadow). Intended for debugging — not production alpha.
    """

    enabled: bool
    token_id: str
    buy: SellTestBuyConfig
    sell: SellTestSellConfig
    run_once: bool


@dataclass(frozen=True)
class NotionalConfig:
    min_usd: Decimal
    max_usd: Decimal
    #: When order notional exceeds max_usd: clip size to max (cap) or reject (deny).
    max_policy: str


@dataclass(frozen=True)
class DeploymentConfig:
    token_cap_usd: Decimal
    portfolio_cap_usd: Decimal


@dataclass(frozen=True)
class CapitalConfig:
    enabled: bool
    max_wallet_age_s: int


@dataclass(frozen=True)
class VenueMinSizeConfig:
    """Pre-submit guard: clip-side and notional-cap math can produce a final ``size`` below
    the venue's hard minimum (Polymarket commonly = 5 shares). Without this gate the order
    reaches the venue and is rejected with ``Size (X) lower than the minimum: 5``.

    ``policy=deny``  → block locally with reason ``below_venue_min_size`` (no submit).
    ``policy=bump``  → raise ``size`` to ``default_min_size`` then re-validate deployment +
    capital with the bumped size; only submit if both still pass.
    """

    enabled: bool
    policy: str  # "deny" | "bump"
    default_min_size: Decimal


@dataclass(frozen=True)
class InventoryConfig:
    sell_requires_venue_position: bool


@dataclass(frozen=True)
class KillSwitchConfig:
    enabled: bool


@dataclass(frozen=True)
class ConcurrencyConfig:
    max_orders_in_flight: int


@dataclass(frozen=True)
class ReadinessConfig:
    require_wallet_sync: bool
    max_wallet_age_s_live: int
    require_heartbeat_live: bool
    require_user_ws_live: bool


@dataclass(frozen=True)
class RiskConfig:
    notional: NotionalConfig
    deployment: DeploymentConfig
    capital: CapitalConfig
    inventory: InventoryConfig
    kill_switch: KillSwitchConfig
    concurrency: ConcurrencyConfig
    readiness: ReadinessConfig
    venue_min_size: VenueMinSizeConfig


@dataclass(frozen=True)
class ReportingConfig:
    enabled: bool
    runs_dir: str


@dataclass(frozen=True)
class ShadowBootstrapConfig:
    """Seed WalletStore for shadow runs when venue sync is not wired (not secrets)."""

    usdc_balance: Decimal
    usdc_allowance: Decimal


@dataclass(frozen=True)
class RuntimeConfig:
    execution_mode: ExecutionMode
    reporting: ReportingConfig
    reconcile_interval_s: int
    #: Provisional repair window (s): age below this is non-blocking ``provisional_pending_venue``.
    submit_grace_s: float
    #: Provisional age (s) past which absent rows auto-resolve to ``UNKNOWN_TERMINAL`` (non-blocking)
    #: when WS is fresh and no venue restart suspected.
    provisional_unknown_terminal_timeout_s: float
    #: Back-compat alias kept for old configs / scripts; mirrors ``provisional_unknown_terminal_timeout_s``.
    venue_confirm_provisional_timeout_s: float
    #: Adoption window (s) for venue-truth mirror race: a venue order id that we don't yet track
    #: locally is matched against recent no-vid provisional rows submitted within this window.
    adoption_grace_s: float
    log_level: str
    shadow_bootstrap: ShadowBootstrapConfig | None


@dataclass(frozen=True)
class AppConfig:
    strategy: StrategyConfig
    risk: RiskConfig
    runtime: RuntimeConfig
    raw: dict[str, Any]
    #: Populated only when the loaded strategy YAML declares ``kind: sell_test``.
    #: Mutually exclusive with normal guru-follow operation; ``strategy`` then
    #: holds default placeholder values so risk gates and other code paths that
    #: read ``app.strategy.*`` remain stable.
    sell_test: SellTestStrategyConfig | None = None


def _dec(d: dict[str, Any], key: str, default: str = "0") -> Decimal:
    v = d.get(key, default)
    return Decimal(str(v))


def _parse_venue_min_size(d: dict[str, Any]) -> VenueMinSizeConfig:
    """Parse the ``risk.venue_min_size`` block (defaults: enabled, deny, 5 shares).

    Polymarket's hard floor is 5 shares regardless of token. ``5`` is therefore the safe
    default when the operator omits the block entirely. ``policy`` accepts ``deny|bump``
    and falls back to ``deny`` for any other string (fail-closed).
    """
    raw_policy = str(d.get("policy", "deny") or "deny").lower().strip()
    policy = raw_policy if raw_policy in ("deny", "bump") else "deny"
    return VenueMinSizeConfig(
        enabled=bool(d.get("enabled", True)),
        policy=policy,
        default_min_size=_dec(d, "default_min_size", "5"),
    )


def _parse_conviction(d: dict[str, Any]) -> ConvictionConfig:
    if not d:
        return ConvictionConfig(
            enabled=False,
            score_min=Decimal("0"),
            score_max=Decimal("1"),
            min_multiplier=Decimal("1"),
            max_multiplier=Decimal("1"),
        )
    return ConvictionConfig(
        enabled=bool(d.get("enabled", False)),
        score_min=_dec(d, "score_min", "0"),
        score_max=_dec(d, "score_max", "1"),
        min_multiplier=_dec(d, "min_multiplier", "0.5"),
        max_multiplier=_dec(d, "max_multiplier", "2.0"),
    )


def _parse_order_style(raw: Any, default: OrderStyle = OrderStyle.GTC) -> OrderStyle:
    """Coerce a YAML ``order_style`` field to the enum (case-insensitive, fail-closed to default)."""
    if isinstance(raw, OrderStyle):
        return raw
    s = str(raw or "").strip().upper()
    if s in OrderStyle.__members__:
        return OrderStyle[s]
    return default


def _parse_pricing_mode(raw: Any, *, where: str) -> str:
    """Validate a ``pricing_mode`` field; default to ``fixed`` when missing/blank."""
    if raw in (None, ""):
        return SELL_TEST_PRICING_FIXED
    s = str(raw).strip().lower()
    if s not in _VALID_SELL_TEST_PRICING_MODES:
        raise ConfigError(
            f"sell_test {where}.pricing_mode '{raw}' is not supported "
            f"(valid: {', '.join(_VALID_SELL_TEST_PRICING_MODES)})"
        )
    return s


def _parse_sell_test_strategy(strategy: dict[str, Any]) -> SellTestStrategyConfig:
    """Parse a ``kind: sell_test`` strategy YAML.

    Required: ``token_id`` (canonical CLOB outcome token id). When
    ``buy.pricing_mode == "fixed"`` (the default) ``buy.limit_price`` is also
    required. Under ``buy.pricing_mode == "auto"`` ``limit_price`` is optional
    and used as a fallback if the venue book lookup fails.
    """
    enabled = bool(strategy.get("enabled", True))
    token_id = str(strategy.get("token_id", "")).strip()
    if not token_id:
        raise ConfigError("sell_test strategy requires non-empty top-level 'token_id'")
    buy_raw = strategy.get("buy") or {}
    sell_raw = strategy.get("sell") or {}

    buy_enabled = bool(buy_raw.get("enabled", True))
    buy_pricing_mode = _parse_pricing_mode(buy_raw.get("pricing_mode"), where="buy")
    buy_aggression = int(buy_raw.get("aggression_ticks", 1))
    if buy_aggression < 0:
        raise ConfigError("sell_test buy.aggression_ticks must be >= 0")
    buy_price_raw = buy_raw.get("limit_price")
    buy_max_price_raw = buy_raw.get("max_price")
    if buy_enabled and buy_pricing_mode == SELL_TEST_PRICING_FIXED and buy_price_raw in (None, ""):
        raise ConfigError(
            "sell_test buy.enabled with pricing_mode=fixed requires buy.limit_price"
        )
    buy_cfg = SellTestBuyConfig(
        enabled=buy_enabled,
        notional_usd=_dec(buy_raw, "notional_usd", "5"),
        limit_price=Decimal(str(buy_price_raw)) if buy_price_raw not in (None, "") else None,
        order_style=_parse_order_style(buy_raw.get("order_style"), OrderStyle.GTC),
        pricing_mode=buy_pricing_mode,
        aggression_ticks=buy_aggression,
        max_price=Decimal(str(buy_max_price_raw)) if buy_max_price_raw not in (None, "") else None,
    )

    sell_pricing_mode = _parse_pricing_mode(sell_raw.get("pricing_mode"), where="sell")
    sell_aggression = int(sell_raw.get("aggression_ticks", 1))
    if sell_aggression < 0:
        raise ConfigError("sell_test sell.aggression_ticks must be >= 0")
    sell_price_raw = sell_raw.get("limit_price")
    sell_min_price_raw = sell_raw.get("min_price")
    sell_cfg = SellTestSellConfig(
        enabled=bool(sell_raw.get("enabled", True)),
        delay_s=float(sell_raw.get("delay_s", 3)),
        order_style=_parse_order_style(sell_raw.get("order_style"), OrderStyle.GTC),
        limit_price=Decimal(str(sell_price_raw)) if sell_price_raw not in (None, "") else None,
        pricing_mode=sell_pricing_mode,
        aggression_ticks=sell_aggression,
        min_price=Decimal(str(sell_min_price_raw)) if sell_min_price_raw not in (None, "") else None,
    )
    return SellTestStrategyConfig(
        enabled=enabled,
        token_id=token_id,
        buy=buy_cfg,
        sell=sell_cfg,
        run_once=bool(strategy.get("run_once", True)),
    )


def _build_risk_runtime(risk: dict[str, Any], runtime: dict[str, Any]) -> tuple[RiskConfig, RuntimeConfig]:
    n = risk.get("notional") or {}
    d = risk.get("deployment") or {}
    c = risk.get("capital") or {}
    inv = risk.get("inventory") or {}
    ks = risk.get("kill_switch") or {}
    co = risk.get("concurrency") or {}
    rd = risk.get("readiness") or {}
    vms = risk.get("venue_min_size") or {}

    mp_raw = str(n.get("max_policy", "deny") or "deny").lower().strip()
    max_policy = mp_raw if mp_raw in ("cap", "deny") else "deny"
    rsk = RiskConfig(
        notional=NotionalConfig(
            min_usd=_dec(n, "min_usd"),
            max_usd=_dec(n, "max_usd"),
            max_policy=max_policy,
        ),
        deployment=DeploymentConfig(
            token_cap_usd=_dec(d, "token_cap_usd"),
            portfolio_cap_usd=_dec(d, "portfolio_cap_usd"),
        ),
        capital=CapitalConfig(
            enabled=bool(c.get("enabled", True)),
            max_wallet_age_s=int(c.get("max_wallet_age_s", 120)),
        ),
        inventory=InventoryConfig(
            sell_requires_venue_position=bool(inv.get("sell_requires_venue_position", True)),
        ),
        kill_switch=KillSwitchConfig(enabled=bool(ks.get("enabled", False))),
        concurrency=ConcurrencyConfig(max_orders_in_flight=int(co.get("max_orders_in_flight", 8))),
        readiness=ReadinessConfig(
            require_wallet_sync=bool(rd.get("require_wallet_sync", True)),
            max_wallet_age_s_live=int(rd.get("max_wallet_age_s_live", 60)),
            require_heartbeat_live=bool(rd.get("require_heartbeat_live", True)),
            require_user_ws_live=bool(rd.get("require_user_ws_live", True)),
        ),
        venue_min_size=_parse_venue_min_size(vms),
    )

    em = str(runtime.get("execution_mode", "shadow")).lower()
    execution_mode = ExecutionMode.LIVE if em == "live" else ExecutionMode.SHADOW
    rep = runtime.get("reporting") or {}
    sup = runtime.get("supervisors") or {}
    log = runtime.get("logging") or {}
    sb_raw = runtime.get("shadow_bootstrap")
    shadow_boot: ShadowBootstrapConfig | None = None
    if isinstance(sb_raw, dict) and sb_raw:
        shadow_boot = ShadowBootstrapConfig(
            usdc_balance=_dec(sb_raw, "usdc_balance", "0"),
            usdc_allowance=_dec(sb_raw, "usdc_allowance", "0"),
        )

    submit_grace = float(sup.get("submit_grace_s", 15))
    unknown_terminal = sup.get("provisional_unknown_terminal_timeout_s")
    if unknown_terminal is None:
        unknown_terminal = sup.get("venue_confirm_provisional_timeout_s", 60)
    unknown_terminal = float(unknown_terminal)
    adoption_grace = float(sup.get("adoption_grace_s", 5))
    rt = RuntimeConfig(
        execution_mode=execution_mode,
        reporting=ReportingConfig(
            enabled=bool(rep.get("enabled", True)),
            runs_dir=str(rep.get("runs_dir", "var/reporting/runs")),
        ),
        reconcile_interval_s=int(sup.get("reconcile_interval_s", 30)),
        submit_grace_s=submit_grace,
        provisional_unknown_terminal_timeout_s=unknown_terminal,
        venue_confirm_provisional_timeout_s=unknown_terminal,
        adoption_grace_s=adoption_grace,
        log_level=str(log.get("level", "INFO")),
        shadow_bootstrap=shadow_boot,
    )
    return rsk, rt


def _finalize_app_config(
    strat: StrategyConfig,
    risk: dict[str, Any],
    runtime: dict[str, Any],
    strategy_raw: dict[str, Any],
    sell_test: SellTestStrategyConfig | None,
) -> AppConfig:
    rsk, rt = _build_risk_runtime(risk, runtime)
    raw = {"risk": risk, "strategy": strategy_raw, "runtime": runtime}
    return AppConfig(strategy=strat, risk=rsk, runtime=rt, raw=raw, sell_test=sell_test)


def _placeholder_guru_strategy_config() -> StrategyConfig:
    """A neutral StrategyConfig used when the loaded YAML is sell_test-only.

    Risk + pipeline code reads ``app.strategy.*`` (e.g. ``exits.demo_forced_exit_enabled``)
    unconditionally. The sell_test path does not need any of it, so we synthesize a
    no-op StrategyConfig so those code paths stay safe instead of branching on None.
    """
    return StrategyConfig(
        guru=GuruConfig(
            wallet="",
            data_api_poll_interval_s=5.0,
            data_api_limit=50,
            data_api_max_pages_per_poll=5,
        ),
        filters=FiltersConfig(
            token_allowlist=frozenset(),
            min_notional_usd=Decimal("0"),
            significance_min_notional_usd=Decimal("0"),
            min_conviction_score=Decimal("-1000000000"),
            exclude_untradeable_markets=False,
        ),
        sizing=SizingConfig(
            copy_scale=Decimal("1"),
            conviction=ConvictionConfig(
                enabled=False,
                score_min=Decimal("0"),
                score_max=Decimal("1"),
                min_multiplier=Decimal("1"),
                max_multiplier=Decimal("1"),
            ),
            static_enabled=False,
            static_amount_usd=Decimal("0"),
        ),
        exits=ExitsConfig(
            dust_notional_usd=Decimal("0.5"),
            sell_mode="proportional_to_guru",
            demo_forced_exit_enabled=False,
            demo_forced_exit_delay_s=3.0,
        ),
    )


def parse_app_config(*, risk: dict[str, Any], strategy: dict[str, Any], runtime: dict[str, Any]) -> AppConfig:
    kind_raw = str(strategy.get("kind", STRATEGY_KIND_GURU_FOLLOW) or STRATEGY_KIND_GURU_FOLLOW).strip().lower()
    if kind_raw not in _VALID_STRATEGY_KINDS:
        raise ConfigError(
            f"strategy.kind '{kind_raw}' is not supported (valid: {', '.join(_VALID_STRATEGY_KINDS)})"
        )
    sell_test_cfg: SellTestStrategyConfig | None = None
    if kind_raw == STRATEGY_KIND_SELL_TEST:
        sell_test_cfg = _parse_sell_test_strategy(strategy)
        strat = _placeholder_guru_strategy_config()
        return _finalize_app_config(strat, risk, runtime, strategy, sell_test_cfg)

    g = strategy.get("guru") or {}
    f = strategy.get("filters") or {}
    sz = strategy.get("sizing") or {}
    ex = strategy.get("exits") or {}

    allow = f.get("token_allowlist") or []
    if not isinstance(allow, list):
        allow = []
    strat = StrategyConfig(
        guru=GuruConfig(
            wallet=str(g.get("wallet", "")),
            data_api_poll_interval_s=float(g.get("data_api_poll_interval_s", 5)),
            data_api_limit=int(g.get("data_api_limit", 50)),
            data_api_max_pages_per_poll=int(g.get("data_api_max_pages_per_poll", 5)),
        ),
        filters=FiltersConfig(
            token_allowlist=frozenset(str(x) for x in allow),
            min_notional_usd=_dec(f, "min_notional_usd", "0"),
            significance_min_notional_usd=_dec(f, "significance_min_notional_usd", "0"),
            min_conviction_score=_dec(f, "min_conviction_score", "-1000000000"),
            exclude_untradeable_markets=bool(f.get("exclude_untradeable_markets", False)),
        ),
        sizing=SizingConfig(
            copy_scale=_dec(sz, "copy_scale", "1"),
            conviction=_parse_conviction(sz.get("conviction") or {}),
            static_enabled=bool(sz.get("static_enabled", False)),
            static_amount_usd=_dec(sz, "static_amount_usd", "0"),
        ),
        exits=ExitsConfig(
            dust_notional_usd=_dec(ex, "dust_notional_usd", "0.5"),
            sell_mode=str(ex.get("sell_mode", "proportional_to_guru")),
            demo_forced_exit_enabled=bool(ex.get("demo_forced_exit_enabled", False)),
            demo_forced_exit_delay_s=float(ex.get("demo_forced_exit_delay_s", 3)),
        ),
    )

    rsk, rt = _build_risk_runtime(risk, runtime)
    raw = {"risk": risk, "strategy": strategy, "runtime": runtime}
    return AppConfig(strategy=strat, risk=rsk, runtime=rt, raw=raw, sell_test=None)


def _resolve_scenario_path(repo_root: Path, scenario_file: str | None) -> str | None:
    """Bare name `shadow_guru` → `config/scenarios/shadow_guru.yaml` under repo_root."""
    if scenario_file is None:
        return None
    raw = scenario_file.strip()
    p = Path(raw)
    if p.is_absolute():
        return raw
    if "/" in raw or "\\" in raw:
        return str(repo_root / raw)
    name = raw if raw.endswith(".yaml") else f"{raw}.yaml"
    return str(repo_root / "config" / "scenarios" / name)


def load_app_config(
    *,
    repo_root: Path,
    strategy_file: str = "config/strategies/guru_follow.yaml",
    scenario_file: str | None = None,
) -> AppConfig:
    scenario_file = _resolve_scenario_path(repo_root, scenario_file)
    risk_p = repo_root / "config" / "risk" / "default.yaml"
    rt_p = repo_root / "config" / "runtime" / "default.yaml"
    st_p = repo_root / strategy_file if not Path(strategy_file).is_absolute() else Path(strategy_file)

    risk = _load_yaml(risk_p)
    runtime = _load_yaml(rt_p)
    strategy = _load_yaml(st_p)

    if scenario_file:
        sc_p = repo_root / scenario_file if not Path(scenario_file).is_absolute() else Path(scenario_file)
        sc = _load_yaml(sc_p)
        if "risk" in sc:
            risk = _deep_merge(risk, sc["risk"])
        if "runtime" in sc:
            runtime = _deep_merge(runtime, sc["runtime"])
        if "strategy" in sc:
            strategy = _deep_merge(strategy, sc["strategy"])
        # scenario top-level keys
        rt_overlay = {k: sc[k] for k in ("execution_mode", "reporting", "supervisors", "logging") if k in sc}
        if rt_overlay:
            runtime = _deep_merge(runtime, rt_overlay)
        st_overlay = {
            k: sc[k]
            for k in ("kind", "guru", "filters", "sizing", "exits", "buy", "sell", "token_id", "run_once")
            if k in sc
        }
        if st_overlay:
            strategy = _deep_merge(strategy, st_overlay)

    return parse_app_config(risk=risk, strategy=strategy, runtime=runtime)
