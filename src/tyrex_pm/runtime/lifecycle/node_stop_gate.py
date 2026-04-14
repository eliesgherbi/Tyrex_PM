"""WP1 — idempotent :meth:`~nautilus_trader.live.node.TradingNode.stop` coordination.

**Stop ownership (stabilization WP1):** ``scripts/run_guru`` and
:class:`~tyrex_pm.runtime.lifecycle.coordinator.StartupReadinessCoordinator` share a single
:class:`NodeStopGate`. The **first** successful call to :meth:`stop_node` invokes ``node.stop()``
under the process-global latch; **later** calls are no-ops. This avoids undefined double-stop
ordering when the startup readiness worker stops the node on terminal ``NOT_READY`` and the main
thread later runs the ``finally`` block (drain already ran or runs first per ``run_guru``).

This does **not** change Nautilus internals; it only serializes Tyrex call sites.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

_LOG = logging.getLogger(__name__)


class NodeStopGate:
    """Thread-safe at-most-once ``node.stop()`` for Tyrex orchestration."""

    __slots__ = ("_lock", "_invoked")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._invoked = False

    def stop_node(self, node: Any, *, log: logging.Logger | None = None) -> None:
        """
        Invoke ``node.stop()`` once. Safe from multiple threads.

        Logs failures at WARNING and does not re-raise (matches ``run_guru`` ``finally`` behavior).
        """
        logger = log if log is not None else _LOG
        with self._lock:
            if self._invoked:
                logger.debug("event=node_stop_skipped reason=already_invoked")
                return
            self._invoked = True
        try:
            node.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("event=node_stop_failed error=%s", exc, exc_info=True)
