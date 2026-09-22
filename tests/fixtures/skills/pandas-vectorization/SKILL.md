---
name: pandas-vectorization
description: Use when the issue or repo involves pandas DataFrames, Series, apply/map operations, NaN handling, or vectorized numeric bugs. Best practices for pandas fix tasks.
---

# pandas vectorization conventions

- Replace iterrows/apply loops with vectorized operations when the
  bug is in the loop's per-row logic — the vectorized form also makes
  the off-by-one/index-alignment class of bug visible.
- NaN semantics: any comparison with NaN is False; use `isna()`/`
  notna()` rather than `== float('nan')` equality checks.
- Index alignment is the top silent-bug source: an arithmetic op on
  two Series aligns on index, not position — `df.reset_index(drop=
  True)` when positional semantics are intended.
- Chained assignment (`df.loc[...]['col'] = x`) silently does
  nothing under copy-on-write; assign through a single `.loc` call.
