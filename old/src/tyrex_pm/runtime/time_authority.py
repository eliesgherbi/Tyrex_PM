"""Application-level time authority for Z-Gap observe/enforce (A0.5).

Primary offset from SNTP (time.cloudflare.com). Binance HTTP is an informational
cross-check. Sampling must complete before any strategy feeds start.
"""

from __future__ import annotations

import logging
import socket
import statistics
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

SYNC_STATUS_SYNCED = "synced"
SYNC_STATUS_FAILED = "failed"
SOURCE_SNTP_CLOUDFLARE = "sntp_time_cloudflare_com"
SOURCE_BINANCE_SERVER_TIME = "binance_server_time"

DEFAULT_SAMPLES_TOTAL = 7
DEFAULT_SAMPLES_KEPT = 3
DEFAULT_SYNC_UNCERTAINTY_MAX_MS = 50.0
DEFAULT_ENFORCE_UNCERTAINTY_MAX_MS = 250.0
DEFAULT_RESYNC_INTERVAL_S = 300.0
DEFAULT_CLOCK_STEP_WARN_MS = 100.0
DEFAULT_OFFSET_DISAGREE_WARN_MS = 250.0
DEFAULT_SYNC_MAX_ATTEMPTS = 3
DEFAULT_SYNC_BACKOFF_S = (0.5, 1.0, 2.0)

SNTP_HOST = "time.cloudflare.com"
SNTP_PORT = 123
NTP_EPOCH_OFFSET = 2208988800  # seconds from 1900-01-01 to 1970-01-01

_feeds_started: bool = False


def feeds_started() -> bool:
    return _feeds_started


def mark_feeds_started(*, caller: str = "unknown") -> None:
    global _feeds_started
    if not _feeds_started:
        log.debug("time_authority: feeds marked started by %s", caller)
    _feeds_started = True


def reset_feeds_started_for_tests() -> None:
    global _feeds_started
    _feeds_started = False


def assert_feeds_not_started() -> None:
    if _feeds_started:
        raise RuntimeError("TimeAuthority sample_offset must complete before feeds start")


@dataclass(frozen=True)
class SyncSample:
    rtt_ms: float
    offset_ms: float
    source: str


