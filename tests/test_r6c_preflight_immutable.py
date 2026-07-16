"""R6C: preflight transport is structurally mutation-impossible."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm
from tyrex_pm.execution.polymarket.endpoint_taxonomy import classify_endpoint
from tyrex_pm.execution.polymarket.preflight_client import PreflightReadClient
from tyrex_pm.execution.polymarket.readonly_transport import (
    AUTH_CLOB_HEARTBEAT,
    AUTH_CLOB_ORDERS,
    PUBLIC_CLOB_TIME,
    PUBLIC_DATA_POSITIONS,
    ReadOnlyAccountTransport,
)
from tyrex_pm.runtime.live_preflight import (
    assert_preflight_cannot_mutate,
    classify_cloudflare,
)


PKG = Path(tyrex_pm.__file__).resolve().parent


def test_preflight_client_has_no_mutation_methods() -> None:
    client = PreflightReadClient()
    assert_preflight_cannot_mutate(client)
    assert not hasattr(client, "submit_order")
    assert not hasattr(client, "cancel_order")
    assert not hasattr(PreflightReadClient, "submit_order")
    assert not hasattr(PreflightReadClient, "cancel_order")


def test_readonly_protocol_omits_submit_cancel() -> None:
    proto_dir = dir(ReadOnlyAccountTransport)
    assert "submit_order" not in proto_dir
    assert "cancel_order" not in proto_dir
    assert "get_open_orders" in proto_dir
    # Annotations define the observation surface only
    ann = getattr(ReadOnlyAccountTransport, "__annotations__", {})
    assert "submit_order" not in ann
    assert "cancel_order" not in ann


def test_live_preflight_composition_does_not_import_mutation_transport() -> None:
    """Composition root must not pull PolymarketTransport submit path."""
    text = (PKG / "runtime" / "live_preflight.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    joined = " ".join(imports)
    assert "tyrex_pm.execution.polymarket.transport" not in joined
    assert "tyrex_pm.execution.polymarket.live_oms" not in joined
    assert "PreflightReadClient" in text
    assert "not_called_r6c_policy" in text
    assert "--enable-mutations" not in text


def test_cli_has_no_enable_mutations_flag() -> None:
    text = (PKG / "application" / "cli.py").read_text(encoding="utf-8")
    assert "--enable-mutations" not in text
    assert "add_argument(\n        \"--enable" not in text
    assert "live-preflight" in text


def test_preflight_module_ast_no_post_delete_trading_calls() -> None:
    """Static: preflight_client must not construct POST/DELETE trading requests."""
    path = PKG / "execution" / "polymarket" / "preflight_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.upper() in {"POST", "DELETE", "PUT", "PATCH"}:
                # Allow only if clearly not used as HTTP method for trading —
                # fail if Request(..., method="POST")
                raise AssertionError(f"mutating HTTP method constant in preflight: {node.value}")


def test_endpoint_classification() -> None:
    assert classify_endpoint(*PUBLIC_CLOB_TIME[:3]) == "public_market_data"
    assert classify_endpoint(*AUTH_CLOB_ORDERS[:3]) == "authenticated_account"
    assert classify_endpoint(*PUBLIC_DATA_POSITIONS[:3]) == "public_data_api"
    assert classify_endpoint(*AUTH_CLOB_HEARTBEAT[:3]) == "authenticated_mutating"


def test_cloudflare_classification() -> None:
    assert classify_cloudflare(403, "error code: 1010") == "1010"
    assert classify_cloudflare(403, "<html>cloudflare attention required") == "cloudflare"
    assert classify_cloudflare(403, "plain deny") == "403_non_json"
    assert classify_cloudflare(401, '{"error":"Invalid API key"}') is None
