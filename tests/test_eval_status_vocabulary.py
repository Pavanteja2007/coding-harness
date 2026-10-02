"""Inverted pins for the eval suite's completion-status vocabulary.

This file exists because six consecutive rounds re-filed the same defect: an
eval site comparing a run status to the bare string ``"success"`` against a
path that honestly reports ``completed_verified``. On 2026-10-01 the claim was
measured and found to be **stale** - see the classes below.

The measured state (reproduce with
``python -m pytest tests/test_daily_driver_evals.py -q`` -> 34 passed, and
``dd.run_feature_evidence(root)`` -> 28/28 arms ok, every
``core_run_succeeded`` genuinely ``True``):

* **Zero** eval sites are currently red.
* All ten remaining ``status == "success"`` sites assert against
  ``harness.core.run_task``'s ``TaskResult``, whose vocabulary is
  ``success | failed | error | timeout`` (``shared/types.TaskResult``) and
  whose ``success`` is minted **only** from ``COMPLETED_VERIFIED``
  (``harness/core.py:1944``). Those assertions are honest and must NOT be
  "fixed".
* The sites that *were* wrong were the agent-loop ones, and they already route
  through ``evals.daily_driver._honest_completion``.

So the pin is deliberately **two-sided**, which is the only honest shape:

1. An eval site must not compare a ``RUN_STATUSES`` value (the kernel/agent
   vocabulary) to the bare ``"success"`` - that is the historical defect.
2. An eval site that DOES compare to ``"success"`` must be comparing a
   ``TaskResult``, whose vocabulary legitimately contains it.

Class 2 is what stops the next round from "fixing" ten correct assertions
into a lie - which is the exact failure mode this project forbids.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "evals"

#: Files whose status comparisons are inspected. `tests/**` is deliberately
#: NOT scanned: a test asserting against its OWN harness vocabulary is a
#: different question from an eval probe making a product claim.
SCANNED = tuple(sorted(EVAL_DIR.glob("*.py")))


def _bare_success_comparisons(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, source)`` for every comparison to the bare word.

    ``status == "success"`` in any spelling: ``==``, ``!=``, ``in``, or a
    membership test against a tuple of literals. Read with ``utf-8-sig``
    because PowerShell writes a BOM into a module here and a parse error in a
    *pin* would read as a green suite that measured nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        values = [node.comparators[0], *node.comparators]
        if not any(
            isinstance(value, ast.Constant) and value.value == "success"
            for value in values
        ):
            continue
        found.append((node.lineno, ast.unparse(node)))
    return sorted(found)


def _run_statuses() -> frozenset[str]:
    from shared.agent_contracts import RUN_STATUSES

    return frozenset(RUN_STATUSES)


def _task_result_statuses() -> frozenset[str]:
    import typing

    from shared.types import TaskResult

    literal = TaskResult.__dataclass_fields__["status"].type
    return frozenset(typing.get_args(literal))


class TestTheTwoVocabulariesAreDisjoint:
    """The premise of every re-filed claim, asserted so it cannot rot."""

    def test_the_kernel_vocabulary_contains_no_bare_success_word(self) -> None:
        assert "success" not in _run_statuses()

    def test_the_task_result_vocabulary_does_contain_success(self) -> None:
        # This is why the ten eval sites are honest rather than wrong.
        assert "success" in _task_result_statuses()

    def test_the_shared_words_do_not_overlap_with_the_task_result_ones(self) -> None:
        """The two vocabularies share no word that could be confused.

        ``success`` belongs ONLY to `TaskResult` and ``completed_verified``
        belongs ONLY to the kernel. That disjointness is the whole reason the
        ten eval assertions are readable: a reader can tell which vocabulary a
        site is in from the word alone.
        """
        assert "success" not in _run_statuses()
        assert "completed_verified" not in _task_result_statuses()
        assert "completed_verified" in _run_statuses()


class TestNoEvalSiteComparesARunStatusToBareSuccess:
    """The inverted pin for the historical defect."""

    def test_every_eval_file_is_scannable(self) -> None:
        assert SCANNED, "no eval modules found - the scan would pass vacuously"

    @pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
    def test_no_run_status_is_compared_to_bare_success(self, path: Path) -> None:
        offenders: list[str] = []
        for lineno, source in _bare_success_comparisons(path):
            window = _calling_context(path, lineno)
            if any(producer in window for producer in _SUCCESS_VOCABULARY_PRODUCERS):
                continue
            if any(
                token in window
                for token in ("run_agent", "AgentKernel", "_kernel", "RunResult")
            ):
                offenders.append(f"{path.name}:{lineno}: {source}")
        assert not offenders, (
            "an eval site compares a RUN_STATUSES value to the bare word "
            '"success". The kernel vocabulary has no such word '
            f"({sorted(_run_statuses())}); use the honest status or route "
            "through evals.daily_driver._honest_completion. If this producer "
            "really does emit 'success', declare it in "
            "_SUCCESS_VOCABULARY_PRODUCERS with its reason:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_pin_would_fire_on_the_historical_defect(self) -> None:
        """A gate nobody has watched fire is a gate nobody reads.

        Feeds the detector the exact source shape a previous round shipped and
        asserts it is caught, so the pin cannot pass because the scan is inert.
        """
        assert _would_be_flagged('result.status == "success"', context="run_agent(")
        assert _would_be_flagged(
            'result.status == "success"', context="_kernel(repo).run(spec)"
        )
        assert not _would_be_flagged(
            'result.status == "success"', context="core.run_task(task)"
        )

    def test_the_exempt_producers_are_still_the_measured_ones(self) -> None:
        """Anti-rot for the exemption table itself.

        Every exempt producer is asserted to still exist AND to still declare
        ``success`` in its own vocabulary, so a renamed or deleted function - or
        one that silently changed its status word - fails this pin instead of
        leaving an assertion exempt for a producer that no longer behaves that
        way.
        """
        import inspect

        import evals.daily_driver as dd
        import harness.core as core
        import harness.qa_mode as qa

        assert callable(core.run_task)
        assert callable(qa.run_question)
        assert callable(dd._honest_completion)

        # `core.run_task` -> a typed TaskResult, so the vocabulary is a Literal.
        assert "success" in _task_result_statuses()

        # `qa_mode.run_question` -> a plain dict, so the DECLARED vocabulary is
        # its own docstring. Read the docstring rather than trusting the
        # exemption table: a producer that stopped emitting "success" would
        # make `daily_driver.py:750` permanently false, which is a real defect.
        qa_doc = inspect.getdoc(qa.run_question) or ""
        assert '"success"' in qa_doc and '"error"' in qa_doc, (
            "harness.qa_mode.run_question no longer documents a "
            "'success' | 'error' status vocabulary, so the eval assertion "
            "that compares its result to 'success' needs retargeting."
        )


def _calling_context(path: Path, lineno: int) -> str:
    """Return the source above the assertion, far enough to see its producer.

    Deliberately coarse: the question is "was this value produced by the
    kernel/agent path or by a producer whose vocabulary is DECLARED to contain
    ``success``", and both answers are visible in the handful of lines above the
    assertion. A 60-line window is wider than any single probe in this file, so
    the answer cannot depend on the window size.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    start = max(0, lineno - 60)
    return "\n".join(lines[start:lineno])


