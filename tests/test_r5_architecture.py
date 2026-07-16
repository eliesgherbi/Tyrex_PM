"""R5 architecture gates: no private APIs, no old/, ownership docs."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm

PKG = Path(tyrex_pm.__file__).resolve().parent

PRIVATE_FRAGMENTS = (
    "clob.polymarket.com",
    "/order",
    "createOrder",
    "postOrder",
    "apiKey",
    "POLYMARKET_PRIVATE",
    "wallet_private",
)


def test_no_old_or_nautilus() -> None:
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("old", "nautilus"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("old", "nautilus"))


def test_no_private_endpoint_literals_in_execution() -> None:
    for path in (PKG / "execution").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for frag in ("clob.polymarket.com", "createOrder", "postOrder"):
            assert frag not in text


def test_oms_protocol_exists() -> None:
    from tyrex_pm.execution.protocol import OMS
    from tyrex_pm.execution.shadow_oms import ShadowOMS

    assert hasattr(OMS, "submit")
    assert hasattr(ShadowOMS, "submit")


def test_ownership_modules_present() -> None:
    assert (PKG / "execution" / "fill_ledger.py").is_file()
    assert (PKG / "execution" / "order_store.py").is_file()
    assert (PKG / "portfolio" / "portfolio.py").is_file()
    assert (PKG / "lifecycle" / "trade_lifecycle.py").is_file()


def test_r4_dry_host_still_usable_without_shadow() -> None:
    text = (PKG / "runtime" / "observe_host.py").read_text(encoding="utf-8")
    assert "ShadowOMS" not in text
