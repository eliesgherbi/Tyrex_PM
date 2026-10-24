"""Collector composition; independent from live trading and account state."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from .capture.files import atomic_json, read_json, within
from .capture.writer import DurabilityError, RawWriter, archive_bytes
from .catalog.sqlite import Catalog
from .config import Credentials, Profile, strict
from .connectors.http import PublicHttp
from .connectors.streams import record_stream
from .contracts import RawEnvelope, canonical, identity
from .discovery import DiscoveryService
from .normalize.registry import normalize
from .reconstruction.books import Books
from .reconstruction.boundaries import resolution_assessment
from .reconstruction.policy import load_policy, resolution_schedule, semantic_status


class Collector:
    def __init__(
        self,
        root: Path,
        profile: Profile,
        credentials: Credentials | None,
        *,
        collection: str | None = None,
        normalize_live: bool = True,
    ):
        self.root = root.resolve()
        self.profile, self.credentials = profile, credentials
        self.collection = collection or uuid.uuid4().hex
        self.session = uuid.uuid4().hex
        self.collection_dir = within(self.root, f"collections/{self.collection}")
        config_path = self.collection_dir / "collection.json"
        if collection:
            saved = read_json(config_path)
            if saved["profile_hash"] != profile.hash:
                raise ValueError("resume requires exact saved effective profile")
        else:
            atomic_json(
                config_path,
                {
                    "collection_id": self.collection,
                    "created_at": time.time_ns(),
                    "profile": asdict(profile),
                    "profile_hash": profile.hash,
                    "source_contract_version": "2026-09-30-unqualified-live",
                    "schema_version": "1",
                    "parser_version": "1",
                },
            )
        self.writer = RawWriter(
            self.root, self.collection, self.session, profile.hash, profile.storage
        )
        self.catalog = Catalog(self.root)
        self.catalog.rebuild()
        self.markets = self.catalog.values(self.collection, "markets")
        self.stop = asyncio.Event()
        self.feed_health: dict[str, dict] = {}
        self.projection_bytes = 0
        self.books = Books(retain_epochs=False)
        self.shard_of: dict[str, int] = {}
        self.http = PublicHttp(self.capture, profile.operations)
        self.discovery = DiscoveryService(self)
        self.normalize_live = normalize_live
        self.normalizer: subprocess.Popen | None = None
        self.shards: dict[int, asyncio.Task] = {}
        self.started_at = time.time_ns()
        # Runway uses this session's own growth; resume must not divide the whole
        # archive by the new session's uptime.
        self.started_bytes = archive_bytes(self.root)
        self.session_path = self.collection_dir / "sessions" / f"{self.session}.json"
        self.status_path = self.collection_dir / "status.json"
        self.normalizer_stop = self.collection_dir / "sessions" / f"{self.session}.normalize-stop"
        self.tasks: list[asyncio.Task] = []
        self.provider_policy = load_policy(self.root, self.collection)
        self.normalizer_restarts = 0
        self.normalizer_retry_at = 0.0
        self.normalizer_log = None
        self.quality_stop = self.collection_dir / "sessions" / f"{self.session}.quality-stop"
        self.collector_stop = self.collection_dir / "sessions" / f"{self.session}.collector-stop"
        self.generation = os.environ.get("TYREX_COLLECTION_GENERATION", "1")
        self.quality_worker = None
        self.quality_log = None
        self.quality_restarts = 0
        self.quality_retry_at = 0.0
        self.unknown_messages = 0
        self.parse_failures = 0
        self.drop_counters: dict[str, int] = {}
        self.recent_gaps: list[dict] = []
        self.last_clock: dict | None = None
        # Closing phases (see run): which part of the session is running and what it covers.
        self.phase = "recording"
        self.capture_until_ns: int | None = None
        self.capture_ended_at: int | None = None
        self.windows_until: int | None = None
        self.outcome_scope: set[str] | None = None
        self.outcome_wait_until: int | None = None
        self.outcome_result: dict | None = None
        self.kind = "capture"

    async def capture(self, body: str | bytes, **kwargs: Any) -> RawEnvelope:
        raw, future = self.writer.submit(body, **kwargs)
        await asyncio.wrap_future(future)
        return raw

    async def event(self, event_type: str, **details: Any) -> RawEnvelope:
        raw = await self.capture(
            canonical({"type": event_type, **details}),
            source="collector",
            stream="operations",
            transport="collector_event",
        )
        for dataset, row in normalize(raw):
            if dataset == "operational_events":
                self.books.apply(dataset, row)
        if event_type == "sequence_gap":
            self.recent_gaps.append(
                {
                    "at": raw.received_at,
                    "source": details.get("source"),
                    "previous": details.get("previous"),
                    "current": details.get("current"),
                }
            )
            self.recent_gaps = self.recent_gaps[-20:]
        if event_type == "clock_sample":
            self.last_clock = details.get("sample")
        return raw

    async def persist(self, namespace: str, key: str, value: dict) -> None:
        raw = await self.event("projection", namespace=namespace, key=key, value=value)
        self.catalog.apply(raw)

    async def persist_batch(self, updates: list[dict]) -> None:
        if updates:
            raw = await self.event("projection_batch", updates=updates)
            self.catalog.apply(raw)

    def active_markets(self) -> list[dict]:
        now = time.time_ns()
        return [
            m
            for m in self.markets.values()
            if not m.get("closed_at")
            or now <= m["closed_at"] + self.profile.coverage.post_close_tail_s * 10**9
        ]

    def book_markets(self) -> list[dict]:
        """Markets whose books are recorded now: open soon, open, or in the close tail."""
        horizon = time.time_ns() + self.profile.coverage.book_subscribe_ahead_s * 10**9
        return [m for m in self.active_markets() if m.get("window_start", 0) <= horizon]

    def tokens(self, shard: int | None = None) -> list[str]:
        tokens = sorted({i["token_id"] for m in self.book_markets() for i in m["instruments"]})
        if shard is None:
            return tokens
        assignment = assign_shards(
            self.shard_of, tokens, self.profile.polymarket.tokens_per_connection
        )
        self.shard_of = assignment
        return sorted(t for t, s in assignment.items() if s == shard)

    async def bind_feeds(self, entity: dict) -> None:
        for feed, role, required in (
            ("chainlink:btcusd:twap:60", "settlement_reference", self.profile.reference.required),
            (
                "binance:spot:BTCUSDT",
                "explanatory_input",
                self.profile.external.binance_spot.required,
            ),
        ):
            await self.event(
                "feed_binding",
                binding={
                    "binding_id": identity(entity["market_id"], feed),
                    "market_id": entity["market_id"],
                    "feed_id": feed,
                    "feed_version": "1",
                    "role": role,
                    "required": required,
                    "applicable_from": entity["window_start"]
                    - self.profile.coverage.external_warmup_s * 10**9,
                    "applicable_until": entity["window_end"]
                    + self.profile.coverage.post_close_tail_s * 10**9,
                    "rule_version_id": entity["rule_fingerprint"],
                    "binding_evidence_ids": [entity["metadata_raw_id"]],
                },
            )

    async def observe(self, raw: RawEnvelope) -> None:
        for dataset, row in normalize(raw):
            self.books.apply(dataset, row)
            if dataset == "processing_results":
                if row["status"] == "UNKNOWN_TYPE":
                    self.unknown_messages += 1
                elif row["status"] == "REJECTED":
                    self.parse_failures += 1
            dropped = None
            if dataset == "reference_observations":
                dropped = row.get("provider_dropped_count")
            if dropped:
                self.drop_counters[raw.stream_id] = int(dropped)
            if dataset in (
                "reference_observations",
                "trade_prints",
                "book_updates",
                "book_snapshots",
            ):
                if raw.stream_id in self.feed_health:
                    self.feed_health[raw.stream_id]["last_observation_at"] = row.get("source_time")

    async def bootstrap_binance(self, connection: str) -> None:
        while not self.stop.is_set():
            book = self.books.books.get("binance:spot:BTCUSDT")
            if book is None or book.connection_id != connection or book.update_id is None:
                _, raw = await self.http.read(
                    "binance_depth",
                    params={"symbol": "BTCUSDT", "limit": 5000},
                    connection=connection,
                )
                await self.observe(raw)
            await asyncio.sleep(2)

    async def manage_shards(self) -> None:
        tokens = self.tokens()
        retained = {f"polymarket:137:outcome:{token}" for token in tokens} | {
            "binance:spot:BTCUSDT"
        }
        for instrument in {key[0] for key in self.books.streams} | set(self.books.books):
            if instrument not in retained:
                self.books.forget(instrument)
        size = self.profile.polymarket.tokens_per_connection
        needed = (len(tokens) + size - 1) // size
        for index in range(needed):
            if index not in self.shards or self.shards[index].done():
                if index in self.shards:
                    self.shards[index].result()
                self.shards[index] = asyncio.create_task(
                    record_stream(
                        self, "clob", f"clob-{index}", tokens=lambda i=index: self.tokens(i)
                    )
                )
        for index in list(self.shards):
            if index >= needed:
                task = self.shards.pop(index)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def metadata_cycle(self) -> None:
        now = time.time_ns()
        for mid, entity in list(self.markets.items()):
            if self.outcome_scope is not None and mid not in self.outcome_scope:
                continue
            hot = entity["window_start"] - self.profile.coverage.pre_window_capture_s * 10**9 <= now
            if entity.get("closed_at") and now > entity["closed_at"] + 120 * 10**9:
                continue
            interval = (
                self.profile.polymarket.metadata_refresh_s
                if hot
                else self.profile.polymarket.cold_metadata_refresh_s
            )
            gamma_due = now - entity["last_metadata_at"] >= interval * 10**9
            constraints_due = now - entity.get("constraints_last_at", 0) >= interval * 10**9
            if not (gamma_due or constraints_due) or entity.get("metadata_retry_at", 0) > now:
                continue
            await self.persist("tasks", f"metadata:{mid}", {"status": "PENDING", "due_at": now})
            try:
                raw_ids = []
                if gamma_due:
                    document, raw = await self.http.read(
                        "gamma_markets", params={"slug": entity["slug"]}
                    )
                    await self.discovery.ingest(document, raw.raw_record_id)
                    raw_ids.append(raw.raw_record_id)
                if constraints_due:
                    _, constraints = await self.http.read(
                        "clob_market", params={"condition": entity["condition_id"]}
                    )
                    current = {
                        **self.markets[mid],
                        "constraints_last_at": constraints.received_at,
                        "metadata_retry_at": 0,
                    }
                    await self.persist("markets", mid, current)
                    self.markets[mid] = current
                    raw_ids.append(constraints.raw_record_id)
                await self.persist(
                    "tasks", f"metadata:{mid}", {"status": "COMPLETE", "raw_ids": raw_ids}
                )
            except DurabilityError:
                raise
            except Exception as exc:
                current = {**self.markets[mid], "metadata_retry_at": now + 30 * 10**9}
                await self.persist("markets", mid, current)
                self.markets[mid] = current
                await self.event("metadata_error", market_id=mid, reason=type(exc).__name__)

    async def book_audit(self) -> None:
        tokens = self.tokens()
        size = self.profile.polymarket.rest_book_audit_batch_size
        for index in range(0, len(tokens), size):
            selectors = [{"token_id": token} for token in tokens[index : index + size]]
            await self.persist(
                "tasks", f"book_audit:{index}", {"status": "PENDING", "selectors": selectors}
            )
            documents, raw = await self.http.read("clob_books", body=selectors)
            returned = {str(d.get("asset_id")) for d in documents if isinstance(d, dict)}
            requested = {d["token_id"] for d in selectors}
            if returned != requested:
                await self.event(
                    "book_audit_mismatch",
                    missing=sorted(requested - returned),
                    unexpected=sorted(returned - requested),
                    raw_id=raw.raw_record_id,
                )
            await self.persist(
                "tasks", f"book_audit:{index}", {"status": "COMPLETE", "raw_id": raw.raw_record_id}
            )

    async def resolution_cycle(self) -> None:
        now = time.time_ns()
        due = [
            m
            for m in self.markets.values()
            if m.get("resolution_next_at") is not None
            and m["resolution_next_at"] <= now
            and (self.outcome_scope is None or m["market_id"] in self.outcome_scope)
        ]
        for index in range(0, len(due), 20):
            batch = due[index : index + 20]
            conditions = ",".join(m["condition_id"] for m in batch)
            for entity in batch:
                await self.persist(
                    "tasks",
                    f"resolution:{entity['market_id']}",
                    {"status": "PENDING", "condition": entity["condition_id"]},
                )
            document, raw = await self.http.read("resolutions", params={"condition": conditions})
            observations = [
                r for dataset, r in normalize(raw) if dataset == "resolution_observations"
            ]
            for entity in batch:
                current = self.markets.get(entity["market_id"], entity)
                rows = [r for r in observations if r["market_id"] == entity["market_id"]]
                previous = current.get("terminal_observation")
                conflicting = current.get("conflicting_terminal_observation")
                assessment = (
                    resolution_assessment(
                        ([previous] if previous else [])
                        + ([conflicting] if conflicting else [])
                        + rows,
                        supported_statuses=tuple(self.provider_policy.get("terminal_statuses", [])),
                        payout_unit=self.provider_policy.get("payout_unit"),
                    )
                    if rows or conflicting
                    else resolution_assessment([])
                )
                update = resolution_schedule(current, assessment, now, self.profile.resolution)
                update["resolution_last_raw_id"] = raw.raw_record_id
                if assessment["status"] == "PROVIDER_TERMINAL" and not previous:
                    update["terminal_observation"] = rows[-1]
                if assessment["status"] == "CONFLICTING" and not conflicting:
                    from .contracts import exact

                    evidence = ([previous] if previous else []) + rows
                    terminal = [
                        r
                        for r in evidence
                        if r.get("provider_status")
                        in self.provider_policy.get("terminal_statuses", [])
                        and r.get("payouts")
                    ]
                    update["terminal_observation"] = terminal[0]
                    first_vector = tuple(exact(p) for p in terminal[0]["payouts"])
                    update["conflicting_terminal_observation"] = next(
                        r for r in terminal if tuple(exact(p) for p in r["payouts"]) != first_vector
                    )
                await self.persist("markets", entity["market_id"], update)
                self.markets[entity["market_id"]] = update
                await self.persist(
                    "tasks",
                    f"resolution:{entity['market_id']}",
                    {"status": "COMPLETE", "raw_id": raw.raw_record_id},
                )

    async def supervise_normalizer(self) -> None:
        if not self.normalize_live or self.stop.is_set():
            return
        if self.normalizer is not None and self.normalizer.poll() is None:
            return
        if time.monotonic() < self.normalizer_retry_at:
            return
        if self.normalizer is not None:
            await self.event(
                "normalizer_exited",
                exit_code=self.normalizer.returncode,
                restart=self.normalizer_restarts + 1,
            )
            self.normalizer_restarts += 1
        if self.normalizer_log:
            self.normalizer_log.close()
        log_path = self.collection_dir / "sessions" / f"{self.session}.normalizer.log"
        self.normalizer_log = log_path.open("ab")
        self.normalizer = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tyrex_pm.data_warehouse.cli",
                "normalize",
                "--root",
                str(self.root),
                "--collection",
                self.collection,
                "--follow",
                "--stop-file",
                str(self.normalizer_stop),
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.normalizer_log,
            stderr=self.normalizer_log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.normalizer_retry_at = time.monotonic() + min(60, 2 ** min(self.normalizer_restarts, 6))

    async def supervise_quality(self) -> None:
        if not self.normalize_live or self.stop.is_set():
            return
        if self.quality_worker is not None and self.quality_worker.poll() is None:
            return
        if time.monotonic() < self.quality_retry_at:
            return
        if self.quality_worker is not None:
            self.quality_restarts += 1
            await self.event(
                "quality_worker_exited",
                exit_code=self.quality_worker.returncode,
                restart=self.quality_restarts,
            )
        if self.quality_log:
            self.quality_log.close()
        self.quality_log = (self.collection_dir / "sessions" / f"{self.session}.quality.log").open(
            "ab"
        )
        self.quality_worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tyrex_pm.data_warehouse.cli",
                "quality",
                "--root",
                str(self.root),
                "--collection",
                self.collection,
                "--follow",
                "--stop-file",
                str(self.quality_stop),
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.quality_log,
            stderr=self.quality_log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.quality_retry_at = time.monotonic() + min(60, 2 ** min(self.quality_restarts, 6))

    async def clocks(self) -> None:
        for endpoint in ("binance_time", "clob_time"):
            payload, raw = await self.http.read(endpoint)
            value = payload.get("serverTime") if isinstance(payload, dict) else payload
            if not isinstance(value, int):
                continue
            await self.event(
                "clock_sample",
                sample=clock_sample(
                    endpoint,
                    value,
                    request_at=raw.request_started_at,
                    response_at=raw.received_at,
                    sample_id=raw.raw_record_id,
                ),
            )

    async def heartbeat(self) -> None:
        if self.writer.fault:
            raise DurabilityError("writer fault")
        normalized = {}
        normalization_path = self.root / "normalization_status.json"
        if normalization_path.exists():
            normalized = read_json(normalization_path)
        newest = normalized.get("last_received_at")
        expected = self.catalog.values(self.collection, "expected")
        due_tasks = self.catalog.values(self.collection, "tasks")
        raw_bytes = archive_bytes(self.root)
        elapsed_s = max((time.time_ns() - self.started_at) / 10**9, 1)
        per_day = max(0, raw_bytes - self.started_bytes) * 86400 / elapsed_s
        free = shutil.disk_usage(self.root).free
        floor = self.profile.storage.min_free_disk_gib * 1024**3
        usable = max(0, free - floor)
        semantic, semantic_reason = semantic_status(self.provider_policy, None)
        quality_path = self.collection_dir / "quality_status.json"
        quality_status = read_json(quality_path) if quality_path.exists() else {}
        status = {
            "collection_id": self.collection,
            "session_id": self.session,
            "profile_hash": self.profile.hash,
            "heartbeat_at": time.time_ns(),
            "uptime_s": (time.time_ns() - self.started_at) / 10**9,
            "markets": len(self.markets),
            "active_markets": len(self.active_markets()),
            "book_markets": len(self.book_markets()),
            "subscribed_tokens": 0 if self.phase == "awaiting_outcomes" else len(self.tokens()),
            "phase": self.phase,
            "capture_until": self.capture_until_ns,
            "outcomes": self.outcome_progress(),
            "unresolved_markets": sum(
                m["resolution_status"] != "PROVIDER_TERMINAL" for m in self.markets.values()
            ),
            "feeds": self.feed_health,
            "normalizer_running": self.normalizer is not None and self.normalizer.poll() is None,
            "normalizer_restarts": self.normalizer_restarts,
            "quality_worker_running": self.quality_worker is not None
            and self.quality_worker.poll() is None,
            "quality_worker_restarts": self.quality_restarts,
            "quality": {
                key: quality_status.get(key)
                for key in ("publication_id", "published_at", "markets", "quality_policy_version")
            },
            "normalization": normalized,
            "normalization_lag_s": max(0, (time.time_ns() - newest) / 10**9) if newest else None,
            "expected_window_statuses": {
                status: sum(v.get("status") == status for v in expected.values())
                for status in {v.get("status") for v in expected.values()}
            },
            "pending_tasks": sum(v.get("status") == "PENDING" for v in due_tasks.values()),
            "unknown_messages": self.unknown_messages,
            "parse_failures": self.parse_failures,
            "drop_counters": self.drop_counters,
            "recent_gaps": self.recent_gaps,
            "clock": self.last_clock,
            "raw_bytes": raw_bytes,
            "raw_bytes_per_day": per_day,
            "projected_days_to_disk_floor": (usable / per_day) if per_day else None,
            "semantic_readiness": semantic,
            "semantic_reason": semantic_reason,
            "free_disk_bytes": free,
            **self.writer.status(),
        }
        await self.event("heartbeat", status=status)
        atomic_json(self.status_path, status)

    async def periodic(
        self, name: str, interval: float, action: Callable[[], Awaitable[None]]
    ) -> None:
        while not self.stop.is_set():
            try:
                await action()
            except DurabilityError:
                self.stop.set()
                raise
            except Exception as exc:
                await self.event("task_error", task=name, reason=type(exc).__name__)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), interval)

    async def watch_collector_stop(self) -> None:
        """Stop this session only when the request names its session and generation."""
        if not self.collector_stop.exists():
            return
        try:
            payload = read_json(self.collector_stop)
        except (OSError, ValueError):
            return
        if collector_stop_matches(
            payload, session_id=self.session, generation=self.generation
        ):
            await self.event(
                "stop_requested", session_id=self.session, generation=self.generation
            )
            self.stop.set()

    def window_boundary(self, deadline_ns: int) -> int:
        """End of the window still open at the deadline (the deadline if none is)."""
        return max(
            (
                m["window_end"]
                for m in self.markets.values()
                if m.get("window_start") is not None
                and m.get("window_end") is not None
                and m["window_start"] < deadline_ns < m["window_end"]
            ),
            default=deadline_ns,
        )

    def recorded_markets(self, spans: list[tuple[int, int]]) -> set[str]:
        """Markets whose whole window lies inside a span of captured windows."""
        return {
            mid
            for mid, m in self.markets.items()
            if m.get("window_start") is not None
            and m.get("window_end") is not None
            and any(start <= m["window_start"] and m["window_end"] <= end for start, end in spans)
        }

    def previous_spans(self) -> list[tuple[int, int]]:
        spans = []
        for path in (self.collection_dir / "sessions").glob("*.json"):
            if path == self.session_path:
                continue
            try:
                state = read_json(path)
            except (OSError, ValueError):
                continue
            start = state.get("started_at")
            end = state.get("windows_until", state.get("stopped_at"))
            if isinstance(start, int) and isinstance(end, int) and end > start:
                spans.append((start, end))
        return spans

    def pending_outcomes(self) -> list[str]:
        return sorted(
            mid
            for mid in self.outcome_scope or ()
            if self.markets.get(mid, {}).get("resolution_status") != "PROVIDER_TERMINAL"
        )

    def outcome_progress(self) -> dict | None:
        if self.outcome_scope is None:
            return None
        return {
            "windows": len(self.outcome_scope),
            "known": len(self.outcome_scope) - len(self.pending_outcomes()),
            "wait_until": self.outcome_wait_until,
        }

    async def wait_tasks(self, tasks: list[asyncio.Task], timeout: float | None) -> bool:
        """Wait; True when the session ended early (a stop request or a task that ended)."""
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                task.result()
        return bool(done)

    async def await_outcomes(self, tasks: list[asyncio.Task], wait_s: float) -> None:
        """Poll the provider until every captured window has a winner, or the wait ends.

        Only resolution and metadata polling run here; books and reference feeds are
        stopped, so nothing new is captured for windows after the recording.
        """
        now = time.time_ns()
        self.phase = "awaiting_outcomes"
        self.outcome_wait_until = now + int(wait_s * 10**9)
        for mid in self.pending_outcomes():
            # Ask now rather than at the slower audit cadence of old windows.
            self.markets[mid] = {**self.markets[mid], "resolution_next_at": now}
        await self.event(
            "awaiting_outcomes",
            markets=self.pending_outcomes(),
            wait_s=wait_s,
            windows=len(self.outcome_scope or ()),
        )
        while self.pending_outcomes():
            left = (self.outcome_wait_until - time.time_ns()) / 10**9
            if left <= 0 or await self.wait_tasks(tasks, min(5.0, left)):
                break
        pending = self.pending_outcomes()
        self.outcome_result = {
            "windows": len(self.outcome_scope or ()),
            "known": len(self.outcome_scope or ()) - len(pending),
            "missing": pending,
        }
        await self.event("outcome_wait_finished", **self.outcome_result)

    async def run(
        self,
        duration: float | None = None,
        *,
        finish_windows: bool = False,
        outcome_wait_s: float = 0.0,
        outcomes_only: bool = False,
    ) -> dict:
        """Record, optionally finish the open window, then optionally wait for winners.

        ``finish_windows`` extends the capture past ``duration`` to the end of the window
        still open, plus CLOSE_MARGIN_S, so the last window is complete. ``outcome_wait_s``
        then keeps only resolution and metadata polling until every captured window has
        a provider winner (or the wait ends). ``outcomes_only`` runs that wait alone, for
        the windows of the collection's earlier sessions. A stop request ends any phase.
        """
        self.kind = "outcomes_only" if outcomes_only else "capture"
        atomic_json(
            self.session_path,
            {
                "session_id": self.session,
                "started_at": self.started_at,
                "clean_stop": False,
                "profile_hash": self.profile.hash,
                "generation": self.generation,
            },
        )
        error = None
        try:
            for previous in (self.collection_dir / "sessions").glob("*.json"):
                state = read_json(previous)
                if previous != self.session_path and not state.get("clean_stop"):
                    await self.event(
                        "session_interrupted",
                        previous_session=state["session_id"],
                        uncertain_loss_interval=True,
                    )
            await self.event(
                "session_started", session_id=self.session, recovery=self.writer.recovery
            )
            feeds = (
                ("chainlink", "chainlink:btcusd:twap:60"),
                ("binance", "binance:spot:BTCUSDT"),
            )
            for source, feed in () if outcomes_only else feeds:
                await self.event(
                    "feed_definition",
                    feed={
                        "feed_id": feed,
                        "feed_version": "1",
                        "provider": source,
                        "transport": "websocket",
                        "channel": feed,
                        "measurement_type": "TWAP" if source == "chainlink" else "trades_and_depth",
                        "price_currency": "USD" if source == "chainlink" else "USDT",
                        "source_contract_version": "2026-09-30",
                        "depth_limit": 5000 if source == "binance" else None,
                    },
                )
            await self.supervise_normalizer()
            await self.supervise_quality()
            actions = [
                ("quality_worker", 2, self.supervise_quality),
                ("normalizer", 2, self.supervise_normalizer),
                ("discovery", self.profile.discovery.refresh_s, self.discovery.cycle),
                ("scan", self.profile.discovery.scan_interval_s, self.discovery.scan),
                ("metadata", 5, self.metadata_cycle),
                ("shards", 1, self.manage_shards),
                ("books", self.profile.polymarket.rest_book_audit_s, self.book_audit),
                ("resolution", self.profile.resolution.poll_initial_s, self.resolution_cycle),
                ("clock", 60, self.clocks),
                ("heartbeat", self.profile.operations.heartbeat_s, self.heartbeat),
                ("collector_stop", 0.5, self.watch_collector_stop),
            ]
            if outcomes_only:
                actions = [item for item in actions if item[0] not in CAPTURE_ACTIONS]
            named = {
                name: asyncio.create_task(self.periodic(name, interval, action))
                for name, interval, action in actions
            }
            if not outcomes_only:
                named["binance_stream"] = asyncio.create_task(
                    record_stream(self, "binance", "binance-main")
                )
                if self.credentials is not None:
                    named["reference_stream"] = asyncio.create_task(
                        record_stream(self, "polybolt", "reference", credentials=self.credentials)
                    )
            named["stop"] = asyncio.create_task(self.stop.wait())
            self.tasks = list(named.values())
            if outcomes_only:
                self.capture_ended_at = self.windows_until = self.started_at
                self.outcome_scope = {
                    mid
                    for mid in self.recorded_markets(self.previous_spans())
                    if self.markets[mid]["window_end"] <= self.started_at
                }
                await self.await_outcomes(self.tasks, outcome_wait_s)
            else:
                ended = await self.wait_tasks(self.tasks, duration)
                windows_until = None
                if not ended and finish_windows and duration is not None:
                    boundary = self.window_boundary(self.started_at + int(duration * 10**9))
                    self.phase = "finishing_window"
                    self.capture_until_ns = boundary + CLOSE_MARGIN_S * 10**9
                    left = (self.capture_until_ns - time.time_ns()) / 10**9
                    ended = left > 0 and await self.wait_tasks(self.tasks, left)
                    windows_until = None if ended else boundary
                self.capture_ended_at = time.time_ns()
                self.windows_until = windows_until or self.capture_ended_at
                if not ended and outcome_wait_s > 0:
                    capture = [named.pop(name) for name in list(named) if name in CAPTURE_TASKS]
                    shards = list(self.shards.values())
                    for task in capture + shards:
                        task.cancel()
                    await asyncio.gather(*capture, *shards, return_exceptions=True)
                    self.shards.clear()
                    await self.event("capture_finished", windows_until=self.windows_until)
                    self.outcome_scope = self.recorded_markets(
                        [(self.started_at, self.windows_until)]
                    )
                    self.tasks = list(named.values())
                    await self.await_outcomes(self.tasks, outcome_wait_s)
        except BaseException as exc:
            error = exc
        finally:
            self.stop.set()
            all_tasks = self.tasks + list(self.shards.values())
            for task in all_tasks:
                task.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)
            await self.http.close()
            if not self.writer.fault:
                await self.event("session_stopped", reason="interrupted" if error else "clean_stop")
            try:
                await asyncio.to_thread(
                    self.writer.close, self.profile.operations.shutdown_timeout_s
                )
            except DurabilityError as exc:
                error = exc
            self.catalog.close()
            stopped_at = time.time_ns()
            session_status = {
                "session_id": self.session,
                "kind": self.kind,
                "started_at": self.started_at,
                "stopped_at": stopped_at,
                # Windows starting before windows_until belong to this session; after it
                # the session only polled outcomes (or had stopped).
                "capture_ended_at": self.capture_ended_at or stopped_at,
                "windows_until": self.windows_until or self.capture_ended_at or stopped_at,
                "outcome_wait": self.outcome_result,
                "clean_stop": error is None,
                "capture_clean": error is None,
                "derived_processing_complete": False,
                "derived_processing_error": None,
                "profile_hash": self.profile.hash,
                "error_class": type(error).__name__ if error else None,
            }
            atomic_json(self.session_path, session_status)
            if self.normalizer:
                self.normalizer_stop.touch()
                try:
                    await asyncio.to_thread(self.normalizer.wait, 30)
                except subprocess.TimeoutExpired:
                    self.normalizer.terminate()
                    await asyncio.to_thread(self.normalizer.wait, 5)
            if self.normalizer_log:
                self.normalizer_log.close()
            if self.quality_worker:
                self.quality_stop.touch()
                try:
                    await asyncio.to_thread(self.quality_worker.wait, 30)
                except subprocess.TimeoutExpired:
                    self.quality_worker.terminate()
                    await asyncio.to_thread(self.quality_worker.wait, 5)
            if self.quality_log:
                self.quality_log.close()
            # Both subprocesses have released their ownership locks. Drain the final
            # sealed segments and publish against the resulting pinned build set before
            # returning. Failure cannot retroactively turn raw durability into success.
            if self.normalize_live:
                try:
                    await asyncio.to_thread(self.drain_derived)
                    session_status["derived_processing_complete"] = True
                except Exception as exc:
                    session_status["derived_processing_error"] = type(exc).__name__
            session_status["derived_finished_at"] = time.time_ns()
            atomic_json(self.session_path, session_status)
        if error and not isinstance(error, asyncio.CancelledError):
            raise error
        return {
            "collection_id": self.collection,
            "session_id": self.session,
            "stopped": True,
            "capture_clean": session_status["capture_clean"],
            "derived_processing_complete": session_status["derived_processing_complete"],
            "derived_processing_error": session_status["derived_processing_error"],
            **self.writer.status(),
        }

    def drain_derived(self) -> None:
        from .normalize.worker import normalize_once
        from .quality.publish import publish_quality

        normalize_once(self.root, self.collection)
        publish_quality(self.root, self.collection)


# Server-time resolution per clock endpoint [ns]: Binance returns milliseconds, the CLOB
# /time endpoint whole seconds.
CLOCK_RESOLUTION_NS = {"binance_time": 1_000_000, "clob_time": 1_000_000_000}


# The last window keeps this much data after its close (replay loads 30 s past the end).
CLOSE_MARGIN_S = 30
# Stopped when a session moves on to waiting for outcomes; resolution, metadata, clock,
# heartbeat, the normalizer and the quality worker keep running.
CAPTURE_ACTIONS = {"discovery", "scan", "shards", "books"}
CAPTURE_TASKS = CAPTURE_ACTIONS | {"binance_stream", "reference_stream"}


def clock_sample(
    endpoint: str, value: int, *, request_at: int, response_at: int, sample_id: str
) -> dict:
    """One clock observation. A truncated server time means the true server time lies in
    [server, server + resolution): the offset is centred on that interval and its half
    width is added to the uncertainty (for clob_time: ±0.5 s on top of the round trip)."""
    resolution = CLOCK_RESOLUTION_NS[endpoint]
    server = value * resolution
    rtt = response_at - request_at
    half = resolution // 2 if resolution > 1_000_000 else 0
    return {
        "sample_id": sample_id,
        "request_at": request_at,
        "response_at": response_at,
        "server_at": server,
        "rtt_ns": rtt,
        "estimated_offset_ns": server + half - (response_at + request_at) // 2,
        "uncertainty_ns": max(0, rtt // 2) + half,
        "measurement_source": endpoint,
        "status": "OBSERVED" if rtt >= 0 else "WALL_CLOCK_DISCONTINUITY",
    }


def assign_shards(previous: dict[str, int], tokens: list[str], size: int) -> dict[str, int]:
    """Sticky token-to-connection assignment.

    A token keeps its connection while it stays active, so a market roll does not move
    neighbouring tokens between sockets (which briefly subscribed them twice). New
    tokens fill the lowest connection with room. Only tokens on a connection that is
    no longer needed move.
    """
    needed = (len(tokens) + size - 1) // size
    active = set(tokens)
    assignment = {t: s for t, s in previous.items() if t in active and s < needed}
    counts = [0] * needed
    for shard in assignment.values():
        counts[shard] += 1
    for token in tokens:
        if token not in assignment:
            shard = next(i for i, count in enumerate(counts) if count < size)
            assignment[token] = shard
            counts[shard] += 1
    return assignment


def collector_stop_matches(payload: dict, *, session_id: str, generation: str) -> bool:
    """A stop file applies only to the session and generation it names."""
    return (
        str(payload.get("session_id") or "") == str(session_id)
        and str(payload.get("generation") or "") == str(generation)
    )


def resume_profile(root: Path, collection: str) -> Profile:
    saved = read_json(within(root, f"collections/{collection}/collection.json"))
    profile = strict(Profile, saved["profile"])
    profile.validate()
    return profile