#: Producers whose returned status vocabulary DOES legitimately contain the word
#: ``success``. Each entry records WHY, because a list of bare strings is a list
#: a future round silently appends to.
#:
#: * ``core.run_task`` -> ``TaskResult.status`` is
#:   ``success | failed | error | timeout``, and ``success`` is minted only from
#:   ``COMPLETED_VERIFIED`` (``harness/core.py``).
#: * ``run_question`` / ``qa_mode`` -> ``ModeResult.status`` is
#:   ``success | error``; it is a read-only ANSWER shape, not a run status, so
#:   the kernel vocabulary does not apply to it.
#:
#: A producer absent from this table is assumed NOT to have ``success`` in its
#: vocabulary, which is the fail-closed direction: adding a producer here is a
#: deliberate, reviewable act.
_SUCCESS_VOCABULARY_PRODUCERS = ("run_task", "run_question")


def _would_be_flagged(source: str, context: str) -> bool:
    """Mirror of the detector above, for the self-test.

    Kept as a separate function on purpose: duplicating the rule is what makes
    the self-test meaningful. If the detector changes and this does not, the
    self-test fails loudly rather than agreeing with a broken gate.
    """
    if any(producer in context for producer in _SUCCESS_VOCABULARY_PRODUCERS):
        return False
    return any(
        token in context
        for token in ("run_agent", "AgentKernel", "_kernel", "RunResult")
    )


