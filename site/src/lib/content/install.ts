/**
 * Install surface.
 *
 * DECISION D2, verified against the repo: there is NO PyPI package. The only
 * honest instruction is a clone plus an editable install.
 * Writing `pip install vex` would be a fabricated claim - it is on the
 * DESIGN.md §5 ban list by name.
 *
 * DECISION D1: the console script is `vex`, with `harness` kept as a working
 * legacy alias. Both map to cli.main:main.
 * Source: pyproject.toml:20-23 ([project.scripts]).
 */

export const REPO_URL = "https://github.com/Pavanteja2007/coding-harness";

export const INSTALL_CLONE = {
  command: `git clone ${REPO_URL} && cd coding-harness && pip install -e .`,
  source: "pyproject.toml:20-23; README.md:161",
};

export const INSTALL_NO_INSTALL = {
  command:
    'python -m cli fix --repo . --issue "the bug report, in plain words"',
  note: "Runs without installing anything.",
  source: "README.md:165-168",
};

export const INSTALL_ALIAS = {
  command: "harness fix --repo . --issue \"...\"",
  note: "`harness` is a legacy alias kept working; it maps to the same entry point as `vex`.",
  source: "pyproject.toml:22-23",
};

/** Stated plainly, because its absence is the honest fact. */
export const NO_PYPI_NOTE = {
  text: "There is no PyPI package yet, so there is no pip install vex. Clone the repo and install it editable.",
  source: "pyproject.toml (no publish configuration)",
};