@dataclass
class TimeAuthority:
    """Corrected UTC time anchored to monotonic clock."""

    sync_status: str
    offset_ms: float | None = None
    uncertainty_ms: float | None = None
    median_offset_ms: float | None = None
    samples_requested: int = 0
    samples_kept: int = 0
    max_rtt_ms: float | None = None
    source: str = SOURCE_SNTP_CLOUDFLARE
    os_drift_ms: float | None = None
    sntp_offset_ms: float | None = None
    binance_offset_ms: float | None = None
    offset_disagreement_ms: float | None = None
    offset_disagreement_warning: bool = False
    sync_attempts: int = 0
    _epoch_at_sync: float | None = field(default=None, repr=False)
    _mono_at_sync: float | None = field(default=None, repr=False)
    _last_resync_offset_ms: float | None = field(default=None, repr=False)

    @property
    def enforce_gate_pass(self) -> bool:
        if self.sync_status != SYNC_STATUS_SYNCED:
            return False
        if self.uncertainty_ms is None:
            return False
        return self.uncertainty_ms <= DEFAULT_ENFORCE_UNCERTAINTY_MAX_MS

    @property
    def observe_warning(self) -> bool:
        return not self.enforce_gate_pass

    def corrected_epoch(self) -> float:
        if self._epoch_at_sync is None or self._mono_at_sync is None:
            return time.time()
        return self._epoch_at_sync + (time.monotonic() - self._mono_at_sync)

    def corrected_now(self) -> datetime:
        return datetime.fromtimestamp(self.corrected_epoch(), tz=timezone.utc)

    def clock_sync_payload(self) -> dict[str, Any]:
        return {
            "offset_ms": self.offset_ms,
            "uncertainty_ms": self.uncertainty_ms,
            "samples_requested": self.samples_requested,
            "samples_kept": self.samples_kept,
            "max_rtt_ms": self.max_rtt_ms,
            "median_offset_ms": self.median_offset_ms,
            "source": self.source,
            "sync_status": self.sync_status,
            "os_drift_ms": self.os_drift_ms,
            "sntp_offset_ms": self.sntp_offset_ms,
            "binance_offset_ms": self.binance_offset_ms,
            "offset_disagreement_ms": self.offset_disagreement_ms,
            "offset_disagreement_warning": self.offset_disagreement_warning,
            "sync_attempts": self.sync_attempts,
            "observe_warning": self.observe_warning,
            "enforce_gate_pass": self.enforce_gate_pass,
        }

    def apply_resync(self, other: TimeAuthority) -> str | None:
        if other.sync_status != SYNC_STATUS_SYNCED or other.offset_ms is None:
            return None
        warning: str | None = None
        if self._last_resync_offset_ms is not None:
            step = abs(other.offset_ms - self._last_resync_offset_ms)
            if step > DEFAULT_CLOCK_STEP_WARN_MS:
                warning = f"clock_step_detected: delta_ms={step:.3f}"
        self.sync_status = other.sync_status
        self.offset_ms = other.offset_ms
        self.uncertainty_ms = other.uncertainty_ms
        self.median_offset_ms = other.median_offset_ms
        self.samples_requested = other.samples_requested
        self.samples_kept = other.samples_kept
        self.max_rtt_ms = other.max_rtt_ms
        self.os_drift_ms = other.os_drift_ms
        self.sntp_offset_ms = other.sntp_offset_ms
        self.binance_offset_ms = other.binance_offset_ms
        self.offset_disagreement_ms = other.offset_disagreement_ms
        self.offset_disagreement_warning = other.offset_disagreement_warning
        self.sync_attempts = other.sync_attempts
        self._epoch_at_sync = other._epoch_at_sync
        self._mono_at_sync = other._mono_at_sync
        self._last_resync_offset_ms = other.offset_ms
        return warning


def compute_offset_ms(*, t_before: float, server_time_ms: float, t_after: float) -> tuple[float, float]:
    rtt_ms = (t_after - t_before) * 1000.0
    server_time_s = server_time_ms / 1000.0
    offset_ms = (server_time_s + (t_after - t_before) / 2.0 - t_after) * 1000.0
    return offset_ms, rtt_ms


def compute_offset_from_server_s(*, t_before: float, server_time_s: float, t_after: float) -> tuple[float, float]:
    rtt_ms = (t_after - t_before) * 1000.0
    offset_ms = (server_time_s + (t_after - t_before) / 2.0 - t_after) * 1000.0
    return offset_ms, rtt_ms


def select_best_samples(samples: list[SyncSample], *, keep: int = DEFAULT_SAMPLES_KEPT) -> list[SyncSample]:
    if not samples:
        return []
    ranked = sorted(samples, key=lambda s: s.rtt_ms)
    return ranked[: min(keep, len(ranked))]


def _ntp_timestamp_to_unix(seconds: int, fraction: int) -> float:
    return (seconds - NTP_EPOCH_OFFSET) + fraction / 2**32


def sample_sntp_offset(
    *,
    host: str = SNTP_HOST,
    port: int = SNTP_PORT,
    timeout_s: float = 5.0,
) -> SyncSample:
    packet = bytearray(48)
    packet[0] = 0x1B  # version 3, mode 3 (client)
    t_before = time.time()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_s)
        sock.sendto(packet, (host, port))
        data, _ = sock.recvfrom(48)
    t_after = time.time()
    if len(data) < 48:
        raise ValueError(f"SNTP response too short: {len(data)} bytes")
    tx_seconds, tx_fraction = struct.unpack("!II", data[40:48])
    server_time_s = _ntp_timestamp_to_unix(tx_seconds, tx_fraction)
    offset_ms, rtt_ms = compute_offset_from_server_s(
        t_before=t_before,
        server_time_s=server_time_s,
        t_after=t_after,
    )
    return SyncSample(rtt_ms=rtt_ms, offset_ms=offset_ms, source=SOURCE_SNTP_CLOUDFLARE)