class TestEvalSitesAssertTheHonestThing:
    """Spot-checks that the honest assertions are real, not vacuous."""

    def test_the_honest_completion_reduction_exists_and_is_used(self) -> None:
        from evals import daily_driver

        assert callable(daily_driver._honest_completion)
        # (True, False): completed, and NOT worded as success without evidence.
        completed, worded_as_success = daily_driver._honest_completion(
            {"status": "completed_verified", "kernel_status": "completed_verified"}
        )
        assert completed is True
        assert worded_as_success is False

    def test_an_unverified_completion_is_never_worded_as_success(self) -> None:
        from evals import daily_driver

        completed, worded_as_success = daily_driver._honest_completion(
            {"status": "completed_unverified"}
        )
        assert completed is True
        assert worded_as_success is False

    def test_success_without_verified_evidence_is_caught(self) -> None:
        """The guard that replaced the wrong predicate must still bite."""
        from evals import daily_driver

        _completed, worded_as_success = daily_driver._honest_completion(
            {"status": "success"}
        )
        assert worded_as_success is True

    def test_a_task_result_success_is_the_verifier_minted_one(self) -> None:
        """`harness.core` mints `TaskResult.status == "success"` only from
        ``COMPLETED_VERIFIED``. If that mapping ever widens, every one of the
        ten eval assertions widens with it - so it is pinned here.
        """
        source = (REPO_ROOT / "harness" / "core.py").read_text(encoding="utf-8")
        assert 'CompletionStatus.COMPLETED_VERIFIED.value: "success"' in source, (
            "harness/core.py no longer maps only COMPLETED_VERIFIED to the "
            'TaskResult word "success". The eval sites that assert '
            '`result.status == "success"` against run_task depend on this '
            "being the sole mint."
        )


class TestTheDisplayPathStripsBeforeRedactingAndFailsClosed:
    """T4's two filed `cli/ui.py` defects, pinned against the CURRENT contract.

    The originally-reported defect (`strip_ansi` redacting before stripping,
    so ANSI escapes could reassemble a credential the redactor never matched)
    has been fixed upstream: `strip_ansi` now delegates to `sanitize_text`,
    which strips escapes at step 1 and redacts at step 2. This pin reads the
    real function rather than a remembered one, so it keeps guarding the
    property instead of a line number.
    """

    def test_sanitize_text_strips_escapes_before_redacting(self) -> None:
        source = (REPO_ROOT / "cli" / "ui.py").read_text(encoding="utf-8-sig")
        body = source.split("def sanitize_text(", 1)[1].split("\ndef ", 1)[0]
        strip_at = body.find("strip_escapes(")
        redact_at = body.find("redact_or_fail(")
        assert strip_at != -1, "sanitize_text no longer strips escapes at all"
        assert redact_at != -1, "sanitize_text no longer redacts at all"
        assert strip_at < redact_at, (
            "cli/ui.py::sanitize_text redacts BEFORE stripping escapes. ANSI "
            "escapes can split a secret into visually contiguous bytes, so "
            "the order must be strip-then-redact (phases/DOCTRINE.md s5)."
        )

    def test_strip_ansi_still_delegates_to_the_one_implementation(self) -> None:
        source = (REPO_ROOT / "cli" / "ui.py").read_text(encoding="utf-8-sig")
        body = source.split("def strip_ansi(", 1)[1].split("\ndef ", 1)[0]
        assert "sanitize_text(" in body, (
            "cli/ui.py::strip_ansi no longer delegates to sanitize_text. Two "
            "sanitizers is the exact fork this repo has paid for before."
        )

    def test_the_display_path_withholds_rather_than_disclosing(self) -> None:
        """The second filed defect: a broken redactor used to become a leak.

        `sanitize_text` must return a withheld marker, never the input, when
        redaction is unavailable.
        """
        from cli.ui import sanitize_text

        # A value whose redaction cannot succeed must not come back intact.
        class _Hostile:
            def __str__(self) -> str:
                raise RuntimeError("cannot render")

        out = sanitize_text(_Hostile(), task_id="t5-probe")
        assert "withheld" in out.lower(), (
            f"sanitize_text disclosed a value it could not render: {out!r}"
        )
        assert "cannot render" not in out


