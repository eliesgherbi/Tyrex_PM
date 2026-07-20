# Z-Gap offline fixtures (F2)

`fair_value_golden.json` inputs (`S`, `K`, `sigma`, `τ`, not-ready cases) were
recovered from the trusted legacy fixture
`old/tests/fixtures/z_gap/fair_value_golden.json` (read-only provenance).

Expected `z` / `p_UP` / `p_DOWN` for ready cases were **independently recomputed**
in F2 using `math.log` + `math.erf` (`normal_cdf`). Legacy fixture probabilities
used a slightly different rounded CDF approximation; F2 locks the erf semantics.

Active tests load **only** this path under `tests/fixtures/z_gap/`.
They must not import `old/` or depend on legacy filesystem paths.
