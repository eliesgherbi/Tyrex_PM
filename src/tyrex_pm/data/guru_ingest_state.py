"""Shared rollout / fallback state for poll + RTDS guru ingestion."""

from __future__ import annotations

import threading


class GuruIngestRuntimeState:
    """
    Coordinates **at most one** publisher to ``GURU_TRADE_TOPIC`` in primary mode.

    - ``poll_only``: timer poll + publish only.
    - ``rtds_shadow``: poll publishes; stream logs ``would_emit`` only.
    - ``rtds_primary``: stream publishes when healthy; poll publishes only during fallback.
    """

    __slots__ = ("_mode", "_fallback_enabled", "_fallback_poll", "_lock")

    def __init__(self, mode: str, *, fallback_enabled: bool = True) -> None:
        if mode not in ("poll_only", "rtds_shadow", "rtds_primary"):
            raise ValueError(f"invalid guru_ingest_mode: {mode!r}")
        self._mode = mode
        self._fallback_enabled = fallback_enabled
        self._fallback_poll = False
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    def poll_timer_should_run(self) -> bool:
        if self._mode in ("poll_only", "rtds_shadow"):
            return True
        if self._mode == "rtds_primary":
            with self._lock:
                return self._fallback_poll
        return True

    def poll_should_publish(self) -> bool:
        if self._mode in ("poll_only", "rtds_shadow"):
            return True
        if self._mode == "rtds_primary":
            with self._lock:
                return self._fallback_poll
        return True

    def poll_run_initial_on_start(self) -> bool:
        if self._mode in ("poll_only", "rtds_shadow"):
            return True
        with self._lock:
            return self._fallback_poll

    def stream_should_publish(self) -> bool:
        if self._mode != "rtds_primary":
            return False
        with self._lock:
            return not self._fallback_poll

    def stream_shadow_log_would_emit(self) -> bool:
        return self._mode == "rtds_shadow"

    def is_fallback_poll(self) -> bool:
        with self._lock:
            return self._fallback_poll

    def activate_fallback_poll(self, reason: str) -> str | None:
        """Returns a log reason string when transition to True, else None."""
        if not self._fallback_enabled or self._mode != "rtds_primary":
            return None
        with self._lock:
            if self._fallback_poll:
                return None
            self._fallback_poll = True
        return reason

    def clear_fallback_poll(self) -> bool:
        """Returns True if fallback was cleared."""
        with self._lock:
            if not self._fallback_poll:
                return False
            self._fallback_poll = False
            return True
