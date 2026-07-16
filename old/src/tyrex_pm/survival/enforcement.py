"""Survival enforcement mode helpers."""

from __future__ import annotations

ADVISORY = "advisory"
ENFORCE = "enforce"


def is_enforce(mode: str) -> bool:
    return str(mode or ADVISORY).lower().strip() == ENFORCE
