"""Durability-backed market projections and expected-window discovery."""

from __future__ import annotations

import time
from typing import Any

from .capture.writer import DurabilityError
from .families.btc_5m import RecurringWindow
from .families.registry import FamilyRegistry


class DiscoveryService:
    def __init__(self, host: Any):
        self.host = host
        registry = FamilyRegistry()
        registry.register(
            "recurring_price_window", RecurringWindow(host.profile.window_family())
        )
        self.family = registry.get(host.profile.family)
        self.next_probe = 0

    async def ingest(self, document: Any, raw_id: str) -> None:
        items = document if isinstance(document, list) else [document]
        for item in items:
            if not isinstance(item, dict):
                continue
            if "markets" in item and "conditionId" not in item and "condition_id" not in item:
                await self.ingest(item["markets"], raw_id)
                continue
            if not self.family.matches(item):
                continue
            try:
                entity = self.family.identify(item)
            except (ValueError, TypeError, KeyError) as exc:
                await self.host.event(
                    "market_rejected", slug=item.get("slug"), reason=type(exc).__name__
                )
                continue
            if entity.get("window_start") is None:
                await self.host.event(
                    "market_rejected", slug=entity.get("slug"), reason="missing_window"
                )
                continue
            now = time.time_ns()
            if entity["window_start"] > now + self.host.profile.discovery.lookahead_s * 10**9:
                continue
            if (
                entity["window_end"] < now - 86400 * 10**9
                and entity["market_id"] not in self.host.markets
            ):
                continue
            mid = entity["market_id"]
            previous = self.host.markets.get(mid, {})
            entity = {**previous, **entity}
            if (
                mid not in self.host.markets
                and len(self.host.active_markets())
                >= self.host.profile.discovery.max_selected_markets
            ):
                await self.host.persist(
                    "expected",
                    entity["slug"],
                    {
                        "status": "CAPACITY_EXCLUDED",
                        "market_id": mid,
                        "window_start": entity["window_start"],
                        "observed_at": now,
                    },
                )
                continue
            entity.update(
                {
                    "first_seen_at": previous.get("first_seen_at", now),
                    "last_metadata_at": now,
                    "metadata_raw_id": raw_id,
                    "closed_at": (
                        (previous.get("closed_at") or now)
                        if entity["closed"] is True
                        else (None if entity["closed"] is False else previous.get("closed_at"))
                    ),
                    "resolution_next_at": previous.get("resolution_next_at", entity["window_end"]),
                    "resolution_status": previous.get("resolution_status", "PENDING"),
                }
            )
            await self.host.persist("markets", mid, entity)
            self.host.markets[mid] = entity
            await self.host.persist(
                "expected",
                entity["slug"],
                {
                    "status": "FOUND",
                    "market_id": mid,
                    "window_start": entity["window_start"],
                    "observed_at": now,
                },
            )
            if not previous:
                await self.host.bind_feeds(entity)

    async def _probe(self, slug: str, window_start: int, previous: dict) -> None:
        """A failed or empty probe must not downgrade a window already found."""
        try:
            document, raw = await self.host.http.read("gamma_markets", params={"slug": slug})
        except DurabilityError:
            raise
        except Exception as exc:
            await self.host.event(
                "discovery_probe_failed",
                slug=slug,
                reason=type(exc).__name__,
                preserved_status=previous.get("status"),
            )
            return
        if document:
            await self.ingest(document, raw.raw_record_id)
            return
        if previous.get("status") == "FOUND":
            await self.host.event("discovery_empty_probe", slug=slug, preserved_status="FOUND")
            return
        await self.host.persist(
            "expected",
            slug,
            {
                "status": "NOT_YET_LISTED",
                "window_start": window_start,
                "observed_at": raw.received_at,
            },
        )

    async def cycle(self) -> None:
        now_s = int(time.time())
        existing = self.host.catalog.values(self.host.collection, "expected")
        updates = []
        for slug, window_start in self.family.expected_windows(
            now_s, self.host.profile.discovery.lookahead_s
        ):
            if slug not in existing:
                updates.append(
                    {
                        "namespace": "expected",
                        "key": slug,
                        "value": {
                            "status": "DISCOVERY_UNCERTAIN",
                            "window_start": window_start,
                            "observed_at": time.time_ns(),
                            "reason": "expected_schedule_not_yet_observed",
                        },
                    }
                )
        await self.host.persist_batch(updates)
        probes, self.next_probe = self.family.probe_windows(
            now_s, self.host.profile.discovery.lookahead_s, self.next_probe
        )
        for slug, window_start in probes:
            previous = existing.get(slug, {})
            await self._probe(slug, window_start, previous)

    async def scan(self) -> None:
        # Bounded paginated scan; retain cursor/result evidence, and label incomplete scans.
        from datetime import datetime, timezone

        start = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
        end = datetime.fromtimestamp(
            time.time() + self.host.profile.discovery.lookahead_s, timezone.utc
        ).isoformat()
        cursor = None
        cursors = set()
        for page in range(100):
            params = {
                "limit": 100,
                "end_date_min": start,
                "end_date_max": end,
            }
            if cursor:
                params["after_cursor"] = cursor
            await self.host.persist(
                "tasks", "discovery_scan", {"status": "PENDING", "params": params}
            )
            try:
                document, raw = await self.host.http.read("gamma_keyset", params=params)
            except DurabilityError:
                raise
            except Exception as exc:
                await self.host.persist(
                    "tasks",
                    "discovery_scan",
                    {
                        "status": "DISCOVERY_UNCERTAIN",
                        "reason": type(exc).__name__,
                        "absence_assurance": "REQUEST_FAILED_NOT_PROOF_OF_ABSENCE",
                    },
                )
                return
            if not isinstance(document, dict) or not isinstance(document.get("markets"), list):
                raise ValueError("unexpected Gamma keyset envelope")
            await self.ingest(document["markets"], raw.raw_record_id)
            cursor = document.get("next_cursor")
            if not cursor:
                await self.host.persist(
                    "tasks",
                    "discovery_scan",
                    {
                        "status": "COMPLETE",
                        "raw_id": raw.raw_record_id,
                        "observed_at": raw.received_at,
                        "absence_assurance": "COMPLETE_KEYSET_SCAN_WITH_OBSERVATION_INTERVAL",
                    },
                )
                return
            if cursor in cursors:
                raise ValueError("repeated discovery cursor")
            cursors.add(cursor)
        await self.host.event("discovery_capacity", reason="scan_page_limit", pages=100)
