"""Non-guru architecture harness (P1).

Proves a generic :class:`tyrex_pm.signals.simple_signal.SimpleSignal` produces an
``EnterIntent`` through the same ``process_signals`` dispatch as guru copy.
"""

from tyrex_pm.strategies.simple_signal_test.strategy import SimpleSignalTestStrategy

__all__ = ["SimpleSignalTestStrategy"]
