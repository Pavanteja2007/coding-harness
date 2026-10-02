"""T1.W2.4 — the three structural claims this module makes, each pinned by name.

Three claims, three recorded measurements, three different kinds of evidence.
Collected in one file because the failure mode they share is the same one
`phases/DOCTRINE.md` is about: a claim that lives only in prose returns the
moment the prose is not read.

| claim | recorded evidence | kind of pin | why that kind |
|---|---|---|---|
| the journal redactor is linear on pathological input | `"y"*40000`: 32 s -> 0.020 s | **perf assertion**, generous budget | a quadratic return passes any single-size budget and fails only on the SHAPE, so the ratio is asserted too |
| `step()` has no I/O, no clock, no globals, no randomness | pinned 4 ways | **verify the 4 exist** | AGT-11 built four pins; this round did not build them and this round is what finds out if they are still there |
| `approve()` defaults to `False` | `LoopEnvironment` | **live call + source** | the default is the load-bearing part of the fail-closed argument, and it is one line somebody could flip in passing |

## Why the purity pins are VERIFIED rather than REBUILT

The four pins live in `tests/test_agent_loop_matrix.py`, which is T5's lane. This
round did not edit that file and cannot add to it. What it can do is assert they
are still present, still named, and still cover what AGT-11 said they cover.

That is a real check rather than a formality: a pin in a file nobody runs is not
a pin, and this file fails if the four disappear. If one HAS disappeared, the
right answer is to restore it in `tests/`, which is a T5 handoff — not to
duplicate it here, because a second copy of a purity pin is a second thing to keep
in sync and it would be the one nobody reads.

Host-only: no Docker, no provider, no network.
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Tuple

HARNESS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_ROOT.parent
STEP_MODULE = HARNESS_ROOT / "agent_loop_step.py"
MATRIX_FILE = REPO_ROOT / "tests" / "test_agent_loop_matrix.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


# ---------------------------------------------------------------------------
# CLAIM 1 — the journal redactor is linear on pathological input
# ---------------------------------------------------------------------------

#: Generous on purpose. These are regression pins, not benchmarks: each budget is
#: 15x-80x the measured cost on the development host, so a host 3x slower still
#: passes while a return to the quadratic shape (which was MINUTES) fails by
#: three orders of magnitude. A perf pin tight enough to notice a slow host is a
#: flake generator and gets deleted by the next terminal.
BUDGET_REPEATED_RUN_40K_S = 2.0
BUDGET_LONG_RUN_400K_S = 4.0
#: A 4x input must not cost 16x the time. Quadratic would be ~16x; 12x leaves
#: room for a loaded host and for the authority's own span-chunking overhead,
#: while still failing a quadratic return by 4x.
LINEARITY_RATIO_BUDGET = 12.0
#: A measurement too small to time reliably would make the ratio meaningless, so
#: the input is big enough that the fixed per-call cost is not dominant.
_RATIO_SMALL = 100_000


def _elapsed(fn: Callable, *args, **kwargs) -> Tuple[object, float]:
    start = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, time.perf_counter() - start


def test_the_quadratic_shape_has_not_returned_to_the_journal_redactor() -> None:
    """`"y"*40000` must cost well under a budget, not minutes.

    Measured through `harness/redaction.py` — the boundary that sits on every
    harness journal write — rather than through `shared.security.redact_text`
    directly. The claim this file is pinning is the one about the JOURNAL, and a
    harness test that only measured the shared authority could not tell a fixed
    authority from a harness cap hiding an unfixed one.
    """
    from harness.redaction import redact_text_for_journal

    _, elapsed = _elapsed(redact_text_for_journal, "y" * 40_000)
    assert elapsed < BUDGET_REPEATED_RUN_40K_S, (
        f'redact_text_for_journal("y"*40000) took {elapsed:.3f}s (budget '
        f"{BUDGET_REPEATED_RUN_40K_S}s). This is the DOCTRINE.md §8 defect: a "
        "per-character run that scales quadratically again hangs every journal "
        "write in the harness."
    )


def test_the_journal_redactor_is_linear_across_a_four_fold_length_range() -> None:
    """THE SHAPE. Four times the input must not cost sixteen times the work.

    This is the assertion that actually catches a quadratic return: every
    single-size budget above would pass a redactor that had gone back to
    quadratic at the sizes they measure, because quadratic at 40k chars was only
    a few seconds. The ratio is the claim, and it is asserted separately from
    the absolute budgets for exactly that reason.
    """
    from harness.redaction import redact_text_for_journal

    small = "y" * _RATIO_SMALL
    _, small_s = _elapsed(redact_text_for_journal, small)
    _, large_s = _elapsed(redact_text_for_journal, small * 4)
    assert large_s < small_s * LINEARITY_RATIO_BUDGET + 0.2, (
        f"4x input cost {large_s / max(small_s, 1e-9):.1f}x the time "
        f"({small_s:.4f}s -> {large_s:.4f}s); quadratic would be ~16x and the "
        f"budget is {LINEARITY_RATIO_BUDGET}x. A per-class-run blowup is the "
        "DOCTRINE.md §8 defect returning in a new place."
    )


def test_a_one_megabyte_redaction_stays_inside_its_budget() -> None:
    """The absolute bound on a megabyte, which the ratio alone does not give.

    A ratio can be perfect and still slow: a linear-but-hopeless redactor scales
    as `4x` and takes ninety seconds on a megabyte. Both properties are needed —
    the ratio proves the shape, this proves the cost.
    """
    from harness.redaction import redact_text_for_journal

    payload = "ordinary prose line\n" * (1_000_000 // len("ordinary prose line\n"))
    cleaned, elapsed = _elapsed(redact_text_for_journal, payload)
    assert isinstance(cleaned, str)
    assert elapsed < 10.0, (
        f"redacting {len(payload)} chars of prose took {elapsed:.3f}s; measured "
        "0.886 s on the development host. Every journal write pays this, so a "
        "megabyte of tool output is a megabyte on this path."
    )


def test_the_redaction_boundary_is_what_the_measurement_goes_through() -> None:
    """Non-vacuity: the value above really travelled through the boundary.

    A perf pin on a function nothing calls measures the function. This drives a
    value through the real `TraceLogger.log` write and asserts the row exists,
    that it carries the authority's placeholder, and that the elapsed time is
    inside the same budget — so the claim is about a live journal write rather
    than about a helper called in isolation.
    """
    from harness.trace import TraceLogger

    secret = "sk-" + "A" * 39
    logger = TraceLogger(Path(_tmp_logs()) / "t1w24")
    payload = ("ordinary prose line\n" * 20_000) + f"token={secret}"
    _, elapsed = _elapsed(logger.log, "tool_result", {"output": payload})

    rows = logger.read_all()
    assert len(rows) == 1, "the row must exist; an absent row proves nothing"
    assert secret not in str(rows[0]), "the raw credential reached the row"
    assert elapsed < 10.0, (
        f"a {len(payload)}-char tool result took {elapsed:.3f}s to journal"
    )


def _tmp_logs() -> str:
    """A throwaway logs root under the OS temp dir, never inside the repo."""
    import tempfile

    return tempfile.mkdtemp(prefix="t1w24-")


# ---------------------------------------------------------------------------
# CLAIM 2 — the pure `step()` has no I/O, no clock, no globals, no randomness
# ---------------------------------------------------------------------------

#: The four pins AGT-11 recorded, by the exact test name in
#: `tests/test_agent_loop_matrix.py`. Kept as a table rather than four loose
#: asserts so a reader can see all four at once and so a removal names itself.
PURITY_PINS: Tuple[Tuple[str, str], ...] = (
    (
        "import pin",
        "test_the_step_function_imports_nothing_that_can_touch_the_outside_world",
    ),
    (
        "module-global pin",
        "test_every_module_level_binding_is_immutable",
    ),
    ("determinism pin", "test_the_same_inputs_produce_the_same_events"),
    (
        "poisoned clock + socket pin",
        "test_step_never_reads_a_clock_or_opens_a_socket",
    ),
)


def test_the_four_step_purity_pins_are_all_present_and_named() -> None:
    """VERIFY the four pins exist. Restore, do not duplicate.

    Read from the file by name. A pin that has been renamed, deleted, or moved
    out of the suite fails here — and the message says to restore it in
    `tests/`, which is a T5 handoff, rather than to re-add it here. A second
    copy of a purity pin inside `harness/` would be a second thing to keep in
    sync and it would be the copy nobody reads.
    """
    if not MATRIX_FILE.exists():
        # The file is untracked in some trees; the structural pins below still
        # assert the properties, so a missing matrix is not a reason to fail.
        return
    source = _source(MATRIX_FILE)
    missing = [
        f"{label} ({name})"
        for label, name in PURITY_PINS
        if f"def {name}(" not in source
    ]
    assert missing == [], (
        "these step() purity pins are missing from "
        "tests/test_agent_loop_matrix.py:\n  "
        + "\n  ".join(missing)
        + "\n\nAGT-11 built four (import / module-global / determinism / "
        "poisoned-clock-and-socket) and recorded that the last one is the one "
        "that earns its place. Restore the missing pin in tests/ — do NOT add a "
        "copy under harness/, which is this file's own file and would then hold "
        "two pins for one property."
    )


def test_the_purity_pins_are_wired_into_a_passing_suite_not_just_present() -> None:
    """Present-but-uncollected is not a pin, and this is how that happens.

    A test file can define the four purity tests and never run them: a
    `__test__ = False`, a rename to a helper, a `pytest.ini` filter. This asserts
    the file is collectible — which is the cheap half — and that the module the
    pins test is the one they name.
    """
    if not MATRIX_FILE.exists():
        return
    source = _source(MATRIX_FILE)
    assert "STEP_SOURCE_TEXT" in source, (
        "tests/test_agent_loop_matrix.py no longer reads STEP_SOURCE_TEXT; the "
        "purity pins read harness/agent_loop_step.py's source, so a rename there "
        "silently changes what they scan"
    )
    # And the source they scan must be the file this file also scans.
    assert "agent_loop_step" in source, (
        "the purity pins no longer name agent_loop_step.py; step() moved or the "
        "matrix is pinned against the wrong module"
    )
    # Collect-only, so a syntax error or an import failure in the matrix fails
    # here rather than looking like a passing absence.
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
            str(MATRIX_FILE),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    for _label, name in PURITY_PINS:
        assert name in result.stdout, (
            f"{name} is defined but pytest does not collect it:\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}\nA pin that is "
            "defined and never collected is a comment with extra steps."
        )


def test_step_declares_the_four_purity_properties_in_its_own_docstring() -> None:
    """The properties are stated where the function is, not only in a test.

    A source-level check on the module docstring, so a future reader who reads
    `agent_loop_step.py` — rather than the matrix — sees the contract. Cheap,
    and it fails if the docstring is rewritten while the properties quietly
    change.
    """
    tree = ast.parse(_source(STEP_MODULE))
    docstring = ast.get_docstring(tree) or ""
    lowered = docstring.lower()
    # The docstring's own wording, which is what the module actually says. Any
    # ONE of each pair is enough; a rewording that keeps the meaning passes, and
    # a rewording that drops the property fails.
    alternatives: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("the filesystem / I/O", ("does not read a clock", "touch the filesystem")),
        ("the clock", ("does not read a clock", '"no clock"')),
        ("a global", ("nothing is read from a", "no global")),
        ("total function of its three arguments", ("total function of",)),
        ("the four ways purity is pinned", ("pinned four ways",)),
    )
    for description, options in alternatives:
        assert any(option in lowered for option in options), (
            f"harness/agent_loop_step.py's docstring no longer states that step() "
            f"is free of {description}. The four purity pins still enforce it, "
            "but a reader of the module would not learn the contract there."
        )


def test_step_reads_no_clock_and_no_randomness_at_the_source_level() -> None:
    """The source-level half of the behavioural poisoned-clock pin.

    `test_step_never_reads_a_clock_or_opens_a_socket` is the behavioural proof and
    it is the stronger one — it catches a transitive helper. This is the cheap
    source-level proof, kept here because it names exactly what it forbids and
    so answers "why is this a purity rule" in one place.
    """
    tree = ast.parse(_source(STEP_MODULE))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    forbidden = {
        "time",
        "monotonic",
        "perf_counter",
        "time_ns",
        "random",
        "randint",
        "choice",
        "uuid4",
        "uuid1",
        "now",
        "utcnow",
        "socket",
        "create_connection",
        "open",
        "input",
    }
    offending = sorted(called & forbidden)
    assert offending == [], (
        f"harness/agent_loop_step.py calls {offending}; step() must reach the "
        "clock, the network and the filesystem only through its injected "
        "boundary. A decision function that reads a clock is not reproducible, "
        "and one that reads `random` is not replayable."
    )


def test_the_module_level_bindings_are_still_immutable() -> None:
    """The module-global pin, re-asserted harness-side as well as in the matrix.

    Duplicated ON PURPOSE, and this is the one place that duplication is right:
    the assertion is nine lines, it reads no state, and it is the property most
    likely to be broken by an innocuous-looking edit (a lookup table someone "
    "converts to a dict for convenience). The matrix still owns the behavioural
    consequence; this owns the cheap structural check.
    """
    tree = ast.parse(_source(STEP_MODULE))
    mutable: List[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(
            node.value, (ast.List, ast.Dict, ast.Set)
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id != "__all__":
                mutable.append(target.id)
    assert mutable == [], (
        f"harness/agent_loop_step.py has module-level mutable state: {mutable}. "
        "A pure function that can read a global is a function whose output "
        "depends on call order."
    )


# ---------------------------------------------------------------------------
# CLAIM 3 — `approve()` defaults to `False`
# ---------------------------------------------------------------------------

#: `LoopEnvironment`'s five defaults, and why each one is what it is. The
#: `approve` row is the load-bearing one and is asserted on its own below.
FAIL_SAFE_DEFAULTS: Dict[str, Tuple[object, str]] = {
    "elapsed_s": (
        0.0,
        "no wall clock, so no timeout — a run with no clock cannot time out",
    ),
    "spent_usd": (0.0, "no meter, so no budget stop"),
    "cancelled": (False, "a cancel nobody asked for must not fire"),
    "approve": (False, "FAIL CLOSED: no approver is a refusal, never a silent yes"),
    "steering": (None, "no inbox, nothing typed"),
}


def test_approve_defaults_to_false_and_a_subclass_cannot_inherit_a_yes() -> None:
    """THE PIN. `LoopEnvironment().approve(...)` answers `False`.

    Asserted by CALLING it, not by reading the source. A default that reads
    `False` in the source and answers `True` at runtime is possible — a
    decorator, a subclass, a `__getattr__` — and the fail-closed argument for
    this boundary depends on the answer, not on the spelling.

    And the second half matters more: a subclass that overrides NOTHING still
    refuses. That is the property the daily path actually relies on, because a
    strategy supplies `ask` and `invoke` and inherits the rest.
    """
    from harness.agent_loop_step import LoopEnvironment

    base = LoopEnvironment()
    assert base.approve("shell", {"command": "rm -rf /"}, turn=0) is False, (
        "LoopEnvironment.approve must answer False with no approver bound. A "
        "require-mode run with no approver has to REFUSE rather than proceed "
        "silently, and that is the same fail-closed answer the legacy loop "
        "reaches."
    )

    # A minimal subclass: exactly the two required capabilities, nothing else.
    class _OnlyRequired(LoopEnvironment):
        def ask(self, messages, *, step):
            return '{"tool": "finish", "answer": "x"}'

        def invoke(self, name, args, *, turn):
            from harness.agent_loop_step import ToolOutcome

            return ToolOutcome(ok=True, output="")

    inherited = _OnlyRequired()
    assert inherited.approve("shell", {"command": "rm -rf /"}, turn=0) is False, (
        "a subclass that overrides only ask/invoke inherited an APPROVING "
        "approve(). This is the exact shape every real boundary takes, so the "
        "fail-safe default must survive subclassing rather than living on the "
        "base class only in a docstring."
    )


def test_the_other_loop_environment_defaults_still_fail_safe() -> None:
    """The other four defaults, asserted live, because they are the same claim.

    Each one's direction is chosen: no clock, no meter, no cancel, no inbox. A
    default that invented a timeout, a budget, a cancel or a message would make
    the loop act on information nobody gave it — which is the same class of
    dishonesty as an approving `approve()`.
    """
    from harness.agent_loop_step import LoopEnvironment

    env = LoopEnvironment()
    assert env.elapsed_s() == 0.0, (
        "elapsed_s must default to 0.0: a run with no clock cannot time out, and "
        "a non-zero default would be the loop inventing a limit it was never "
        "given"
    )
    assert env.spent_usd() == 0.0, (
        "spent_usd must default to 0.0: no meter means no budget stop, and a "
        "non-zero default would stop runs on a budget nobody set"
    )
    assert env.cancelled() is False, (
        "cancelled must default to False: a cancel nobody asked for must not fire"
    )
    # `steering` takes `where`, because it is called at named checkpoints. The
    # signature is passed explicitly rather than relying on a default, so this
    # pin cannot start failing for an unrelated reason if the checkpoint name
    # gains a parameter.
    assert env.steering("agent-turn-0", turn=0) is None, (
        "steering must default to None: no inbox means nothing was typed, and an "
        "empty-but-not-None delivery would read as an arrived message"
    )


def test_the_approve_default_is_still_false_in_the_source() -> None:
    """The source-level half, so a rename of the default fails here too.

    The call above proves the ANSWER. This proves the answer is still the
    literal `False` in `LoopEnvironment.approve` and not arriving from
    somewhere else — a subclass, a metaclass, or a `__getattr__` that would make
    the call above pass while the declared default had changed.
    """
    tree = ast.parse(_source(STEP_MODULE))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "approve":
            body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            assert re.search(r"\breturn\s+False\b", body), (
                "LoopEnvironment.approve no longer returns the literal False; "
                f"its body reads: {body[:200]}"
            )
            return
    raise AssertionError(
        "harness/agent_loop_step.py no longer defines LoopEnvironment.approve; "
        "the fail-safe default's owner has moved and this pin needs updating"
    )


def test_the_required_methods_are_the_only_two_without_a_default() -> None:
    """`ask` and `invoke` raise; the other five answer. Both halves of the shape.

    The boundary is "two capabilities you must supply, five defaults chosen in
    the safe direction". Asserting only that `approve` refuses would pass if the
    class had quietly started answering `ok` for `invoke` too — which would be a
    tool that reported success without running anything.
    """
    from harness.agent_loop_step import LoopEnvironment, ToolOutcome

    env = LoopEnvironment()
    calls = (
        ("ask", (([{"role": "user", "content": "x"}],), {"step": "s"})),
        ("invoke", (("shell", {}), {"turn": 0})),
    )
    for name, (args, kwargs) in calls:
        method = getattr(env, name)
        try:
            method(*args, **kwargs)
        except NotImplementedError as exc:
            assert "must implement" in str(exc), (
                f"{name}() raises NotImplementedError without naming what the "
                f"caller must supply: {exc}"
            )
        except Exception as exc:  # pragma: no cover - a wrong exception type
            raise AssertionError(
                f"{name}() raised {type(exc).__name__} instead of "
                f"NotImplementedError; a base class that guesses is a base class "
                f"that hides a wiring error: {exc}"
            ) from exc
        else:
            raise AssertionError(
                f"{name}() answered instead of raising NotImplementedError; a "
                "boundary that answers its own required capabilities cannot "
                "distinguish 'nobody wired this' from 'the tool ran'"
            )
    assert ToolOutcome is not None, "ToolOutcome must remain importable"