def test_this_file_does_not_import_the_run_path_at_module_scope() -> None:
    """A status pin must not DEPEND ON the run path being importable.

    Scoped to module scope deliberately. Two reasons:

    * A module-level ``import harness.core`` turns this file into a second
      victim of the cross-terminal import break this repo has paid for three
      times - when `harness.tool_errors.py` was mid-write, every module that
      imported `harness.core` at module scope raised `AttributeError` at
      COLLECTION, and a pin file that cannot be collected reports nothing.
    * The function-level imports in the anti-rot test are fine: they run inside
      one test, so a broken `harness` fails that test with a readable verdict
      rather than taking the whole file down before any test runs.

    The shared contract modules (`shared.agent_contracts`, `shared.types`) ARE
    imported for real: they are the spec this pin encodes, and they import
    nothing from the run path.
    """
    import ast as _ast

    tree = _ast.parse(Path(__file__).read_text(encoding="utf-8"))
    module_level: set[str] = set()
    for node in tree.body:  # top level only - not ast.walk
        if isinstance(node, _ast.Import):
            module_level.update(alias.name for alias in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            module_level.add(node.module)
    banned = {"harness", "harness.core", "harness.agent_loop", "runtime", "execution"}
    assert not (module_level & banned), (
        "a status pin must not import the run path at module scope: "
        f"{sorted(module_level & banned)}"
    )
    # The contract reads are deliberately DEFERRED too, into `_run_statuses`
    # and `_task_result_statuses`. They are the spec this file encodes, but a
    # pin file should cost nothing to collect: every import it does at module
    # scope is a way for an unrelated cross-terminal break to silence it.
    assert module_level <= {"__future__", "ast", "pathlib", "pytest"}, (
        "unexpected module-scope import in a pin file: "
        f"{sorted(module_level - {'__future__', 'ast', 'pathlib', 'pytest'})}"
    )
    # And the deferred contract reads really do resolve - a file that imported
    # a vocabulary module that no longer exists would raise inside a test
    # rather than at collection, so prove it here, once, loudly.
    assert "success" in _task_result_statuses()
    assert "success" not in _run_statuses()


def test_the_scan_covers_the_file_that_actually_has_the_sites() -> None:
    """Non-vacuity: the scanned set must contain the module with the sites.

    If `daily_driver.py` were renamed or the glob broke, an empty scan would
    pass every test above while measuring nothing.
    """
    names = {p.name for p in SCANNED}
    assert "daily_driver.py" in names
    target = EVAL_DIR / "daily_driver.py"
    comparisons = _bare_success_comparisons(target)
    assert comparisons, (
        "expected bare-'success' comparisons in evals/daily_driver.py; their "
        "absence means this pin is no longer guarding the thing it names"
    )
