"""Interactive operator acknowledgment for Z-Gap single-session orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

CALIBRATION_ACK_PHRASE = "ACCEPT TINY OPERATIONAL TEST"


@dataclass(frozen=True)
class CalibrationAckPrompt:
    message: str
    required_phrase: str = CALIBRATION_ACK_PHRASE


@dataclass(frozen=True)
class WindowApprovalPrompt:
    market_id: str
    condition_id: str
    event_start_ts: float
    event_end_ts: float
    maximum_usd: str
    run_name: str

    @property
    def required_phrase(self) -> str:
        return f"APPROVE ONE $5 TRADE FOR {self.market_id}"


def prompt_calibration_ack(
    *,
    input_fn: Callable[[str], str] | None = None,
    write_fn: Callable[[str], None] | None = None,
) -> bool:
    out = write_fn or print
    inp = input_fn or input
    out(
        "\n".join(
            [
                "",
                "=== Calibration acknowledgment (required once per config hash) ===",
                "Available evidence is operational and limited.",
                "This is not proof of profitability.",
                "Maximum live risk is $5.",
                "One position and one entry attempt only.",
                "",
                f"Type exactly: {CALIBRATION_ACK_PHRASE}",
                "",
            ]
        )
    )
    typed = inp("> ").strip()
    return typed == CALIBRATION_ACK_PHRASE


def prompt_window_approval(
    prompt: WindowApprovalPrompt,
    *,
    input_fn: Callable[[str], str] | None = None,
    write_fn: Callable[[str], None] | None = None,
) -> bool:
    out = write_fn or print
    inp = input_fn or input
    out(
        "\n".join(
            [
                "",
                "=== Exact-window risk approval ===",
                f"Market ID:      {prompt.market_id}",
                f"Condition ID:   {prompt.condition_id}",
                f"Window start:   {prompt.event_start_ts}",
                f"Window end:     {prompt.event_end_ts}",
                f"maximum_usd:    {prompt.maximum_usd}",
                "maximum entries: 1",
                "maximum positions: 1",
                "no reentry",
                "stop after terminal",
                f"run_name:       {prompt.run_name}",
                "",
                f"Type exactly: {prompt.required_phrase}",
                "",
            ]
        )
    )
    typed = inp("> ").strip()
    return typed == prompt.required_phrase
