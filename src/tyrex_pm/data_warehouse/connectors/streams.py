"""Reconnectable WS capture with bounded, durability-acknowledged projections."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from typing import Any

from ..config import Credentials
from ..contracts import canonical, decode

URLS = {
    "clob": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    "polybolt": "wss://ws-live-v2.polymarket.com/ws",
    "binance": "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth@100ms",
}


async def record_stream(
    host: Any,
    source: str,
    stream: str,
    *,
    tokens: Any = None,
    credentials: Credentials | None = None,
) -> None:
    import websockets

    delay = 1.0
    while not host.stop.is_set():
        connection = uuid.uuid4().hex
        tasks = []
        projection = None
        permanent = False
        try:
            async with websockets.connect(
                URLS[source],
                max_size=16 * 1024**2,
                max_queue=4,
                ping_interval=10,
                ping_timeout=10,
                open_timeout=10,
                close_timeout=5,
            ) as socket:
                if source == "polybolt":
                    if credentials is None:
                        raise ValueError("reference credentials unavailable")
                    await socket.send(
                        canonical({"op": "auth", "rid": "auth", "auth": credentials.auth()})
                    )
                    # Authentication exchange is deliberately outside the raw archive.
                    acknowledgement = decode(await asyncio.wait_for(socket.recv(), 10))
                    if acknowledgement.get("op") != "authed":
                        code = acknowledgement.get("code")
                        permanent = code in ("auth_invalid", "auth_required")
                        raise ValueError("reference authentication failed")
                    await socket.send(
                        canonical(
                            {
                                "op": "subscribe",
                                "rid": "btc",
                                "subscriptions": [
                                    {
                                        "channel": "price.crypto.twap",
                                        "filter": {"symbol": "btcusd", "window_seconds": 60},
                                    }
                                ],
                            }
                        )
                    )
                if source == "clob":
                    selected = list(tokens())
                    await socket.send(
                        canonical(
                            {
                                "assets_ids": selected,
                                "type": "market",
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    await host.event(
                        "subscription",
                        source=source,
                        connection_id=connection,
                        tokens=selected,
                        operation="subscribe",
                    )
                await host.event(
                    "connected", source=source, stream=stream, connection_id=connection
                )
                host.feed_health[stream] = {
                    "source": source,
                    "connected": True,
                    "connection_id": connection,
                    "last_frame_at": None,
                    "last_observation_at": None,
                }
                projection = asyncio.Queue(maxsize=8192)

                async def receive() -> None:
                    while not host.stop.is_set():
                        body = await asyncio.wait_for(
                            socket.recv(),
                            host.profile.reference.liveness_s if source == "polybolt" else 30,
                        )
                        now = time.time_ns()
                        raw, future = host.writer.submit(
                            body,
                            source=source,
                            stream=stream,
                            connection=connection,
                            received_at=now,
                        )
                        host.feed_health[stream]["last_frame_at"] = now
                        weight = len(body.encode() if isinstance(body, str) else body) * 2 + 4096
                        limit = host.profile.storage.capture_queue_mib * 1024**2
                        if host.projection_bytes + weight > limit:
                            raise RuntimeError("durable projection byte budget exceeded")
                        try:
                            projection.put_nowait((raw, future, weight))
                            host.projection_bytes += weight
                        except asyncio.QueueFull as exc:
                            raise RuntimeError("durable projection queue overflow") from exc

                async def project() -> None:
                    last_sequence = None
                    while True:
                        raw, future, weight = await projection.get()
                        try:
                            await asyncio.wrap_future(future)
                            try:
                                payload = decode(raw.bytes())
                            except ValueError:
                                continue
                            if source == "polybolt" and isinstance(payload, dict):
                                seq = payload.get("seq")
                                if seq is not None:
                                    if (
                                        last_sequence is not None
                                        and int(seq) != last_sequence + 1
                                        or payload.get("dropped", 0)
                                    ):
                                        await host.event(
                                            "sequence_gap",
                                            source=source,
                                            connection_id=connection,
                                            previous=last_sequence,
                                            current=seq,
                                        )
                                    last_sequence = int(seq)
                                if payload.get("op") == "error":
                                    raise ValueError("reference subscription error")
                            await host.observe(raw)
                        finally:
                            host.projection_bytes -= weight
                            projection.task_done()

                async def heartbeat() -> None:
                    nonlocal selected
                    while True:
                        await asyncio.sleep(10 if source != "clob" else 2)
                        if source == "clob":
                            await socket.send("PING")
                            current = list(tokens())
                            added, removed = (
                                sorted(set(current) - set(selected)),
                                sorted(set(selected) - set(current)),
                            )
                            for operation, members in (
                                ("subscribe", added),
                                ("unsubscribe", removed),
                            ):
                                if members:
                                    await host.event(
                                        "subscription",
                                        source=source,
                                        connection_id=connection,
                                        tokens=members,
                                        operation=operation,
                                    )
                                    await socket.send(
                                        canonical(
                                            {
                                                "assets_ids": members,
                                                "operation": operation,
                                                "custom_feature_enabled": True,
                                            }
                                        )
                                    )
                            selected = current
                        elif source == "polybolt":
                            await socket.send(canonical({"op": "ping"}))

                tasks = [
                    asyncio.create_task(receive()),
                    asyncio.create_task(project()),
                    asyncio.create_task(heartbeat()),
                    asyncio.create_task(host.stop.wait()),
                ]
                if source == "binance":
                    tasks.append(asyncio.create_task(host.bootstrap_binance(connection)))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not tasks[3]:
                        task.result()
                delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception text may contain server echoes; only bounded class identifiers are stored.
            await host.event(
                "source_error",
                source=source,
                stream=stream,
                reason=type(exc).__name__,
                permanent=permanent,
            )
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if projection is not None:
                while not projection.empty():
                    _, _, weight = projection.get_nowait()
                    host.projection_bytes -= weight
                    projection.task_done()
            host.feed_health[stream] = {
                "source": source,
                "connected": False,
                "connection_id": connection,
            }
            await host.event("disconnected", source=source, stream=stream, connection_id=connection)
        if permanent:
            await host.stop.wait()  # Other feeds continue with visible degraded health.
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(host.stop.wait(), random.uniform(1, delay))
        delay = min(host.profile.operations.reconnect_max_s, delay * 2)
