"""Live stub behavior (v1.03)."""

from tyrex_pm.runtime import live_stub


def test_smoke_skipped_by_default(monkeypatch):
    monkeypatch.delenv("TYREX_LIVE_NODE_SMOKE", raising=False)
    assert live_stub.maybe_run_live_node_smoke() == "skipped"


def test_waiver_mentions_env():
    assert "TYREX_LIVE_NODE_SMOKE" in live_stub.live_node_smoke_waiver_text()
