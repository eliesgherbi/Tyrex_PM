"""REMOVED — N7 operator-authorization ceremony.

The operator's deliberate execution of the live CLI (``--live``) is the
authorization. Envelopes, phrases, nonces, and chat approval artifacts are
obsolete and must not be used.

Invalidated historical envelope IDs (never reuse):

- ``b3a95919-73a7-43e6-a1d8-3876dd09c2b6``
- ``69cff32a-5f84-4b07-902e-dc56b7b93c80``
"""

from __future__ import annotations

INVALIDATED_ENVELOPE_IDS = frozenset(
    {
        "b3a95919-73a7-43e6-a1d8-3876dd09c2b6",
        "69cff32a-5f84-4b07-902e-dc56b7b93c80",
    }
)

_REMOVED = (
    "N7 authorization ceremony removed. "
    "Run: python tools/n7_live/run_n7_live_oneshot.py --live "
    "(operator invocation is the authorization)."
)


def create_authorization_request(*_a, **_k):  # noqa: ANN001
    raise RuntimeError(_REMOVED)


def write_authorization_request(*_a, **_k):  # noqa: ANN001
    raise RuntimeError(_REMOVED)


def make_test_envelope(*_a, **_k):  # noqa: ANN001
    raise RuntimeError(_REMOVED)


def build_approval_phrase(*_a, **_k):  # noqa: ANN001
    raise RuntimeError(_REMOVED)
