"""Logging helpers (v1.03 baseline)."""

from __future__ import annotations

import logging
from typing import Any


def setup_logging(level: str = "INFO") -> None:
    """Idempotent basic config for local scripts; Nautilus nodes use their own logging."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(levelname)s %(name)s %(message)s",
    )


def log_kv(logger: logging.Logger, **kwargs: Any) -> None:
    """Emit a single line key=value for operational grep (no secrets)."""
    parts = [f"{k}={v}" for k, v in kwargs.items()]
    logger.info(" ".join(parts))
