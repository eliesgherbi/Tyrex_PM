"""Preflight / N7 classification for official CLOB server-time repair."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import tyrex_pm
from tyrex_pm.adapters.polymarket.clob_server_time import (
    ClobServerTimeResponseError,
    ClobServerTimeResult,
    ClobServerTimeTransportError,
)
from tyrex_pm.adapters.polymarket.sdk_errors import (
    PolymarketErrorCategory,
    classify_polymarket_error,
)
from tyrex_pm.execution.polymarket.preflight_client import PreflightReadClient
from tyrex_pm.execution.polymarket.readiness import ReadinessReason
from tyrex_pm.runtime.live_preflight import run_live_preflight
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_preflight import run_n7_preflight

PKG = Path(tyrex_pm.__file__).resolve().parent


def test_request_rejected_not_transport_disconnected() -> None:
    try:
        from polymarket import RequestRejectedError
    except ImportError:  # pragma: no cover
        pytest.skip("polymarket-client not installed")
    exc = RequestRejectedError(
        "No orderbook exists for the requested token id",
        status=404,
    )
    classified = classify_polymarket_error(exc)
    assert classified.category is PolymarketErrorCategory.REJECTED
    assert classified.category is not PolymarketErrorCategory.TRANSPORT
    assert classified.category is not PolymarketErrorCategory.TRANSIENT


def test_get_server_time_success_records_separated_flags() -> None:
    client = PreflightReadClient()
    fake = ClobServerTimeResult(unix_seconds=1_700_000_123, http_status=200)
    with patch(
        "tyrex_pm.execution.polymarket.preflight_client.fetch_clob_server_time",
        return_value=fake,
    ):
        value = client.get_server_time()
    assert value == 1_700_000_123
    assert client.last_public_clob["transport_reachable"] is True
    assert client.last_public_clob["venue_time_valid"] is True
    assert client.last_public_clob["failure_kind"] is None
    assert any(p.ok and p.path == "/time" for p in client.probes)


def test_transport_failure_marks_unreachable_not_time_valid() -> None:
    client = PreflightReadClient()
    with patch(
        "tyrex_pm.execution.polymarket.preflight_client.fetch_clob_server_time",
        side_effect=ClobServerTimeTransportError("dns:getaddrinfo failed"),
    ):
        assert client.get_server_time() is None
    assert client.last_public_clob["transport_reachable"] is False
    assert client.last_public_clob["venue_time_valid"] is False
    assert client.last_public_clob["failure_kind"] == "transport"


def test_malformed_time_marks_reachable_invalid_time() -> None:
    client = PreflightReadClient()
    with patch(
        "tyrex_pm.execution.polymarket.preflight_client.fetch_clob_server_time",
        side_effect=ClobServerTimeResponseError(
            "invalid_time_response_type:str",
            failure_kind="invalid_time_response",
            venue_reached=True,
            http_status=200,
        ),
    ):
        assert client.get_server_time() is None
    assert client.last_public_clob["transport_reachable"] is True
    assert client.last_public_clob["venue_time_valid"] is False
    assert client.last_public_clob["failure_kind"] == "invalid_time_response"


def test_live_preflight_advances_on_valid_time(tmp_path: Path) -> None:
    fake = ClobServerTimeResult(unix_seconds=1_700_000_123, http_status=200)
    with (
        patch(
            "tyrex_pm.execution.polymarket.preflight_client.fetch_clob_server_time",
            return_value=fake,
        ),
        patch("tyrex_pm.runtime.live_preflight.credentials_present", return_value=False),
    ):
        result = run_live_preflight(
            output_path=tmp_path / "pf.json",
            skip_auth=True,
            user_stream_observe_s=0.0,
        )
    assert result.payload.get("blocker") == "credentials_missing_or_skipped"
    assert result.payload["public_clob"]["venue_time_valid"] is True
    assert result.payload["public_clob"]["transport_reachable"] is True
    assert (
        ReadinessReason.TRANSPORT_DISCONNECTED.value not in result.payload["readiness"]["reasons"]
    )
    assert ReadinessReason.PUBLIC_TIME_INVALID.value not in result.payload["readiness"]["reasons"]


def test_live_preflight_malformed_time_not_unreachable(tmp_path: Path) -> None:
    with patch(
        "tyrex_pm.execution.polymarket.preflight_client.fetch_clob_server_time",
        side_effect=ClobServerTimeResponseError(
            "invalid_time_response_malformed_json",
            failure_kind="invalid_time_response",
            venue_reached=True,
            http_status=200,
        ),
    ):
        result = run_live_preflight(
            output_path=tmp_path / "pf.json",
            skip_auth=True,
            user_stream_observe_s=0.0,
        )
    assert result.payload["blocker"] == "public_time_invalid"
    assert result.payload["public_clob"]["transport_reachable"] is True
    assert ReadinessReason.PUBLIC_TIME_INVALID.value in result.payload["readiness"]["reasons"]
    assert (
        ReadinessReason.TRANSPORT_DISCONNECTED.value not in result.payload["readiness"]["reasons"]
    )
    assert result.payload.get("reconciliation") is None


def test_live_preflight_transport_failure_unreachable(tmp_path: Path) -> None:
    with patch(
        "tyrex_pm.execution.polymarket.preflight_client.fetch_clob_server_time",
        side_effect=ClobServerTimeTransportError("tls:CERTIFICATE_VERIFY_FAILED"),
    ):
        result = run_live_preflight(
            output_path=tmp_path / "pf.json",
            skip_auth=True,
            user_stream_observe_s=0.0,
        )
    assert result.payload["blocker"] == "public_clob_unreachable"
    assert ReadinessReason.TRANSPORT_DISCONNECTED.value in result.payload["readiness"]["reasons"]


def _fake_preflight_result(
    *,
    ok: bool,
    blocker: str | None,
    public_clob: dict[str, Any],
    reconciliation: dict[str, Any] | None,
    classifications_seed: str = "clean",
) -> MagicMock:
    payload: dict[str, Any] = {
        "blocker": blocker,
        "public_clob": public_clob,
        "cloudflare_blocked": False,
        "credentials_present": True,
        "identity_mapping": {
            "private_key_derives_valid_signer": True,
            "funder_present": True,
            "signer_equals_funder": True,
            "signature_type_present": True,
            "historical_and_current_identity_mapping_match": True,
        },
        "balance_evidence": {"retrieved": True},
        "user_stream": {"attempted": False},
        "reconciliation": reconciliation,
        "classifications_seed": classifications_seed,
    }
    m = MagicMock()
    m.ok = ok
    m.payload = payload
    return m


def test_n7_vpn_hint_only_on_transport(tmp_path: Path) -> None:
    cfg = tmp_path / "n7.yaml"
    # Minimal sealed config path is exercised via mock of load + run_live_preflight
    transport = _fake_preflight_result(
        ok=False,
        blocker="public_clob_unreachable",
        public_clob={
            "transport_reachable": False,
            "venue_time_valid": False,
            "failure_kind": "transport",
        },
        reconciliation=None,
    )
    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", return_value=transport),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        result = run_n7_preflight(
            out_dir=tmp_path / "out",
            config_path=cfg,
            repo=tmp_path,
            require_clean_worktree=False,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.CONNECTIVITY_UNAVAILABLE.value in result.abort_codes
    assert "hint_check_vpn_or_dns" in result.abort_codes
    assert N7AbortCode.PREFLIGHT_RECON_DISAGREEMENT.value not in result.abort_codes


def test_n7_no_vpn_hint_on_invalid_time(tmp_path: Path) -> None:
    bad_time = _fake_preflight_result(
        ok=False,
        blocker="public_time_invalid",
        public_clob={
            "transport_reachable": True,
            "venue_time_valid": False,
            "failure_kind": "invalid_time_response",
        },
        reconciliation=None,
    )
    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", return_value=bad_time),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        result = run_n7_preflight(
            out_dir=tmp_path / "out",
            config_path=cfg_path(tmp_path),
            repo=tmp_path,
            require_clean_worktree=False,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.PUBLIC_TIME_INVALID.value in result.abort_codes
    assert "hint_check_vpn_or_dns" not in result.abort_codes
    assert N7AbortCode.PREFLIGHT_RECON_DISAGREEMENT.value not in result.abort_codes
    assert N7AbortCode.CONNECTIVITY_UNAVAILABLE.value not in result.abort_codes


def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "n7.yaml"
    p.write_text("x: 1\n", encoding="utf-8")
    return p


def test_n7_no_false_recon_disagreement_from_upstream_unknown(tmp_path: Path) -> None:
    """Public-time failure must not fabricate preflight_reconciliation_disagreement."""
    bad_time = _fake_preflight_result(
        ok=False,
        blocker="public_time_invalid",
        public_clob={
            "transport_reachable": True,
            "venue_time_valid": False,
            "failure_kind": "invalid_time_response",
        },
        reconciliation=None,
    )
    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", return_value=bad_time),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        result = run_n7_preflight(
            out_dir=tmp_path / "out2",
            config_path=cfg_path(tmp_path),
            repo=tmp_path,
            require_clean_worktree=False,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.PREFLIGHT_RECON_DISAGREEMENT.value not in result.abort_codes


def test_n7_genuine_recon_disagreement_still_blocks(tmp_path: Path) -> None:
    from tyrex_pm.runtime.n6_account_classify import classify_account

    first = _fake_preflight_result(
        ok=True,
        blocker=None,
        public_clob={
            "transport_reachable": True,
            "venue_time_valid": True,
            "failure_kind": None,
        },
        reconciliation={
            "unreachable_account": False,
            "open_order_count": 0,
            "position_row_count": 0,
            "observation_only_local_empty": True,
        },
    )
    second = _fake_preflight_result(
        ok=True,
        blocker=None,
        public_clob={
            "transport_reachable": True,
            "venue_time_valid": True,
            "failure_kind": None,
        },
        reconciliation={
            "unreachable_account": False,
            "open_order_count": 0,
            "position_row_count": 0,
            "observation_only_local_empty": True,
        },
    )

    calls = {"n": 0}

    def _side_effect(*_a: Any, **_k: Any) -> MagicMock:
        calls["n"] += 1
        return first if calls["n"] == 1 else second

    flip = {"unknown": False}

    def _classify_account(**kwargs: Any) -> Any:
        unknown = flip["unknown"]
        flip["unknown"] = not flip["unknown"]
        return classify_account(
            open_orders=kwargs.get("open_orders") or [],
            positions=kwargs.get("positions") or [],
            selected_market_token_ids=kwargs.get("selected_market_token_ids") or set(),
            acknowledged=kwargs.get("acknowledged") or (),
            unknown=unknown,
        )

    with (
        patch("tyrex_pm.runtime.n7_preflight.load_n7_sealed_config") as sealed,
        patch("tyrex_pm.runtime.n7_preflight.inspect_git") as git,
        patch("tyrex_pm.runtime.n7_preflight.run_live_preflight", side_effect=_side_effect),
        patch("tyrex_pm.runtime.n7_preflight.classify_account", side_effect=_classify_account),
        patch(
            "tyrex_pm.runtime.n7_preflight.PRODUCTION_TIMING_VALUES_STATUS",
            "FROZEN_FOR_N7",
        ),
    ):
        sealed.return_value.live.mutations_enabled = False
        sealed.return_value.fingerprint.return_value = "fp"
        sealed.return_value.to_dict.return_value = {}
        git.return_value.head = "abc"
        git.return_value.worktree_clean = True
        result = run_n7_preflight(
            out_dir=tmp_path / "out3",
            config_path=cfg_path(tmp_path),
            repo=tmp_path,
            require_clean_worktree=False,
            user_stream_observe_s=0.0,
        )
    assert N7AbortCode.PREFLIGHT_RECON_DISAGREEMENT.value in result.abort_codes


def test_no_get_order_book_zero_in_active_source() -> None:
    root = PKG
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if 'get_order_book(token_id="0")' in text or "get_order_book(token_id='0')" in text:
            offenders.append(str(path))
        if 'get_order_book("0")' in text or "get_order_book('0')" in text:
            offenders.append(str(path))
    assert offenders == []


def test_no_hasattr_get_server_time_shim() -> None:
    text = (PKG / "execution" / "polymarket" / "preflight_client.py").read_text(encoding="utf-8")
    assert "hasattr" not in text or "get_server_time" not in text.split("hasattr", 1)[-1][:80]
    assert 'hasattr(client, "get_server_time")' not in text
    assert 'token_id="0"' not in text
    assert "probe_public_book" not in text
    assert "expected_bad_token" not in text
    text2 = (PKG / "execution" / "polymarket" / "sdk_readonly.py").read_text(encoding="utf-8")
    assert 'hasattr(pub, "get_server_time")' not in text2
    assert 'hasattr(client, "get_server_time")' not in text2


def test_single_authoritative_fetch_definition() -> None:
    """Exactly one production fetch_clob_server_time implementation."""
    path = PKG / "adapters" / "polymarket" / "clob_server_time.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defs = [
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "fetch_clob_server_time"
    ]
    assert defs == ["fetch_clob_server_time"]
    # Callers delegate — no second urllib /time implementation in execution/
    for rel in (
        "execution/polymarket/preflight_client.py",
        "execution/polymarket/sdk_readonly.py",
        "runtime/live_preflight.py",
    ):
        src = (PKG / rel).read_text(encoding="utf-8")
        assert "clob.polymarket.com/time" not in src or rel.endswith("unused")
        if rel.endswith("preflight_client.py") or rel.endswith("sdk_readonly.py"):
            assert "fetch_clob_server_time" in src


def test_preflight_has_no_mutation_methods() -> None:
    client = PreflightReadClient()
    for name in ("submit_order", "cancel_order", "post_heartbeat", "post_order"):
        assert not hasattr(client, name)


def test_sdk_readonly_delegates_to_adapter() -> None:
    from tyrex_pm.execution.polymarket.sdk_readonly import SdkReadonlyTransport

    transport = object.__new__(SdkReadonlyTransport)
    transport.spy = MagicMock()
    fake = ClobServerTimeResult(unix_seconds=42, http_status=200)
    with patch(
        "tyrex_pm.adapters.polymarket.clob_server_time.fetch_clob_server_time",
        return_value=fake,
    ) as mocked:
        assert SdkReadonlyTransport.get_server_time(transport) == 42
        mocked.assert_called_once()
        transport.spy.check.assert_called_once_with(method="GET", path="/time")
