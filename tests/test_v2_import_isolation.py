"""Phase 9: import-isolation guard for the V2 venue adapter boundary.

Two invariants the V2 migration plan locks in (see Docs/Implementation/V2_migration_plan.md §7 Phase 9):

1. **No V1 SDK imports anywhere in ``src/tyrex_pm/``.** ``py-clob-client`` (the V1
   SDK) was uninstalled and replaced by ``py-clob-client-v2``. A regression that
   reintroduces ``import py_clob_client`` would silently drag the V1 SDK back in
   on developer machines that still have it cached.

2. **No direct ``py_clob_client_v2`` imports outside ``src/tyrex_pm/venue/polymarket/``.**
   The venue adapter is the only module group allowed to touch the V2 SDK
   directly. Other modules that need V2 types (notably the ``PolyApiException``
   catch in ``runtime/pipeline.py``) consume them via the venue re-export at
   ``tyrex_pm.venue.polymarket.exceptions`` so the SDK boundary stays one
   directory deep.

The test scans ``src/tyrex_pm/`` source files (no AST parse — plain text scan
is intentional so a hidden ``importlib.import_module("py_clob_client")`` is not
considered "fine").
"""

from __future__ import annotations

import re
from pathlib import Path


def _src_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[1] / "src" / "tyrex_pm"


def _venue_polymarket_root() -> Path:
    return _src_root() / "venue" / "polymarket"


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# Matches:
#   import py_clob_client
#   import py_clob_client.something
#   from py_clob_client import ...
#   from py_clob_client.exceptions import ...
# but NOT:
#   import py_clob_client_v2
#   from py_clob_client_v2.exceptions import ...
_V1_IMPORT_RE = re.compile(
    r"""(?xm)
    ^\s*                                  # line start, optional indent
    (?:
        from\s+py_clob_client(?:\.\S+)?\s+import\s+
      | import\s+py_clob_client(?:\.\S+)?(?!\w)  # not followed by alnum / underscore (excludes _v2)
    )
    """
)

_V2_IMPORT_RE = re.compile(
    r"""(?xm)
    ^\s*
    (?:
        from\s+py_clob_client_v2(?:\.\S+)?\s+import\s+
      | import\s+py_clob_client_v2(?:\.\S+)?
    )
    """
)


def test_no_v1_sdk_imports_in_src() -> None:
    """``py_clob_client`` (V1) must not be imported anywhere in production code."""
    offenders: list[str] = []
    for path in _iter_python_files(_src_root()):
        text = path.read_text(encoding="utf-8")
        for m in _V1_IMPORT_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(_src_root().parent.parent)}:{line_no}: {m.group(0).strip()}")
    assert not offenders, (
        "V1 SDK (`py_clob_client`) is no longer allowed in src/. Offending imports:\n  "
        + "\n  ".join(offenders)
    )


def test_v2_sdk_imports_only_inside_venue_polymarket() -> None:
    """Only ``src/tyrex_pm/venue/polymarket/`` may import ``py_clob_client_v2`` directly."""
    venue_root = _venue_polymarket_root().resolve()
    offenders: list[str] = []
    for path in _iter_python_files(_src_root()):
        try:
            path.resolve().relative_to(venue_root)
            continue  # inside venue/polymarket/ — allowed
        except ValueError:
            pass
        text = path.read_text(encoding="utf-8")
        for m in _V2_IMPORT_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(_src_root().parent.parent)}:{line_no}: {m.group(0).strip()}")
    assert not offenders, (
        "py_clob_client_v2 imports are only allowed under src/tyrex_pm/venue/polymarket/. "
        "Other modules must consume V2 types via the venue re-exports "
        "(e.g. tyrex_pm.venue.polymarket.exceptions). Offending imports:\n  "
        + "\n  ".join(offenders)
    )


def test_polyapiexception_reexport_is_the_v2_class() -> None:
    """The venue re-export must be the same class that the V2 SDK ships.

    Defensive: catches a future regression where someone replaces the
    re-export with a local stub or a typo introduces a name collision.
    """
    from py_clob_client_v2.exceptions import PolyApiException as SdkExc
    from tyrex_pm.venue.polymarket.exceptions import PolyApiException as ReExc

    assert ReExc is SdkExc


def test_pipeline_uses_venue_reexport_not_direct_v2_import() -> None:
    """Spot-check the Phase 9 refactor: ``runtime.pipeline`` consumes
    ``PolyApiException`` via the venue re-export, never via the V2 SDK directly.

    The two regex-based tests above already cover this in aggregate; this
    test exists to keep the specific reason for the re-export discoverable
    (a regression that reintroduces ``from py_clob_client_v2.exceptions import``
    at the top of ``pipeline.py`` should fail here with a clear message).
    """
    pipeline_path = _src_root() / "runtime" / "pipeline.py"
    text = pipeline_path.read_text(encoding="utf-8")
    assert "from tyrex_pm.venue.polymarket.exceptions import PolyApiException" in text, (
        "runtime/pipeline.py must import PolyApiException via the venue "
        "re-export (tyrex_pm.venue.polymarket.exceptions), not directly from "
        "py_clob_client_v2 — see Docs/Implementation/V2_migration_plan.md §7 Phase 9."
    )
    assert "from py_clob_client_v2" not in text, (
        "runtime/pipeline.py reintroduced a direct py_clob_client_v2 import. "
        "Route it through tyrex_pm.venue.polymarket.* instead."
    )
