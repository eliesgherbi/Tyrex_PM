"""Instrument readiness — ``execution_truth_alignment.md`` §7 / startup §8.2.5."""

from __future__ import annotations

from typing import Any

from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.lifecycle.instrument_policy import static_instruments_in_cache


class InstrumentReadinessPolicy:
    """
    Frozen rule: **no** live submit for a token until ``cache.instrument`` exists for that token’s
    resolved ``InstrumentId`` when the token is **statically** mapped in runtime YAML.

    **WS subscription / ``_maintain_active_market``** remain adapter-owned; Tyrex only enforces
    cache presence (same as §8.2.5 static list check at startup).
    """

    __slots__ = ("_runtime",)

    def __init__(self, runtime: RuntimeSettings) -> None:
        self._runtime = runtime

    def gate_ready(self, cache: Any) -> tuple[bool, str | None]:
        """Startup gate: same contract as :func:`static_instruments_in_cache` for configured ids."""
        return static_instruments_in_cache(
            cache,
            self._runtime.polymarket_instrument_ids,
        )

    def allow_submit(self, token_id: str, cache: Any) -> bool:
        """
        Live submit path: if ``token_id`` has a YAML map entry, require ``cache.instrument``.

        Dynamic-only tokens (no map entry) return ``True`` here — activation is handled in
        :class:`~tyrex_pm.execution.nautilus_guru_exec.NautilusGuruExecutionPort`.
        """
        rt = self._runtime
        if not rt.polymarket_instrument_ids:
            return True
        tid = str(token_id)
        instr_s = dict(rt.polymarket_token_to_instrument).get(tid)
        if instr_s is None:
            return True
        try:
            iid = InstrumentId.from_str(instr_s)
        except ValueError:
            return False
        return cache.instrument(iid) is not None