def sample_sntp_offsets(
    *,
    samples_total: int = DEFAULT_SAMPLES_TOTAL,
    samples_kept: int = DEFAULT_SAMPLES_KEPT,
    host: str = SNTP_HOST,
    timeout_s: float = 5.0,
) -> list[SyncSample]:
    collected: list[SyncSample] = []
    for _ in range(samples_total):
        try:
            collected.append(sample_sntp_offset(host=host, timeout_s=timeout_s))
        except OSError:
            time.sleep(0.02)
            continue
        time.sleep(0.02)
    return select_best_samples(collected, keep=samples_kept)


class _BinanceTimeClient:
    def __init__(self, *, timeout_s: float = 10.0) -> None:
        self._client = httpx.Client(timeout=timeout_s)
        self._warmed = False

    def close(self) -> None:
        self._client.close()

    def fetch_server_time_ms(self) -> float:
        if not self._warmed:
            try:
                self._client.get("https://api.binance.com/api/v3/time")
            except Exception:
                pass
            self._warmed = True
        resp = self._client.get("https://api.binance.com/api/v3/time")
        resp.raise_for_status()
        data = resp.json()
        server_ms = data.get("serverTime")
        if server_ms is None:
            raise ValueError("Binance /api/v3/time missing serverTime")
        return float(server_ms)


def sample_binance_offsets(
    *,
    samples_total: int = DEFAULT_SAMPLES_TOTAL,
    samples_kept: int = DEFAULT_SAMPLES_KEPT,
    timeout_s: float = 10.0,
    client: _BinanceTimeClient | None = None,
) -> list[SyncSample]:
    owned = client is None
    binance = client or _BinanceTimeClient(timeout_s=timeout_s)
    collected: list[SyncSample] = []
    try:
        for _ in range(samples_total):
            t_before = time.time()
            try:
                server_ms = binance.fetch_server_time_ms()
            except Exception:
                time.sleep(0.05)
                continue
            t_after = time.time()
            offset_ms, rtt_ms = compute_offset_ms(t_before=t_before, server_time_ms=server_ms, t_after=t_after)
            collected.append(SyncSample(rtt_ms=rtt_ms, offset_ms=offset_ms, source=SOURCE_BINANCE_SERVER_TIME))
            time.sleep(0.05)
    finally:
        if owned:
            binance.close()
    return select_best_samples(collected, keep=samples_kept)


def _build_from_kept(
    *,
    kept: list[SyncSample],
    samples_requested: int,
    sntp_median: float | None,
    binance_median: float | None,
    os_drift_ms: float | None,
    sync_attempts: int,
) -> TimeAuthority:
    offsets = [s.offset_ms for s in kept]
    rtts = [s.rtt_ms for s in kept]
    median_offset = statistics.median(offsets)
    uncertainty = max(rtts) / 2.0
    local_epoch_at_sync = time.time()
    mono_at_sync = time.monotonic()
    epoch_at_sync = local_epoch_at_sync + median_offset / 1000.0

    disagreement: float | None = None
    warn = False
    if sntp_median is not None and binance_median is not None:
        disagreement = abs(sntp_median - binance_median)
        warn = disagreement > DEFAULT_OFFSET_DISAGREE_WARN_MS

    return TimeAuthority(
        sync_status=SYNC_STATUS_SYNCED,
        offset_ms=median_offset,
        uncertainty_ms=uncertainty,
        median_offset_ms=median_offset,
        samples_requested=samples_requested,
        samples_kept=len(kept),
        max_rtt_ms=max(rtts),
        source=SOURCE_SNTP_CLOUDFLARE,
        os_drift_ms=os_drift_ms,
        sntp_offset_ms=sntp_median,
        binance_offset_ms=binance_median,
        offset_disagreement_ms=disagreement,
        offset_disagreement_warning=warn,
        sync_attempts=sync_attempts,
        _epoch_at_sync=epoch_at_sync,
        _mono_at_sync=mono_at_sync,
        _last_resync_offset_ms=median_offset,
    )


