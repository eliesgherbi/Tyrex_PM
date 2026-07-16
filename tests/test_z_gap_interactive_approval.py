"""Tests for interactive calibration and window approval UX."""

from __future__ import annotations

from tyrex_pm.runtime.z_gap_interactive_approval import (
    CALIBRATION_ACK_PHRASE,
    WindowApprovalPrompt,
    prompt_calibration_ack,
    prompt_window_approval,
)


def test_calibration_rejects_wrong_phrase() -> None:
    assert not prompt_calibration_ack(input_fn=lambda _: "NO", write_fn=lambda _: None)


def test_calibration_accepts_exact_phrase() -> None:
    assert prompt_calibration_ack(input_fn=lambda _: CALIBRATION_ACK_PHRASE, write_fn=lambda _: None)


def test_window_approval_requires_exact_market_phrase() -> None:
    prompt = WindowApprovalPrompt(
        market_id="btc_5m_20260715_1955",
        condition_id="0xabc",
        event_start_ts=1.0,
        event_end_ts=2.0,
        maximum_usd="5",
        run_name="test",
    )
    assert prompt.required_phrase == "APPROVE ONE $5 TRADE FOR btc_5m_20260715_1955"
    assert not prompt_window_approval(prompt, input_fn=lambda _: "APPROVE", write_fn=lambda _: None)
    assert prompt_window_approval(
        prompt,
        input_fn=lambda _: prompt.required_phrase,
        write_fn=lambda _: None,
    )
