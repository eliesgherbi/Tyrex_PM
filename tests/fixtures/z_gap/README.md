# Z-Gap offline fixtures

## Fair-value golden (`fair_value_golden.json`)

**Inputs** and **legacy_expected_*** values come from the trusted legacy fixture
`old/tests/fixtures/z_gap/fair_value_golden.json` (read-only provenance).

**Production expectations** are **not** stored as F2 `compute_fair_value` outputs.
Tests assert production against the independent dual in
`tests/oracles/binary_fair_value_oracle.py` (does not import indicators).

Legacy rounded Φ values may differ slightly from `math.erf`; tests document an
explicit legacy-tolerance check separately from the dual-oracle lock.

Active tests must not import `old/` or depend on legacy filesystem paths.
