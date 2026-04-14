"""§8.2.1 — :class:`~tyrex_pm.runtime.lifecycle.exec_predicate.NautilusExecEngineClientsConnected`."""

from __future__ import annotations

from unittest.mock import MagicMock

from tyrex_pm.runtime.lifecycle.exec_predicate import NautilusExecEngineClientsConnected


def test_predicate_false_when_no_clients_registered() -> None:
    eng = MagicMock()
    eng._clients = {}
    eng.check_connected = MagicMock(return_value=True)
    assert NautilusExecEngineClientsConnected(eng)() is False
    eng.check_connected.assert_not_called()


def test_predicate_false_when_check_connected_false() -> None:
    eng = MagicMock()
    eng._clients = {"a": object()}
    eng.check_connected = MagicMock(return_value=False)
    assert NautilusExecEngineClientsConnected(eng)() is False


def test_predicate_true_when_clients_and_check_connected() -> None:
    eng = MagicMock()
    eng._clients = {"a": object()}
    eng.check_connected = MagicMock(return_value=True)
    assert NautilusExecEngineClientsConnected(eng)() is True


def test_predicate_false_when_check_connected_missing() -> None:
    eng = MagicMock(spec=["_clients"])
    eng._clients = {"a": object()}
    assert NautilusExecEngineClientsConnected(eng)() is False
