/**
 * Install surface.
 *
 * SUPERSEDES the earlier "there is no published package" decision (D2). The
 * package IS published and installable:
 *
 *     pip install vex-harness     -> verified, pypi.org returns 200
 *
 * The distribution name and the command deliberately differ, which is the one
 * thing a reader will trip over, so the site states it plainly rather than
 * leaving it to be discovered: `vex` was taken on PyPI by an unrelated legacy
 * package, as were `vex-cli` and `vexx`/`pyvex`. `vex-harness` was the
 * available descriptive name. The installed COMMAND is still `vex` — the same
 * relationship beautifulsoup4 has with `bs4`.
 * Source: pyproject.toml:1-8 (the comment explaining the choice) and
 * pyproject.toml:52-55 ([project.scripts]).
 *
 * Deliberately NOT here: install.sh / install.cmd / install.ps1. Those exist
 * in the repository but are still in flight, so the site does not document
 * them. A published instruction that does not work yet is the same class of
 * error as a fabricated benchmark.
 */

export const REPO_URL = "https://github.com/Pavanteja2007/coding-harness";

/** The fastest path, and now the recommended one. */
export const INSTALL_PIP = {
  command: "pip install vex-harness",
  note: "Installs the `vex` command. The distribution is named vex-harness because `vex` was already taken on PyPI.",
  source: "pyproject.toml:8 (name), pyproject.toml:52-55 ([project.scripts])",
};

/** For working on vex itself, or running an unreleased revision. */
export const INSTALL_CLONE = {
  command: `git clone ${REPO_URL} && cd coding-harness && pip install -e .`,
  note: "An editable install from a clone. Use this to work on vex itself.",
  source: "pyproject.toml:52-55; README.md:161",
};

export const INSTALL_NO_INSTALL = {
  command:
    'python -m cli fix --repo . --issue "the bug report, in plain words"',
  note: "Runs from a clone without installing anything.",
  source: "README.md:165-168",
};

export const INSTALL_ALIAS = {
  command: 'harness fix --repo . --issue "..."',
  note: "`harness` is a legacy alias kept working; it maps to the same entry point as `vex`.",
  source: "pyproject.toml:54-55",
};

/**
 * The name/command mismatch, stated once so every surface can reuse it rather
 * than paraphrasing it differently.
 */
export const PACKAGE_NAME_NOTE = {
  text: "The package is vex-harness on PyPI; the command it installs is vex. The short name was already taken by an unrelated project.",
  source: "pyproject.toml:1-8",
};
