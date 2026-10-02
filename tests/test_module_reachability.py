"""VEX-PF-10 - reachability: a mechanism nothing imports cannot fire.

## The defect class this file exists to close

Three mechanisms shipped with ZERO production callers and were still dark:

- ``execution.flake_gate`` - the repetition layer that makes the flake gate
  capable of firing. With the shipped default of one repetition,
  ``flaky = len(set(outcomes)) > 1`` was unsatisfiable, so a test that passed
  once and failed later was reported as a clean, non-flaky pass. The gate was
  present in the vocabulary and unreachable in the code.
- ``execution/baseline_set`` - the recorded pre-existing failure set and the
  environment triage. "Was this test already broken before I touched it?" was
  never answered, and an environment failure looked exactly like a code
  failure.
- the verification gate on ``harness/agent_loop.py`` - the general agent's one
  declared-test run never carried any configuration, so none of the
  verification intelligence could reach the daily interactive session.

This is the THIRD time this project has shipped a mechanism nothing imports. A
comment saying "wired" is not evidence, and a passing test that calls the
module directly is not evidence either: the module was reachable FROM A TEST
and from nothing else. So the property is pinned at the level it was violated -
the import graph.

## What the gate asserts

Every production module in the package tree must have at least one importer
that is itself a production file. An importer in ``tests/`` or ``evals/`` does
NOT count: a module that only its own test names is precisely the defect.

Two exemptions exist, and each is narrow on purpose:

1. **A derived entry point.** A module that is LAUNCHED rather than imported: a
   ``__main__.py``, a module with an ``if __name__ == "__main__"`` guard, or a
   ``[project.scripts]`` target in ``pyproject.toml``. Derived from the tree
   and the manifest, so it cannot go stale the way a hand-kept name list does.
2. **A recorded orphan**, in :data:`RECORDED_UNREACHED`, with a mandatory
   non-empty reason. This is a reviewed backlog, not a licence: a NEW orphan
   fails, and an entry that has since GAINED an importer also fails, so the
   set can only shrink.

Three properties make the gate non-vacuous, and each is asserted by its own
test against a synthetic tree: a genuinely orphaned module IS detected, a
module imported ONLY from ``tests/`` is still reported as an orphan, and an
unparseable file stays in the graph instead of vanishing from it.

## The resolver's two semantics, pinned by their own tests

- **A dotted import reaches every parent package.** ``from acp.client import X``
  executes ``acp/__init__.py``, so ``acp`` is reached. Without this the gate
  would report every package root in the tree as an orphan, which is a false
  positive big enough to make the gate untrustworthy - and a gate people learn
  to ignore is not a gate.
- **An unparseable file is still a module.** It is scanned into the module map
  and its importers are still recorded by the files that name it, so a
  ``SyntaxError`` cannot silently delete a module. ``head_iv.py`` in this tree
  is UTF-16, so the reader decodes a BOM before anything else.

## The wiring this file also proves

A reachable import nobody triggers is still a dead mechanism, so the BEHAVIOUR
is pinned too, through the real ``execution.verify.verify`` with only the
container round trip replaced: the flake gate fires at its configured
repetition count, the baseline set is written and read back, the rung is named
on the receipt, and the baseline-set fold can only ever CLEAR a boolean.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every package directory that ships. The scan is an explicit list rather than
#: a glob, so a new top-level directory cannot silently escape the gate.
PRODUCTION_DIRECTORIES: Tuple[str, ...] = (
    "acp",
    "agent_sdk",
    "cli",
    "dashboard",
    "evals",
    "execution",
    "extensions",
    "harness",
    "integrations",
    "mcp_server",
    "memory",
    "recipes",
    "runtime",
    "shared",
)

#: Root-level modules that ship.
PRODUCTION_ROOT_FILES: Tuple[str, ...] = ("head_iv.py",)

#: Directories whose files are NOT production. An importer in one of these is
#: recorded separately and never counts toward reachability.
NON_PRODUCTION_DIRECTORIES: Tuple[str, ...] = ("tests", "evals", "demo", "scripts")

_IGNORED_DIR_NAMES = {"__pycache__", "node_modules", ".git"}


# ---------------------------------------------------------------------------
# the reachability scan
# ---------------------------------------------------------------------------


def _read_source(path: Path) -> str | None:
    """Return a module's source text, or None when it cannot be read at all.

    A UTF-16 BOM is decoded FIRST: this tree has shipped a UTF-16 root module
    (``head_iv.py``), and a reader that strips null bytes before choosing an
    encoding sees half a code unit and reports a bogus ``SyntaxError`` for a
    file that is fine.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _module_name(path: Path, root: Path) -> str:
    """Return the dotted module name for ``path`` relative to ``root``."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_entry_point(path: Path, source: str | None) -> bool:
    """Whether this module is LAUNCHED rather than imported."""
    if path.name == "__main__.py":
        return True
    return bool(
        source and re.search(r'if\s+__name__\s*==\s*["\']__main__["\']', source)
    )


def _console_script_targets(root: Path) -> Set[str]:
    """Return the dotted targets of ``[project.scripts]`` in ``pyproject.toml``."""
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    targets: Set[str] = set()
    for match in re.finditer(r"^\[project\.scripts\]\s*$", text, re.M):
        body = text[match.end() :].split("\n[", 1)[0]
        for line in body.splitlines():
            row = re.match(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"([^"]+)"\s*$', line)
            if row:
                targets.add(row.group(2).split(":")[0].strip())
    return targets


def _mentioned_modules(path: Path, root: Path) -> Set[str]:
    """Return every dotted module name ``path`` mentions importing.

    Relative imports are resolved against the importing module's own package,
    because this tree uses ``from .x import y`` throughout. A
    ``from pkg import mod`` contributes BOTH ``pkg`` and ``pkg.mod``: the first
    because importing a submodule executes the package's ``__init__``, the
    second because that is the name the caller meant.
    """
    source = _read_source(path)
    if source is None:
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    here = _module_name(path, root).split(".")
    package = here if path.name == "__init__.py" else here[:-1]
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = (
                    package[: len(package) - (node.level - 1)]
                    if node.level > 1
                    else package
                )
                stem = ".".join(
                    base + ((node.module or "").split(".") if node.module else [])
                )
                found.add(stem)
                for alias in node.names:
                    found.add(f"{stem}.{alias.name}")
            elif node.module:
                found.add(node.module)
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


def _with_parents(dotted: str, modules: Dict[str, Path]) -> List[str]:
    """Return every real module ``dotted`` names, from longest to shortest.

    ``import acp.client`` executes ``acp/__init__.py``, so the parent is a real
    importer edge. Without this the gate reports every package root in the tree
    as an orphan - a false positive big enough to make the gate untrustworthy.
    """
    parts = dotted.split(".")
    return [
        ".".join(parts[:cut])
        for cut in range(len(parts), 0, -1)
        if ".".join(parts[:cut]) in modules
    ]


def scan_reachability(root: Path) -> Dict[str, object]:
    """Return the whole reachability picture for the tree at ``root``.

    The return value is the audit record, not just a verdict:

    ``orphans``
        production modules with no production importer, minus the derived entry
        points.
    ``importers`` / ``test_only_importers``
        the production and NON-production importer set of every module, so
        "this module has three importers and all three are tests" is visible
        rather than implied.
    ``unparsed``
        production MODULES whose source could not be parsed. They stay in the
        module map (a ``SyntaxError`` must not delete a module from the graph)
        and are reported so a broken file is never silently excused.

    ``entry_points``
        the derived exemption set.
    """
    production: List[Path] = []
    # A package is any top-level directory carrying an ``__init__.py``, in
    # addition to the declared list. That keeps the "a new top-level directory
    # cannot silently escape the gate" property true without the gate depending
    # on somebody remembering to edit it.
    declared = list(PRODUCTION_DIRECTORIES)
    for child in sorted(root.iterdir() if root.is_dir() else []):
        if (
            child.is_dir()
            and (child / "__init__.py").exists()
            and child.name not in declared
        ):
            declared.append(child.name)
    for directory in declared:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part in _IGNORED_DIR_NAMES for part in path.parts):
                continue
            production.append(path)
    for name in PRODUCTION_ROOT_FILES:
        candidate = root / name
        if candidate.exists():
            production.append(candidate)

    modules = {_module_name(path, root): path for path in production}
    importers: Dict[str, Set[str]] = {name: set() for name in modules}
    test_only: Dict[str, Set[str]] = {name: set() for name in modules}
    unparsed: List[str] = []

    for path in production:
        name = _module_name(path, root)
        source = _read_source(path)
        if source is not None:
            try:
                ast.parse(source)
            except SyntaxError:
                unparsed.append(name)

        for dotted in _mentioned_modules(path, root):
            for target in _with_parents(dotted, modules):
                if target != name:
                    importers[target].add(name)

    production_set = set(production)
    for directory in NON_PRODUCTION_DIRECTORIES:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path in production_set:
                continue
            name = _module_name(path, root)
            for dotted in _mentioned_modules(path, root):
                for target in _with_parents(dotted, modules):
                    test_only[target].add(f"<non-production:{name}>")

    entry_points = {
        name
        for name, path in modules.items()
        if _is_entry_point(path, _read_source(path))
    }
    entry_points |= {name for name in _console_script_targets(root) if name in modules}

    orphans = sorted(
        name for name in modules if not importers[name] and name not in entry_points
    )
    return {
        "modules": modules,
        "orphans": orphans,
        "importers": importers,
        "test_only_importers": test_only,
        "unparsed": sorted(unparsed),
        "entry_points": sorted(entry_points),
    }


# ---------------------------------------------------------------------------
# the reviewed backlog
# ---------------------------------------------------------------------------

#: Modules with no production importer that are NOT entry points, each with the
#: reason it is still dark. This is a backlog somebody has to argue for, not an
#: exemption list: ``test_no_recorded_exemption_may_be_stale`` fails the moment
#: an entry gains an importer, and a NEW orphan fails
#: ``test_every_production_module_has_a_production_importer``. Every reason
#: names an owner, a shipped handoff, or what the module IS - never a bare
#: "TODO".
RECORDED_UNREACHED: Dict[str, str] = {
    "acp.protocol": (
        "compatibility re-export shim left behind by the acp models/transport "
        "rename; acp/__init__ re-exports the same names from acp.models and "
        "acp.transport, so this is a duplicate surface nobody names"
    ),
    "cli.fixtures.smoke_repo.mathutil": (
        "FIXTURE, not a module: a demo repository shipped as package data so the "
        "install/doctor smoke flows have something to install. It is meant to be "
        "read as a repository, never imported by the package"
    ),
    "cli.fixtures.smoke_repo.tests.test_mathutil": (
        "FIXTURE, not a module: the failing test of the shipped smoke repository "
        "above; same reason as cli.fixtures.smoke_repo.mathutil"
    ),
    "cli.models": (
        "the model-picker state machine. Its surfaces are cli/tui.py and "
        "cli/interactive.py, which belong to Prompt 01 and were being edited "
        "concurrently, so this terminal may not wire it - see the 'Handoff to "
        "01' section of the round's AGENTS.md entries"
    ),
    "extensions.skill_policy": (
        "VEX-CEILING-12 shipped it and filed the wiring as a request to the "
        "planning-path owner; nothing calls resolve_permissions/explain_selection "
        "from production, so a skill declaration is parsed and never enforced"
    ),
    "harness.approver": (
        "AGT-07 shipped ApproverAgent and filed the wiring as a request to the "
        "cli/interactive.py and cli/tui.py owners. The verifier rung now reaches "
        "the agent loop; the APPROVER does not"
    ),
    "harness.router": (
        "the Modes-round router, superseded by harness/agent_kernel's own "
        "resolve_agent_strategy as the single dispatch authority. Kept as a "
        "compatibility surface for external callers; nothing in-tree names it"
    ),
    "head_iv": (
        "a root-level interactive module another terminal is writing (UTF-16, "
        "untracked, no __main__ guard and no importer yet). NOT a deliberate "
        "exemption - it is expected to be wired or deleted, and this entry fails "
        "the moment it gains an importer"
    ),
    "memory.checkpoint_store": (
        "compatibility re-export shim left behind by the memory/checkpoints.py "
        "extraction; nothing in-tree names it"
    ),
    "recipes.builtin": (
        "bundled recipe DOCUMENTS (data), not a module: the directory ships "
        "portable recipe files and its __init__ only names them"
    ),
    "recipes.core": (
        "compatibility facade for the recipe implementation after the "
        "resolve/loader/validation split; no in-tree importer"
    ),
    "recipes.execution": (
        "public export shim for the recipe runner after the resolve/loader/"
        "validation split; no in-tree importer"
    ),
    "recipes.loader": (
        "public export shim for the recipe YAML loader; recipes/__init__ imports "
        "recipes._yaml directly, so this duplicate name is dark"
    ),
    "recipes.resolve": (
        "public export shim for recipe resolution; recipes/__init__ imports "
        "recipes.resolver directly, so this duplicate name is dark"
    ),
    "recipes.validation": (
        "public export shim for recipe validation; recipes/__init__ imports "
        "recipes.validator directly, so this duplicate name is dark"
    ),
    "recipes.yaml_loader": (
        "compatibility re-export shim for the strict recipe YAML loader; "
        "recipes/__init__ imports recipes._yaml directly"
    ),
    "shared.platform": (
        "another terminal's in-flight platform-parity helpers (untracked, and it "
        "appeared while this suite was running - which is exactly what the gate "
        "is for). Its owner is expected to wire it; this entry fails the day it "
        "gains an importer"
    ),
    "shared.security_corpus": (
        "a deterministic adversarial CORPUS for trust-boundary regressions; the "
        "regression tests that drive it are the consumer, and a test is not a "
        "production importer"
    ),
    "shared.threat_model": (
        "a machine-readable threat model (documentation as data); the trust "
        "checks that would consume it are not wired"
    ),
}

#: The two dark MODULES this round wired. Asserted reachable so the gate can
#: never be satisfied by the code going back to sleep.
THE_MODULES_THAT_WERE_DARK: Tuple[str, ...] = (
    "execution.baseline_set",
    "execution.flake_gate",
)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_every_production_module_has_a_production_importer() -> None:
    """The gate. A module nothing in production imports cannot fire."""
    scan = scan_reachability(REPO_ROOT)
    orphans = list(scan["orphans"])
    unrecorded = [name for name in orphans if name not in RECORDED_UNREACHED]
    assert not unrecorded, (
        f"{len(unrecorded)} production module(s) have NO production importer and "
        f"are not recorded in RECORDED_UNREACHED: {unrecorded}\n"
        f"{len(scan['modules'])} production modules scanned; "
        f"{len(scan['entry_points'])} entry points derived and auto-exempt.\n"
        "A module nothing imports cannot fire. Either wire it at its real call "
        "site, or record it in RECORDED_UNREACHED with a reason. An importer in "
        f"tests/ or evals/ does not count."
    )


def test_no_recorded_exemption_may_be_stale() -> None:
    """A recorded orphan that gained an importer must be deleted.

    Otherwise a real orphan hides behind a bookkeeping line, which is how a
    backlog quietly turns into a licence.
    """
    scan = scan_reachability(REPO_ROOT)
    orphans = set(scan["orphans"])
    importers = scan["importers"]
    stale = sorted(
        name
        for name in RECORDED_UNREACHED
        if name not in orphans and importers.get(name)
    )
    missing = sorted(name for name in RECORDED_UNREACHED if name not in scan["modules"])
    assert not stale, (
        "RECORDED_UNREACHED names modules that now HAVE a production importer; "
        f"delete those entries: {stale}"
    )
    assert not missing, (
        f"RECORDED_UNREACHED names modules that no longer exist: {missing}"
    )


def test_every_recorded_exemption_states_a_reason() -> None:
    """A blank reason is a bare exemption wearing a note."""
    blank = sorted(name for name, why in RECORDED_UNREACHED.items() if not why.strip())
    assert not blank, f"RECORDED_UNREACHED entries with no reason: {blank}"
    assert list(RECORDED_UNREACHED) == sorted(RECORDED_UNREACHED), (
        "RECORDED_UNREACHED must stay sorted so a diff shows only real changes"
    )


def test_the_modules_that_were_dark_are_reachable() -> None:
    """The two modules this round fixed must stay fixed.

    The message reports each module's TEST-ONLY importers, because "reachable
    from its own test and nothing else" is exactly the state this round found.
    """
    scan = scan_reachability(REPO_ROOT)
    importers = scan["importers"]
    test_only = scan["test_only_importers"]
    dark = []
    for name in THE_MODULES_THAT_WERE_DARK:
        if not importers.get(name):
            dark.append(
                f"{name}: 0 production importers "
                f"(test-only: {sorted(test_only.get(name, set()))})"
            )
    assert not dark, (
        "these modules shipped with zero production callers and must stay wired: "
        f"{dark}"
    )


def test_every_production_file_parses() -> None:
    """A file this scanner could not read is reported, not silently excused."""
    scan = scan_reachability(REPO_ROOT)
    assert not scan["unparsed"], (
        "these production files do not parse, so the scanner cannot conclude "
        f"anything about their importers: {scan['unparsed']}"
    )


def test_a_genuinely_orphaned_module_is_detected(tmp_path: Path) -> None:
    """Non-vacuity: the scanner must be able to FAIL."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "dark.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "consumer.py").write_text("VALUE = 2\n", encoding="utf-8")
    scan = scan_reachability(tmp_path)
    assert "pkg.dark" in scan["orphans"], scan["orphans"]


