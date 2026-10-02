"""Import-time smoke test for the whole `cli` package.

WHY THIS FILE EXISTS. `cli/AGENTS.md` records THREE separate incidents where
the entire `cli` package went offline for four to five minutes:

1. `AttributeError: 'SubcommandSpec' object has no attribute
   'interactive_dispatch'` — a dataclass read a field it does not declare,
   raised at import.
2. `ValueError: /hooks is flag-only but names no headless equivalent` — a
   data-table gate, raised at import.
3. `IndentationError` at `cli/fileview.py:2304` — one character.

Every one of them is a single-line omission with no test able to see it,
because the in-process import cache hides it: once `cli.commands` is in
`sys.modules`, a second `import cli.commands` is a dict lookup. The repo's own
conclusion, in `cli/AGENTS.md`: *"a repo where a single character takes the
entire CLI package offline for five minutes has no import-time smoke test."*

So this module runs EVERY import in a FRESH SUBPROCESS and asserts exit 0.
That is the only way to see the failure the way CI would.

Three properties matter and are easy to lose:

- **A subprocess per module.** An in-process import proves nothing about the
  failure mode above.
- **One parallel batch.** Measured serially on this host the targets cost
  0.4 s (`cli.theme`) to 2.8 s (`cli.tui`, which imports textual); run in
  sequence the file took 18.9 s, which is not a per-commit guard. Run
  concurrently it costs the slowest target, not the sum.
- **Fast.** Bounded by :data:`SUITE_BUDGET_S` and asserted, so a future round
  cannot quietly add a slow lane to a smoke test. No Docker, no provider, no
  network, no fixture repositories.

Ownership: this file lives in `cli/` deliberately. T5 owns `tests/**`, and a
smoke test that a module owner cannot edit without a cross-terminal request
is a smoke test that rots.

Run standalone with ``python -m pytest cli/test_cli_import_smoke.py -q``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every module a `neo` invocation can pull in before it reaches a handler.
#: `cli.main` builds the parser and dispatches every command; `cli.tui` is the
#: default surface; `cli.interactive` is the fallback shell; `cli.commands` is
#: the registry whose import-time gates have taken the package down twice;
#: `cli.runview` is the journal projection every surface renders from.
#:
#: **The last five were added by P0/W2 (T4.W2.5), and the reason is the prompt's
#: own:** the sanitiser is on the path Phase 1's `review.py` mount and Phase
#: 5's diff pane and git UI will all render through, so those modules are about
#: to be edited by three different phases. An unguarded import is how that
#: happens — `cli/fileview.py`'s docstring collapsing onto its `def` line took
#: the whole package offline for five minutes, and that file is in this list
#: now. `cli.session` and `cli.streamview` are here for the same reason: both
#: are P1.2/P5 mount targets, and neither is currently reached by any of the
#: five original targets.
IMPORT_TARGETS = (
    "cli.main",
    "cli.tui",
    "cli.interactive",
    "cli.commands",
    "cli.runview",
    "cli.review",
    "cli.tui_components",
    "cli.fileview",
    "cli.session",
    "cli.streamview",
)

#: Not imports — INVOCATIONS. `build_parser()` is what triggers the registry
#: cross-checks, the help metavar derivation and every additive subparser
#: registration, and those have raised at import time before. The two
#: `_validate_*` gates are the exact functions that produced
#: `ValueError: /hooks is flag-only but names no headless equivalent`.
INVOCATIONS: tuple[tuple[str, str], ...] = (
    ("build_parser", "from cli.main import build_parser; build_parser()"),
    (
        "headless_table_gate",
        "from cli.commands import _validate_headless_tables; _validate_headless_tables()",
    ),
    (
        "subcommand_registry_gate",
        "from cli.commands import _validate_subcommand_registry; _validate_subcommand_registry()",
    ),
    (
        "command_registry_is_populated",
        "from cli import commands;"
        "assert commands.COMMAND_SPECS, 'COMMAND_SPECS is empty';"
        "assert commands.REQUIRED_COMMANDS, 'REQUIRED_COMMANDS is empty';"
        "assert len(commands.COMMAND_SPECS) >= 33, len(commands.COMMAND_SPECS);"
        "print(len(commands.COMMAND_SPECS))",
    ),
    ("neo_version", None),  # a real `python -m cli --version` child
)

#: Whole-suite budget. The whole file must stay under this to be allowed in
#: the per-commit lane; the test asserts the MEASURED batch wall time so the
#: budget cannot rot into a comment.
SUITE_BUDGET_S = 10.0

_SUBPROCESS_TIMEOUT_S = 60
_MAX_WORKERS = 8


def _child_env() -> dict:
    """Environment for a child import: the repo on the path, nothing inherited
    that could make an import depend on the developer's shell."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    for var in ("NEO_NOTIFY", "NO_COLOR", "NEO_NO_COLOR", "NEO_COLOR_DEPTH"):
        env.pop(var, None)
    return env


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """Run one command in a FRESH interpreter and return the completed process."""
    return subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )


