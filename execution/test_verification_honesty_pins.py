"""T2.W2.4 — the honesty of the verification layer, pinned.

``VerificationResult`` has four booleans and each one can lie by default. This
module pins all four, plus the statelessness of ``verify()`` itself.

The four lies, and the direction each one errs in
-------------------------------------------------

===================  =====================================================
field                the lie this prevents
===================  =====================================================
``baseline_passed``  ``verify()`` **always returns ``False``** — only the
                     harness knows the pristine outcome, because ``verify()``
                     is a stateless evaluator of ONE repo state and cannot
                     reconstruct the pristine tree from an edited one. A
                     caller that trusted it would ALWAYS think the baseline
                     failed, and would never mint a verified completion.
``regression_passed``  A MISSING term must be ``False``, never defaulted
                     ``True``. ``harness/agent_kernel/legacy.py`` defaults it
                     ``True``; that divergence is real, is recorded, and is
                     pinned EXACTLY below rather than described.
``flaky``            The ``not_run`` case: ``False`` means "NOT CHECKED", not
                     "stable". Fully pinned in
                     ``execution/test_flake_gate_pins.py``; the pin here is the
                     RECEIPT half — ``verify()`` must publish ``flake_check``
                     whenever it publishes ``flaky``.
``target_test_passed``  The only field that may mint a verified completion,
                     so it is the only one the rungs may ever CLEAR, and it is
                     the only one a reader may treat as a claim.
===================  =====================================================

Why the statelessness pin matters
---------------------------------

``verify()`` is called twice per attempt — once on the pristine tree, once on
the work tree — and the harness relies on the two being independent evaluations
of different states. A ``verify()`` that mutated the repository, or whose verdict
depended on what it ran before, would make the baseline comparison meaningless:
a run whose baseline mutated the tree would be verified against a tree it had
already changed.

Run: ``python -m pytest execution/test_verification_honesty_pins.py -q``
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from execution.flake_gate import NOT_FLAKY, NOT_RUN, OUTCOME_PASS, flake_verdict
from execution.verify import _ecosystem_absent_result, verify

_REPO = Path(__file__).resolve().parent.parent


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


# ---------------------------------------------------------------------------
# The honest default
# ---------------------------------------------------------------------------


def test_a_default_construction_refuses_every_claim() -> None:
    """A ``VerificationResult`` built with the honest shape refuses to mint.

    The mint condition the harness uses is
    ``target_test_passed and regression_passed and not flaky``. With every field
    at its honest value the condition is False, so an unpopulated result cannot
    become a completion by accident.
    """
    from shared.types import VerificationResult

    honest = VerificationResult(
        target_test_passed=False,
        baseline_passed=False,
        regression_passed=False,
        flaky=False,
        raw_output="",
    )
    assert not (
        honest.target_test_passed and honest.regression_passed and not honest.flaky
    )
    assert honest.baseline_passed is False


def test_a_malicious_construction_cannot_be_expressed_by_the_honest_defaults() -> None:
    """The control: the mint DOES fire when the three terms are all satisfied.

    Without this, the assertion above would pass for a dataclass whose
    ``target_test_passed`` was permanently False and the gate could never fire
    at all — a gate that cannot pass is not a gate.
    """
    from shared.types import VerificationResult

    minting = VerificationResult(
        target_test_passed=True,
        baseline_passed=False,
        regression_passed=True,
        flaky=False,
        raw_output="",
    )
    assert (
        minting.target_test_passed and minting.regression_passed and not minting.flaky
    )
    # ...and note the field that may NOT be relied on for the mint.
    assert minting.baseline_passed is False


# ---------------------------------------------------------------------------
# baseline_passed — the lie the harness alone can answer
# ---------------------------------------------------------------------------


def _construction_sites() -> List[Dict[str, object]]:
    """Return every ``VerificationResult(...)`` construction in ``execution/``.

    Read by AST rather than by import, so a site that is unreachable at runtime
    (an error branch) is still counted. A field that is only correct on the
    happy path is a field that is wrong on the others.
    """
    sites: List[Dict[str, object]] = []
    for path in sorted((_REPO / "execution").glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "VerificationResult":
                continue
            keywords: Dict[str, ast.AST] = {}
            for keyword in node.keywords:
                if keyword.arg:
                    keywords[keyword.arg] = keyword.value
            sites.append(
                {
                    "path": path.relative_to(_REPO).as_posix(),
                    "lineno": node.lineno,
                    "keywords": keywords,
                }
            )
    return sites


def test_every_construction_in_execution_hardcodes_baseline_passed_false() -> None:
    """``verify()`` may NEVER emit ``baseline_passed=True``.

    It cannot: it is a stateless evaluator of ONE repo state and does not know
    whether it is looking at the pristine tree. A construction site that passed
    anything but ``False`` would be asserting a fact the function does not have,
    and the harness mint (``target and regression and not flaky``) plus the
    baseline division of labour would both be reading a fiction.
    """
    sites = _construction_sites()
    assert sites, (
        "the scan found no VerificationResult construction; it has stopped working"
    )
    offenders: List[str] = []
    for site in sites:
        value = site["keywords"].get("baseline_passed")  # type: ignore[union-attr]
        if value is None:
            offenders.append(f"{site['path']}:{site['lineno']} omits baseline_passed")
            continue
        if not (isinstance(value, ast.Constant) and value.value is False):
            offenders.append(
                f"{site['path']}:{site['lineno']} baseline_passed={ast.dump(value)}"
            )
    assert not offenders, (
        "a VerificationResult construction in execution/ can set "
        f"baseline_passed to something other than False: {offenders!r}"
    )


def test_the_error_paths_refuse_every_claim() -> None:
    """The absent-suite path is a refusal on all three gating booleans.

    No pytest config, no runner, no command: the honest answer is "I could not
    evaluate this", and every gating boolean must say so. A ``flaky=True`` here
    would be a defensible choice too — but a ``target_test_passed=True`` or a
    ``regression_passed=True`` would be a claim about a run that never happened.
    """
    from shared.types import VerificationResult

    absent = _ecosystem_absent_result(None, "/nonexistent/repo", None)
    assert isinstance(absent, VerificationResult)
    assert absent.target_test_passed is False
    assert absent.regression_passed is False
    assert absent.flaky is False
    assert absent.baseline_passed is False
    assert absent.raw_output.strip(), "a refusal must say why in raw_output"


def test_a_flaky_only_mint_is_blocked_by_the_honest_default() -> None:
    """``not flaky`` alone can never satisfy the mint.

    Two of the three mint terms are required; this makes the shape explicit so a
    future refactor that reads only ``flaky`` is caught here.
    """
    from shared.types import VerificationResult

    checked_but_not_flaky = VerificationResult(
        target_test_passed=True,
        baseline_passed=False,
        regression_passed=False,  # the suite FAILED
        flaky=False,
        raw_output="",
    )
    assert not (
        checked_but_not_flaky.target_test_passed
        and checked_but_not_flaky.regression_passed
        and not checked_but_not_flaky.flaky
    )


# ---------------------------------------------------------------------------
# regression_passed — a missing term is False, never True
# ---------------------------------------------------------------------------


def test_a_missing_regression_term_is_never_read_as_passed() -> None:
    """Every reader of ``regression_passed`` in ``execution/`` defaults to False.

    The divergence this prevents is real and is already recorded:
    ``harness/agent_kernel/legacy.py`` defaults a MISSING
    ``regression_passed`` to ``True``, where the pure core treats absent as
    ``False``. ``execution/`` must be on the strict side of that line, because
    ``execution/`` produces the result these readers consume.
    """
    strict_readers: List[str] = []
    offenders: List[str] = []
    for path in sorted((_REPO / "execution").glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        rel = path.relative_to(_REPO).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "regression_passed"
            ):
                strict_readers.append(f"{rel}:{node.lineno}")
                fallback = node.args[2] if len(node.args) >= 3 else None
                if isinstance(fallback, ast.Constant) and fallback.value is True:
                    offenders.append(f"{rel}:{node.lineno} defaults to True")
    assert strict_readers, (
        "the scan found no regression_passed read; it has stopped working"
    )
    assert not offenders, (
        "a reader in execution/ defaults a MISSING regression_passed to True: "
        f"{offenders!r}. An absent signal is not a green light"
    )


def test_the_baseline_verdict_treats_unknown_regression_as_not_passed() -> None:
    """``BaselineVerdict.blocks_success`` refuses an ``Unknown`` regression.

    Its field is ``Optional[bool]`` precisely so "not observed" is
    distinguishable from "observed failing", and the gate must refuse the former.
    """
    from execution.baseline_set import BaselineVerdict

    unknown = BaselineVerdict(target_test="tests/test_x.py", target_passed=True)
    assert unknown.regression_passed is None
    assert unknown.blocks_success is True, (
        "a missing regression term was treated as a pass"
    )

    failing = BaselineVerdict(
        target_test="tests/test_x.py", target_passed=True, regression_passed=False
    )
    assert failing.blocks_success is True

    green = BaselineVerdict(
        target_test="tests/test_x.py",
        target_passed=True,
        regression_passed=True,
        flaky=False,
    )
    assert green.blocks_success is False, (
        "the fully-green arm must NOT block, or the gate refuses every run and "
        "is theatre rather than a gate"
    )


def test_the_baseline_gate_rung_cannot_disagree_with_the_harness_mint() -> None:
    """``_baseline_verdict`` is byte-identical to the harness mint condition.

    The rung's own docstring claims it "can only explain" the mint. That claim is
    pinned over the whole boolean cube rather than described, because a rung that
    could disagree with the mint is a second authority on a completion claim.
    """
    from execution.verification_gate import GATE_BASELINE, RUNG_BASELINE
    from execution.verification_gate import _baseline_verdict as baseline_verdict

    class _Result:
        def __init__(self, target: bool, regression: bool, flaky: bool) -> None:
            self.target_test_passed = target
            self.regression_passed = regression
            self.flaky = flaky

    checked = 0
    for target in (True, False):
        for regression in (True, False):
            for flaky in (True, False):
                verdict = baseline_verdict(_Result(target, regression, flaky))
                harness_mint = target and regression and not flaky
                assert verdict.passed is harness_mint, (
                    f"the rung says {verdict.passed} where the harness mint says "
                    f"{harness_mint} for target={target} regression={regression} "
                    f"flaky={flaky}"
                )
                assert verdict.rung == RUNG_BASELINE
                assert verdict.name == GATE_BASELINE
                checked += 1
    assert checked == 8, checked


# ---------------------------------------------------------------------------
# flaky / target_passed — the receipt half
# ---------------------------------------------------------------------------


def test_verify_publishes_the_three_valued_verdict_beside_the_boolean() -> None:
    """``verify()`` must not publish ``flaky`` without ``flake_check``.

    A caller reading ``flaky`` alone cannot tell "we checked and it was stable"
    from "we never checked", so the disambiguation has to travel with the
    boolean or it does not exist. The seam helper is what decides whether the
    receipt is produced, so this is provable without a daemon.
    """
    from execution import verify as verify_module

    for repetitions, outcomes in (
        (2, (OUTCOME_PASS, OUTCOME_PASS)),
        (1, (OUTCOME_PASS,)),
    ):
        receipt = verify_module._flake_gate_rungs(
            ".",
            "tests/test_x.py",
            repetitions,
            outcomes,
            {"post_fix_reruns": repetitions},
        )
        assert isinstance(receipt, tuple) and len(receipt) == 2, receipt
        verdict, evidence = receipt
        assert verdict.flake_check in (NOT_FLAKY, NOT_RUN)
        assert evidence["verdict"]["flake_check"] == verdict.flake_check
        assert evidence["verdict"]["detection_possible"] is (
            verdict.flake_check != NOT_RUN
        )

    # The refusal branch: with NO rung config the seam stays dark and returns
    # None, which is the honest "not configured" answer rather than a receipt
    # claiming a check that was never configured.
    assert (
        verify_module._flake_gate_rungs(
            ".", "tests/test_x.py", 2, [OUTCOME_PASS, OUTCOME_PASS], None
        )
        is None
    )


#: The booleans that participate in the completion mint. A rung may CLEAR any
#: of them and may never SET any of them.
MINT_BOOLEANS: Tuple[str, ...] = (
    "target_test_passed",
    "baseline_passed",
    "regression_passed",
)

#: The one assignment that is NOT a clear and is legitimately computed: the
#: flake gate PROJECTING its three-valued verdict onto the historical boolean.
#: It is allowlisted by exact site with a reason, and the projection itself is
#: pinned in ``execution/test_flake_gate_pins.py`` — so this exemption cannot
#: hide a divergence, it only records where the one computed write lives.
COMPUTED_WRITE_ALLOWLIST: Dict[Tuple[str, str], str] = {
    ("execution/flake_gate.py", "flaky"): (
        "attach_evidence projects FlakeVerdict.flaky onto result.flaky. This is "
        "the projection the module exists to perform; it can only ever agree "
        "with flake_check on the SAME object, and it never sets the boolean to "
        "a value the verdict did not produce."
    ),
}


def test_the_baseline_set_fold_can_only_clear_the_mint_booleans() -> None:
    """A rung may CLEAR a mint boolean and may never SET one.

    This is the structural half of the honesty claim: an extra rung can remove a
    completion claim, and there is no code path anywhere in ``execution/`` that
    can add one. Enforced by AST over every assignment to a
    ``VerificationResult`` boolean.
    """
    cleared: List[str] = []
    raised: List[str] = []
    inspected = 0
    for path in sorted((_REPO / "execution").glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        rel = path.relative_to(_REPO).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute):
                    continue
                if target.attr not in (*MINT_BOOLEANS, "flaky"):
                    continue
                # Only count assignments to something that IS a verification
                # result. A `policy.flaky` or a `BaselineVerdict.flaky` must not
                # be mistaken for a mint write.
                owner = target.value
                if not (
                    isinstance(owner, ast.Name)
                    and owner.id in ("result", "v", "verdict_result", "_result")
                ):
                    continue
                inspected += 1
                site = f"{rel}:{node.lineno} {target.attr}"
                if isinstance(node.value, ast.Constant) and node.value.value is False:
                    cleared.append(f"{site} = False")
                elif isinstance(node.value, ast.Constant) and node.value.value is True:
                    raised.append(f"{site} = True")
                elif (rel, target.attr) in COMPUTED_WRITE_ALLOWLIST:
                    cleared.append(f"{site} = <the one allowlisted projection>")
                else:
                    raised.append(f"{site} = {ast.dump(node.value)}")
    assert inspected >= 1, (
        "the scan found no boolean assignment; it has stopped working"
    )
    assert not raised, (
        "a seam function in execution/ can SET a VerificationResult mint boolean "
        f"to True or to an arbitrary computed value: {raised!r}. Only a CLEAR is "
        "safe; a promote path is how a rung becomes a second mint authority. "
        f"Cleared sites: {cleared!r}"
    )
    assert cleared, "no clear-only assignment was found either; the scan is misfiring"
    for (rel, field), reason in COMPUTED_WRITE_ALLOWLIST.items():
        assert len(reason) >= 60, f"({rel}, {field}) has no real reason: {reason!r}"


def test_plan_fold_only_ever_clears() -> None:
    """``verification_gate.plan_fold`` clears and never promotes.

    Read by AST, because the property is about the SHAPE of the code and an
    in-place execution cannot distinguish "set False" from "set True".
    """
    from execution import verification_gate

    source = Path(verification_gate.__file__).read_text(encoding="utf-8-sig")
    tree = ast.parse(
        source,
        filename=str(verification_gate.__file__)
        if verification_gate.__file__
        else "verification_gate.py",
    )
    fold = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "plan_fold"
    )
    assignments = [
        (target.attr, node.value)
        for node in ast.walk(fold)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and target.attr
        in ("target_test_passed", "baseline_passed", "regression_passed", "flaky")
    ]
    assert assignments, "plan_fold no longer assigns a VerificationResult boolean"
    for field, value in assignments:
        assert isinstance(value, ast.Constant) and value.value is False, (
            f"plan_fold assigns {field} = {ast.dump(value)}; it may only CLEAR"
        )


# ---------------------------------------------------------------------------
# Statelessness — the same repo state gives the same verdict, twice
# ---------------------------------------------------------------------------


def _fixture_repo(root: Path) -> Path:
    """A minimal two-test pytest repository that PASSES."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "mymod.py").write_text(
        "VALUE = 1\n\n\ndef double(x):\n    return x * 2\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "honesty"\nversion = "0"\n', encoding="utf-8"
    )
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_mymod.py").write_text(
        "from mymod import double, VALUE\n\n\n"
        "def test_double():\n    assert double(2) == 4\n\n\n"
        "def test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    return root


