# Module: `tyrex_pm.indicator`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

Reserved for **technical / market indicators** (v1 interface placeholder in source).

## B. Boundaries

**Will belong here:** Reusable indicator computations that strategies or actors can call.

**Does not belong here:** Guru-specific copy rules (`signal/`) or CLOB I/O (`execution/`).

## C. Internal structure

`__init__.py` — stub only.

## D. Main interactions

None in v1 guru follow.

## E. Status

**Placeholder.**

## F. Extension guidance

Keep indicators **pure** (input series / bars → output series). If Nautilus indicator adapters are needed later, wrap them in thin classes without pulling Polymarket execution into this package.
