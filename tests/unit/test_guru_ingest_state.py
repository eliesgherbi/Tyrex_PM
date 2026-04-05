"""Guru ingest rollout state (C1)."""

from __future__ import annotations

from tyrex_pm.data.guru_ingest_state import GuruIngestRuntimeState


def test_poll_only_always_polls_and_publishes() -> None:
    st = GuruIngestRuntimeState("poll_only")
    assert st.poll_timer_should_run()
    assert st.poll_should_publish()
    assert st.stream_should_publish() is False
    assert st.stream_shadow_log_would_emit() is False


def test_rtds_shadow_poll_publishes_stream_logs() -> None:
    st = GuruIngestRuntimeState("rtds_shadow")
    assert st.poll_timer_should_run()
    assert st.poll_should_publish()
    assert st.stream_should_publish() is False
    assert st.stream_shadow_log_would_emit() is True


def test_rtds_primary_stream_publishes_until_fallback() -> None:
    st = GuruIngestRuntimeState("rtds_primary", fallback_enabled=True)
    assert st.poll_timer_should_run() is False
    assert st.poll_should_publish() is False
    assert st.stream_should_publish() is True
    assert st.activate_fallback_poll("test") == "test"
    assert st.poll_timer_should_run() is True
    assert st.poll_should_publish() is True
    assert st.stream_should_publish() is False


def test_fallback_disabled() -> None:
    st = GuruIngestRuntimeState("rtds_primary", fallback_enabled=False)
    assert st.activate_fallback_poll("x") is None
    assert st.is_fallback_poll() is False