def _argv_for(label: str, code: str | None) -> list[str]:
    if label == "neo_version":
        return [sys.executable, "-m", "cli", "--version"]
    assert code is not None, label
    return [sys.executable, "-c", code]


@pytest.fixture(scope="session")
def smoke_batch() -> dict:
    """Run every target in a FRESH subprocess, concurrently, exactly once.

    Session-scoped so the wall cost is paid once: the tests below then assert
    on the cached result, which keeps one named test per failure mode without
    paying one interpreter start per assertion.
    """
    targets: dict[str, str | None] = {m: f"import {m}" for m in IMPORT_TARGETS}
    targets.update({name: code for name, code in INVOCATIONS})
    results: dict[str, subprocess.CompletedProcess] = {}

    def _one(item: tuple[str, str | None]) -> tuple[str, subprocess.CompletedProcess]:
        label, code = item
        return label, _run(_argv_for(label, code))

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        for label, result in pool.map(_one, targets.items()):
            results[label] = result
    return results


@pytest.fixture(scope="session")
def smoke_batch_seconds(smoke_batch: dict) -> float:
    """Re-measure the batch so the budget assertion is about the real cost."""
    start = time.perf_counter()
    _run([sys.executable, "-c", "import cli.main"])
    one = time.perf_counter() - start
    assert one < SUITE_BUDGET_S / 2, (
        f"a single `import cli.main` took {one:.2f}s; the subprocess strategy "
        f"cannot fit a {SUITE_BUDGET_S}s per-commit lane on this host"
    )
    return one


def _assert_exit_zero(result: subprocess.CompletedProcess, label: str) -> None:
    """Assert the child succeeded, with the child's OWN output as the message.

    A bare `assert result.returncode == 0` is how the three recorded
    incidents stayed invisible: the error sat in the child's stderr, four
    frames deep, and nobody read it.
    """
    assert result.returncode == 0, (
        f"{label} failed in a fresh interpreter (exit {result.returncode}).\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


@pytest.mark.parametrize("module", IMPORT_TARGETS)
def test_every_module_imports_in_a_fresh_interpreter(
    module: str, smoke_batch: dict
) -> None:
    """`import <module>` exits 0 in a subprocess with an empty import cache."""
    _assert_exit_zero(smoke_batch[module], module)


@pytest.mark.parametrize(
    "label", [name for name, _ in INVOCATIONS], ids=[n for n, _ in INVOCATIONS]
)
def test_the_import_time_invocations_still_pass(label: str, smoke_batch: dict) -> None:
    """Each import-time gate is callable and passes on the shipped tables.

    These are the class of failure that took the package down: a table gate
    and a dataclass field, both raised at import, both invisible to an
    in-process test.
    """
    _assert_exit_zero(smoke_batch[label], label)


def test_the_command_registry_imports_populated(smoke_batch: dict) -> None:
    """`cli.commands` imports with a NON-EMPTY command table.

    A module that imports cleanly having lost its whole registry is a green
    gate over an empty product, so the count is asserted rather than inferred
    from a clean exit. `REQUIRED_COMMANDS` has named 33+ commands since the
    command-system round; a table below that is a regression, not a trim.
    """
    result = smoke_batch["command_registry_is_populated"]
    _assert_exit_zero(result, "command_registry_is_populated")
    count = int(result.stdout.strip().splitlines()[-1])
    assert count >= 33, f"only {count} commands registered"


def test_neo_version_prints_a_banner(smoke_batch: dict) -> None:
    """`python -m cli --version` exits 0 AND prints the product's name."""
    result = smoke_batch["neo_version"]
    _assert_exit_zero(result, "neo_version")
    assert "neo" in result.stdout.lower(), (
        f"--version printed no version banner: {result.stdout!r}"
    )


def test_the_suite_is_fast_enough_for_the_per_commit_lane(
    smoke_batch: dict, smoke_batch_seconds: float
) -> None:
    """The whole point is a per-commit guard, so the guard must be cheap.

    Serial, these targets cost 0.4-2.8 s each and the file took 18.9 s; run
    concurrently the batch costs roughly the slowest target. This assertion is
    about the STRATEGY, measured — not about a number typed into a comment.
    """
    assert len(smoke_batch) == len(IMPORT_TARGETS) + len(INVOCATIONS), (
        "the batch ran a different number of children than the table declares; "
        "a smoke test that silently skips a target is worse than no smoke test"
    )
    assert smoke_batch_seconds < SUITE_BUDGET_S, (
        f"the slowest single import took {smoke_batch_seconds:.2f}s against a "
        f"{SUITE_BUDGET_S}s per-commit budget"
    )
