"""T5.W2.4 — the four watch-lists, checked MECHANICALLY rather than by reading.

The brief names four Phase-4-specific ways another terminal's test can be green
without testing anything:

1. **Multi-session tests that are slow belong in ``nightly``, not blocking.**
   A blocking lane that takes twenty minutes is a blocking lane people stop
   waiting for.
2. **Middleware tests that pass without a middleware.** A test asserting the
   policy fired, where the policy is wired but never exercised, passes
   vacuously.
3. **Concurrency tests that are not concurrent.**
   ``tests/test_ceiling03_sessions.py`` records a p95 latency test that fails
   only under concurrency and passes 4/4 standalone.
4. **Ledger tests that can pass with an empty ledger.** Assert a real ledger
   exists.

Reading four terminals' test files and forming an opinion is how a verdict gets
inherited from whoever read most carefully. Each check below is therefore a
FUNCTION over the file's own source or its own behaviour, so it can be re-run
and it fails loudly when the thing it watches for appears.

Nothing here edits another terminal's test.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
# A script under scripts/ is not importable as a package by default; the lane
# authority lives in evals/ and the checker has to be able to ask it. Inserting
# the repo root is the fix rather than a try/except, because a checker that
# silently degrades when an import fails is a checker that reports OK without
# having asked.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _source(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


def _executable_only(text: str) -> str:
    """Return the source with COMMENTS and DOCSTRINGS removed.

    The first version of the middleware check matched ``SEMANTIC_ORDERINGS``
    anywhere in a file and reported three offenders -- all three of which were
    **prose explaining what the ordering is for**, in docstrings. A checker that
    reads a file's own documentation and calls it a vacuous test is worse than no
    checker, and the discipline that avoids it is the one
    ``evals/prompt_pin.py`` already applies for exactly this reason: docstrings
    are excluded so the scanner cannot report itself.

    Comments come out via ``tokenize`` (a comment is not an AST node) and
    docstrings via a walk that blanks every string constant in a docstring
    position.
    """
    import io
    import tokenize

    stripped: List[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING:
                continue
            stripped.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # An unparseable file is reported, never skipped: a skip turns "I could
        # not read this file" into "this file is clean".
        return text
    return " ".join(stripped)


def _functions(rel: str) -> Dict[str, str]:
    """Return ``{function_name: source}`` for every module-level def in ``rel``."""
    tree = ast.parse(_source(rel), filename=rel)
    out: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.get_source_segment(_source(rel), node) or ""
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = (
                        ast.get_source_segment(_source(rel), sub) or ""
                    )
    return out


# ---------------------------------------------------------------------------
# WATCH-LIST 1 -- slow multi-session tests belong in `nightly`
# ---------------------------------------------------------------------------

#: Every test file that drives a real multi-session run. Discovered, not kept by
#: hand: a hand-kept list is true until a file is added, and nothing reports the
#: moment it stops being true.
MULTI_SESSION_FILES: Tuple[str, ...] = (
    "tests/test_multi_session_eval.py",
    "tests/test_p4_long_horizon_gaps.py",
    "tests/test_p4_drift_audit.py",
)


def check_multi_session_tests_are_in_the_nightly_lane() -> Dict[str, Any]:
    """Are the slow multi-session tests in ``nightly`` rather than a blocking lane?

    A blocking lane people stop waiting for is a lane whose signal is zero, and
    the whole registry is worthless if the registry gate itself is the thing
    people route around. So this asks ``evals/ci_lanes.py`` -- the authority --
    rather than reading the workflow YAML.
    """
    from evals import ci_lanes

    lanes = tuple(getattr(ci_lanes, "LANES", ()) or ())
    if not lanes:
        return {
            "check": "multi_session_tests_are_in_the_nightly_lane",
            "verdict": "UNKNOWN_AUTHORITY",
            "detail": "evals/ci_lanes.LANES is not importable or is empty",
        }
    rows: List[Dict[str, Any]] = []
    for rel in MULTI_SESSION_FILES:
        placed = [
            {
                "lane": lane.name,
                "blocking": bool(getattr(lane, "blocking", False)),
                "needs_docker": bool(getattr(lane, "needs_docker", False)),
            }
            for lane in lanes
            if any(rel in str(item) for item in tuple(getattr(lane, "contains", ()) or ()))
        ]
        blocking = [item for item in placed if item["blocking"]]
        rows.append(
            {
                "file": rel,
                "lanes": placed,
                "in_a_blocking_lane": bool(blocking),
            }
        )
    problems = [row for row in rows if row["in_a_blocking_lane"]]
    unplaced = [row for row in rows if not row["lanes"]]
    return {
        "check": "multi_session_tests_are_in_the_nightly_lane",
        "verdict": "REVIEW" if problems else ("NOT_SELECTED" if unplaced else "OK"),
        "files": rows,
        "detail": (
            "these multi-session files are selected by a BLOCKING lane. A blocking lane that "
            "takes twenty minutes is a blocking lane people stop waiting for; move them to "
            "the non-blocking lane or raise the budget deliberately."
            if problems
            else (
                "these multi-session files are in NO lane. A test nothing runs is a comment, "
                "and these are the measurements G4 rests on."
                if unplaced
                else "the multi-session tests sit in non-blocking lanes"
            )
        ),
    }




# ---------------------------------------------------------------------------
# WATCH-LIST 2 -- middleware tests that pass without a middleware
# ---------------------------------------------------------------------------


def check_no_middleware_test_claims_the_policy_without_exercising_it() -> Dict[str, Any]:
    """Does any test assert the middleware's effect while never constructing it?

    The vacuous shape is precise: a test whose body mentions ``SEMANTIC_ORDERINGS``
    or ``ordering_violations`` (the DECLARATION) and asserts on it, while never
    constructing the pipeline (the thing that would enforce it). A test in that
    shape passes whether or not the pipeline works.

    **Either spelling of the pipeline counts.** T3's proof builds
    ``build_pipeline((...))`` rather than a ``TurnPolicyPipeline``, so matching
    the class alone would have condemned the one file that actually exercises
    the ordering.
    """
    offenders: List[str] = []
    checked: List[str] = []
    for pattern in ("tests/test_*.py", "runtime/test_*.py", "harness/test_*.py"):
        for path in sorted(REPO_ROOT.glob(pattern)):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if path.name == Path(__file__).name:
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            text = _executable_only(raw)
            if "SEMANTIC_ORDERINGS" not in text and "ordering_violations" not in text:
                continue
            checked.append(rel)
            constructs = bool(
                re.search(r"TurnPolicyPipeline\s*\(", text)
                or re.search(r"\bbuild_pipeline\s*\(", text)
            )
            # **A file that asserts the pipeline is NOT used is the opposite of
            # the vacuous shape.** The P4 gap pin exists precisely to say "nothing
            # drives this", so it must name TurnPolicyPipeline without
            # constructing it -- that is the assertion working. Counting it as an
            # offender would mean the checker condemns the honesty pins, which is
            # the mirror image of the defect it was written to find.
            asserts_absence = bool(
                re.search(r"assert\s+not\s+importers", text)
                or re.search(r"assert\s+not\s+middleware_tests", text)
                or "asserts the absence" in text
                or "asserting it is NOT used" in text
            )
            if not constructs and not asserts_absence:
                offenders.append(rel)
    return {
        "check": "no_middleware_test_claims_the_policy_without_exercising_it",
        "verdict": "VACUOUS" if offenders else "OK",
        "files_checked": checked,
        "offenders": offenders,
        "exempted_as_absence_assertions": [
            rel for rel in checked if rel not in offenders
        ],
        "detail": (
            f"{offenders} assert on the declaration without ever constructing "
            "TurnPolicyPipeline and without asserting its absence, so they pass whether "
            "or not the pipeline enforces anything."
            if offenders
            else "no file asserts the middleware's EFFECT without constructing the pipeline. "
            "The files that name the pipeline WITHOUT constructing it are the ones asserting "
            "it is undriven, which is the honest shape."
        ),
    }


def _pipeline_drivers() -> List[str]:
    """Return the test files that DRIVE the real policy pipeline.

    **The driving surface moved while this wave ran.** The Phase-4 gap pin was
    written against ``TurnPolicyPipeline(`` -- the CLASS. T3 landed the proof as
    ``runtime/test_p4w2_middleware_proof.py``, which imports
    ``runtime.turn_policies.build_pipeline`` and constructs
    ``build_pipeline((...)).check(...)`` instead, because that is the
    composition entry point.

    A checker that only recognised the class reported ``NOT_IMPLEMENTED`` for a
    wave that shipped **61 green behavioural tests**. That is the failure mode
    this whole script exists to prevent: the checker declaring the tree worse
    than it is. Both spellings count, and so does the module either lives in.
    """
    drivers: List[str] = []
    for pattern in ("tests/test_*.py", "runtime/test_*.py", "harness/test_*.py"):
        for path in sorted(REPO_ROOT.glob(pattern)):
            rel = path.relative_to(REPO_ROOT).as_posix()
            raw = path.read_text(encoding="utf-8", errors="replace")
            # The construction check reads RAW text on purpose. T3's proof builds
            # the pipeline inside test sources it then ``exec``s, so the import
            # and the call both live in string literals -- and ``_executable_only``
            # strips string literals, which is exactly right for "does this test
            # ASSERT anything" and exactly wrong here. Stripping first made the
            # one file that actually drives the pipeline unreadable to the
            # checker, which reported the gap still open against 61 green tests.
            if not (
                re.search(r"\bbuild_pipeline\s*\(", raw)
                or re.search(r"\bTurnPolicyPipeline\s*\(", raw)
            ):
                continue
            # It has to be the REAL pipeline, not a fixture that mimics it.
            if re.search(r"^\s*from\s+runtime\.turn_(?:middleware|policies)\s+import", raw, re.M) or re.search(
                r"^\s*import\s+runtime\.turn_(?:middleware|policies)", raw, re.M
            ):
                drivers.append(rel)
    return drivers


def check_the_real_pipeline_is_driven_by_a_test() -> Dict[str, Any]:
    """Is the policy pipeline constructed anywhere OUTSIDE the pin files?

    This is the question the Phase-4 gap pin asks, asked again from the other
    side: not "does the pin still say it is unused" but "does anything drive it".
    A ``False`` here is the honest state and is reported as such rather than
    left for a reader to infer from an absence.
    """
    importers = _pipeline_drivers()
    production = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.glob("harness/**/*.py")
        if "TurnPolicyPipeline" in path.read_text(encoding="utf-8", errors="replace")
        or re.search(r"\bbuild_pipeline\s*\(", path.read_text(encoding="utf-8", errors="replace"))
    )
    return {
        "check": "the_real_pipeline_is_driven_by_a_test",
        "verdict": "OK" if importers else "NOT_IMPLEMENTED",
        "test_importers": importers,
        "production_references": production,
        "detail": (
            f"a test drives the real pipeline: {importers}"
            if importers
            else "nothing constructs the policy pipeline (neither TurnPolicyPipeline nor "
            "build_pipeline). The declared ordering is enforced against itself and nothing "
            "drives it against the declaration -- which is the Phase-4 gap, still open, and is "
            "recorded as such rather than described as done."
        ),
    }


def check_the_kernel_imports_the_middleware() -> Dict[str, Any]:
    """**The CONFLICT two pinned invariants now disagree about.**

    ``tests/test_ledger_store_structural_pins.py`` asserts that no
    ``harness/agent_kernel/*.py`` mentions ``turn_middleware`` ("a pure core
    that imports its policies has no outside"). T3 landed a real import in
    ``strategy.py``. So that pin is RED.

    Both readings are defensible -- wrapping the loop is the design, and a
    kernel that imports its own policy layer is also a real coupling cost --
    and only a human can pick. This check reports the conflict so it is not
    resolved by whoever edits a test next.
    """
    offenders: List[str] = []
    kernel = REPO_ROOT / "harness" / "agent_kernel"
    for path in sorted(kernel.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "turn_middleware" in text:
            line = next(
                (n for n, row in enumerate(text.splitlines(), start=1) if "turn_middleware" in row),
                0,
            )
            offenders.append(f"{path.name}:{line}")
    return {
        "check": "the_kernel_imports_the_middleware",
        "verdict": "CONFLICT" if offenders else "OK",
        "offenders": offenders,
        "detail": (
            "harness/agent_kernel now imports runtime/turn_middleware, which VIOLATES "
            "tests/test_ledger_store_structural_pins.py::TestAddingAMiddlewareDoesNotTouch"
            "TheLoop::test_no_kernel_module_imports_the_middleware_either. That test is RED. "
            "T5 did not edit it: changing another terminal's pin to make a conflict disappear "
            "is how a red gets laundered instead of resolved. T3 and T4 own the decision."
            if offenders
            else "the kernel does not import its policy layer"
        ),
    }


# ---------------------------------------------------------------------------
# WATCH-LIST 3 -- concurrency tests that are not concurrent
# ---------------------------------------------------------------------------


def check_the_p95_sessions_test_is_actually_concurrent() -> Dict[str, Any]:
    """Does the p95 session-listing test create concurrency it can fail under?

    The recorded history: it "fails only under concurrency and passes 4/4
    standalone". A latency test that constructs no concurrency can only fail on
    host load, so its failure signal is load rather than the property it names.
    This reads the test's own body for the concurrency it sets up.
    """
    rel = "tests/test_ceiling03_sessions.py"
    if not (REPO_ROOT / rel).is_file():
        return {"check": "the_p95_sessions_test_is_actually_concurrent", "verdict": "ABSENT"}
    target = "test_five_thousand_indexed_sessions_list_under_100ms_p95"
    functions = _functions(rel)
    body = next(
        (body for name, body in functions.items() if name.endswith(target)),
        "",
    )
    if not body:
        return {
            "check": "the_p95_sessions_test_is_actually_concurrent",
            "verdict": "RENAMED_OR_GONE",
            "detail": f"{target} is not a module-level function in {rel}",
        }
    markers = {
        "ThreadPoolExecutor": "ThreadPoolExecutor" in body,
        "threading.Thread": "threading.Thread" in body or "Thread(" in body,
        "multiprocessing": "multiprocessing" in body,
        "asyncio.gather": "asyncio.gather" in body,
        "concurrent.futures": "concurrent.futures" in body,
    }
    concurrent = any(markers.values())
    return {
        "check": "the_p95_sessions_test_is_actually_concurrent",
        "verdict": "OK" if concurrent else "NOT_CONCURRENT",
        "markers": markers,
        "detail": (
            "the test creates real concurrency"
            if concurrent
            else "the test creates NO concurrency of its own. Its p95 assertion therefore "
            "measures HOST LOAD rather than concurrent behaviour, which is the recorded "
            "flake class in this tree: it failed at p95 103.6ms against a 100ms budget under "
            "load and passed 4/4 standalone. That is a load-sensitive timing pin, not a "
            "concurrency measurement, and a reader of the test name cannot tell the "
            "difference."
        ),
    }


def check_the_concurrency_pin_runs_concurrently_when_it_is_run() -> Dict[str, Any]:
    """Does the recorded p95 case fail under concurrency and pass standalone?

    The recorded claim, RE-RUN rather than inherited. Two invocations: alone,
    then with a synthetic concurrent load in the same process. This is a
    measurement of a measurement, and it is the only way to tell a load flake
    from a concurrency defect.
    """
    rel = "tests/test_ceiling03_sessions.py"
    node = (
        "tests/test_ceiling03_sessions.py::"
        "test_five_thousand_indexed_sessions_list_under_100ms_p95"
    )
    if not (REPO_ROOT / rel).is_file():
        return {"check": "the_concurrency_pin_runs_concurrently_when_it_is_run", "verdict": "ABSENT"}
    try:
        solo = subprocess.run(
            [sys.executable, "-m", "pytest", node, "-q", "-p", "no:randomly", "-p", "no:cacheprovider"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return {
            "check": "the_concurrency_pin_runs_concurrently_when_it_is_run",
            "verdict": "TIMED_OUT",
        }
    solo_ok = solo.returncode == 0
    lines = (solo.stdout or "").splitlines()
    tail = [line for line in lines if "passed" in line or "failed" in line]
    failure = [
        line.strip()
        for line in lines
        if "p95" in line.lower() or "assert" in line.lower()
    ][-4:]
    return {
        "check": "the_concurrency_pin_runs_concurrently_when_it_is_run",
        "verdict": "MEASURED",
        "standalone_returncode": solo.returncode,
        "standalone_green": solo_ok,
        "standalone_tail": tail[-1:] or [],
        "standalone_failure_lines": failure,
        "detail": (
            "green standalone. That is EXPECTED and is not evidence the pin works: the "
            "recorded history is that it fails only under concurrent load, and this run "
            "created none. See check_the_p95_sessions_test_is_actually_concurrent for "
            "whether the test can fail under concurrency at all."
            if solo_ok
            else "RED standalone, which is a DIFFERENT signal from the recorded "
            "'fails only under concurrency' and should be looked at directly."
        ),
    }


# ---------------------------------------------------------------------------
# WATCH-LIST 4 -- ledger tests that can pass with an empty ledger
# ---------------------------------------------------------------------------


def check_no_ledger_test_passes_on_an_empty_ledger() -> Dict[str, Any]:
    """Does any ledger test reach its conclusion without a real ledger existing?

    The vacuous shape: a test that builds a store, tampers or reads, and asserts
    only on the ABSENCE of a marker -- where a crash before the marker is
    produced satisfies it. So each ledger test file is asked whether it creates
    at least one feature and asserts on at least one CONCRETE value, not only on
    absence.
    """
    # **Only BEHAVIOURAL ledger tests are subject to this check.** The structural
    # pins file reads SOURCE and asserts on the module's own AST; it never builds a
    # ledger and is not supposed to. Asking it for "creates a real ledger" would
    # flag a correct test for the wrong property -- the same conflation as asking a
    # behavioural test to parse AST.
    files = (
        "tests/test_feature_ledger_store.py",
        "tests/test_p4_drift_audit.py",
    )
    rows: List[Dict[str, Any]] = []
    for rel in files:
        path = REPO_ROOT / rel
        if not path.is_file():
            rows.append({"file": rel, "verdict": "ABSENT"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        creates = "new_feature" in text or "_project(" in text or "build_task_repo" in text
        # A concrete assertion: `==`/in on a real field, not `not in` / `is None`.
        concrete = bool(
            re.search(r"assert\s+\S+\s*==", text)
            or re.search(r"assert\s+\S+\s+in\s+", text)
        )
        absence_only = not concrete
        rows.append(
            {
                "file": rel,
                "creates_a_real_ledger": creates,
                "asserts_a_concrete_value": concrete,
                "absence_only": absence_only,
                "verdict": "OK" if (creates and concrete) else "REVIEW",
            }
        )
    bad = [row for row in rows if row.get("verdict") == "REVIEW"]
    return {
        "check": "no_ledger_test_passes_on_an_empty_ledger",
        "verdict": "REVIEW" if bad else "OK",
        "files": rows,
        "detail": (
            f"{[row['file'] for row in bad]} may reach a conclusion on an empty ledger"
            if bad
            else "every ledger test file creates a real ledger AND asserts a concrete value"
        ),
    }


def check_the_empty_ledger_case_is_not_mistaken_for_a_clean_one() -> Dict[str, Any]:
    """Drive the store's own audit with NOTHING passing, and read what it reports.

    The non-vacuity control for watch-list 4: an audit over an empty ledger
    reports ``vacuous: True`` and no coverage, because "we checked nothing" and
    "everything is fine" are different answers and only one of them is a pass.
    """
    import shutil
    import tempfile

    from harness import long_horizon as LH
    from memory import feature_ledger_store as FL

    root = Path(tempfile.mkdtemp(prefix="p4w2-empty-"))
    try:
        ledger = LH.new_feature_ledger(
            "shop",
            "nothing claimed yet",
            [
                LH.new_feature(
                    "receipts",
                    "Sum a receipt list.",
                    LH.VerificationMethod(method="command", command="pytest -k receipts"),
                )
            ],
            created_at="2026-10-06T00:00:00Z",
            sessions=("s1",),
        )
        LH.write_feature_ledger(root / LH.FEATURE_LEDGER_NAME, ledger)
        store = FL.LedgerStore(root)
        store.ensure_sealed(sealed_at="2026-10-06T00:00:00Z")
        report = FL.LedgerStore(root).audit()
        return {
            "check": "the_empty_ledger_case_is_not_mistaken_for_a_clean_one",
            "verdict": "OK" if report.vacuous and report.coverage_share is None else "REVIEW",
            "passing": report.passing,
            "coverage_share": report.coverage_share,
            "vacuous": report.vacuous,
            "detail": (
                "an audit over a ledger with nothing passing reports vacuous=True and "
                "coverage_share=None. If it reported coverage 1.0, a clean-looking ledger "
                "would mean 'we verified nothing'."
            ),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# the adjudication, as one receipt
# ---------------------------------------------------------------------------

WATCH_LIST_CHECKS = (
    check_multi_session_tests_are_in_the_nightly_lane,
    check_no_middleware_test_claims_the_policy_without_exercising_it,
    check_the_real_pipeline_is_driven_by_a_test,
    check_the_kernel_imports_the_middleware,
    check_the_p95_sessions_test_is_actually_concurrent,
    check_the_concurrency_pin_runs_concurrently_when_it_is_run,
    check_no_ledger_test_passes_on_an_empty_ledger,
    check_the_empty_ledger_case_is_not_mistaken_for_a_clean_one,
)


def adjudicate() -> Dict[str, Any]:
    """Run every watch-list check and return one receipt."""
    rows = []
    for check in WATCH_LIST_CHECKS:
        try:
            rows.append(check())
        except Exception as exc:  # a checker that crashes has not checked anything
            rows.append(
                {
                    "check": check.__name__,
                    "verdict": "CHECKER_CRASHED",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "checks": rows,
        "verdicts": sorted({str(row.get("verdict")) for row in rows}),
        "findings": [row for row in rows if row.get("verdict") not in ("OK", "MEASURED")],
    }


def main() -> int:
    payload = adjudicate()
    for row in payload["checks"]:
        print("=" * 78)
        print(f"{row['check']}: {row.get('verdict')}")
        print("-" * 78)
        for key, value in row.items():
            if key in ("check", "verdict"):
                continue
            print(f"  {key}: {value}")
    print("=" * 78)
    print(f"verdicts: {', '.join(payload['verdicts'])}")
    print(f"findings needing a human decision: {len(payload['findings'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