def _failed(
    *,
    samples_requested: int,
    os_drift_ms: float | None,
    sync_attempts: int,
    sntp_median: float | None = None,
    binance_median: float | None = None,
) -> TimeAuthority:
    return TimeAuthority(
        sync_status=SYNC_STATUS_FAILED,
        samples_requested=samples_requested,
        samples_kept=0,
        os_drift_ms=os_drift_ms,
        source=SOURCE_SNTP_CLOUDFLARE,
        sntp_offset_ms=sntp_median,
        binance_offset_ms=binance_median,
        sync_attempts=sync_attempts,
    )


def sample_offset(
    *,
    samples_total: int = DEFAULT_SAMPLES_TOTAL,
    samples_kept: int = DEFAULT_SAMPLES_KEPT,
    uncertainty_max_ms: float = DEFAULT_SYNC_UNCERTAINTY_MAX_MS,
    max_attempts: int = DEFAULT_SYNC_MAX_ATTEMPTS,
    timeout_s: float = 5.0,
    require_feeds_not_started: bool = True,
) -> TimeAuthority:
    """Sample clock offset (SNTP primary) before feeds start. Retries with backoff."""
    if require_feeds_not_started:
        assert_feeds_not_started()

    binance_client = _BinanceTimeClient(timeout_s=timeout_s)
    sntp_median: float | None = None
    binance_median: float | None = None
    os_drift_ms: float | None = None

    try:
        backoffs = list(DEFAULT_SYNC_BACKOFF_S[: max_attempts - 1])
        while len(backoffs) < max(0, max_attempts - 1):
            backoffs.append(backoffs[-1] if backoffs else 1.0)

        for attempt in range(max(1, max_attempts)):
            kept = sample_sntp_offsets(
                samples_total=samples_total,
                samples_kept=samples_kept,
                timeout_s=timeout_s,
            )
            if kept:
                sntp_median = statistics.median([s.offset_ms for s in kept])
                uncertainty = max(s.rtt_ms for s in kept) / 2.0
                if len(kept) >= samples_kept and uncertainty <= uncertainty_max_ms:
                    binance_kept = sample_binance_offsets(
                        samples_total=samples_total,
                        samples_kept=samples_kept,
                        timeout_s=timeout_s,
                        client=binance_client,
                    )
                    if binance_kept:
                        binance_median = statistics.median([s.offset_ms for s in binance_kept])
                        os_drift_ms = binance_median
                    return _build_from_kept(
                        kept=kept,
                        samples_requested=samples_total,
                        sntp_median=sntp_median,
                        binance_median=binance_median,
                        os_drift_ms=os_drift_ms,
                        sync_attempts=attempt + 1,
                    )

            if attempt + 1 < max_attempts:
                time.sleep(backoffs[attempt])

        # Final attempt snapshot for diagnostics
        final_sntp = sample_sntp_offsets(samples_total=samples_total, samples_kept=samples_kept, timeout_s=timeout_s)
        if final_sntp:
            sntp_median = statistics.median([s.offset_ms for s in final_sntp])
        final_binance = sample_binance_offsets(
            samples_total=samples_total,
            samples_kept=samples_kept,
            timeout_s=timeout_s,
            client=binance_client,
        )
        if final_binance:
            binance_median = statistics.median([s.offset_ms for s in final_binance])
            os_drift_ms = binance_median
        return _failed(
            samples_requested=samples_total,
            os_drift_ms=os_drift_ms,
            sync_attempts=max_attempts,
            sntp_median=sntp_median,
            binance_median=binance_median,
        )
    finally:
        binance_client.close()


def sync_time_authority(**kwargs: Any) -> TimeAuthority:
    """Backward-compatible alias for sample_offset."""
    return sample_offset(**kwargs)