def test_a_test_only_import_does_not_count_as_production(tmp_path: Path) -> None:
    """A module only its own test names IS the defect, so it must be reported."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "orphan.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_it.py").write_text(
        "from pkg import orphan\n\n\ndef test_it():\n    assert orphan.VALUE == 1\n",
        encoding="utf-8",
    )
    scan = scan_reachability(tmp_path)
    assert "pkg.orphan" in scan["orphans"], (
        "a module imported ONLY from tests/ was not reported as an orphan; the "
        "gate would have been satisfied by exactly the defect it exists to catch"
    )
    assert sorted(scan["test_only_importers"]["pkg.orphan"]) == [
        "<non-production:tests.test_it>"
    ], scan["test_only_importers"]["pkg.orphan"]


def test_importing_a_submodule_reaches_its_parent_package(tmp_path: Path) -> None:
    """Python executes the parent __init__, so the parent IS imported."""
    (tmp_path / "outer" / "inner").mkdir(parents=True)
    (tmp_path / "outer" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "outer" / "inner" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "outer" / "inner" / "leaf.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (tmp_path / "outer" / "consumer.py").write_text(
        "from outer.inner import leaf\n", encoding="utf-8"
    )
    scan = scan_reachability(tmp_path)
    assert "outer" not in scan["orphans"], (
        "a package whose submodule is imported must not be reported as an "
        "orphan; without parent accounting the gate cries wolf on every package "
        "root in the tree"
    )


def test_an_unparseable_production_file_is_still_reported(tmp_path: Path) -> None:
    """A SyntaxError must not silently delete a module from the audit."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "broken.py").write_text("def (:\n", encoding="utf-8")
    (tmp_path / "pkg" / "consumer.py").write_text("VALUE = 2\n", encoding="utf-8")
    scan = scan_reachability(tmp_path)
    assert "pkg.broken" in scan["unparsed"], scan["unparsed"]


# ---------------------------------------------------------------------------
# the wiring itself (a reachable import nobody triggers is still dead)
# ---------------------------------------------------------------------------


def _fake_verify(tmp_path, monkeypatch, *, gates, rung_config, phase="", run_dir=""):
    """Drive the REAL ``execution.verify.verify`` with a scripted runner.

    Only the container round trip is replaced. The repetition count, the verdict
    reduction, the baseline-set fold and the rung receipt are all computed by
    the production functions, which is why this is host-only evidence and why it
    is still evidence about the SEAM rather than about a module's own tests.

    Returns ``(result, reports)``; ``reports`` is the real ``reports`` sink, so a
    test can read the gate rows the seam wrote rather than a re-derivation.
    """
    from execution import verify as verify_mod
    from execution.result_parsing import TestRunReport
    from shared.types import ExecutionResult

    pending = list(gates)

    def fake_run_tests(repo_path, command, eco, **kwargs):
        gate = pending.pop(0) if pending else "pass"
        exit_code = 0 if gate == "pass" else 1
        raw = f"$ {command}\nexit={exit_code}\n1 passed\n"
        return verify_mod._EcosystemRun(
            command=command,
            dispatched=command,
            result=ExecutionResult(exit_code, raw, "", False),
            report=TestRunReport(
                outcome="pass" if gate == "pass" else "fail",
                exit_code=exit_code,
                source="prose",
                confidence="high",
            ),
            gate=gate,
            reason="",
            cases=(),
        )

    monkeypatch.setattr(verify_mod, "_run_tests", fake_run_tests)
    monkeypatch.setattr(verify_mod, "_ecosystem_for", lambda repo_path: None)
    reports = []
    kwargs = {
        "test_command": "python -m pytest -q",
        "verify_timeout_s": 30,
        "rung_config": rung_config,
        "reports": reports,
    }
    if phase:
        kwargs["phase"] = phase
    if run_dir:
        kwargs["run_dir"] = run_dir
    outcome = verify_mod.verify(tmp_path, "tests/test_x.py::test_a", 1, **kwargs)
    return outcome, reports


def test_the_flake_gate_fires_at_its_configured_repetition_count(
    tmp_path, monkeypatch
) -> None:
    """A genuinely alternating series must reach flaky_detected ON the live path.

    The control matters as much as the proof: the SAME two labels at ONE
    repetition must report ``not_run``, which is what makes the two-repetition
    verdict mean "stable" rather than "unchecked".
    """
    from execution.flake_gate import (
        FLAKE_CHECK_VALUES,
        FLAKE_DETECTED,
        NOT_RUN,
        evidence_of,
    )

    alternating, _reports = _fake_verify(
        tmp_path,
        monkeypatch,
        gates=["pass", "fail"],
        rung_config={"post_fix_reruns": 2},
    )
    assert alternating.flaky is True
    assert alternating.flake_check == FLAKE_DETECTED
    assert alternating.flake_check in FLAKE_CHECK_VALUES
    assert alternating.repetitions == 2
    assert list(alternating.observed_outcomes) == ["pass", "fail"]
    assert evidence_of(alternating).detection_possible is True, (
        "the receipt a consumer reads must say stability was TESTED"
    )

    single, _reports = _fake_verify(
        tmp_path,
        monkeypatch,
        gates=["pass", "fail"],
        rung_config={"post_fix_reruns": 1},
    )
    assert single.flaky is False, "one repetition cannot be flaky"
    assert single.flake_check == NOT_RUN, (
        "a single repetition reported a flake verdict; 'we did not check' must "
        "never render as 'we checked and it was stable'"
    )


def test_the_no_config_path_is_byte_identical(tmp_path, monkeypatch) -> None:
    """Pinned by BEHAVIOUR, not by a comment: no key means no receipt, no change."""
    from execution.flake_gate import evidence_of

    without, _reports = _fake_verify(
        tmp_path, monkeypatch, gates=["pass", "pass"], rung_config={}
    )
    assert without.flaky is False
    assert without.target_test_passed is True
    assert evidence_of(without) is None, (
        "an unconfigured run must carry no flake receipt: its bytes are the "
        "historical bytes"
    )
    # The only addition is the rung line, and it NAMES the ungated path rather
    # than leaving a reader to guess whether a gate was configured.
    assert without.verification_rung == "baseline"
    assert "## verification-rung" in without.raw_output


def test_the_baseline_set_is_reachable_from_the_verifier(tmp_path, monkeypatch) -> None:
    """``execution/baseline_set`` must be reached by a verification, not only by
    its own test."""
    import json

    from execution.baseline_set import BASELINE_FILE, load_baseline

    run_dir = str(tmp_path / "run")
    recorded, recorded_rows = _fake_verify(
        tmp_path,
        monkeypatch,
        gates=["fail", "pass"],
        rung_config={"baseline_set_enabled": True},
        phase="baseline",
        run_dir=run_dir,
    )
    path = tmp_path / "run" / BASELINE_FILE
    assert path.is_file(), f"no baseline record was written at {path}"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert load_baseline(run_dir) is not None
    baseline_rows = [row for row in recorded_rows if row.get("rung") == "baseline_set"]
    assert baseline_rows and baseline_rows[0]["gate"] == "preexisting_failure_set", (
        "the baseline phase wrote no reports row, so the receipt a reader "
        f"reconstructs from is incomplete: {recorded_rows}"
    )
    assert "baseline_set" in recorded.verification_rungs

    # The post-fix phase reads the record back and names the rung.
    post, post_rows = _fake_verify(
        tmp_path,
        monkeypatch,
        gates=["pass", "pass"],
        rung_config={"baseline_set_enabled": True},
        phase="postfix",
        run_dir=run_dir,
    )
    assert any(row.get("gate") == "preexisting_failure_triple" for row in post_rows), (
        post_rows
    )
    assert "baseline_set" in post.verification_rungs


def test_the_baseline_set_fold_can_only_clear_a_boolean(tmp_path, monkeypatch) -> None:
    """Additional evidence must never mean overriding.

    The control is the shape that matters: with NO baseline-set key the same
    green run stays green, so the clearing below is attributable to the extra
    evidence and to nothing else.
    """
    run_dir = str(tmp_path / "run")
    _fake_verify(
        tmp_path,
        monkeypatch,
        gates=["fail", "pass"],
        rung_config={"baseline_set_enabled": True},
        phase="baseline",
        run_dir=run_dir,
    )
    # A post-fix run that collected nothing is never a pass; the fold refuses.
    vacuous, _reports = _fake_verify(
        tmp_path,
        monkeypatch,
        gates=["no_tests_collected", "pass"],
        rung_config={"baseline_set_enabled": True},
        phase="postfix",
        run_dir=run_dir,
    )
    assert vacuous.target_test_passed is False

    ungated, _reports = _fake_verify(
        tmp_path, monkeypatch, gates=["pass", "pass"], rung_config={}
    )
    assert ungated.target_test_passed is True, (
        "an unconfigured run must be byte-identical: a green target stays green"
    )


def test_every_verdict_names_the_rung_that_produced_it(tmp_path, monkeypatch) -> None:
    """A reader must be able to tell which mechanism claimed a run verified."""
    from execution.verify import RUNG_BASELINE, RUNG_FLAKE, VERIFICATION_RUNGS

    plain, _reports = _fake_verify(
        tmp_path, monkeypatch, gates=["pass", "pass"], rung_config={}
    )
    assert plain.verification_rung == RUNG_BASELINE
    assert plain.verification_rungs == (RUNG_BASELINE,)

    gated, _reports = _fake_verify(
        tmp_path,
        monkeypatch,
        gates=["pass", "pass"],
        rung_config={"post_fix_reruns": 2},
    )
    assert gated.verification_rung == RUNG_FLAKE
    assert "## verification-rung" in gated.raw_output
    assert "rung=flake" in gated.raw_output
    for name in gated.verification_rungs:
        assert name in VERIFICATION_RUNGS


def test_no_reachability_key_is_in_the_harness_defaults() -> None:
    """A value in DEFAULTS is merged into every task and every eval arm."""
    from execution.verify import BASELINE_SET_CONFIG_KEYS, FLAKE_GATE_CONFIG_KEYS
    from harness.config import DEFAULTS

    published = sorted(
        key
        for key in FLAKE_GATE_CONFIG_KEYS + BASELINE_SET_CONFIG_KEYS
        if key in DEFAULTS
    )
    assert not published, (
        "these keys must stay opt-in by presence only; a DEFAULTS value would "
        f"switch every task and every eval arm at once: {published}"
    )


def test_the_harness_call_sites_actually_hand_the_verifier_a_rung_config() -> None:
    """A reachable import the harness never triggers is still a dead mechanism.

    Structural rather than behavioural on purpose: the property is that each
    ``verify()`` call site supplies the config, and there are five of them.
    """
    core = (REPO_ROOT / "harness" / "core.py").read_text(encoding="utf-8")
    loop = (REPO_ROOT / "harness" / "agent_loop.py").read_text(encoding="utf-8")
    # Whitespace is normalised so a call site split across lines by a formatter
    # still counts: this is a structural pin, not a formatting pin.
    found = len(re.findall(r"_verify_rung_kwargs\(\s*verify\s*,\s*cfg\b", core))
    assert found >= 4, (
        "harness/core.py must hand the resolved config to the verifier at every "
        "gating call site (baseline, final gate, agent-tests post-fix, per-step "
        f"checkpoint); found {found} of 4"
    )
    assert '_verify_boundary_names(boundary, "rung_config")' in loop, (
        "harness/agent_loop.py's declared-test run must forward the rung config, "
        "or the verification intelligence stays unreachable from the daily "
        "interactive session"
    )
    # And the forwarding is signature-gated, so a double carrying the historical
    # five-parameter signature keeps working instead of dying on a TypeError.
    assert "def _verify_boundary_names" in loop
    assert "def _verify_rung_kwargs" in core
