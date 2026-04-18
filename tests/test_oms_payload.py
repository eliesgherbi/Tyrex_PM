from __future__ import annotations

from tyrex_pm.reporting.oms_payload import get_oms_result_text


def test_canonical_oms_result_preferred() -> None:
    assert get_oms_result_text({"oms_result": "ack", "shadow_result": "old"}) == "ack"


def test_legacy_shadow_result_fallback() -> None:
    assert get_oms_result_text({"shadow_result": "shadow_ack"}) == "shadow_ack"
