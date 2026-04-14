"""
§8.2.1 exec-client connectivity — Nautilus ``ExecutionEngine.check_connected``.

``startup_readiness.md`` §8.2(1): require all registered exec clients connected.
Pinned API: ``nautilus_trader.execution.engine.ExecutionEngine.check_connected`` (Cython),
which is true only when every client in ``_clients`` has ``is_connected``.

:class:`SpikePendingExecClientsConnected` remains the default for callers that inject no
live engine (tests / shadow-only paths).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecClientsConnected(Protocol):
    def __call__(self) -> bool:
        """True when live exec/data clients are connected per framework contract."""
        ...


class SpikePendingExecClientsConnected:
    """
    Placeholder: always **false** for live strict READY when no engine is wired.

    ``build_guru_trading_node`` uses :class:`NautilusExecEngineClientsConnected` for
    ``execution_mode: live`` instead.
    """

    __slots__ = ()

    def __call__(self) -> bool:
        return False


class NautilusExecEngineClientsConnected:
    """
    True when the live execution engine has at least one registered client and
    ``check_connected()`` is true (all of those clients report ``is_connected``).
    """

    __slots__ = ("_exec_engine",)

    def __init__(self, exec_engine: Any) -> None:
        self._exec_engine = exec_engine

    def __call__(self) -> bool:
        ee = self._exec_engine
        clients = getattr(ee, "_clients", None)
        try:
            if clients is None or len(clients) == 0:
                return False
        except TypeError:
            return False
        check = getattr(ee, "check_connected", None)
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            return False