def _tree_fingerprint(root: Path) -> Tuple[Tuple[str, int, int], ...]:
    """Return ``(relative path, size, mtime_ns)`` for every file under ``root``.

    ``mtime_ns`` is included so a file that is rewritten with identical bytes is
    still detected — a verification that touched the tree without changing its
    content is still a verification that touched the tree.
    """
    entries: List[Tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            stat = path.stat()
            entries.append(
                (path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns)
            )
    return tuple(entries)


@requires_docker
def test_verify_is_stateless_across_two_calls_on_the_same_repo_state(
    tmp_path: Path,
) -> None:
    """Two calls on one repo state give the SAME verdict.

    A verdict that depended on what the previous call observed would make the
    baseline/post-fix comparison meaningless: the harness calls ``verify()``
    twice per attempt and treats the two as independent evaluations of
    different trees.
    """
    root = _fixture_repo(tmp_path / "repo")
    command = "python -m pytest -q -p no:randomly -o addopts="

    first = verify(str(root), "tests/test_mymod.py::test_double", 1, command, 180)
    second = verify(str(root), "tests/test_mymod.py::test_double", 1, command, 180)

    for field in (
        "target_test_passed",
        "baseline_passed",
        "regression_passed",
        "flaky",
    ):
        assert getattr(first, field) == getattr(second, field), (
            f"{field} differed between two calls on the same repo state: "
            f"{getattr(first, field)!r} then {getattr(second, field)!r}"
        )
    # The control: the run really did pass, so "the same answer" is not the same
    # answer twice because nothing ran.
    assert first.target_test_passed is True, first.raw_output[-2000:]
    assert first.regression_passed is True, first.raw_output[-2000:]
    # baseline_passed is False on BOTH calls, including the "green" one.
    assert first.baseline_passed is False
    assert second.baseline_passed is False


@requires_docker
def test_verify_never_mutates_the_repository_sources_it_evaluates(
    tmp_path: Path,
) -> None:
    """No SOURCE file may change; only documented generated artifacts may appear.

    Stated precisely, because the imprecise version is FALSE and asserting it
    falsely would be worse than not asserting it. ``verify()`` runs the suite in
    a container whose ``/workspace`` is a READ-WRITE bind mount - that is the
    design, because agent edits must persist for host-side diffing - so the
    suite CAN write, and pytest in fact DOES create ``.pytest_cache/`` on every
    run. Claiming ``verify()`` never touches the tree is a claim the
    measurement refutes.

    So the invariant pinned here is the one that actually protects the diff:
    **no file the repository declares as its own content may change, and
    anything that does appear must already be on the project's generated-artifact
    list.** The classifier is the repository's OWN
    (:func:`execution.workspace.is_generated_path`), not a list written here, so
    a path the project already treats as generated is accepted by construction
    and a path it does not is a failure.
    """
    from execution.workspace import is_generated_path

    root = _fixture_repo(tmp_path / "repo")
    before = _tree_fingerprint(root)
    before_by_name = {entry[0]: entry for entry in before}

    result = verify(
        str(root),
        "tests/test_mymod.py::test_double",
        1,
        "python -m pytest -q -p no:randomly -o addopts=",
        180,
    )

    after = _tree_fingerprint(root)
    after_by_name = {entry[0]: entry for entry in after}
    changed = sorted(
        name
        for name in before_by_name.keys() & after_by_name.keys()
        if before_by_name[name] != after_by_name[name]
    )
    added = sorted(after_by_name.keys() - before_by_name.keys())
    removed = sorted(before_by_name.keys() - after_by_name.keys())

    source_changes = [
        name
        for name in sorted(set(changed) | set(added))
        if not is_generated_path(root / name)
    ]
    assert not source_changes, (
        "verify() changed repository SOURCE while evaluating it. A task could "
        f"achieve a green baseline by editing the tests it is judged against.\n"
        f"  changed: {changed}\n  added: {added}\n  removed: {removed}\n"
        f"  offending source paths: {source_changes}\n"
        f"  raw_output tail: {result.raw_output[-500:]}"
    )
    assert not removed, (
        f"verify() REMOVED files from the repository: {removed!r}\n"
        f"  raw_output tail: {result.raw_output[-500:]}"
    )
    # The control: the run really executed, so "nothing changed" is not
    # "nothing happened".
    assert result.target_test_passed is True, result.raw_output[-2000:]


@requires_docker
def test_a_suite_cannot_write_vcs_metadata_through_the_verifier(
    tmp_path: Path,
) -> None:
    """The boundary that protects the diff: the suite cannot forge ``.git``.

    The workspace mount is read-write BY DESIGN (an agent's edits must persist),
    so a hostile test suite CAN write into the working tree - measured, and
    recorded rather than hidden. What it must NOT be able to do is rewrite the
    repository metadata, because that is the tree whose forgery changes what a
    reviewer believes happened, and the harness's own diff/patch path reads it
    as repository content.

    This is the containment claim reached through the VERIFIER rather than
    through an agent step, which is why it earns a separate pin: it proves the
    read-only ``.git`` overlay applies to the gate's own container too.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mymod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
    )
    original_config = (root / ".git" / "config").read_text(encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "vcs"\nversion = "0"\n', encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_forger.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_forger():\n"
        "    try:\n"
        "        Path('.git/config').write_text('[core]\\n\\tforged = 1\\n')\n"
        "    except OSError:\n"
        "        pass\n",
        encoding="utf-8",
    )

    result = verify(
        str(root),
        "tests/test_forger.py::test_forger",
        1,
        "python -m pytest -q -p no:randomly -o addopts=",
        180,
    )

    assert (root / ".git" / "config").read_text(encoding="utf-8") == original_config, (
        "a test running inside the VERIFIER's container rewrote .git/config; "
        "the read-only overlay did not apply to the gate\n"
        f"  raw_output tail: {result.raw_output[-1500:]}"
    )
    # The control: the forger test really ran and really attempted the write.
    assert "test_forger" in result.raw_output, result.raw_output[-1500:]


def test_verify_raises_rather_than_returning_when_the_daemon_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail loud, not silent: no daemon means an exception, never a verdict.

    A ``VerificationResult`` returned for a run that never happened is the worst
    shape available — it is a claim about tests that were never executed. Pinned
    here without a daemon by replacing the availability probe.
    """
    root = _fixture_repo(tmp_path / "repo")
    import execution.sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module, "docker_available", lambda: False)
    with pytest.raises(sandbox_module.SandboxUnavailableError):
        verify(str(root), None, 1, "python -m pytest -q", 60)


def test_a_flake_verdict_is_never_computed_from_the_flaky_boolean() -> None:
    """The gate's verdict is derived from OBSERVED OUTCOMES, not from ``flaky``.

    A future refactor that reduced ``flake_verdict`` to ``not result.flaky``
    would make every ``not_run`` indistinguishable from ``not_flaky``, which is
    the exact misreading this whole module exists to prevent.
    """
    verdict = flake_verdict(1, (OUTCOME_PASS,))
    assert verdict.flake_check == NOT_RUN
    assert verdict.flaky is False
    assert verdict.to_dict()["flake_check"] == NOT_RUN
    assert verdict.to_dict()["flaky"] is False
    assert verdict.to_dict()["detection_possible"] is False
