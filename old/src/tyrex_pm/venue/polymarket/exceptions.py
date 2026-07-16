"""Re-exports of Polymarket V2 SDK exceptions for the rest of Tyrex_PM.

Centralizing this here keeps ``py_clob_client_v2`` imports inside the
``venue/polymarket/`` adapter boundary, so a single test
(``tests/test_v2_import_isolation.py``) can enforce that no other module
takes a direct dependency on the venue SDK.

Modules outside ``venue/polymarket/`` should import ``PolyApiException``
from here, never from ``py_clob_client_v2.exceptions`` directly.
"""

from __future__ import annotations

from py_clob_client_v2.exceptions import PolyApiException

__all__ = ["PolyApiException"]
