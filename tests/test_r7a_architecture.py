"""R7A architecture: mutation separation, no old/, LIVE still denied, secrets."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import tyrex_pm
from tyrex_pm.core.ids import MarketId, StrategyId, TokenId, new_correlation_id
from tyrex_pm.core.intents import EnterIntent, new_intent_id
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.execution.polymarket.signing_dry import validate_dry_signing_vector
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment, FreshnessReason, TimestampBasis
from tyrex_pm.risk.context import BookReadiness, RiskConfigView, RiskContext
from tyrex_pm.risk.dedup import IntentDedupRegistry
from tyrex_pm.risk.engine import RiskEngine
from tyrex_pm.risk.reasons import RiskReason


ENV_HASH = "27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772"
ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
PKG = Path(tyrex_pm.__file__).resolve().parent


def test_env_hash_unchanged() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    digest = hashlib.sha256(env.read_bytes()).hexdigest().upper()
    assert digest == ENV_HASH


def test_live_tiny_still_denied() -> None:
    ts = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    mid = MarketId("m1")
    yes, no = make_binary_instruments(
        market_id=mid, yes_token=TokenId("y"), no_token=TokenId("n")
    )
    market = BinaryMarket(
        market_id=mid,
        condition_id="m1",
        question="q",
        yes=yes,
        no=no,
        status=MarketStatus.ACTIVE,
    )
    fresh = FreshnessAssessment(
        is_fresh=True,
        age_ms=1,
        threshold_ms=1000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH,
        observed_at=ts,
    )
    q = ExecutableQuote(
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.52"),
        mid=Decimal("0.5"),
        spread=Decimal("0.04"),
        bid_size_at_touch=Decimal("100"),
        ask_size_at_touch=Decimal("100"),
    )
    snap = DecisionSnapshot(
        market=market,
        yes_book=None,
        no_book=None,
        yes_quote=q,
        no_quote=q,
        reference=None,
        yes_freshness=fresh,
        no_freshness=fresh,
        reference_freshness=fresh,
        observed_at=ts,
        correlation_id=new_correlation_id(),
    )
    ctx = RiskContext(
        mode=RuntimeMode.LIVE_TINY,
        now=ts,
        market=market,
        snapshot=snap,
        yes_quote=q,
        no_quote=q,
        yes_book=BookReadiness(
            initialized=True, recovery_required=False, tick_size=Decimal("0.01")
        ),
        no_book=BookReadiness(
            initialized=True, recovery_required=False, tick_size=Decimal("0.01")
        ),
        risk_config=RiskConfigView(
            max_notional=Decimal("10"),
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.2"),
            min_liquidity_notional=Decimal("1"),
            no_entry_before_close=timedelta(0),
            kill_switch_active=False,
            config_fingerprint="fp",
        ),
        dedup=IntentDedupRegistry(lifetime=timedelta(hours=1)),
        exposure_available=False,
    )
    intent = EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("reference_momentum"),
        instrument_id=yes.instrument_id,
        market_id=mid,
        created_at=ts,
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="TEST",
        target_notional=Decimal("5"),
    )
    d = RiskEngine().evaluate(intent, ctx)
    assert not d.approved
    assert RiskReason.LIVE_NOT_SUPPORTED in d.reason_codes


def test_no_old_imports_in_execution_polymarket() -> None:
    pkg = PKG / "execution" / "polymarket"
    for path in pkg.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from old" not in text
        assert "import old" not in text
        assert "nautilus" not in text.lower()


def test_live_preflight_has_no_mutation_transport() -> None:
    text = (PKG / "runtime" / "live_preflight.py").read_text(encoding="utf-8")
    assert "SdkMutationTransport" not in text
    assert "mutation_transport" not in text
    assert "enable-mutations" not in text
    # Must assert absence of mutation methods on the client (guard present).
    assert 'hasattr(client, name)' in text
    assert "mutations_enabled" in text or "MUTATIONS_DISABLED" in text


def test_cli_has_no_enable_mutations_shortcut() -> None:
    text = (PKG / "application" / "cli.py").read_text(encoding="utf-8")
    assert "--enable-mutations" not in text


def test_dry_sign_vector_has_no_secrets() -> None:
    report = validate_dry_signing_vector()
    assert report["ok"] is True
    assert report["would_send"] is False
    assert report["real_private_key_used"] is False
    assert "0x11" not in str(report)


def test_live_once_still_refuses() -> None:
    text = (PKG / "application" / "cli.py").read_text(encoding="utf-8")
    assert "live-once" in text
    assert "R7B BLOCKED" in text
    assert "not enabled in this build" in text or "Mutations remain disabled" in text


def test_r7a1_prepare_never_issues_approval_by_default() -> None:
    text = (PKG / "runtime" / "r7a_proposal.py").read_text(encoding="utf-8")
    assert "issue_approval: bool = False" in text
    assert "R7A.1: never issue approval artifact" in text or "issue_approval" in text
