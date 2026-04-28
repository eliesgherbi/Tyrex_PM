from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import GuruTradeSignal
from tyrex_pm.runtime.config import load_app_config, parse_app_config
from tyrex_pm.signals.guru_copy_signal import to_copy_signal
from tyrex_pm.strategies.guru_follow import filters
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy


def test_significance_skip_reason() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    strat = deepcopy(app.raw["strategy"])
    strat["filters"]["significance_min_notional_usd"] = "10"
    app2 = parse_app_config(risk=app.raw["risk"], strategy=strat, runtime=app.raw["runtime"])
    sig = GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        price=Decimal("0.7"),
        notional_usd=Decimal("7"),
        dedup_key="s1",
        ts_venue=datetime.now(timezone.utc),
        conviction_score=None,
    )
    fr = filters.apply_filters(to_copy_signal(sig), app2.strategy)
    assert not fr.ok
    from tyrex_pm.core import reason_codes as rc

    assert fr.reason == rc.GURU_SIGNIFICANCE_REJECT


def test_conviction_requires_score_when_enabled() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    strat = deepcopy(app.raw["strategy"])
    strat["sizing"]["conviction"] = {
        "enabled": True,
        "score_min": "0",
        "score_max": "1",
        "min_multiplier": "0.5",
        "max_multiplier": "2.0",
    }
    app2 = parse_app_config(risk=app.raw["risk"], strategy=strat, runtime=app.raw["runtime"])
    sig = GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        price=Decimal("0.5"),
        notional_usd=Decimal("10"),
        dedup_key="c1",
        ts_venue=datetime.now(timezone.utc),
        conviction_score=None,
    )
    fr = filters.apply_filters(to_copy_signal(sig), app2.strategy)
    assert not fr.ok
    from tyrex_pm.core import reason_codes as rc

    assert fr.reason == rc.GURU_LOW_CONVICTION


def test_conviction_scales_enter_size() -> None:
    """Conviction multiplier only applies when ``static_enabled`` is off.

    The default ``config/strategies/guru_follow.yaml`` ships with
    ``sizing.static_enabled: true`` (BUY entries use a fixed USD notional and
    ignore both ``copy_scale`` and conviction). To exercise the proportional
    path that conviction scales, the test must explicitly opt out of static
    sizing in addition to enabling conviction.
    """

    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    strat = deepcopy(app.raw["strategy"])
    strat["sizing"]["static_enabled"] = False
    strat["sizing"]["conviction"] = {
        "enabled": True,
        "score_min": "0",
        "score_max": "1",
        "min_multiplier": "1",
        "max_multiplier": "2",
    }
    app2 = parse_app_config(risk=app.raw["risk"], strategy=strat, runtime=app.raw["runtime"])
    gf = GuruFollowStrategy(app2.strategy)
    sig = GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        price=Decimal("0.5"),
        notional_usd=Decimal("5"),
        dedup_key="c2",
        ts_venue=datetime.now(timezone.utc),
        conviction_score=Decimal("1"),
    )
    intents, skip, meta = gf.on_guru_signal(to_copy_signal(sig), {})
    assert skip is None
    assert len(intents) == 1
    assert intents[0].size == Decimal("20")
    assert meta is not None
    assert meta["sizing_mode"] == "proportional"


def test_exit_proportional_caps_to_holdings() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    gf = GuruFollowStrategy(app.strategy)
    sig = GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TokenId("1234567890"),
        side=Side.SELL,
        size=Decimal("100"),
        price=Decimal("0.5"),
        notional_usd=Decimal("50"),
        dedup_key="e1",
        ts_venue=datetime.now(timezone.utc),
    )
    holdings = {TokenId("1234567890"): Decimal("3")}
    intents, skip, meta = gf.on_guru_signal(to_copy_signal(sig), holdings)
    assert skip is None
    assert len(intents) == 1
    assert meta is None
    from tyrex_pm.core.models import ExitIntent

    assert isinstance(intents[0], ExitIntent)
    assert intents[0].size == Decimal("3")


def test_static_buy_uses_fixed_notional_and_meta() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    strat = deepcopy(app.raw["strategy"])
    strat["sizing"]["static_enabled"] = True
    strat["sizing"]["static_amount_usd"] = "100"
    strat["sizing"]["copy_scale"] = "0.01"
    strat["sizing"]["conviction"] = {
        "enabled": True,
        "score_min": "0",
        "score_max": "1",
        "min_multiplier": "2",
        "max_multiplier": "2",
    }
    app2 = parse_app_config(risk=app.raw["risk"], strategy=strat, runtime=app.raw["runtime"])
    gf = GuruFollowStrategy(app2.strategy)
    sig = GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("99999"),
        price=Decimal("0.5"),
        notional_usd=Decimal("99999"),
        dedup_key="st1",
        ts_venue=datetime.now(timezone.utc),
        conviction_score=Decimal("1"),
    )
    intents, skip, meta = gf.on_guru_signal(to_copy_signal(sig), {})
    assert skip is None
    assert meta == {"sizing_mode": "static"}
    assert len(intents) == 1
    assert intents[0].size == Decimal("200")


def test_proportional_respects_copy_scale_when_static_off() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    strat = deepcopy(app.raw["strategy"])
    strat["sizing"]["static_enabled"] = False
    strat["sizing"]["copy_scale"] = "0.5"
    app2 = parse_app_config(risk=app.raw["risk"], strategy=strat, runtime=app.raw["runtime"])
    gf = GuruFollowStrategy(app2.strategy)
    sig = GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        price=Decimal("0.5"),
        notional_usd=Decimal("5"),
        dedup_key="pr1",
        ts_venue=datetime.now(timezone.utc),
        conviction_score=None,
    )
    intents, skip, meta = gf.on_guru_signal(to_copy_signal(sig), {})
    assert skip is None
    assert meta == {"sizing_mode": "proportional"}
    assert intents[0].size == Decimal("5")
